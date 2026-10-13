"""Cirrus Logic CS40L26 wavetable (coefficient file "WMDR", e.g. Pixel /vendor/firmware/cs40l26.bin).

Container (Linux cs_dsp / Pixel cl_dsp): 16-byte header {magic "WMDR", le32 header_len, le32 ver, le32 core};
blocks {le16 offset, le16 type, le32 alg_id, le32 alg_ver, le32 sample_rate, le32 len, data[len]} padded to 4.
Type 5 (ADSP2 XM) carries 24-bit DSP words as big-endian u32. The wavetable is the XM block of the vibegen
algorithm (alg id 0x400BD): a header of {flags:16 type:8, offset:24, size:24} entries (offsets/sizes in words),
terminated by 0xFFFFFF, followed by the wave bodies. Each body starts with a 24-bit length in samples @ 8 kHz
(bit 23: length calculated, bit 22: indefinite).

  type 8/9  PCM (F0/ReDC compensated): [wlen][f0 Q10.14 Hz, 0 = none][int8 samples, 3 per word, MSB first]
  type 12   PWLE: [wlen] repeat:8 wait:12 nsections:8, then per section delay:16 (0.25 ms) level:12
            (signed Q1.11) freq:12 (0.25 Hz) flags:8 ((chirp 8|brake 4|amp_reg 2|1) << 4) [+ vbemf:24 if amp_reg]
  type 10   composite (references other entries) - listed, not rendered
Entries with flags bit 10 carry metadata after the body; SVC metadata {type 1} holds the closed-loop braking time
(word / 8 = ms) the DSP applies after the waveform. PWLE frequencies outside the HAL's 30..300 Hz range (seen on the
buzz entries) are treated as f0 tracking and rendered at f0_track (assumption, marked in the section).

Sources: Pixel kernel cl_dsp.c (cl_dsp_read_wt), cs40l26.c (cs40l26_owt_get_wlength), Pixel vibrator HAL
DspMemChunk.cpp/.h (PWLE packing). Wave index names follow the Pixel cs40l26 HAL WaveformIndex enum.
"""
from __future__ import annotations

import os
import struct

import numpy as np

WAVETABLE_ALG = 0x400BD
FS_WT = 8000
PWLE_F_MIN, PWLE_F_MAX = 30.0, 300.0      # Pixel HAL PWLE_FREQUENCY_{MIN,MAX}_HZ
SVC_ID = 1
PIXEL_CS40L26_NAMES = {
    0: "LONG_VIBRATION", 1: "RESERVED_1", 2: "CLICK", 3: "SHORT_VIBRATION", 4: "THUD", 5: "SPIN",
    6: "QUICK_RISE", 7: "SLOW_RISE", 8: "QUICK_FALL", 9: "LIGHT_TICK", 10: "LOW_TICK",
    11: "RESERVED_MFG_1", 12: "RESERVED_MFG_2", 13: "RESERVED_MFG_3",
}
TYPE_NAMES = {0: "pcm_v4", 1: "pwle_v4", 2: "pcm_f0_redc_v4", 4: "composite_v4", 8: "pcm_f0_redc",
              9: "pcm_f0_redc_var", 10: "composite", 11: "pcm_f0_redc_q", 12: "pwle"}


def parse_wmdr(data: bytes) -> dict:
    if data[:4] != b"WMDR":
        raise ValueError("not a Cirrus coefficient file (no WMDR magic)")
    hlen, ver, core = struct.unpack("<III", data[4:16])
    pos, blocks, texts = hlen, [], []
    while pos + 20 <= len(data):
        off, typ, alg, aver, sr, ln = struct.unpack("<HHIIII", data[pos:pos + 20])
        body = data[pos + 20: pos + 20 + ln]
        if len(body) < ln:
            raise ValueError(f"truncated block at 0x{pos:x}")
        if typ in (0xFE, 0xFF, 0xFE00, 0xFF00):
            texts.append(body.decode("ascii", "replace").rstrip("\x00"))
        blocks.append({"offset": off, "type": typ, "alg": alg, "alg_ver": aver, "sr": sr, "data": body})
        pos = (pos + 20 + ln + 3) & ~3
    return {"header_len": hlen, "ver": ver, "core": core, "blocks": blocks, "texts": texts}


def xm_words(block: dict) -> list[int]:
    d = block["data"]
    return [int.from_bytes(d[i:i + 4], "big") & 0xFFFFFF for i in range(0, len(d) - 3, 4)]


class Bits:
    """Sequential reader over 24-bit DSP words."""

    def __init__(self, words: list[int]):
        self.words, self.pos = words, 0

    def read(self, n: int) -> int:
        v = 0
        for _ in range(n):
            w, b = divmod(self.pos, 24)
            if w >= len(self.words):
                raise ValueError("read past end of wave")
            v = (v << 1) | ((self.words[w] >> (23 - b)) & 1)
            self.pos += 1
        return v


def parse_wavetable(words: list[int]) -> list[dict]:
    entries, i = [], 0
    while i < len(words) and words[i] != 0xFFFFFF:
        if i + 3 > len(words):
            raise ValueError("wavetable header runs past the block")
        w0, off, size = words[i:i + 3]
        entries.append({"index": len(entries), "flags": w0 >> 8, "type": w0 & 0xFF, "offset": off, "size": size})
        i += 3
        if len(entries) > 254:
            raise ValueError("no wavetable terminator")
    for e in entries:
        if e["offset"] + e["size"] > len(words):
            raise ValueError(f"entry {e['index']} exceeds block ({e['offset']}+{e['size']} > {len(words)})")
        e["body"] = words[e["offset"]: e["offset"] + e["size"]]
        e["metadata"] = words[e["offset"] + e["size"]: e["offset"] + e["size"] + 7] if e["flags"] & 0x400 else []
    return entries


def _s12(v: int) -> int:
    return v - 4096 if v & 0x800 else v


def decode_entry(e: dict) -> dict:
    body, typ = e["body"], e["type"]
    wlen = body[0] if body else 0
    info = {"wlen": wlen & 0x3FFFFF, "wlen_calc": bool(wlen & 0x800000), "indefinite": bool(wlen & 0x400000),
            "type_name": TYPE_NAMES.get(typ, f"type{typ}"), "flags": e["flags"]}
    md = e.get("metadata") or []
    for i, w in enumerate(md):
        if w == 0xFFFFFF:
            break
        if (w >> 16) & 0xFF == SVC_ID and i + 1 < len(md):
            info["svc_braking_ms"] = md[i + 1] / 8.0
            break
    if typ in (8, 9, 11):
        n = wlen & 0x3FFFFF
        f0 = body[1] / (1 << 14) if len(body) > 1 else 0.0
        raw = []
        for w in body[2:]:
            raw += [(w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF]
        s = np.array([v - 256 if v > 127 else v for v in raw[:n]], dtype=np.float64) / 128.0
        info.update(kind="pcm", fs=FS_WT, samples=s, design_f0=round(f0, 3))
    elif typ == 12:
        b = Bits(body[1:])
        repeat, wait, nsec = b.read(8), b.read(12), b.read(8)
        secs = []
        for _ in range(nsec):
            delay, level, freq, flags = b.read(16), _s12(b.read(12)), b.read(12), b.read(8)
            f = flags >> 4
            sec = {"t_ms": delay / 4.0, "level": round(level / 2048.0, 5), "freq_hz": freq / 4.0,
                   "chirp": bool(f & 8), "brake": bool(f & 4), "amp_reg": bool(f & 2), "raw_freq": freq}
            if not PWLE_F_MIN <= sec["freq_hz"] <= PWLE_F_MAX:
                sec["f0_track"] = True
            if f & 2:
                sec["vbemf"] = b.read(24)
            secs.append(sec)
        info.update(kind="pwle", repeat=repeat, wait=wait, sections=secs)
    elif typ in (4, 10):
        info.update(kind="composite")
    else:
        info.update(kind="unknown")
    return info


def render_pwle(sections: list[dict], fs: int, f0_track: float = 145.0, max_ms: float = 500.0) -> np.ndarray:
    """Piecewise-linear level; frequency ramps on chirp sections, steps otherwise; brake sections are silent."""
    if not sections:
        return np.zeros(0)
    out, phi, total = [], 0.0, 0
    hz = lambda s: f0_track if s.get("f0_track") else s["freq_hz"]  # noqa: E731
    lvl, f = sections[0]["level"], hz(sections[0])
    cap = int(max_ms * fs / 1000.0)
    for s in sections[1:]:
        n = min(int(round(s["t_ms"] * fs / 1000.0)), cap - total)
        if n <= 0 and total >= cap:
            break
        total += max(n, 0)
        if n <= 0:
            lvl, f = s["level"], s["freq_hz"]
            continue
        u = np.arange(n) / n
        lv = np.zeros(n) if s["brake"] else lvl + (s["level"] - lvl) * u
        fr = f + (hz(s) - f) * u if s["chirp"] else np.full(n, hz(s))
        ph = phi + np.cumsum(2 * np.pi * fr / fs) - 2 * np.pi * fr[0] / fs
        out.append(lv * np.sin(ph))
        phi = ph[-1] + 2 * np.pi * fr[-1] / fs
        lvl, f = (0.0 if s["brake"] else s["level"]), hz(s)
    return np.concatenate(out) if out else np.zeros(0)


def load(data: bytes) -> dict:
    wmdr = parse_wmdr(data)
    wt = [b for b in wmdr["blocks"] if b["alg"] == WAVETABLE_ALG and b["type"] in (5, 0x11)]
    if not wt:
        raise ValueError(f"no wavetable block (alg 0x{WAVETABLE_ALG:x}) in file")
    blk = wt[0]
    entries = parse_wavetable(xm_words(blk))
    for e in entries:
        e.update(decode_entry(e))
    return {"wmdr": wmdr, "entries": entries, "block": blk}


def import_cirrus(filename: str, data: bytes, options: dict, project: dict) -> dict:
    from . import f32_b64, make_id
    wt = load(data)
    taken = {x["id"] for k in ("waves", "sequences", "effects", "references") for x in project.get(k, [])}
    stem = options.get("prefix") or os.path.splitext(os.path.basename(filename or "cs40l26"))[0]
    names = PIXEL_CS40L26_NAMES if options.get("names", "pixel_cs40l26") == "pixel_cs40l26" else {}
    out_fs = int(project["device"]["fs"])
    refs, notes = [], []
    for e in wt["entries"]:
        label = names.get(e["index"], f"#{e['index']}")
        meta = {k: e[k] for k in ("index", "type", "type_name", "flags", "wlen", "indefinite", "svc_braking_ms") if k in e}
        if e["kind"] == "pcm":
            x, fs = e["samples"], FS_WT
            meta["f0_field"] = e["design_f0"]
        elif e["kind"] == "pwle":
            x, fs = render_pwle(e["sections"], out_fs, float(options.get("f0_track", 145.0)),
                                float(options.get("max_ms", 500.0))), out_fs
            meta.update(sections=e["sections"], repeat=e["repeat"], wait=e["wait"])
        else:
            notes.append(f"#{e['index']} {e['type_name']} not rendered")
            continue
        meta["ms"] = round(len(x) * 1000.0 / fs, 3)
        if e["kind"] == "pcm" and e["wlen"]:
            meta["ms_wlen"] = round(e["wlen"] * 1000.0 / FS_WT, 3)
        rid = make_id(f"{stem}_{e['index']}_{label}", taken)
        refs.append({"id": rid, "name": f"{stem} #{e['index']} {label}", "source": f"cirrus:{os.path.basename(filename)}#{e['index']}",
                     "fs": fs, "data_b64": f32_b64(x), "proprietary": bool(options.get("proprietary", True)), "meta": meta})
    texts = [t for t in wt["wmdr"]["texts"] if t]
    report = f"{len(wt['entries'])} wavetable entries, {len(refs)} imported" + (f"; {'; '.join(notes)}" if notes else "") + \
             (f"; {' | '.join(texts)}" if texts else "")
    return {"references": refs, "waves": [], "ram_order": None, "report": report}
