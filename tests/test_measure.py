import unittest

import numpy as np

from hapkit.analysis import lra
from hapkit.analysis.dsp import resample
from hapkit.core import project
from hapkit.importers import csvfile, import_data, ref_samples

FS, F0 = 24000, 232.4


class FitQ(unittest.TestCase):
    def recording(self, qd, qf, fs_out=4000, noise=0.01):
        n = int(round(20 * FS / F0))
        sim = lra.simulate(np.sin(2 * np.pi * F0 * np.arange(n) / FS), FS, F0, qd, qf, tail_ms=150)
        rec = sim["acc"] + np.random.default_rng(7).normal(0, noise, len(sim["acc"]))
        return resample(rec, FS, fs_out)

    def test_recovers_q(self):
        for qd, qf in ((5, 18), (3, 12), (8, 30)):
            f = lra.fit_q(self.recording(qd, qf), 4000)
            self.assertAlmostEqual(f["f0"], F0, delta=F0 * 0.01)
            self.assertAlmostEqual(f["q_free"], qf, delta=qf * 0.05)
            self.assertAlmostEqual(f["q_drive"], qd, delta=qd * 0.15)

    def test_plateau_burst(self):
        # saturated drive (flat plateau, as recorded on 214d028e at full scale): the decay must start at the end
        # of the plateau, not at the envelope maximum somewhere inside it
        fs, qf = 48000, 26.0
        t = np.arange(int(0.6 * fs)) / fs
        t_on, t_off = 0.1, 0.36
        droop = 1 - 0.1 * (t - t_on) / (t_off - t_on)      # maximum early in the plateau, ~1 dB droop
        env = np.where(t < t_on, 0, np.where(t < t_off, (1 - np.exp(-(t - t_on) / 0.004)) * droop,
                                             0.9 * np.exp(-np.pi * F0 * (t - t_off) / qf)))
        rng = np.random.default_rng(3)
        x = env * (1 + 0.002 * rng.normal(size=len(t))) * np.sin(2 * np.pi * F0 * t) + rng.normal(0, 1e-3, len(t))
        f = lra.fit_q(x, fs, F0)
        self.assertAlmostEqual(f["q_free"], qf, delta=1.0)
        # -3 dB below the peak: ~0.9 dB of droop, then ~2.1 dB of decay (~7.5 ms at Q 26)
        self.assertAlmostEqual(f["release_ms"], t_off * 1000 + 7.5, delta=2)

    def test_csv_roundtrip(self):
        x = self.recording(5, 18)
        t = np.arange(len(x)) / 4000.0
        lines = ["time_s,x,y,z"] + [f"{a:.6f},{0.01 * b:.5f},{b:.5f},{9.81 + 0.001 * b:.5f}" for a, b in zip(t, x)]
        y, fs, meta = csvfile.read_csv("\n".join(lines).encode(), {})
        self.assertEqual((meta["axis"], meta["time_column"]), ("y", "time_s"))
        self.assertAlmostEqual(fs, 4000.0, delta=0.5)
        self.assertAlmostEqual(lra.fit_q(y, int(round(fs)))["q_free"], 18, delta=1)
        p = project.new_project("empty")
        r = import_data("csv", "acc.csv", "\n".join(lines).encode(), {}, p)
        self.assertEqual(r["references"][0]["fs"], 4000)
        self.assertEqual(len(ref_samples(r["references"][0])), len(x))

    def test_csv_no_time(self):
        data = "\n".join(f"{v:.4f};{-v:.4f}" for v in np.sin(np.arange(100))).encode()
        with self.assertRaises(ValueError):
            csvfile.read_csv(data, {})
        y, fs, meta = csvfile.read_csv(data, {"fs": 8000, "axis": "1"})
        self.assertEqual((fs, meta["axis"]), (8000.0, "c1"))


if __name__ == "__main__":
    unittest.main()
