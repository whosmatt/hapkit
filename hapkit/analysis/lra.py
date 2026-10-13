"""Lumped LRA model: second-order resonator driven by the output voltage.

    x'' + (w0/Q) x' + w0^2 x = w0^2 u,   a = x'' = w0^2 u - (w0/Q) x' - w0^2 x

u is the drive (int8/127). Q is q_drive while the output stage drives (back-EMF current adds electrical
damping) and q_free after playback ends (output released, mechanical damping only). Exact zero-order-hold
discretisation while driven; closed-form free response afterwards. acc is normalised so a full-scale
steady-state sine at f0 with Q = q_drive peaks at 1.0. The moving-mass acceleration stands in for the felt
(housing) acceleration, which is its reaction.
"""
from __future__ import annotations

import numpy as np
from scipy import linalg, optimize, signal


def _zoh(w0: float, q: float, fs: float) -> tuple[np.ndarray, np.ndarray]:
    a = np.array([[0.0, 1.0], [-w0 * w0, -w0 / q]])
    b = np.array([[0.0], [w0 * w0]])
    m = np.zeros((3, 3))
    m[:2, :2], m[:2, 2:] = a, b
    e = linalg.expm(m / fs)
    return e[:2, :2], e[:2, 2]


def _driven(u: np.ndarray, w0: float, q: float, fs: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ad, bd = _zoh(w0, q, fs)
    # x[n+1] = ad x[n] + bd u[n]; outputs are states before applying u[n]
    num_x, den = signal.ss2tf(ad, bd.reshape(2, 1), np.array([[1.0, 0.0]]), np.zeros((1, 1)))
    num_v, _ = signal.ss2tf(ad, bd.reshape(2, 1), np.array([[0.0, 1.0]]), np.zeros((1, 1)))
    x = signal.lfilter(num_x[0], den, u)
    v = signal.lfilter(num_v[0], den, u)
    last = ad @ np.array([x[-1], v[-1]]) + bd * u[-1] if len(u) else np.zeros(2)
    return x, v, last


def _free(state: np.ndarray, n: int, w0: float, q: float, fs: float) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(n) / fs
    alpha = w0 / (2.0 * q)
    wd = w0 * np.sqrt(max(1e-12, 1.0 - 1.0 / (4.0 * q * q)))
    x0, v0 = state
    c1, c2 = x0, (v0 + alpha * x0) / wd
    ea = np.exp(-alpha * t)
    cos, sin = np.cos(wd * t), np.sin(wd * t)
    x = ea * (c1 * cos + c2 * sin)
    v = ea * ((c2 * wd - alpha * c1) * cos - (c1 * wd + alpha * c2) * sin)
    return x, v


def simulate(x, fs: int, f0: float, q_drive: float, q_free: float, active_len: int | None = None,
             tail_ms: float = 60.0) -> dict:
    u = np.asarray(x, dtype=np.float64)
    n_act = len(u) if active_len is None else int(min(active_len, len(u)))
    w0 = 2.0 * np.pi * f0
    ud = u[:n_act]
    xd, vd, last = _driven(ud, w0, q_drive, fs)
    acc_d = w0 * w0 * ud - (w0 / q_drive) * vd - w0 * w0 * xd
    n_tail = (len(u) - n_act) + int(round(tail_ms * fs / 1000.0))
    xf, vf = _free(last, n_tail, w0, q_free, fs)
    acc_f = -(w0 / q_free) * vf - w0 * w0 * xf
    norm = w0 * w0 * q_drive
    disp_norm = q_drive
    return {"acc": np.concatenate([acc_d, acc_f]) / norm,
            "disp": np.concatenate([xd, xf]) / disp_norm,
            "vel": np.concatenate([vd, vf]) / (w0 * q_drive),
            "active_len": n_act}


def _env(acc: np.ndarray) -> np.ndarray:
    if len(acc) < 4:
        return np.abs(acc)
    return np.abs(signal.hilbert(acc))


def metrics(sim: dict, fs: int, active_len: int | None = None) -> dict:
    acc = np.asarray(sim["acc"])
    n_act = sim.get("active_len") if active_len is None else active_len
    if len(acc) == 0:
        return {}
    env = _env(acc)
    peak = float(np.max(np.abs(acc)))
    ipk = int(np.argmax(env))
    above10 = np.nonzero(env >= 0.1 * env[ipk])[0]
    above90 = np.nonzero(env >= 0.9 * env[ipk])[0]
    rise = (above90[0] - above10[0]) * 1000.0 / fs if len(above10) and len(above90) else 0.0
    after = np.nonzero(env[n_act:] >= 0.1 * env[ipk])[0]
    ring = (after[-1] + 1) * 1000.0 / fs if len(after) else 0.0
    e = acc * acc
    total = float(e.sum())
    return {"peak_acc": round(peak, 4), "t_peak_ms": round(ipk * 1000.0 / fs, 3), "rise_ms": round(rise, 3),
            "ring_ms": round(ring, 3), "residual_ratio": round(float(e[n_act:].sum()) / total, 5) if total else 0.0,
            "energy": round(total / fs * 1000.0, 5), "active_ms": round(n_act * 1000.0 / fs, 3)}


def robustness(x, fs: int, f0: float, q_drive: float, q_free: float, rel=(-0.05, 0.0, 0.05)) -> list[dict]:
    out = []
    for r in rel:
        sim = simulate(x, fs, f0 * (1.0 + r), q_drive, q_free)
        out.append({"rel": r, "f0": f0 * (1.0 + r), **metrics(sim, fs)})
    return out


def click_db(x, fs: int, f0: float, start: int | None = None, end: int | None = None) -> float:
    """Broadband energy (drive above 2.5 f0) relative to the drive's total energy, dB. Hard edges and phase
    reversals put energy there, which the actuator and housing radiate as an audible click. With start/end (sample
    range, e.g. a brake) only the high-passed signal within that range +- 1 cycle counts, so the onset of the drive
    does not mask what the brake adds."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 8 or not np.any(x):
        return -120.0
    sos = signal.butter(4, min(2.5 * f0, 0.45 * fs), btype="highpass", fs=fs, output="sos")
    pad = fs // 50
    hf = signal.sosfiltfilt(sos, np.concatenate([np.zeros(pad), x, np.zeros(pad)]))
    if start is not None:
        cyc = int(round(fs / f0))
        lo, hi = pad + max(0, start - cyc), pad + min(len(x), (end if end is not None else len(x))) + cyc
        hf = hf[lo:hi]
    return round(float(10 * np.log10(max(1e-12, np.sum(hf * hf) / np.sum(x * x)))), 2)


ROBUST_VARIANTS = ((1.0, 1.0), (0.997, 1.0), (1.003, 1.0), (1.0, 0.85), (1.0, 1.15))   # (f0, q_drive) scales


def optimize_brake(build, fs: int, f0: float, q_drive: float, q_free: float, bounds: dict | None = None,
                   objective: dict | None = None) -> dict:
    """Search brake parameters {cycles, amp, phase_deg}; build(params) returns the full wave (float array).

    Cost = energy after the wave ends / energy during it + len_penalty * total length in ms. With objective
    robust=True the residual term is averaged over ROBUST_VARIANTS (f0 +-0.3 %, q_drive +-15 %: about how well they
    are known), which favours brakes that still work off-nominal over deep nominal nulls the device never reaches.
    """
    bounds = {"cycles": [0.5, 1.0, 1.5, 2.0], "amp": [0.05, 1.0], "phase_deg": [-90.0, 90.0], **(bounds or {})}
    objective = {"tail_ms": 60.0, "len_penalty": 0.002, "robust": False, **(objective or {})}
    variants = ROBUST_VARIANTS if objective["robust"] else ROBUST_VARIANTS[:1]
    evals = [0]

    def residual(w, fsc=1.0, qsc=1.0):
        sim = simulate(w, fs, f0 * fsc, q_drive * qsc, q_free, tail_ms=objective["tail_ms"])
        e = sim["acc"] ** 2
        inside = e[: len(w)].sum()
        return e[len(w):].sum() / inside if inside > 0 else 1e9

    def cost(cycles: float, amp: float, phase: float) -> float:
        evals[0] += 1
        w = np.asarray(build({"cycles": cycles, "amp": amp, "phase_deg": phase}), dtype=np.float64)
        res = float(np.mean([residual(w, a, b) for a, b in variants]))
        return float(res + objective["len_penalty"] * len(w) * 1000.0 / fs)

    lo_a, hi_a = bounds["amp"]
    lo_p, hi_p = bounds["phase_deg"]
    best = None
    for cyc in bounds["cycles"]:
        for amp in np.linspace(lo_a, hi_a, 8):
            for ph in np.linspace(lo_p, hi_p, 7):
                c = cost(cyc, float(amp), float(ph))
                if best is None or c < best[0]:
                    best = (c, cyc, float(amp), float(ph))
    _, cyc, amp0, ph0 = best

    def f(p):
        amp = float(np.clip(p[0], lo_a, hi_a))
        ph = float(np.clip(p[1], lo_p, hi_p))
        return cost(cyc, amp, ph)

    res = optimize.minimize(f, [amp0, ph0], method="Nelder-Mead",
                            options={"xatol": 1e-3, "fatol": 1e-6, "maxfev": 120,
                                     "initial_simplex": [[amp0, ph0], [amp0 + 0.08, ph0], [amp0, ph0 + 10.0]]})
    amp = float(np.clip(res.x[0], lo_a, hi_a))
    ph = float(np.clip(res.x[1], lo_p, hi_p))
    params = {"cycles": cyc, "amp": round(amp, 4), "phase_deg": round(ph, 2)}
    w = np.asarray(build(params), dtype=np.float64)
    sim = simulate(w, fs, f0, q_drive, q_free, tail_ms=objective["tail_ms"])
    m = metrics(sim, fs)
    m["click_db"] = click_db(w, fs, f0, *objective.get("click_region", (None, None)))
    m["residual_worst"] = round(float(max(residual(w, a, b) for a, b in ROBUST_VARIANTS)), 5)
    return {"params": params, "cost": float(res.fun), "metrics": m, "evaluations": evals[0]}


def fit_q(x, fs: int, f0_hint: float | None = None, fmin: float = 50.0, fmax: float = 1000.0) -> dict:
    """Estimate f0 and Q from a recording of a burst near resonance followed by free ringdown.

    Free decay: envelope ~ exp(-pi f0 t / Q_free), fitted log-linearly from the end of the drive (the last sample
    of the >= -3 dB region around the peak, so a flat or saturated plateau is excluded) down to 5 % (or 3x the
    noise floor). Rise: envelope ~ A (1 - exp(-t / tau)), tau = Q_drive / (pi f0), fitted from 5 % up to the
    end of the drive; only meaningful when the drive was at f0, lasted >= ~1.5 tau and did not saturate.
    """
    x = np.asarray(x, dtype=np.float64)
    x = x - np.mean(x)
    n = len(x)
    if n < 64:
        raise ValueError("recording too short")
    if not f0_hint:
        spec = np.abs(np.fft.rfft(x * np.hanning(n), n=max(8192, 1 << int(np.ceil(np.log2(n))))))
        f = np.fft.rfftfreq(max(8192, 1 << int(np.ceil(np.log2(n)))), 1.0 / fs)
        band = (f >= fmin) & (f <= min(fmax, fs / 2.2))
        f0_hint = float(f[band][np.argmax(spec[band])])
    lo, hi = f0_hint * 0.6, min(f0_hint * 1.6, fs / 2.2)
    sos = signal.butter(4, [lo, hi], btype="bandpass", fs=fs, output="sos")
    y = signal.sosfiltfilt(sos, x)
    env = np.abs(signal.hilbert(y))
    k = max(1, int(round(fs / f0_hint / 2)))
    env_s = np.convolve(env, np.ones(k) / k, mode="same")
    ipk = int(np.argmax(env_s))
    peak = env_s[ipk]
    tail = env_s[int(n * 0.9):] if ipk < n * 0.8 else env_s[: max(8, int(n * 0.05))]
    floor = float(np.median(tail)) if len(tail) else 0.0
    out = {"f0_hint": round(f0_hint, 2), "peak_ms": round(ipk * 1000.0 / fs, 3), "snr_db": round(
        20 * np.log10(peak / floor), 1) if floor > 0 else None}
    # free decay
    guard = int(round(1.5 * fs / f0_hint))
    stop_lvl = max(0.05 * peak, 3.0 * floor)
    low = np.nonzero(env_s[ipk:] < 0.7 * peak)[0]
    iend = ipk + (int(low[0]) if len(low) else n - ipk)
    out["release_ms"] = round(iend * 1000.0 / fs, 3)
    i0 = iend + guard
    after = np.nonzero(env_s[i0:] < stop_lvl)[0]
    i1 = i0 + (int(after[0]) if len(after) else len(env_s) - i0)
    if i1 - i0 > max(16, guard):
        t = np.arange(i0, i1) / fs
        ly = np.log(env_s[i0:i1])
        slope, icpt = np.polyfit(t, ly, 1)
        resid = ly - (slope * t + icpt)
        r2 = 1 - np.sum(resid ** 2) / np.sum((ly - ly.mean()) ** 2)
        zc = np.nonzero(np.diff(np.signbit(y[i0:i1])))[0]
        f_free = (len(zc) - 1) / 2.0 / ((zc[-1] - zc[0]) / fs) if len(zc) > 3 else f0_hint
        out["f0"] = round(float(f_free), 2)
        out["q_free"] = round(float(np.pi * f_free / -slope), 2) if slope < 0 else None
        out["decay"] = {"t0_ms": round(i0 * 1000.0 / fs, 2), "t1_ms": round(i1 * 1000.0 / fs, 2), "r2": round(float(r2), 4)}
    else:
        out["f0"] = round(f0_hint, 2)
        out["q_free"] = None
    # rise
    quiet = np.nonzero(env_s[:ipk] <= max(0.05 * peak, 3.0 * floor))[0]
    r0 = int(quiet[-1]) + 1 if len(quiet) else 0
    r1 = max(ipk, iend - guard)
    if ipk - r0 > guard:
        tr = np.arange(r0, r1) / fs - r0 / fs
        er = env_s[r0:r1]
        f0v = out["f0"]

        def model(t, a, tau, t0):
            return a * (1.0 - np.exp(-np.clip(t - t0, 0, None) / tau))

        try:
            p, _ = optimize.curve_fit(model, tr, er, p0=[peak * 1.2, 5.0 / (np.pi * f0v), 0.0],
                                      bounds=([0, 1e-5, -0.02], [peak * 20, 1.0, 0.02]), maxfev=4000)
            resid = er - model(tr, *p)
            r2 = 1 - np.sum(resid ** 2) / np.sum((er - er.mean()) ** 2)
            out["q_drive"] = round(float(np.pi * f0v * p[1]), 2)
            out["rise"] = {"t0_ms": round(r0 * 1000.0 / fs, 2), "t1_ms": round(r1 * 1000.0 / fs, 2), "r2": round(float(r2), 4),
                           "reached": round(float(er[-1] / p[0]), 3)}
        except (RuntimeError, ValueError):
            out["q_drive"] = None
    else:
        out["q_drive"] = None
    return out
