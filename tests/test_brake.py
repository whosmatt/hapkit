import unittest

from hapkit import server
from hapkit.core import project


def tone(cycles, amp, label):
    return {"type": "tone", "length": {"unit": "cycles", "value": cycles},
            "freq": {"unit": "f0", "points": [[0, 1.0], [1, 1.0]], "interp": "linear"},
            "amp": {"points": [[0, amp], [1, amp]], "interp": "linear"},
            "shape": {"kind": "sine", "sharp": 0.0}, "phase": {"mode": "continue", "deg": 0.0}, "label": label}


class SoftBrake(unittest.TestCase):
    def setUp(self):
        self.app = server.App()
        p = project.new_project("empty", "t")
        p["waves"] = [{"id": "c", "name": "c", "provenance": "original", "post": {"gain": 1.0}, "notes": "",
                       "segments": [tone(2, 1.0, "drive"), tone(1, -0.5, "brake")]}]
        self.app.project = p

    def test_soft_is_quieter_and_still_brakes(self):
        hard = self.app.brake_optimize({"wave": "c", "segment": 1, "robust": True})
        soft = self.app.brake_optimize({"wave": "c", "segment": 1, "robust": True,
                                        "soft": {"ramp_in": 1.0, "ramp_out": 0.5, "taper": 0.5}})
        seg = soft["segment"]
        self.assertEqual(seg["amp"]["interp"], "cos")
        self.assertEqual(seg["amp"]["points"][0][1], 0.0)          # starts from zero amplitude
        self.assertEqual(seg["amp"]["points"][-1][1], 0.0)         # and ends there
        self.assertEqual(soft["prev_segment"]["amp"]["points"][-1], [1.0, 0.0])   # drive tapered out
        self.assertLess(soft["after"]["click_db"], hard["after"]["click_db"] - 3)
        self.assertGreater(soft["after"]["length_ms"], hard["after"]["length_ms"])
        self.assertLess(soft["after"]["residual_worst"], 0.01)

    def test_sweep_returns_candidates(self):
        r = self.app.brake_optimize({"wave": "c", "segment": 1, "soft": {"ramp_out": 0.5}, "sweep": [0.5, 1.0]})
        self.assertEqual([c["soft"]["ramp_in"] for c in r["candidates"]], [0.5, 1.0])


if __name__ == "__main__":
    unittest.main()
