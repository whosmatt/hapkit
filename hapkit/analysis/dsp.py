"""Signal analysis for haptic waveforms (float input, -1..1 = int8 full scale)."""
from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy import signal

MAX_FRAMES = 4000


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def spectrogram(x, fs: int, win: int = 256, hop: int = 8, nfft: int = 2048, fmax: float = 1000.0,
                db_range: float = 60.0) -> dict:
    x = _arr(x)
    win = int(max(16, win))
    nfft = int(max(nfft, win))
    pad = win // 2
    xp = np.concatenate([np.zeros(pad), x, np.zeros(pad)])
    hop = int(max(1, hop))
    nt = (len(xp) - win) // hop + 1
    if nt > MAX_FRAMES:
        hop = int(np.ceil((len(xp) - win) / (MAX_FRAMES - 1)))
        nt = (len(xp) - win) // hop + 1
    if nt < 1:
        xp = np.concatenate([xp, np.zeros(win - len(xp))])
        nt = 1
    w = signal.get_window("hann", win, fftbins=True)
    frames = np.lib.stride_tricks.sliding_window_view(xp, win)[::hop][:nt]
    spec = np.abs(np.fft.rfft(frames * w, n=nfft, axis=1)) / (w.sum() / 2.0)
    df = fs / nfft
    nf = int(min(spec.shape[1], np.floor(fmax / df) + 1))
    db = 20.0 * np.log10(spec[:, :nf] + 1e-12)
    db_max, db_min = 0.0, -float(db_range)
    q = np.clip((db - db_min) / (db_max - db_min), 0.0, 1.0)
    return {"t0_ms": 0.0, "dt_ms": hop * 1000.0 / fs, "df_hz": df, "nt": int(nt), "nf": int(nf),
            "db_min": db_min, "db_max": db_max, "hop": hop, "win": win,
            "data": np.round(q * 255.0).astype(np.uint8)}


def spectrum(x, fs: int, nfft: int = 8192, fmax: float = 1000.0) -> dict:
    x = _arr(x)
    n = len(x)
    if n == 0:
        return {"f": [], "db": []}
    nfft = int(max(nfft, 1 << int(np.ceil(np.log2(max(n, 2))))))
    mag = np.abs(np.fft.rfft(x, n=nfft)) * 2.0 / n
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    keep = f <= fmax
    return {"f": f[keep].tolist(), "db": (20.0 * np.log10(mag[keep] + 1e-9)).tolist()}


def envelope(x, fs: int, smooth_ms: float = 1.0) -> np.ndarray:
    x = _arr(x)
    if len(x) < 2:
        return np.abs(x)
    env = np.abs(signal.hilbert(x))
    k = int(max(1, round(smooth_ms * fs / 1000.0)))
    if k > 1:
        env = np.convolve(env, np.ones(k) / k, mode="same")
    return env


def _crossings(x: np.ndarray) -> np.ndarray:
    """Fractional sample positions of sign changes (zero runs count once, at their centre)."""
    s = np.sign(x)
    nz = np.nonzero(s)[0]
    if len(nz) < 2:
        return np.array([])
    a, b = nz[:-1], nz[1:]
    flip = s[a] != s[b]
    a, b = a[flip], b[flip]
    adjacent = b == a + 1
    t = np.where(adjacent, a + x[a] / (x[a] - x[b] + 1e-300), (a + b) / 2.0)
    return t


def half_cycle_peaks(x) -> list[tuple[int, float]]:
    x = _arr(x)
    t = _crossings(x)
    edges = np.concatenate([[0], np.ceil(t).astype(int), [len(x)]])
    out = []
    for s, e in zip(edges[:-1], edges[1:]):
        if e > s:
            i = s + int(np.argmax(np.abs(x[s:e])))
            out.append((i, float(x[i])))
    return out


def inst_freq(x, fs: int, min_amp: float = 0.02) -> list[list[float]]:
    x = _arr(x)
    t = _crossings(x)
    out = []
    for t0, t1 in zip(t[:-1], t[1:]):
        half = t1 - t0
        if half <= 0:
            continue
        seg = x[int(np.floor(t0)): int(np.ceil(t1)) + 1]
        if len(seg) == 0 or np.max(np.abs(seg)) < min_amp:
            continue
        out.append([float((t0 + t1) / 2.0 * 1000.0 / fs), float(fs / (2.0 * half))])
    return out


def resample(x, fs_in: float, fs_out: float) -> np.ndarray:
    x = _arr(x)
    if fs_in == fs_out or len(x) == 0:
        return x.copy()
    r = Fraction(fs_out / fs_in).limit_denominator(2000)
    return signal.resample_poly(x, r.numerator, r.denominator)


def retarget(x, fs: int, f_ref: float, f_target: float, mode: str = "timescale") -> np.ndarray:
    """Move a reference waveform from an actuator with f_ref to one with f_target.

    timescale: resample in time so every cycle is preserved (duration scales by f_ref/f_target).
    envelope: keep duration; demodulate against a carrier at f_ref (complex envelope keeps amplitude,
              polarity flips/brakes and frequency offsets) and remodulate at f_target.
    """
    x = _arr(x)
    if mode == "none" or f_ref <= 0 or f_target <= 0 or len(x) < 2:
        return x.copy()
    if mode == "timescale":
        return resample(x, fs, fs * f_ref / f_target)
    if mode == "envelope":
        n = np.arange(len(x)) / fs
        z = signal.hilbert(x) * np.exp(-2j * np.pi * f_ref * n)
        return np.real(z * np.exp(2j * np.pi * f_target * n))
    raise ValueError(f"unknown retarget mode {mode!r}")
