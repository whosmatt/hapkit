import json
import os
import tempfile
import unittest

from hapkit.core import project


class Sidecar(unittest.TestCase):
    def test_proprietary_refs_stay_local(self):
        p = project.new_project("empty", "t")
        p["references"] = [{"id": "pub", "name": "pub", "fs": 8000, "data_b64": "", "proprietary": False},
                           {"id": "priv", "name": "priv", "fs": 8000, "data_b64": "", "proprietary": True}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.hapkit.json")
            project.save(p, path)
            with open(path) as f:
                self.assertEqual([r["id"] for r in json.load(f)["references"]], ["pub"])
            self.assertTrue(os.path.exists(project.refs_sidecar(path)))
            self.assertEqual(sorted(r["id"] for r in project.load(path)["references"]), ["priv", "pub"])
            p["references"] = p["references"][:1]
            project.save(p, path)
            self.assertFalse(os.path.exists(project.refs_sidecar(path)))


if __name__ == "__main__":
    unittest.main()
