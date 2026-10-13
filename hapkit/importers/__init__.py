"""Import dispatcher: bytes -> references / waves for a project."""
from __future__ import annotations

import base64
import os
import re

import numpy as np

from ..analysis.dsp import resample
from . import carray, csvfile, wavfile


def make_id(name: str, taken: set[str], limit: int = 40) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "item"
    base = base[:limit]
    cand, i = base, 2
    while cand in taken:
        suffix = f"_{i}"
        cand = base[: limit - len(suffix)] + suffix
        i += 1
    taken.add(cand)
    return cand


def f32_b64(x) -> str:
    return base64.b64encode(np.asarray(x, dtype="<f4").tobytes()).decode()


def ref_samples(ref: dict) -> np.ndarray:
    return np.frombuffer(base64.b64decode(ref["data_b64"]), dtype="<f4").astype(np.float64)


def to_int8(x) -> list[int]:
    q = np.clip(np.round(np.asarray(x, dtype=np.float64) * 127.0), -127, 127)
    return q.astype(np.int64).tolist()


def wave_from_samples(wid: str, name: str, x, fs: int, device_fs: int, provenance: str) -> dict:
    x = np.asarray(x, dtype=np.float64)
    if fs != device_fs:
        x = resample(x, fs, device_fs)
    data = bytes(v & 0xFF for v in to_int8(x))
    return {"id": wid, "name": name, "provenance": provenance,
            "segments": [{"type": "pcm", "fs": device_fs, "encoding": "int8",
                          "data_b64": base64.b64encode(data).decode(), "gain": 1.0, "trim": None, "label": name}],
            "post": {"gain": 1.0}, "notes": ""}


def _taken(project: dict) -> set[str]:
    return {x["id"] for k in ("waves", "sequences", "effects", "references") for x in project.get(k, [])}


def import_data(kind: str, filename: str, data: bytes, options: dict, project: dict) -> dict:
    options = options or {}
    stem = os.path.splitext(os.path.basename(filename or kind))[0]
    taken = _taken(project)
    device_fs = int(project["device"]["fs"])
    proprietary = bool(options.get("proprietary", False))
    items: list[tuple[str, np.ndarray, int, dict]] = []
    result = {"references": [], "waves": [], "ram_order": None, "report": ""}

    if kind == "ram_bin":
        from ..core.project import waves_from_ram_bin
        prefix = make_id(options.get("prefix") or stem, set(taken), 20)
        waves, order, report = waves_from_ram_bin(data, prefix, options.get("provenance", "stock"))
        clash = [w["id"] for w in waves if w["id"] in taken]
        if clash:
            raise ValueError(f"ids already in project: {', '.join(clash)} (choose another prefix)")
        result.update(waves=waves, ram_order=order, report=report)
        return result
    if kind == "carray":
        text = data.decode("utf-8", errors="replace")
        arrays = carray.extract(text, int(options.get("fs", 24000)))
        for a in arrays:
            label = f" {a['label']}" if a.get("label") else ""
            items.append((f"{stem}:{a['name']}{label}", np.asarray(a["values"], dtype=np.float64) / 127.0,
                          int(a["fs"]), {k: a[k] for k in ("kind", "aidl", "label", "table", "line") if k in a}))
        result["report"] = f"{len(arrays)} int8 arrays from {filename}"
    elif kind == "wav":
        x, fs, meta = wavfile.read_wav(data, options.get("channel", "first"))
        items.append((stem, x, fs, meta))
        result["report"] = f"{filename}: {meta['frames']} frames @ {fs} Hz, {meta['bits']} bit, {meta['channels']} ch"
    elif kind == "raw":
        enc = options.get("encoding", "int8")
        fs = int(options.get("fs", device_fs))
        x = wavfile.read_raw(data, enc)
        items.append((stem, x, fs, {"encoding": enc}))
        result["report"] = f"{filename}: {len(x)} samples {enc} @ {fs} Hz"
    elif kind == "csv":
        x, fs, meta = csvfile.read_csv(data, options)
        items.append((stem, x, int(round(fs)), meta))
        result["report"] = f"{filename}: {meta['rows']} rows @ {meta['fs']} Hz, axis {meta['axis']}"
    elif kind == "cirrus":
        from . import cirrus
        return cirrus.import_cirrus(filename, data, options, project)
    else:
        raise ValueError(f"unknown import kind {kind!r}")

    as_wave = options.get("as", "reference") == "wave"
    for name, x, fs, meta in items:
        source = f"{kind}:{os.path.basename(filename or '')}" + (f"#{name.split(':', 1)[1].split()[0]}" if ":" in name else "")
        if as_wave:
            wid = make_id(name, taken)
            prov = options.get("provenance") or f"reference:{make_id(name, set(), 60)}"
            result["waves"].append(wave_from_samples(wid, name, x, fs, device_fs, prov))
        else:
            rid = make_id(name, taken)
            result["references"].append({"id": rid, "name": name, "source": source, "fs": fs,
                                         "data_b64": f32_b64(x), "proprietary": proprietary, "meta": meta})
    return result
