import os
import unittest

from hapkit.core import project
from hapkit.importers import cirrus, import_data

KOMODO = os.path.join(os.path.dirname(__file__), "..", "ref", "vendor", "komodo", "cs40l26.bin")


class Bits(unittest.TestCase):
    def test_reader(self):
        b = cirrus.Bits([0xABCDEF, 0x123456])
        self.assertEqual((b.read(4), b.read(12), b.read(16)), (0xA, 0xBCD, 0xEF12))


@unittest.skipUnless(os.path.exists(KOMODO), "komodo cs40l26.bin missing")
class Komodo(unittest.TestCase):
    def setUp(self):
        with open(KOMODO, "rb") as f:
            self.data = f.read()

    def test_table(self):
        wt = cirrus.load(self.data)
        e = wt["entries"]
        self.assertEqual(len(e), 14)
        self.assertEqual([x["type"] for x in e], [12, 8, 8, 12, 12, 12, 12, 12, 12, 8, 8, 12, 8, 8])
        self.assertEqual((e[2]["kind"], len(e[2]["samples"]), e[2]["svc_braking_ms"]), ("pcm", 66, 20.0))
        thud = e[4]["sections"]
        self.assertEqual([(s["t_ms"], s["freq_hz"], s["chirp"]) for s in thud], [(0.0, 75.0, False), (300.0, 45.0, True)])
        self.assertAlmostEqual(thud[0]["level"], 0.44727, places=4)
        self.assertEqual(len(cirrus.render_pwle(thud, 24000)), 7200)

    def test_import(self):
        p = project.new_project("empty")
        r = import_data("cirrus", "cs40l26.bin", self.data, {"prefix": "px9"}, p)
        self.assertEqual(len(r["references"]), 14)
        self.assertTrue(all(ref["proprietary"] for ref in r["references"]))
        self.assertEqual(r["references"][2]["id"], "px9_2_click")


if __name__ == "__main__":
    unittest.main()
