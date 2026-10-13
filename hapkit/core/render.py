"""Wave segment renderer (stdlib only, deterministic)."""
from __future__ import annotations

import base64
import math
import struct

from .ramfmt import bytes_to_int8

TWO_PI = 2.0 * math.pi
MAX_SAMPLES = 24000 * 10


def qround(x: float) -> int:
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _interp(points: list, u: float, mode: str, positive: bool = False) -> float:
    if u <= points[0][0]:
        return float(points[0][1])
    if u >= points[-1][0]:
        return float(points[-1][1])
    for (u0, v0), (u1, v1) in zip(points, points[1:]):
        if u0 <= u <= u1:
            t = 0.0 if u1 == u0 else (u - u0) / (u1 - u0)
            return _blend(float(v0), float(v1), t, mode, positive)
    return float(points[-1][1])


def _blend(v0: float, v1: float, t: float, mode: str, positive: bool) -> float:
    if mode == "cos":
        t = (1.0 - math.cos(math.pi * t)) / 2.0
    elif mode == "exp":
        if positive and v0 > 0 and v1 > 0:
            return v0 * (v1 / v0) ** t
        if not positive and v0 * v1 > 0 and min(abs(v0), abs(v1)) >= 1e-4:
            return math.copysign(abs(v0) * (abs(v1) / abs(v0)) ** t, v0)
    return v0 + (v1 - v0) * t


def _shape_fn(shape: dict):
    kind = shape.get("kind", "sine")
    if kind == "triangle":
        return lambda p: (2.0 / math.pi) * math.asin(max(-1.0, min(1.0, math.sin(p))))
    sharp = float(shape.get("sharp", 0.0))
    if sharp <= 0.0:
        return math.sin
    g = 1.0 + 19.0 * sharp
    norm = math.tanh(g)
    return lambda p: math.tanh(g * math.sin(p)) / norm


def _points(spec: dict | None, default: float) -> list:
    pts = (spec or {}).get("points") or [[0, default], [1, default]]
    pts = sorted(([float(u), float(v)] for u, v in pts), key=lambda p: p[0])
    return pts


class _State:
    def __init__(self, fs: int, f0: float):
        self.fs = fs
        self.f0 = f0
        self.phi = 0.0
        self.residue = 0.0
        self.out: list[float] = []
        self.warnings: list[str] = []


def _n_samples(length: dict, fs: int) -> int:
    unit, val = length.get("unit", "ms"), float(length.get("value", 0))
    n = qround(val) if unit == "samples" else qround(val * fs / 1000.0)
    return max(1, n)


def _guard(st: _State, n: int, what: str) -> None:
    if len(st.out) + n > MAX_SAMPLES:
        raise ValueError(f"{what}: wave longer than {MAX_SAMPLES} samples")


def _tone(st: _State, seg: dict, idx: int) -> None:
    freq = seg.get("freq") or {}
    fscale = st.f0 if freq.get("unit", "f0") == "f0" else 1.0
    fpts = [[u, v * fscale] for u, v in _points(freq, 1.0)]
    if any(v <= 0 for _, v in fpts):
        raise ValueError(f"segment {idx}: frequency must be > 0")
    fmode = freq.get("interp", "linear")
    amp = seg.get("amp") or {}
    apts, amode = _points(amp, 1.0), amp.get("interp", "linear")
    shape = _shape_fn(seg.get("shape") or {})
    phase = seg.get("phase") or {}
    offset = math.radians(float(phase.get("deg", 0.0)))
    if phase.get("mode", "continue") == "absolute":
        st.phi, st.residue = offset, 0.0
    else:
        st.phi += offset
    length = seg.get("length") or {"unit": "cycles", "value": 1}
    out, fs = st.out, st.fs
    if length.get("unit") == "cycles":
        target = TWO_PI * float(length["value"])
        if target <= 0:
            raise ValueError(f"segment {idx}: cycles must be > 0")
        acc = st.residue if st.residue < target else 0.0
        start = len(out)
        while acc < target - 1e-9:
            u = acc / target
            out.append(_interp(apts, u, amode) * shape(st.phi))
            d = TWO_PI * _interp(fpts, u, fmode, positive=True) / fs
            st.phi += d
            acc += d
            if len(out) - start > MAX_SAMPLES:
                raise ValueError(f"segment {idx}: too long")
        if len(out) == start:
            out.append(_interp(apts, 0.0, amode) * shape(st.phi))
            d = TWO_PI * _interp(fpts, 0.0, fmode, positive=True) / fs
            st.phi += d
            acc += d
        st.residue = acc - target
    else:
        n = _n_samples(length, fs)
        _guard(st, n, f"segment {idx}")
        for k in range(n):
            u = k / n
            out.append(_interp(apts, u, amode) * shape(st.phi))
            st.phi += TWO_PI * _interp(fpts, u, fmode, positive=True) / fs
        st.residue = 0.0
    st.phi = math.fmod(st.phi, TWO_PI)


def _gap(st: _State, seg: dict, idx: int) -> None:
    length = seg.get("length") or {"unit": "ms", "value": 1}
    d = TWO_PI * st.f0 / st.fs
    if length.get("unit") == "cycles":
        target = TWO_PI * float(length["value"])
        acc = st.residue if st.residue < target else 0.0
        n = 0
        while acc < target - 1e-9:
            n += 1
            acc += d
        n = max(n, 1)
        st.residue = max(0.0, acc - target)
    else:
        n = _n_samples(length, st.fs)
        st.residue = 0.0
    _guard(st, n, f"segment {idx}")
    st.out.extend([0.0] * n)
    st.phi = math.fmod(st.phi + n * d, TWO_PI)


def decode_pcm(seg: dict) -> list[float]:
    raw = base64.b64decode(seg.get("data_b64", ""))
    if seg.get("encoding", "int8") == "f32":
        vals = list(struct.unpack(f"<{len(raw) // 4}f", raw[: len(raw) // 4 * 4]))
    else:
        vals = [v / 127.0 for v in bytes_to_int8(raw)]
    trim = seg.get("trim")
    if trim:
        vals = vals[int(trim[0]): int(trim[1])]
    return vals


def _pcm(st: _State, seg: dict, idx: int) -> None:
    vals = decode_pcm(seg)
    gain = float(seg.get("gain", 1.0))
    src_fs = int(seg.get("fs", st.fs))
    if src_fs != st.fs and vals:
        st.warnings.append(f"segment {idx}: pcm at {src_fs} Hz linearly resampled to {st.fs} Hz")
        n = max(1, qround(len(vals) * st.fs / src_fs))
        res = []
        for k in range(n):
            x = k * src_fs / st.fs
            i = int(x)
            fr = x - i
            a = vals[min(i, len(vals) - 1)]
            b = vals[min(i + 1, len(vals) - 1)]
            res.append(a + (b - a) * fr)
        vals = res
    _guard(st, len(vals), f"segment {idx}")
    st.out.extend(v * gain for v in vals)
    st.phi = 0.0
    st.residue = 0.0


_SEGMENTS = {"tone": _tone, "gap": _gap, "pcm": _pcm}


def quantize(values: list[float]) -> tuple[list[int], int]:
    samples, clips = [], 0
    for v in values:
        q = qround(v * 127.0)
        if q > 127:
            q, clips = 127, clips + 1
        elif q < -127:
            q, clips = -127, clips + 1
        samples.append(q)
    return samples, clips


def render_wave(wave: dict, device: dict) -> dict:
    st = _State(int(device["fs"]), float(device["f0"]))
    bounds = []
    for idx, seg in enumerate(wave.get("segments") or []):
        fn = _SEGMENTS.get(seg.get("type"))
        if fn is None:
            raise ValueError(f"segment {idx}: unknown type {seg.get('type')!r}")
        start = len(st.out)
        fn(st, seg, idx)
        bounds.append({"start": start, "end": len(st.out), "label": seg.get("label") or seg["type"],
                       "index": idx, "type": seg["type"],
                       "brake": seg["type"] == "tone" and _is_brake(seg)})
    gain = float((wave.get("post") or {}).get("gain", 1.0))
    values = [v * gain for v in st.out]
    samples, clips = quantize(values)
    if clips:
        st.warnings.append(f"{clips} samples clipped")
    return {"fs": st.fs, "samples": samples, "float": values, "bounds": bounds, "clips": clips,
            "warnings": st.warnings}


def _is_brake(seg: dict) -> bool:
    pts = _points(seg.get("amp"), 1.0)
    return all(v <= 0 for _, v in pts) and any(v < 0 for _, v in pts)
