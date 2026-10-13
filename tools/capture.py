#!/usr/bin/env python3
"""Record the dev device's own microphone (or IMU) while it plays RAM sequences, then fit f0 / Q per sequence.

usage: python3 tools/capture.py [project] [--seq m_burst20,m_half20] [--reps 6] [--sensor mic:6] [--gain 128]
                                 [--compare REF_SEQ [--window 40,100]]
       python3 tools/capture.py [project] --fit out/capture/<dir> [--compare REF_SEQ]   # re-analyse recordings
Uploads the project's RAM, plays each sequence --reps times through tools/android/hkcap (app_process, root),
pulls the WAV/CSV files to out/capture/<project>-<time>/, restores the device, then either
- fits f0, Q_free, Q_drive and the plateau level per sequence (bursts; mean ± std over runs), or
- with --compare, checks the LRA model against the recordings: the f0 ringing left in a late window after the
  end of each sequence (free-decay fit 15-80 ms after it), relative to REF_SEQ (e.g. an unbraked wave), measured
  vs simulated.
  The ratio cancels the acoustic path's gain at f0, so a brake's effect can be compared with the model directly.
Put the phone on something soft: on a hard table the table rings along and the decay grows a long tail.
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
from scipy import signal  # noqa: E402

from hapkit.analysis import lra  # noqa: E402
from hapkit.analysis.lra import fit_q  # noqa: E402
from hapkit.core import exports, project as proj  # noqa: E402
from hapkit.core.sequence import render_sequence  # noqa: E402
from hapkit.device.aw import WORK, DeviceManager  # noqa: E402
from hapkit.importers import csvfile, wavfile  # noqa: E402

DEX = os.path.join(ROOT, "tools", "android", "hkcap", "hkcap.dex")
DEX_REMOTE = f"{WORK}/hkcap.dex"
SKIP_S = 0.2        # AudioRecord start-up transient ~100 ms into every recording; the trigger fires at 300 ms


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    if path.endswith(".wav"):
        x, fs, _ = wavfile.read_wav(data)
        x = x[int(SKIP_S * fs):]
    else:
        x, fs, _ = csvfile.read_csv(data, {})
    return x, int(round(fs))


def runs_of(d):
    return sorted({os.path.basename(p).rsplit("_", 1)[0] for p in glob.glob(os.path.join(d, "*_[0-9]*.*"))
                   if p.endswith((".wav", ".csv"))})


def lockin(x, fs, f, bw=20.0):
    t = np.arange(len(x)) / fs
    z = x * np.exp(-2j * np.pi * f * t)
    sos = signal.butter(4, bw, fs=fs, output="sos")
    return 2 * np.abs(signal.sosfiltfilt(sos, z.real) + 1j * signal.sosfiltfilt(sos, z.imag))


def distortion(x, fs, f0):
    """Harmonics 2..8 relative to the fundamental over the strongest part of a recording (dB each, and their
    power sum). Rattles and other nonlinearity show up as even/high harmonics growing with drive level. Needs a
    steady stretch of >= 12 cycles; shorter waves have no line spectrum to separate (returns None, None)."""
    b = signal.sosfiltfilt(signal.butter(4, [f0 * 0.65, f0 * 1.4], "bandpass", fs=fs, output="sos"), x)
    k = max(1, int(0.005 * fs))
    e = np.sqrt(np.convolve(b * b, np.ones(k) / k, "same"))
    act = np.nonzero(e > 0.5 * e.max())[0]
    seg = x[act[0]:act[-1] + 1]
    if len(seg) < 12 * fs / f0:
        return None, None
    seg = seg - seg.mean()
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 1 << 17))
    freq = np.fft.rfftfreq(1 << 17, 1.0 / fs)
    win = max(12.0, 1.5 * fs / max(len(seg), 1))

    def peak(fc):
        sel = (freq > fc - win) & (freq < fc + win)
        return spec[sel].max()

    h1 = peak(f0)
    h = [float(20 * np.log10(peak(f0 * m) / h1)) for m in range(2, 9)]
    return h, float(10 * np.log10(np.sum(10 ** (np.array(h) / 10))))


def ring_amp(y, fs, i0, i1, t_ref, f0, alpha, span=6.0):
    """Amplitude at sample t_ref of a free decay A exp(-alpha t) cos(2 pi f t + phi) fitted to y[i0:i1] (least
    squares, f scanned over f0 +- span Hz). Time-gated, so the drive pulse cannot leak into the estimate the way a
    lock-in filter's tails do."""
    seg = y[i0:i1]
    tau = (np.arange(i0, i1) - t_ref) / fs
    dec = np.exp(-alpha * tau)
    best = (-1.0, 0.0)
    for f in np.arange(f0 - span, f0 + span + 1e-9, 0.25):
        B = np.stack([dec * np.cos(2 * np.pi * f * tau), dec * np.sin(2 * np.pi * f * tau)], axis=1)
        coef, *_ = np.linalg.lstsq(B, seg, rcond=None)
        fit = B @ coef
        expl = float(fit @ fit)
        if expl > best[0]:
            best = (expl, float(np.hypot(*coef)))
    return best[1]


def compare_dir(d, p, ref, t0_ms, t1_ms, sim=None):
    """Residual ringing at the end of each sequence (free-decay fit t0..t1 ms after the end, median over runs),
    relative to sequence ref, measured vs model. Recordings are aligned to the simulated envelope. sim overrides
    the LRA parameters of the simulation (f0, q_drive, q_free) without re-rendering the waves that were played."""
    dev = p["device"]
    f0, fsd = float(dev["f0"]), int(dev["fs"])
    lp = {"f0": f0, "q_drive": float(dev["q_drive"]), "q_free": float(dev["q_free"]), **(sim or {})}
    alpha = np.pi * f0 / float(dev["q_free"])
    cache = {}

    def bp(x, fs):
        return signal.sosfiltfilt(signal.butter(4, [f0 * 0.65, f0 * 1.4], "bandpass", fs=fs, output="sos"), x)

    def env(x, fs):
        k = max(1, int(fs / f0))
        return np.convolve(np.abs(x), np.ones(k) / k, "same")

    def model(sid):
        if sid not in cache:
            seq = next(s for s in p["sequences"] if s["id"] == sid)
            w = np.asarray(render_sequence(seq, p)["samples"], dtype=np.float64) / 127.0
            r = lra.simulate(w, fsd, lp["f0"], lp["q_drive"], lp["q_free"], tail_ms=t1_ms + 100)
            pad = fsd // 10
            acc = np.concatenate([np.zeros(pad), r["acc"]])
            e = pad + len(w)
            res = ring_amp(acc, fsd, e + int(t0_ms * fsd / 1000), e + int(t1_ms * fsd / 1000), e, f0, alpha)
            cache[sid] = (res, env(bp(acc, fsd), fsd), pad, len(w))
        return cache[sid]

    def measured(sid):
        _, menv, pad, n = model(sid)
        vals, noise, hd = [], [], []
        for path in sorted(glob.glob(os.path.join(d, f"{sid}_[0-9]*.wav"))):
            x, fs = load(path)
            hd.append(distortion(x, fs, f0)[1])
            y = bp(x - x.mean(), fs)
            ref_env = signal.resample_poly(menv, fs, fsd) if fs != fsd else menv
            c = signal.correlate(env(y, fs), ref_env, mode="full", method="fft")
            start = int(np.argmax(c)) - (len(ref_env) - 1) + int(pad * fs / fsd)   # wave start in the recording
            e = start + int(n * fs / fsd)
            i0, i1 = e + int(t0_ms * fs / 1000), e + int(t1_ms * fs / 1000)
            vals.append(ring_amp(y, fs, i0, i1, e, f0, alpha))
            # same estimator on a stretch before the wave: what noise alone reads as
            q1 = start - int(0.01 * fs)
            if q1 - (i1 - i0) > 0:
                noise.append(ring_amp(y, fs, q1 - (i1 - i0), q1, q1 - (i1 - i0) - (i0 - e), f0, alpha))
        ok = [v for v in hd if v is not None]
        return (float(np.median(vals)), float(np.median(noise)) if noise else float("nan"), vals,
                float(np.median(ok)) if len(ok) * 2 >= len(hd) and ok else None)

    db = lambda v, r: round(float(20 * np.log10(v / r)), 2)  # noqa: E731
    mref, sref = measured(ref)[0], model(ref)[0]
    out = {}
    print(f"residual ringing at the end of each sequence (free-decay fit {t0_ms:g}-{t1_ms:g} ms after it), dB rel. {ref} "
          f"(model f0 {lp['f0']:g}, Qd {lp['q_drive']:g}, Qf {lp['q_free']:g})")
    print(f"{'sequence':18s} {'measured':>8s} {'model':>7s} {'diff':>6s} {'noise':>6s} {'HD2-8':>6s}  runs")
    for sid in runs_of(d):
        if not any(s["id"] == sid for s in p["sequences"]):
            continue
        v, noise, vals, hd = measured(sid)
        m = model(sid)[0]
        r = {"measured_db": db(v, mref), "model_db": db(m, sref), "noise_db": db(noise, mref),
             "hd_db": round(hd, 2) if hd is not None else None, "runs_db": [db(x, mref) for x in vals]}
        r["diff_db"] = round(r["measured_db"] - r["model_db"], 2)
        out[sid] = r
        print(f"{sid:18s} {r['measured_db']:8.1f} {r['model_db']:7.1f} {r['diff_db']:6.1f} {r['noise_db']:6.1f} {hd if hd is not None else float('nan'):6.1f}  "
              f"{min(r['runs_db']):.1f} .. {max(r['runs_db']):.1f}")
    path = os.path.join(d, "compare.json")
    with open(path, "w") as f:
        json.dump({"ref": ref, "window_ms": [t0_ms, t1_ms], "device": dev, "sim": lp, "sequences": out}, f, indent=1)
    return out


def fit_dir(d, f0_hint):
    summary = {}
    for seq in runs_of(d):
        rows = []
        for p in sorted(glob.glob(os.path.join(d, f"{seq}_[0-9]*.*"))):
            x, fs = load(p)
            r = fit_q(x, fs, f0_hint)
            sos = signal.butter(4, [f0_hint * 0.6, min(f0_hint * 1.6, fs / 2.2)], "bandpass", fs=fs, output="sos")
            env = np.abs(signal.hilbert(signal.sosfiltfilt(sos, x - x.mean())))
            k = max(1, int(fs / f0_hint))
            r["plateau_dbfs"] = round(float(20 * np.log10(np.convolve(env, np.ones(k) / k, "same").max())), 2)
            h, r["hd_db"] = distortion(x, fs, f0_hint)
            r["h2_db"] = round(h[0], 2) if h else None
            rows.append(r)
        stats = {}
        for key in ("f0", "q_free", "q_drive", "snr_db", "plateau_dbfs", "hd_db", "h2_db"):
            v = np.array([r.get(key) if r.get(key) is not None else np.nan for r in rows], dtype=float)
            stats[key] = [round(float(np.nanmean(v)), 2), round(float(np.nanstd(v)), 2)] if np.isfinite(v).any() else None
        summary[seq] = {"n": len(rows), **stats, "runs": rows}
        fmt = lambda s: f"{s[0]:7.2f} ± {s[1]:5.2f}" if s else "      -        "  # noqa: E731
        print(f"{seq:16s} n={len(rows):2d}  f0 {fmt(stats['f0'])}  q_free {fmt(stats['q_free'])}  "
              f"q_drive {fmt(stats['q_drive'])}  plateau {fmt(stats['plateau_dbfs'])} dBFS  "
              f"HD2-8 {fmt(stats['hd_db'])} dB")
    with open(os.path.join(d, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project", nargs="?", default=os.path.join(ROOT, "projects", "fp6-measure.hapkit.json"))
    ap.add_argument("--seq", default="m_burst20,m_half20,m_burst60", help="comma list, or 'all'")
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--sensor", default="mic:6")
    ap.add_argument("--gain", type=int, default=128)
    ap.add_argument("--dur", type=int, default=1000, help="capture length per run, ms (trigger at 300 ms)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fit", default=None, help="only analyse the recordings in this directory")
    ap.add_argument("--compare", default=None, help="reference sequence: compare residual f0 ringing with the model")
    ap.add_argument("--window", default="15,80", help="free-decay fit window after the end of each sequence, ms")
    a = ap.parse_args()
    p = proj.load(a.project)
    f0 = float(p["device"]["f0"])
    t0, t1 = (float(v) for v in a.window.split(","))

    def analyse(d):
        if a.compare:
            compare_dir(d, p, a.compare, t0, t1)
        else:
            fit_dir(d, f0)

    if a.fit:
        analyse(a.fit)
        return
    out = a.out or os.path.join(ROOT, "out", "capture", f"{p['name']}-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out, exist_ok=True)
    ext = "wav" if a.sensor.startswith("mic") else "csv"
    seqs = [s["id"] for s in p["sequences"]] if a.seq == "all" else a.seq.split(",")
    dm = DeviceManager(lambda m, lvl="info": print(f"  {m}") if lvl == "error" else None)
    dm._ensure()
    try:
        with open(DEX, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        if dm._sh(f"sha256sum {DEX_REMOTE} 2>/dev/null; true").split()[:1] != [want]:
            dm._sh(f"mkdir -p {WORK}")
            dm.adb.push(DEX, DEX_REMOTE)
        blob, rep = exports.export_ram_bin(p)
        print(dm.upload_ram(blob, rep["sha256"][:12])["report"])
        for sid in seqs:
            play, rep = dm.ram_play_script(p, {"type": "sequence", "id": sid}, a.gain)
            print(rep)
            play = play.replace("'", "'\\''")
            for r in range(a.reps):
                remote = f"{WORK}/cap.{ext}"
                res = dm._sh(f"CLASSPATH={DEX_REMOTE} app_process / HkCap -s {a.sensor} -d {a.dur} -w 300 "
                             f"-c '{play}' -o {remote}", timeout=a.dur / 1000 + 30).strip()
                if "frames" not in res and "samples" not in res:
                    raise SystemExit(f"capture failed: {res}")
                dm.adb.pull(remote, os.path.join(out, f"{sid}_{r}.{ext}"))
        dm._sh(f"rm -f {WORK}/cap.{ext}")
    finally:
        print(dm.restore()["report"])
    print(f"recordings in {out}")
    analyse(out)


if __name__ == "__main__":
    main()
