import time
import unittest

import numpy as np

from hapkit.analysis import dsp, lra

FS, F0 = 24000, 232.4


def sine(f, n, amp=1.0):
    return amp * np.sin(2 * np.pi * f * np.arange(n) / FS)


class Lra(unittest.TestCase):
    def test_normalisation(self):
        x = sine(F0, FS // 2)
        sim = lra.simulate(x, FS, F0, 6.0, 20.0, tail_ms=0)
        self.assertAlmostEqual(np.max(np.abs(sim["acc"][-2000:])), 1.0, delta=0.03)

    def test_off_resonance(self):
        q, f = 6.0, F0 * 1.2
        sim = lra.simulate(sine(f, FS), FS, F0, q, q, tail_ms=0)
        r = f / F0
        h_acc = (r * r) / np.sqrt((1 - r * r) ** 2 + (r / q) ** 2)   # |a/u| / w0^2, relative to Q at r=1
        self.assertAlmostEqual(np.max(np.abs(sim["acc"][-3000:])), h_acc / q, delta=0.02)

    def test_free_decay(self):
        q_free = 20.0
        sim = lra.simulate(sine(F0, 2400), FS, F0, 6.0, q_free, tail_ms=100)
        env = np.abs(__import__("scipy.signal", fromlist=["hilbert"]).hilbert(sim["acc"]))
        i0, i1 = 2400 + 240, 2400 + 1440               # 10 ms .. 60 ms after release
        rate = np.log(env[i0] / env[i1]) / ((i1 - i0) / FS)
        self.assertAlmostEqual(rate, np.pi * F0 / q_free, delta=0.05 * np.pi * F0 / q_free)

    def test_metrics_and_brake(self):
        n3 = int(round(3 * FS / F0))

        def build(p):
            drive = sine(F0, n3)
            nb = int(round(p["cycles"] * FS / F0))
            t = np.arange(n3, n3 + nb) / FS
            brake = -p["amp"] * np.sin(2 * np.pi * F0 * t + np.radians(p["phase_deg"]))
            return np.concatenate([drive, brake])

        base = lra.metrics(lra.simulate(sine(F0, n3), FS, F0, 6.0, 20.0), FS)
        best = lra.optimize_brake(build, FS, F0, 6.0, 20.0)
        self.assertLess(best["metrics"]["residual_ratio"] * 5, base["residual_ratio"])
        self.assertLess(best["metrics"]["ring_ms"], base["ring_ms"])

    def test_speed(self):
        x = sine(F0, FS)
        t = time.perf_counter()
        lra.simulate(x, FS, F0, 6.0, 20.0)
        self.assertLess(time.perf_counter() - t, 0.1)


class Dsp(unittest.TestCase):
    def test_spectrogram_peak(self):
        sg = dsp.spectrogram(sine(F0, 2400), FS, fmax=800)
        mid = sg["data"][sg["nt"] // 2]
        self.assertLessEqual(abs(int(np.argmax(mid)) * sg["df_hz"] - F0), sg["df_hz"])
        self.assertGreaterEqual(int(mid.max()), 250)

    def test_short_wave_frames(self):
        sg = dsp.spectrogram(sine(F0, 120), FS)
        self.assertGreaterEqual(sg["nt"], 8)

    def test_inst_freq_chirp(self):
        n = FS // 2
        t = np.arange(n) / FS
        f1, f2, T = 100.0, 400.0, n / FS
        k = T / np.log(f2 / f1)
        x = np.sin(2 * np.pi * f1 * k * (np.exp(t / k) - 1))
        pts = np.array(dsp.inst_freq(x, FS))
        want = f1 * np.exp(pts[:, 0] / 1000.0 / k)
        self.assertLess(np.max(np.abs(pts[:, 1] / want - 1)), 0.02)

    def test_inst_freq_tone(self):
        pts = dsp.inst_freq(sine(F0, 2400), FS)
        self.assertAlmostEqual(np.median([p[1] for p in pts]), F0, delta=F0 * 0.01)

    def test_spectrum_level(self):
        s = dsp.spectrum(sine(F0, 4800), FS)
        self.assertAlmostEqual(max(s["db"]), 0.0, delta=1.0)

    def test_retarget(self):
        x = sine(150.0, 480) * np.hanning(480)
        y = dsp.retarget(x, FS, 150.0, F0, "timescale")
        self.assertAlmostEqual(len(y), 480 * 150.0 / F0, delta=2)
        z = dsp.retarget(x, FS, 150.0, F0, "envelope")
        self.assertEqual(len(z), 480)
        f = np.median([p[1] for p in dsp.inst_freq(z, FS, min_amp=0.2)])
        self.assertAlmostEqual(f, F0, delta=F0 * 0.03)

    def test_envelope_and_peaks(self):
        x = sine(F0, 2400) * np.linspace(0, 1, 2400)
        env = dsp.envelope(x, FS)
        self.assertAlmostEqual(env[1800], 0.75, delta=0.05)
        pk = dsp.half_cycle_peaks(x)
        self.assertGreater(len(pk), 40)


if __name__ == "__main__":
    unittest.main()
