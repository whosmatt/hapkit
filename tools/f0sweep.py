"""Brake robustness vs LRA f0 / Q_drive offset (model only). For each braked effect of the fp6 project, played as the
fixed RAM data: residual ringing amplitude at the end of the effect, relative to (a) the same drive without the final
brake (brake attenuation) and (b) the effect's peak.
usage: f0sweep.py project [--offsets -5,...,5] [--q-drive a,b,c] [--q-free q]   (offsets in % of the project's f0;
cells are the worst case over the q-drive list, default project Q_drive x0.85/1/1.15)"""
import argparse
import sys
import numpy as np
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
from hapkit.analysis import lra  # noqa: E402
from hapkit.core import project as proj  # noqa: E402
from hapkit.core.render import render_wave  # noqa: E402
from hapkit.core.sequence import effect_sequence, render_sequence  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("project")
ap.add_argument("--offsets", default="-5,-3,-2,-1,-0.5,0,0.5,1,2,3,5")
ap.add_argument("--q-drive", default=None)
ap.add_argument("--q-free", type=float, default=None)
a = ap.parse_args()
p = proj.load(a.project)
dev = p["device"]
fs, F0, QD, QF = int(dev["fs"]), dev["f0"], dev["q_drive"], dev["q_free"]
if a.q_free:
    QF = a.q_free
waves = {w["id"]: w for w in p["waves"]}
OFFS = [float(v) for v in a.offsets.split(",")]
QS = [float(v) for v in a.q_drive.split(",")] if a.q_drive else [QD * 0.85, QD, QD * 1.15]


def amp(sim, i):
    return float(np.hypot(sim["disp"][i], sim["vel"][i]))


def case(name, x, bs, end):
    """x: samples (float, -1..1); bs: brake start; end: end of brake (= end of drive)."""
    rows = []
    for off in OFFS:
        att, rel = [], []
        for q in QS:
            f0 = F0 * (1 + off / 100)
            sim = lra.simulate(x[:end], fs, f0, q, QF, tail_ms=5)
            a_end = amp(sim, end - 1)
            a_bs = amp(sim, bs)
            ref = a_bs * np.exp(-np.pi * f0 * (end - bs) / fs / QF)       # no brake: free decay from brake start
            peak = max(amp(sim, i) for i in range(0, end, 4))
            att.append(20 * np.log10(a_end / ref))
            rel.append(20 * np.log10(a_end / peak))
        rows.append((off, att, rel))
    return rows


cases, seen = [], set()
for e in p["effects"]:
    if not e.get("enabled", True):
        continue
    seq = effect_sequence(e, p)
    if seq is None or seq["id"] in seen:
        continue
    seen.add(seq["id"])
    r = render_sequence(seq, p)
    x = np.asarray(r["samples"], float) / 127
    last = [b for b in r["slot_bounds"] if b["kind"] == "wave"][-1]
    wid = seq["slots"][last["index"]]["wave"]
    wb = [b for b in render_wave(waves[wid], dev)["bounds"] if b["brake"]]
    if not wb:
        print(f"{e['name']:12s} ({seq['id']}): ends on {wid} without a brake, skipped")
        continue
    wlen = len(render_wave(waves[wid], dev)["samples"])
    start_last = last["end"] - wlen
    bs = start_last + wb[-1]["start"]
    names = sorted({f"{x['kind'][0]}:{x['name']}" for x in p["effects"]
                    if effect_sequence(x, p) and effect_sequence(x, p)["id"] == seq["id"]})
    cases.append((" ".join(names), wid, case(seq["id"], x, bs, last["end"])))

print(f"model: f0 {F0} Hz, Q_drive {QD} (range {QS[0]:.1f}-{QS[-1]:.1f}), Q_free {QF}; residual at the end of the brake")
print("cells: brake attenuation vs no brake, worst over Q_drive range [dB]  /  residual vs effect peak, worst [dB]\n")
hdr = "".join(f"{o:+5.1f}% " for o in OFFS)
print(f"{'effects':44s} {'last wave':18s} {hdr}")
for names, wid, rows in cases:
    print(f"{names[:44]:44s} {wid:18s} " + "".join(f"{max(a):6.0f} " for _, a, _ in rows))
    print(f"{'':44s} {'  vs peak':18s} " + "".join(f"{max(r):6.0f} " for _, _, r in rows))
