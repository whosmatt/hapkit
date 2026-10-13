"""WAV (PCM int / IEEE float, any channel count) and raw PCM readers."""
from __future__ import annotations

import struct

import numpy as np


def read_wav(data: bytes, channel: str = "first") -> tuple[np.ndarray, int, dict]:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8: pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
            if fmt[0] == 0xFFFE and len(body) >= 26:
                fmt = (struct.unpack("<H", body[24:26])[0],) + fmt[1:]
        elif cid == b"data":
            raw = body
        pos += 8 + size + (size & 1)
    if fmt is None or raw is None:
        raise ValueError("missing fmt or data chunk")
    tag, ch, fs, _, _, bits = fmt
    width = bits // 8
    n = len(raw) // (width * ch)
    raw = raw[: n * width * ch]
    if tag == 3 and bits in (32, 64):
        x = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float64)
    elif tag == 1 and bits == 8:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif tag == 1 and bits == 16:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif tag == 1 and bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64) / 8388608.0
    elif tag == 1 and bits == 32:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV format tag {tag}, {bits} bit")
    x = x.reshape(-1, ch)
    x = x.mean(axis=1) if channel == "mix" else x[:, 0]
    return x, fs, {"channels": ch, "bits": bits, "format_tag": tag, "frames": n}


def read_raw(data: bytes, encoding: str = "int8") -> np.ndarray:
    if encoding == "int8":
        return np.frombuffer(data, dtype=np.int8).astype(np.float64) / 127.0
    if encoding == "int16le":
        return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float64) / 32767.0
    if encoding == "f32":
        return np.frombuffer(data[: len(data) // 4 * 4], dtype="<f4").astype(np.float64)
    raise ValueError(f"unknown raw encoding {encoding!r}")
