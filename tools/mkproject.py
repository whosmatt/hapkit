#!/usr/bin/env python3
"""Build projects/fp6-primitives.hapkit.json: stock waves (comparison), references, starter designs.

usage: python3 tools/mkproject.py [--out projects/fp6-primitives.hapkit.json] [--measure | --brakes]
References are added when present: ref/vendor/komodo/cs40l26.bin (Pixel 9 Pro XL, proprietary) and
ref/aosp/crosshatch/*.wav (Pixel 3 CS40L20 wavetable sources from AOSP device/google/crosshatch).
"""
import argparse
import glob
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

from hapkit.core import project as proj  # noqa: E402
from hapkit.importers import import_data  # noqa: E402


def tone(cycles, amp, amp_end=None, f=1.0, f_end=None, label="", mode="continue", deg=0.0, interp="linear"):
    return {"type": "tone", "length": {"unit": "cycles", "value": cycles},
            "freq": {"unit": "f0", "points": [[0, f], [1, f if f_end is None else f_end]], "interp": "linear"},
            "amp": {"points": [[0, amp], [1, amp if amp_end is None else amp_end]], "interp": interp},
            "shape": {"kind": "sine", "sharp": 0.0}, "phase": {"mode": mode, "deg": deg}, "label": label}


def wave(wid, segs, notes=""):
    return {"id": wid, "name": wid, "provenance": "original", "segments": segs, "post": {"gain": 1.0}, "notes": notes}


def seq(sid, *slots, mainloop=0):
    out = []
    for s in slots:
        if isinstance(s, (int, float)) and not isinstance(s, bool):
            out.append({"kind": "wait", "ms": float(s)})
        else:
            wid, plays = (s, 1) if isinstance(s, str) else s
            out.append({"kind": "wave", "wave": wid, "loops": plays - 1})
    return {"id": sid, "name": "", "slots": out, "mainloop": mainloop, "notes": ""}


def starters():
    """Starter designs for the measured model (f0 231.7, Q_drive 26, Q_free 26). Brakes are in anti-phase at f0 and
    chosen for the worst case over f0 +-0.3 % and Q_drive 22..30 rather than the nominal null, which the device
    never reaches (tools/capture.py --compare: optimised brakes land at -25..-35 dB). Whole or half-cycle brakes:
    a brake that ends on a peak (1.25 / 1.75 cycles at phase 0) leaves a step and brakes badly.
    Ladder steps stop at 0.5 FS: steady drive above ~0.3-0.5 FS compresses and grows harmonics (rattles)."""
    lad = [0.125, 0.25, 0.375, 0.5]
    waves = [
        wave("click", [tone(2, 1.0, label="drive"), tone(2, -0.785, label="brake")],
             "starter: 2 cycles + 2-cycle brake at 0.785 (model worst case -29 dB vs unbraked)"),
        wave("tick", [tone(1, 1.0, label="drive"), tone(1, -0.886, label="brake")],
             "starter: 1 cycle + 1-cycle brake at 0.886 (model worst case -34 dB vs unbraked)"),
        wave("thud", [tone(5, 1.0, 0.35, f=0.92, interp="exp", label="drive"),
                      tone(1.75, -0.82, label="brake", deg=90.0)],
             "starter: decaying 5 cycles slightly below f0, model-tuned brake (worst case -22 dB)"),
        *[wave(f"lad{i + 1}", [tone(1, a, mode="absolute", label=f"{a:g}")], "ladder step, 1 cycle")
          for i, a in enumerate(lad)],
        wave("brake", [tone(1, -0.5, mode="absolute", label="brake")],
             "anti-phase cycle after a ladder; plays per sequence model-tuned"),
        wave("loop", [tone(1, 0.55, mode="absolute", label="loop")], "FF_CONSTANT on()/setAmplitude loop wave"),
    ]
    sequences = [
        seq("s_click", "click"),
        seq("s_double_click", "click", 100, "click"),
        seq("s_tick", "tick"),
        seq("s_thud", "thud"),
        seq("s_heavy_click", "click"),
        seq("s_quick_rise", ("lad1", 2), ("lad2", 2), ("lad3", 2), ("lad4", 3), ("brake", 3)),
        seq("s_slow_rise", ("lad1", 8), ("lad2", 8), ("lad3", 8), ("lad4", 8), ("brake", 5)),
        seq("s_quick_fall", ("lad4", 2), ("lad3", 2), ("lad2", 2), ("lad1", 2), ("brake", 2)),
        seq("s_spin", ("lad1", 2), ("lad2", 2), ("lad3", 2), ("lad4", 2), ("lad3", 2), ("lad2", 2), ("lad1", 2),
            ("brake", 3)),
        seq("s_low_tick", "lad2", "lad3", "brake"),
        seq("s_ref", ("lad4", 4)),   # unbraked reference for tools/capture.py --compare s_ref
    ]
    mapping = {("effect", "CLICK"): "s_click", ("effect", "DOUBLE_CLICK"): "s_double_click", ("effect", "TICK"): "s_tick",
               ("effect", "THUD"): "s_thud", ("effect", "POP"): "s_tick", ("effect", "HEAVY_CLICK"): "s_heavy_click",
               ("primitive", "CLICK"): "s_click", ("primitive", "THUD"): "s_thud", ("primitive", "SPIN"): "s_spin",
               ("primitive", "QUICK_RISE"): "s_quick_rise", ("primitive", "SLOW_RISE"): "s_slow_rise",
               ("primitive", "QUICK_FALL"): "s_quick_fall", ("primitive", "LIGHT_TICK"): "s_tick",
               ("primitive", "LOW_TICK"): "s_low_tick"}
    return waves, sequences, mapping


def measure_project() -> dict:
    """f0/Q measurement: bursts built from looped 1-cycle RAM waves (RAM mode has no chip auto-brake), RTP sweep."""
    p = proj.new_project("empty", "fp6-measure")
    cyc = lambda wid, a, f, note: wave(wid, [tone(1, a, f=f, mode="absolute", label=wid)], note)  # noqa: E731
    p["waves"] = [
        cyc("cyc100", 1.0, 1.0, "1 cycle at f0, full scale"),
        cyc("cyc50", 0.5, 1.0, "1 cycle at f0, half scale (linearity)"),
        cyc("cyc25", 0.25, 1.0, "1 cycle at f0, quarter scale (linear range: Q_drive)"),
        cyc("cyc12", 0.125, 1.0, "1 cycle at f0, 1/8 scale"),
        cyc("cyc_lo", 1.0, 0.9, "1 cycle at 0.9 f0"),
        cyc("cyc_hi", 1.0, 1.1, "1 cycle at 1.1 f0"),
        wave("loop", [tone(1, 0.55, mode="absolute", label="loop")], "FF_CONSTANT loop wave"),
        {"id": "sweep", "name": "sweep", "provenance": "original", "post": {"gain": 1.0},
         "notes": "RTP only: 2 s log sweep 80-600 Hz (frequency response)",
         "segments": [{"type": "tone", "length": {"unit": "ms", "value": 2000},
                       "freq": {"unit": "hz", "points": [[0, 80], [1, 600]], "interp": "exp"},
                       "amp": {"points": [[0, 0], [0.01, 0.8], [0.99, 0.8], [1, 0]], "interp": "linear"},
                       "shape": {"kind": "sine", "sharp": 0}, "phase": {"mode": "absolute", "deg": 0}, "label": "sweep"}]},
    ]
    p["ram"]["waves"] = ["cyc100", "cyc50", "cyc25", "cyc12", "cyc_lo", "cyc_hi", "loop"]
    p["sequences"] = [
        seq("m_burst20", ("cyc100", 15), ("cyc100", 5)),
        seq("m_burst60", ("cyc100", 15), ("cyc100", 15), ("cyc100", 15), ("cyc100", 15)),
        seq("m_half20", ("cyc50", 15), ("cyc50", 5)),
        seq("m_lo20", ("cyc_lo", 15), ("cyc_lo", 5)),
        seq("m_hi20", ("cyc_hi", 15), ("cyc_hi", 5)),
        seq("m_q20", ("cyc25", 15), ("cyc25", 5)),
        seq("m_e20", ("cyc12", 15), ("cyc12", 5)),
        seq("m_q60", ("cyc25", 15), ("cyc25", 15), ("cyc25", 15), ("cyc25", 15)),
    ]
    return p


def brakes_project() -> dict:
    """Brake model check: one drive, brake amplitude / phase / length varied; play all with
    tools/capture.py projects/fp6-brakes.hapkit.json --seq all --compare s_c_nb. Measured on 214d028e (2026-10-11),
    model f0 231.7 / Q_drive 26 / Q_free 26: within 1.4 dB except c_b20_75 (+2.5) and t_b10_87, where the model
    predicts a deep null (-41 dB) and the device reaches -34."""
    p = proj.new_project("empty", "fp6-brakes")
    drive2, drive1 = tone(2, 1.0, label="drive"), tone(1, 1.0, label="drive")
    br = lambda c, a, deg=0.0: tone(c, -a, label="brake", deg=deg)  # noqa: E731
    w = [("c_nb", [drive2])]
    w += [(f"c_b15_{int(a * 100)}", [drive2, br(1.5, a)]) for a in (0.5, 0.7, 0.85, 1.0)]
    w += [("c_b15_100_m30", [drive2, br(1.5, 1.0, -30)]), ("c_b15_100_p30", [drive2, br(1.5, 1.0, 30)]),
          ("c_b10_100", [drive2, br(1.0, 1.0)]), ("c_b20_75", [drive2, br(2.0, 0.75)])]
    w += [(f"t_b10_{int(a * 100)}", [drive1, br(1.0, a)]) for a in (0.5, 0.7, 0.87, 1.0)]
    p["waves"] = [wave(i, s) for i, s in w] + [wave("loop", [tone(1, 0.55, mode="absolute", label="loop")])]
    p["ram"]["waves"] = [x["id"] for x in p["waves"]]
    p["sequences"] = [seq(f"s_{i}", i) for i, _ in w]
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--measure", action="store_true", help="build projects/fp6-measure.hapkit.json instead")
    ap.add_argument("--brakes", action="store_true", help="build projects/fp6-brakes.hapkit.json instead")
    a = ap.parse_args()
    if a.measure or a.brakes:
        p = measure_project() if a.measure else brakes_project()
        out = a.out or os.path.join(ROOT, "projects", f"{p['name']}.hapkit.json")
        issues = [i for i in proj.validate(p) if i["level"] == "error"]
        if issues:
            raise SystemExit(f"validation errors: {issues}")
        proj.save(p, out)
        print(f"wrote {os.path.relpath(out, ROOT)}")
        return
    a.out = a.out or os.path.join(ROOT, "projects", "fp6-primitives.hapkit.json")
    p = proj.new_project("stock", "fp6-primitives")
    waves, sequences, mapping = starters()
    p["waves"] += waves
    p["sequences"] = sequences
    p["ram"]["waves"] = [w["id"] for w in waves]
    for e in p["effects"]:
        sid = mapping.get((e["kind"], e["name"]))
        if sid:
            e["sequence"], e["enabled"] = sid, True
    komodo = os.path.join(ROOT, "ref", "vendor", "komodo", "cs40l26.bin")
    if os.path.exists(komodo):
        with open(komodo, "rb") as f:
            r = import_data("cirrus", "cs40l26.bin", f.read(), {"prefix": "px9", "proprietary": True}, p)
        p["references"] += r["references"]
        print("komodo:", r["report"])
    for path in sorted(glob.glob(os.path.join(ROOT, "ref", "aosp", "crosshatch", "*.wav"))):
        with open(path, "rb") as f:
            r = import_data("wav", "px3_" + os.path.basename(path), f.read(), {"proprietary": False}, p)
        p["references"] += r["references"]
    issues = [i for i in proj.validate(p) if i["level"] == "error"]
    if issues:
        raise SystemExit(f"validation errors: {issues}")
    proj.save(p, a.out)
    print(f"wrote {os.path.relpath(a.out, ROOT)}: {len(p['waves'])} waves, {len(p['sequences'])} sequences, "
          f"{len(p['references'])} references")


if __name__ == "__main__":
    main()
