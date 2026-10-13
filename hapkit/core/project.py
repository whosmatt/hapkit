"""Project schema (hapkit/1): templates, validation, persistence."""
from __future__ import annotations

import base64
import copy
import json
import os
import re

from . import ids, ramfmt
from .render import render_wave
from .sequence import LOOP_INFINITE, SLOTS, wait_slot

FORMAT = "hapkit/1"
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
PROVENANCE_RE = re.compile(r"^(original|stock|(reference|traced):[A-Za-z0-9_.:#/-]+)$")
HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_BIN = os.path.normpath(os.path.join(HERE, "..", "..", "ref", "vendor", "haptic_ram.stock.bin"))

# f0 / Q measured on 214d028e with the phone's microphone (tools/capture.py, 2026-10-11): free-decay f0
# 231.5-232.0 Hz (driver calibration: 232.4), Q_free 25-27 at all levels. Q_drive 26 from 32 brake variants
# (residual ringing after the brake, median |measured - model| 0.4 dB; Q_drive 22 from the rise fit leaves brakes
# ~5 % too weak). Steady-state drive above ~0.3-0.5 FS compresses and grows even harmonics (rattles).
DEFAULT_DEVICE = {
    "fs": 24000,
    "f0": 231.7,
    "q_drive": 26.0,
    "q_free": 26.0,
    "vmax_mv": 8500,
    "base_addr": ramfmt.DEFAULT_BASE,
    "ram_end": ramfmt.SRAM_END,
    "ram_version": ramfmt.DEFAULT_VERSION,
    "max_waves": 16,
    "wait_unit": 64,
    "loop_extra_plays": 1,
}

SEGMENT_TYPES = ("tone", "gap", "pcm")
LENGTH_UNITS = ("cycles", "ms", "samples")
INTERPS = ("linear", "cos", "exp")


def effect_entries() -> list[dict]:
    out = []
    for name in ("CLICK", "DOUBLE_CLICK", "TICK", "THUD", "POP", "HEAVY_CLICK"):
        out.append({"id": f"eff_{name.lower()}", "kind": "effect", "aidl": ids.EFFECTS[name], "name": name,
                    "sequence": None, "stream_wave": None, "scale": 1.0, "enabled": False, "notes": ""})
    for name, val in ids.PRIMITIVES.items():
        if name == "NOOP":
            continue
        out.append({"id": f"prim_{name.lower()}", "kind": "primitive", "aidl": val, "name": name,
                    "sequence": None, "stream_wave": None, "scale": 1.0, "enabled": False, "notes": ""})
    return out


def new_project(template: str = "empty", name: str | None = None) -> dict:
    p = {"format": FORMAT, "name": name or template, "device": dict(DEFAULT_DEVICE), "ram": {"waves": []},
         "waves": [], "sequences": [], "effects": effect_entries(), "references": []}
    if template == "empty":
        return p
    if template != "stock":
        raise ValueError(f"unknown template {template!r}")
    if not os.path.exists(STOCK_BIN):
        raise FileNotFoundError(f"stock RAM image not found: {STOCK_BIN}")
    with open(STOCK_BIN, "rb") as f:
        blob = f.read()
    waves, order, _ = waves_from_ram_bin(blob, prefix="stock", provenance="stock")
    img = ramfmt.parse(blob)
    p["device"]["base_addr"], p["device"]["ram_version"] = img.base_addr, img.version
    p["waves"], p["ram"]["waves"] = waves, order
    w = order
    p["sequences"] = [
        _seq("seq_click", [w[0]]),
        _seq("seq_double_click", [w[0], 100.0, w[0]]),
        _seq("seq_tick", [w[2]]),
        _seq("seq_thud", [w[1]]),
        _seq("seq_pop", [w[2]]),
        _seq("seq_heavy_click", [w[0]]),
    ]
    for e in p["effects"]:
        if e["kind"] == "effect":
            e["sequence"], e["enabled"] = f"seq_{e['name'].lower()}", True
    return p


def _seq(sid: str, items: list) -> dict:
    slots = [{"kind": "wait", "ms": it} if isinstance(it, float) else {"kind": "wave", "wave": it, "loops": 0}
             for it in items]
    return {"id": sid, "name": "", "slots": slots, "mainloop": 0, "notes": ""}


def waves_from_ram_bin(blob: bytes, prefix: str = "ram", provenance: str = "stock") -> tuple[list[dict], list[str], str]:
    img = ramfmt.parse(blob)
    waves, order = [], []
    for i, rw in enumerate(img.waves, 1):
        wid = f"{prefix}_w{i}"
        waves.append({"id": wid, "name": f"{prefix} w{i}", "provenance": provenance,
                      "segments": [{"type": "pcm", "fs": 24000, "encoding": "int8",
                                    "data_b64": base64.b64encode(rw.data).decode(), "gain": 1.0,
                                    "trim": None, "label": f"w{i}"}],
                      "post": {"gain": 1.0}, "notes": f"0x{rw.start:04x}-0x{rw.end:04x}"})
        order.append(wid)
    report = (f"base 0x{img.base_addr:04x}, version 0x{img.version:02x}, {len(img.waves)} waves, checksum "
              f"{'ok' if img.checksum_ok else 'MISMATCH'}"
              + (f", {len(img.extra)} trailing bytes" if img.extra else "")
              + (f", gaps {img.gaps}" if img.gaps else ""))
    return waves, order, report


def project_from_ram_bin(blob: bytes, name: str) -> dict:
    p = new_project("empty", name)
    img = ramfmt.parse(blob)
    p["device"]["base_addr"], p["device"]["ram_version"] = img.base_addr, img.version
    p["waves"], p["ram"]["waves"], _ = waves_from_ram_bin(blob, prefix="ram", provenance="stock")
    return p


def canonical_json(project: dict) -> bytes:
    return json.dumps(project, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def refs_sidecar(path: str) -> str:
    """Local-only file next to a project that holds its proprietary references (gitignored: *.local.json)."""
    base = path[: -len(".hapkit.json")] if path.endswith(".hapkit.json") else os.path.splitext(path)[0]
    return base + ".refs.local.json"


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    if p.get("format") != FORMAT:
        raise ValueError(f"{path}: not a {FORMAT} project")
    dev = dict(DEFAULT_DEVICE)
    dev.update(p.get("device") or {})
    p["device"] = dev
    for key in ("waves", "sequences", "effects", "references"):
        p.setdefault(key, [])
    p.setdefault("ram", {"waves": []})
    side = refs_sidecar(path)
    if os.path.exists(side):
        with open(side, encoding="utf-8") as f:
            extra = json.load(f).get("references", [])
        have = {r["id"] for r in p["references"]}
        p["references"] += [r for r in extra if r["id"] not in have]
    return p


def save(project: dict, path: str) -> None:
    """Write the project; references marked proprietary go to the local sidecar (refs_sidecar), so the
    project file itself can be published."""
    prop = [r for r in project.get("references", []) if r.get("proprietary")]
    main = dict(project, references=[r for r in project.get("references", []) if not r.get("proprietary")])
    side = refs_sidecar(path)
    if prop:
        _write_json({"format": FORMAT, "references": prop}, side)
    elif os.path.exists(side):
        os.remove(side)
    _write_json(main, path)


def _write_json(obj: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def strip_reference_data(project: dict) -> dict:
    p = copy.deepcopy(project)
    for r in p.get("references", []):
        if "data_b64" in r:
            r["n"] = len(base64.b64decode(r["data_b64"])) // 4
            del r["data_b64"]
    return p


def validate(project: dict) -> list[dict]:
    issues: list[dict] = []

    def err(path: str, msg: str, level: str = "error") -> None:
        issues.append({"level": level, "path": path, "msg": msg})

    dev = project.get("device") or {}
    for k in ("fs", "f0", "q_drive", "q_free", "base_addr", "ram_end", "wait_unit"):
        if not isinstance(dev.get(k), (int, float)) or dev.get(k) <= 0:
            err(f"device.{k}", "must be a positive number")
    if issues:
        return issues

    for coll in ("waves", "sequences", "effects", "references"):
        seen = set()
        for i, item in enumerate(project.get(coll, [])):
            iid = item.get("id", "")
            if not ID_RE.match(str(iid)):
                err(f"{coll}[{i}].id", f"invalid id {iid!r}")
            if iid in seen:
                err(f"{coll}[{i}].id", f"duplicate id {iid!r}")
            seen.add(iid)

    waves = {w.get("id"): w for w in project.get("waves", [])}
    for i, w in enumerate(project.get("waves", [])):
        _validate_wave(w, f"waves[{i}]", err)

    order = project.get("ram", {}).get("waves", [])
    for wid in order:
        if wid not in waves:
            err("ram.waves", f"unknown wave {wid!r}")
    if len(set(order)) != len(order):
        err("ram.waves", "duplicate entries")
    if len(order) > dev.get("max_waves", 16):
        err("ram.waves", f"{len(order)} waves exceed driver limit {dev.get('max_waves')}", "warn")
    lens = []
    for wid in order:
        if wid in waves:
            try:
                lens.append(len(render_wave(waves[wid], dev)["samples"]))
            except ValueError:
                lens.append(1)
    if lens:
        lay = ramfmt.layout(lens, int(dev["base_addr"]), int(dev["ram_end"]))
        if not lay["fits"]:
            err("ram.waves", f"RAM overflow: {lay['used']} of {lay['capacity']} bytes")
        if order and order[-1] in waves:
            last = len(render_wave(waves[order[-1]], dev)["samples"]) if order[-1] in waves else 0
            period = dev["fs"] / dev["f0"]
            if last > 3 * period:
                err("ram.waves", f"last RAM wave {order[-1]!r} ({last} samples) is the FF_CONSTANT loop wave; "
                                 f"expected ~1-2 cycles ({period:.0f} samples/cycle)", "warn")

    seqs = {s.get("id"): s for s in project.get("sequences", [])}
    for i, s in enumerate(project.get("sequences", [])):
        path = f"sequences[{i}]"
        slots = s.get("slots") or []
        if not slots:
            err(path, "no slots")
        if len(slots) > SLOTS:
            err(path, f"{len(slots)} slots (max {SLOTS})")
        for j, sl in enumerate(slots):
            sp = f"{path}.slots[{j}]"
            if sl.get("kind") == "wait":
                ms = sl.get("ms")
                if not isinstance(ms, (int, float)) or ms <= 0:
                    err(sp, "wait ms must be > 0")
                else:
                    _, eff = wait_slot(ms, dev)
                    if abs(eff - ms) > dev["wait_unit"] * 500.0 / dev["fs"] + 1e-9:
                        err(sp, f"wait {ms} ms clamped to {eff:.2f} ms", "warn")
            elif sl.get("kind") == "wave":
                if sl.get("wave") not in waves:
                    err(sp, f"unknown wave {sl.get('wave')!r}")
                elif sl.get("wave") not in order:
                    err(sp, f"wave {sl.get('wave')!r} is not in RAM")
                lp = sl.get("loops", 0)
                if not isinstance(lp, int) or not 0 <= lp < LOOP_INFINITE:
                    err(sp, f"loops {lp!r} must be 0..14")
            else:
                err(sp, f"unknown slot kind {sl.get('kind')!r}")
        ml = s.get("mainloop", 0)
        if not isinstance(ml, int) or not 0 <= ml < LOOP_INFINITE:
            err(path, f"mainloop {ml!r} must be 0..14")

    for i, e in enumerate(project.get("effects", [])):
        path = f"effects[{i}]"
        if e.get("kind") not in ("effect", "primitive"):
            err(path, f"unknown kind {e.get('kind')!r}")
        if e.get("sequence") and e["sequence"] not in seqs:
            err(path, f"unknown sequence {e['sequence']!r}")
        if e.get("stream_wave") and e["stream_wave"] not in waves:
            err(path, f"unknown stream wave {e['stream_wave']!r}")
        if e.get("enabled") and not (e.get("sequence") or e.get("stream_wave")):
            err(path, "enabled without sequence or stream wave", "warn")
        sc = e.get("scale", 1.0)
        if not isinstance(sc, (int, float)) or not 0 < sc <= 4:
            err(path, f"scale {sc!r} out of range")
    for i, r in enumerate(project.get("references", [])):
        if not isinstance(r.get("fs"), (int, float)) or r["fs"] <= 0:
            err(f"references[{i}].fs", "must be > 0")
    return issues


def _validate_wave(w: dict, path: str, err) -> None:
    prov = str(w.get("provenance", ""))
    if not PROVENANCE_RE.match(prov):
        err(f"{path}.provenance", f"invalid provenance {prov!r}")
    segs = w.get("segments") or []
    if not segs:
        err(path, "no segments")
    for j, s in enumerate(segs):
        sp = f"{path}.segments[{j}]"
        t = s.get("type")
        if t not in SEGMENT_TYPES:
            err(sp, f"unknown type {t!r}")
            continue
        if t in ("tone", "gap"):
            ln = s.get("length") or {}
            if ln.get("unit") not in LENGTH_UNITS:
                err(sp, f"length unit {ln.get('unit')!r}")
            if not isinstance(ln.get("value"), (int, float)) or ln.get("value") <= 0:
                err(sp, "length value must be > 0")
        if t == "tone":
            for key in ("freq", "amp"):
                spec = s.get(key) or {}
                pts = spec.get("points") or []
                if len(pts) < 1 or any(len(p) != 2 for p in pts):
                    err(f"{sp}.{key}", "needs [[u, value], ...] points")
                    continue
                if any(not 0 <= p[0] <= 1 for p in pts):
                    err(f"{sp}.{key}", "u must be within 0..1")
                if spec.get("interp", "linear") not in INTERPS:
                    err(f"{sp}.{key}", f"interp {spec.get('interp')!r}")
                if key == "freq" and any(p[1] <= 0 for p in pts):
                    err(f"{sp}.freq", "frequency must be > 0")
                if key == "amp" and any(abs(p[1]) > 2 for p in pts):
                    err(f"{sp}.amp", "|amp| > 2", "warn")
            sh = s.get("shape") or {}
            if sh.get("kind", "sine") not in ("sine", "triangle"):
                err(f"{sp}.shape", f"kind {sh.get('kind')!r}")
            if not 0 <= float(sh.get("sharp", 0)) <= 1:
                err(f"{sp}.shape", "sharp must be 0..1")
            if (s.get("phase") or {}).get("mode", "continue") not in ("continue", "absolute"):
                err(f"{sp}.phase", "mode must be continue|absolute")
        if t == "pcm":
            try:
                base64.b64decode(s.get("data_b64", ""), validate=True)
            except Exception:
                err(sp, "data_b64 is not valid base64")
            if s.get("encoding", "int8") not in ("int8", "f32"):
                err(sp, f"encoding {s.get('encoding')!r}")
