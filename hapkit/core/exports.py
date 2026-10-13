"""Exports: haptic_ram.bin, kernel sequence table, QTI effect-stream C source, raw/WAV."""
from __future__ import annotations

import hashlib
import io
import json
import struct
import wave as wavemod

from .. import __version__
from . import ids, ramfmt
from .project import canonical_json, strip_reference_data
from .render import render_wave
from .sequence import effect_sequence, render_effect, seq_registers

PRIMITIVE_ID_MASK = 0x8000  # QTI HAL: get_effect_stream(primitive | PRIMITIVE_ID_MASK)


def project_hash(project: dict) -> str:
    return hashlib.sha256(canonical_json(strip_reference_data(project))).hexdigest()[:16]


def _provenance_check(project: dict, wave_ids: list[str], upstream: bool) -> list[str]:
    waves = {w["id"]: w for w in project.get("waves", [])}
    bad, notes = [], []
    for wid in wave_ids:
        prov = waves[wid].get("provenance", "")
        if prov == "stock" or prov.startswith("reference:"):
            bad.append(f"{wid} ({prov})")
        elif prov.startswith("traced:"):
            notes.append(f"warning: {wid} is {prov}")
    if upstream and bad:
        raise ValueError("upstream export refused, non-original waves: " + ", ".join(bad))
    return notes


def _header(project: dict, wave_ids: list[str], comment: str = "c") -> list[str]:
    waves = {w["id"]: w for w in project.get("waves", [])}
    dev = project.get("device", {})
    lines = [f"hapkit {__version__}, project \"{project.get('name', '')}\", sha256 {project_hash(project)}",
             f"fs {dev.get('fs')} Hz, f0 {dev.get('f0')} Hz (xf0 below is relative to this)",
             "provenance: " + ", ".join(f"{w}={waves[w].get('provenance', '?')}" for w in wave_ids)]
    if comment == "c":
        return ["/*"] + [f" * {ln}" for ln in lines] + [" */"]
    return lines


def ram_wave_samples(project: dict) -> list[tuple[str, list[int]]]:
    waves = {w["id"]: w for w in project.get("waves", [])}
    return [(wid, render_wave(waves[wid], project["device"])["samples"])
            for wid in project.get("ram", {}).get("waves", [])]


def export_ram_bin(project: dict, upstream: bool = False) -> tuple[bytes, dict]:
    dev = project["device"]
    rendered = ram_wave_samples(project)
    if not rendered:
        raise ValueError("RAM list is empty")
    notes = _provenance_check(project, [wid for wid, _ in rendered], upstream)
    data = [ramfmt.int8_to_bytes(s) for _, s in rendered]
    blob = ramfmt.build(data, int(dev["base_addr"]), int(dev["ram_version"]), int(dev["ram_end"]))
    lay = ramfmt.layout([len(d) for d in data], int(dev["base_addr"]), int(dev["ram_end"]))
    report = {
        "file_bytes": len(blob),
        "checksum": f"0x{blob[0] << 8 | blob[1]:04x}",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "used": lay["used"], "capacity": lay["capacity"], "free": lay["free"],
        "waves": [{"index": i + 1, "id": wid, "start": f"0x{s:04x}", "end": f"0x{e:04x}", "samples": e - s + 1,
                   "ms": round((e - s + 1) * 1000.0 / dev["fs"], 3)}
                  for i, ((wid, _), (s, e)) in enumerate(zip(rendered, lay["spans"]))],
        "notes": notes,
    }
    return blob, report


def _enabled(project: dict) -> list[dict]:
    return [e for e in project.get("effects", []) if e.get("enabled")]


def _c_name(e: dict) -> str:
    return ("AW_FF_EFFECT_" if e["kind"] == "effect" else "AW_FF_PRIMITIVE_") + e.get(
        "name", ids.name_of(e["kind"], e["aidl"])).upper()


def _kernel_wait(units: int, device: dict, ms: float) -> str:
    """A wait slot as haptic_hv writes it: AW_SEQ_WAIT_MS(ms) when the driver's macro gives the same unit count
    (the designed ms if possible, else the closest), else the raw register value."""
    fs, unit = int(device.get("fs", 24000)), int(device.get("wait_unit", 64))
    fits = [m for m in range(1, 1000) if (m * (fs // 1000) + unit // 2) // unit == units]
    if not fits:
        return f"AW_SEQ_WAIT_FLAG | {units}"
    return f"AW_SEQ_WAIT_MS({min(fits, key=lambda m: abs(m - ms))})"


def _kernel_row(name: str, slots: list[str], loops: list[int], scale: int) -> list[str]:
    """One table entry in the driver's layout: values start at column 40, lines stay within 100 columns."""
    def width(t: str) -> int:
        return len(t.expandtabs(8))
    head = f"\t{{ {name},"
    head += "\t" * max(1, (40 - width(head) + 7) // 8)
    while loops and loops[-1] == 0 and len(loops) > 1:
        loops = loops[:-1]
    tail = f"{{ {', '.join(map(str, loops))} }}, {scale} }},"
    one = f"{head}{{ {', '.join(slots)} }}, {tail}"
    if width(one) <= 100:
        return [one]
    seq = f"{head}{{ {', '.join(slots)} }},"
    if width(seq) <= 100:
        return [seq, "\t" * 5 + tail]
    lines, cur = [], f"{head}{{ {slots[0]},"
    for v in slots[1:]:
        nxt = f"{cur} {v},"
        if width(nxt) > 100:
            lines.append(cur)
            cur = "\t" * 5 + f"  {v},"
        else:
            cur = nxt
    cur = cur[:-1] + f" }}, {tail}"
    return lines + [cur]


def export_kernel_table(project: dict, upstream: bool = False) -> str:
    """C table for haptic_hv: struct aw_ff_effect { u16 id; u8 seq[8]; u8 loop[8]; u8 scale; } per enabled
    effect/primitive, laid out as in drivers/misc/haptic_hv/haptic_hv.c. seq: RAM wave index or a wait;
    loop: extra plays of a wave; scale: percent of the requested gain. NOOP is added before the primitives
    (plays nothing). The RAM blob it indexes is named by sha256 in the header."""
    rows, used = [], []
    notes_extra = []
    noop = False
    for e in _enabled(project):
        seq = effect_sequence(e, project)
        if seq is None:
            continue
        regs = seq_registers(seq, project)
        n = regs["count"]
        used += [sl["wave"] for sl in seq["slots"] if sl.get("kind") == "wave"]
        slots = [_kernel_wait(v & 0x7f, project["device"], sl.get("ms", 0)) if v & 0x80 else str(v)
                 for v, sl in zip(regs["wavcfg"][:n], seq["slots"])]
        loops = list(regs["loops"][:n])
        scale = int(round(float(e.get("scale", 1.0)) * 100))
        if scale > 100:
            notes_extra.append(f"note: {_c_name(e)} scale {scale}% clamped to 100%")
            scale = 100
        if regs["mainloop"]:
            notes_extra.append(f"warning: {_c_name(e)} uses MAINLOOP {regs['mainloop']}, which this table cannot express")
        if e["kind"] != "effect" and not noop:
            rows += _kernel_row("AW_FF_PRIMITIVE_NOOP", ["0"], [0], 100)
            noop = True
        rows += _kernel_row(_c_name(e), slots, loops, scale)
    notes = _provenance_check(project, sorted(set(used)), upstream) + notes_extra
    order = project.get("ram", {}).get("waves", [])
    out = _header(project, order)
    try:
        blob, _ = export_ram_bin(project)
        out.insert(-1, f" * haptic_ram.bin sha256 {hashlib.sha256(blob).hexdigest()} ({len(blob)} B)")
    except ValueError:
        pass
    out += [f"/* {n} */" for n in notes]
    out.append("static const struct aw_ff_effect aw_ff_effects[] = {")
    out += rows
    out.append("};")
    return "\n".join(out) + "\n"


def _fmt_spec(spec: dict, unit: str = "") -> str:
    pts = spec.get("points") or []
    vals = [p[1] for p in pts]
    if len(pts) <= 2 and pts and pts[0][0] == 0 and pts[-1][0] == 1:
        txt = f"{vals[0]:g}{unit}" if len(set(vals)) == 1 else "->".join(f"{v:g}" for v in vals) + unit
    else:
        txt = " ".join(f"{u:g}:{v:g}" for u, v in pts) + unit
    interp = spec.get("interp", "linear")
    return txt + (f" ({interp})" if interp != "linear" and len(set(vals)) > 1 else "")


def _recipe_lines(wave: dict) -> list[str]:
    lines = []
    for i, s in enumerate(wave.get("segments", [])):
        ln = s.get("length", {})
        length = f"{ln.get('value', 0):g} {ln.get('unit', '')}"
        if s["type"] == "pcm":
            lines.append(f"  {i}: pcm {s.get('label', '')}".rstrip())
        elif s["type"] == "gap":
            lines.append(f"  {i}: gap {length}")
        else:
            freq = s.get("freq") or {}
            parts = [f"{i}: tone {length}", "f " + _fmt_spec(freq, "xf0" if freq.get("unit", "f0") == "f0" else " Hz"),
                     "a " + _fmt_spec(s.get("amp") or {})]
            sh = s.get("shape") or {}
            if sh.get("kind", "sine") != "sine" or sh.get("sharp", 0):
                parts.append(f"{sh.get('kind', 'sine')} sharp {sh.get('sharp', 0):g}")
            ph = s.get("phase") or {}
            if ph.get("mode") == "absolute" or ph.get("deg", 0):
                parts.append(f"phase {ph.get('mode', 'continue')} {ph.get('deg', 0):g} deg")
            lines.append("  " + ", ".join(parts))
    if (wave.get("post") or {}).get("gain", 1.0) != 1.0:
        lines.append(f"  post gain {wave['post']['gain']:g}")
    return lines


def export_effect_stream_c(project: dict, upstream: bool = False) -> str:
    dev = project["device"]
    waves = {w["id"]: w for w in project.get("waves", [])}
    seqs = {s["id"]: s for s in project.get("sequences", [])}
    entries, used = [], []
    for e in _enabled(project):
        if not (e.get("sequence") or e.get("stream_wave")):
            continue
        r = render_effect(e, project, "stream")
        if e.get("stream_wave"):
            src = [e["stream_wave"]]
        else:
            src = [sl["wave"] for sl in seqs[e["sequence"]]["slots"] if sl.get("kind") == "wave"]
        used += src
        entries.append((e, r["samples"], src))
    notes = _provenance_check(project, sorted(set(used)), upstream)
    out = _header(project, sorted(set(used)))
    out += [f"/* {n} */" for n in notes]
    out += ["", "#include \"effect.h\"", "", "#include <stddef.h>", "",
            f"#define PRIMITIVE_ID_MASK 0x{PRIMITIVE_ID_MASK:04x}", ""]
    recipes = []
    for wid in sorted(set(used)):
        recipes.append(f" * {wid}:")
        recipes += [f" *{ln}" for ln in _recipe_lines(waves[wid])]
    if recipes:
        out += ["/*", " * recipes (hapkit segments):"] + recipes + [" */", ""]
    for e, samples, src in entries:
        name = f"{e['kind']}_{e['name'].lower()}_data"
        out.append(f"/* {e['kind']} {e['name']} ({e['aidl']}): {len(samples)} samples, "
                   f"{len(samples) * 1000.0 / dev['fs']:.2f} ms, from {', '.join(src)} */")
        out.append(f"static const int8_t {name}[] = {{")
        for i in range(0, len(samples), 16):
            out.append("\t" + ", ".join(str(v) for v in samples[i:i + 16]) + ",")
        out.append("};")
        out.append("")
    out.append("static const struct effect_stream effects[] = {")
    for e, samples, _ in entries:
        name = f"{e['kind']}_{e['name'].lower()}_data"
        eid = str(e["aidl"]) if e["kind"] == "effect" else f"PRIMITIVE_ID_MASK | {e['aidl']}"
        out.append("\t{")
        out.append(f"\t\t.effect_id = {eid},")
        out.append(f"\t\t.length = sizeof({name}),")
        out.append(f"\t\t.play_rate_hz = {int(dev['fs'])},")
        out.append(f"\t\t.data = {name},")
        out.append("\t},")
    out += ["};", "",
            "const struct effect_stream *get_effect_stream(uint32_t effect_id)",
            "{",
            "\tfor (size_t i = 0; i < sizeof(effects) / sizeof(effects[0]); i++) {",
            "\t\tif (effects[i].effect_id == effect_id)",
            "\t\t\treturn &effects[i];",
            "\t}",
            "",
            "\treturn NULL;",
            "}"]
    return "\n".join(out) + "\n"


def export_raw(samples: list[int]) -> bytes:
    return ramfmt.int8_to_bytes(samples)


def export_wav(samples: list[int], fs: int) -> bytes:
    buf = io.BytesIO()
    with wavemod.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(fs))
        w.writeframes(struct.pack(f"<{len(samples)}h", *(s * 256 for s in samples)))
    return buf.getvalue()
