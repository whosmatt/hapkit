import base64
import math
import os
import shutil
import subprocess
import tempfile
import unittest
import wave as wavemod

from hapkit.core import exports, project, ramfmt
from hapkit.core.render import qround, render_wave
from hapkit.core.sequence import render_effect, render_sequence, seq_registers, wait_slot

STOCK = project.STOCK_BIN
HAVE_STOCK = os.path.exists(STOCK)
DEV = dict(project.DEFAULT_DEVICE)


def tone(cycles, amp=1.0, f=1.0, mode="continue", deg=0.0, unit="cycles", amp_end=None, interp="linear"):
    return {"type": "tone", "length": {"unit": unit, "value": cycles},
            "freq": {"unit": "f0", "points": [[0, f], [1, f]], "interp": "linear"},
            "amp": {"points": [[0, amp], [1, amp if amp_end is None else amp_end]], "interp": interp},
            "shape": {"kind": "sine", "sharp": 0.0}, "phase": {"mode": mode, "deg": deg}}


def wave(*segs, wid="w"):
    return {"id": wid, "name": wid, "provenance": "original", "segments": list(segs), "post": {"gain": 1.0}}


def expected_cycle_samples(cycles, f0=DEV["f0"], fs=DEV["fs"]):
    d = 2 * math.pi * f0 / fs
    acc, n = 0.0, 0
    while acc < 2 * math.pi * cycles - 1e-9:
        acc += d
        n += 1
    return n


@unittest.skipUnless(HAVE_STOCK, "stock haptic_ram.bin missing")
class StockRam(unittest.TestCase):
    def setUp(self):
        with open(STOCK, "rb") as f:
            self.blob = f.read()

    def test_parse(self):
        img = ramfmt.parse(self.blob)
        self.assertEqual((img.base_addr, img.version), (0x800, 0x85))
        self.assertTrue(img.checksum_ok)
        self.assertEqual([(w.start, w.end) for w in img.waves],
                         [(0x811, 0xadc), (0xadd, 0xcf0), (0xcf1, 0xe4c), (0xe4d, 0xeb2)])
        self.assertEqual(img.extra, b"")

    def test_build_roundtrip(self):
        img = ramfmt.parse(self.blob)
        self.assertEqual(ramfmt.build([w.data for w in img.waves], img.base_addr, img.version), self.blob)

    def test_template_export_identical(self):
        p = project.new_project("stock")
        self.assertEqual([i for i in project.validate(p) if i["level"] == "error"], [])
        blob, rep = exports.export_ram_bin(p)
        self.assertEqual(blob, self.blob)
        self.assertEqual(rep["used"], len(self.blob) - 4)

    def test_kernel_durations(self):
        p = project.new_project("stock")
        want = {"CLICK": 30, "DOUBLE_CLICK": 161, "TICK": 15, "THUD": 23, "POP": 15, "HEAVY_CLICK": 30}
        got = {e["name"]: render_effect(e, p, "ram")["kernel_ms"] for e in p["effects"] if e["enabled"]}
        self.assertEqual(got, want)
        dbl = next(s for s in p["sequences"] if s["id"] == "seq_double_click")
        self.assertEqual(seq_registers(dbl, p)["wavcfg"][:3], [1, 0x80 | 38, 1])

    def test_upstream_refused(self):
        p = project.new_project("stock")
        with self.assertRaisesRegex(ValueError, "stock_w1"):
            exports.export_ram_bin(p, upstream=True)

    def test_malformed(self):
        for bad in (self.blob[:6], self.blob[:4] + b"\x85\x08\x00\x08\x00" + self.blob[9:]):
            with self.assertRaises(ValueError):
                ramfmt.parse(bad)
        b = bytearray(self.blob)
        b[9:11] = b"\x00\x10"            # wave 1 end before start
        with self.assertRaises(ValueError):
            ramfmt.parse(bytes(b))


class Render(unittest.TestCase):
    def test_cycle_length(self):
        r = render_wave(wave(tone(2)), DEV)
        self.assertEqual(len(r["samples"]), expected_cycle_samples(2))
        self.assertEqual(r["samples"][0], 0)
        self.assertGreater(r["samples"][5], 0)

    def test_split_equals_whole(self):
        a = render_wave(wave(tone(2)), DEV)["samples"]
        b = render_wave(wave(tone(1), tone(1)), DEV)["samples"]
        self.assertEqual(a, b)
        c = render_wave(wave(tone(0.5), tone(1), tone(0.5)), DEV)["samples"]
        self.assertEqual(a, c)

    def test_brake_inverted(self):
        drive = render_wave(wave(tone(3)), DEV)["samples"]
        braked = render_wave(wave(tone(2), tone(1, amp=-1.0)), DEV)["samples"]
        n2 = len(render_wave(wave(tone(2)), DEV)["samples"])
        self.assertEqual(braked[:n2], drive[:n2])
        tail_d, tail_b = drive[n2:n2 + 40], braked[n2:n2 + 40]
        self.assertTrue(all(x == -y for x, y in zip(tail_d, tail_b)))
        self.assertTrue(render_wave(wave(tone(1, amp=-0.5)), DEV)["bounds"][0]["brake"])

    def test_hz_vs_f0(self):
        w1 = wave(tone(2))
        seg = tone(2)
        seg["freq"] = {"unit": "hz", "points": [[0, DEV["f0"]], [1, DEV["f0"]]]}
        self.assertEqual(render_wave(w1, DEV)["samples"], render_wave(wave(seg), DEV)["samples"])

    def test_exp_interp(self):
        seg = tone(10, amp=0.1, amp_end=1.0, interp="exp")
        s = render_wave(wave(seg), DEV)["float"]
        env_mid = max(abs(x) for x in s[len(s) // 2 - 60: len(s) // 2 + 60])
        self.assertAlmostEqual(env_mid, math.sqrt(0.1), delta=0.06)

    def test_quantize_and_clip(self):
        w = wave(tone(2, amp=1.5))
        r = render_wave(w, DEV)
        self.assertGreater(r["clips"], 0)
        self.assertEqual(min(r["samples"]), -127)
        self.assertEqual(max(r["samples"]), 127)
        self.assertEqual(qround(-2.5), -3)
        self.assertEqual(qround(2.5), 3)

    def test_gap_phase(self):
        d = 2 * math.pi * DEV["f0"] / DEV["fs"]
        a = render_wave(wave(tone(1), {"type": "gap", "length": {"unit": "samples", "value": 10}}, tone(1)), DEV)
        n1 = len(render_wave(wave(tone(1)), DEV)["samples"])
        whole = render_wave(wave(tone(3)), DEV)["float"]
        tail = a["float"][n1 + 10:]
        self.assertAlmostEqual(tail[3], whole[n1 + 10 + 3], places=9)
        self.assertGreater(d, 0)

    def test_pcm_roundtrip(self):
        vals = [0, 5, -127, 127, -1, 64]
        seg = {"type": "pcm", "fs": 24000, "encoding": "int8",
               "data_b64": base64.b64encode(ramfmt.int8_to_bytes(vals)).decode()}
        self.assertEqual(render_wave(wave(seg), DEV)["samples"], vals)

    def test_ms_length(self):
        r = render_wave(wave(tone(10, unit="ms")), DEV)
        self.assertEqual(len(r["samples"]), 240)

    def test_sharp(self):
        seg = tone(2)
        seg["shape"] = {"kind": "sine", "sharp": 1.0}
        s = render_wave(wave(seg), DEV)["samples"]
        self.assertGreater(sum(1 for x in s if abs(x) >= 120), len(s) // 2)


class Sequence(unittest.TestCase):
    def proj(self):
        p = project.new_project("empty")
        p["waves"] = [wave(tone(2), wid="a"), wave(tone(1, amp=0.5), wid="b")]
        p["ram"]["waves"] = ["a", "b"]
        p["sequences"] = [{"id": "s", "slots": [{"kind": "wave", "wave": "a", "loops": 2},
                                                {"kind": "wait", "ms": 100.0},
                                                {"kind": "wave", "wave": "b", "loops": 0}], "mainloop": 1}]
        return p

    def test_wait_slot(self):
        reg, eff = wait_slot(100, DEV)
        self.assertEqual(reg, 0x80 | 38)
        self.assertAlmostEqual(eff, 101.3333333, places=5)
        self.assertEqual(wait_slot(0.1, DEV)[0], 0x81)
        self.assertEqual(wait_slot(1000, DEV)[0], 0xFF)

    def test_render(self):
        p = self.proj()
        r = render_sequence(p["sequences"][0], p)
        la, lb = (len(render_wave(w, DEV)["samples"]) for w in p["waves"])
        per_pass = 3 * la + 38 * 64 + lb
        self.assertEqual(len(r["samples"]), 2 * per_pass)
        self.assertEqual(len(r["slot_bounds"]), 6)
        regs = seq_registers(p["sequences"][0], p)
        self.assertEqual(regs["wavcfg"], [1, 0x80 | 38, 2, 0, 0, 0, 0, 0])
        self.assertEqual(regs["loops"][:3], [2, 0, 0])

    def test_loop_extra_plays(self):
        p = self.proj()
        p["device"]["loop_extra_plays"] = 0
        p["sequences"][0]["mainloop"] = 1
        r = render_sequence(p["sequences"][0], p)
        la, lb = (len(render_wave(w, p["device"])["samples"]) for w in p["waves"])
        self.assertEqual(len(r["samples"]), 2 * la + 38 * 64 + lb)


class Validate(unittest.TestCase):
    def test_issues(self):
        p = project.new_project("empty")
        p["waves"] = [wave(tone(2), wid="a"), wave(tone(2), wid="a"), wave(tone(1), wid="Bad-Id")]
        p["ram"]["waves"] = ["a", "zz"]
        p["sequences"] = [{"id": "s", "slots": [{"kind": "wave", "wave": "c", "loops": 15}] * 9, "mainloop": 0}]
        msgs = " | ".join(i["msg"] for i in project.validate(p))
        for frag in ("duplicate id", "invalid id", "unknown wave 'zz'", "9 slots", "loops 15", "unknown wave 'c'"):
            self.assertIn(frag, msgs)

    def test_overflow(self):
        p = project.new_project("empty")
        p["waves"] = [wave(tone(300, unit="ms"), wid="big")]
        p["ram"]["waves"] = ["big"]
        self.assertTrue(any("overflow" in i["msg"] for i in project.validate(p)))
        with self.assertRaises(ValueError):
            exports.export_ram_bin(p)

    def test_not_in_ram(self):
        p = project.new_project("empty")
        p["waves"] = [wave(tone(2), wid="a")]
        p["sequences"] = [{"id": "s", "slots": [{"kind": "wave", "wave": "a", "loops": 0}], "mainloop": 0}]
        self.assertTrue(any("not in RAM" in i["msg"] for i in project.validate(p)))


class Exports(unittest.TestCase):
    def proj(self):
        p = project.new_project("empty", "t")
        p["waves"] = [wave(tone(2), tone(1, amp=-0.6), wid="click"), wave(tone(1, amp=0.4), wid="lad")]
        p["ram"]["waves"] = ["click", "lad"]
        p["sequences"] = [{"id": "s_click", "slots": [{"kind": "wave", "wave": "click", "loops": 0}], "mainloop": 0},
                          {"id": "s_rise", "slots": [{"kind": "wave", "wave": "lad", "loops": 3},
                                                     {"kind": "wait", "ms": 10.0},
                                                     {"kind": "wave", "wave": "click", "loops": 0}], "mainloop": 0}]
        for e in p["effects"]:
            if e["name"] == "CLICK" and e["kind"] == "effect":
                e["sequence"], e["enabled"] = "s_click", True
            if e["name"] == "QUICK_RISE":
                e["sequence"], e["enabled"] = "s_rise", True
        return p

    def compile_c(self, src, prelude):
        cc = shutil.which("gcc") or shutil.which("clang")
        if not cc:
            self.skipTest("no C compiler")
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "effect.h"), "w") as f:
                f.write("#include <stdint.h>\nstruct effect_stream { uint32_t effect_id; uint32_t length; "
                        "uint32_t play_rate_hz; const int8_t *data; };\n"
                        "const struct effect_stream *get_effect_stream(uint32_t effect_id);\n")
            path = os.path.join(d, "t.c")
            with open(path, "w") as f:
                f.write(prelude + src)
            res = subprocess.run([cc, "-fsyntax-only", "-Wall", "-Werror", "-I", d, path], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_kernel_table_compiles(self):
        src = exports.export_kernel_table(self.proj())
        self.assertIn("AW_SEQ_WAIT_MS(", src)
        prelude = ("#include <stdint.h>\nstruct aw_ff_effect { uint16_t id; uint8_t seq[8]; uint8_t loop[8]; uint8_t scale; };\n"
                   "#define AW_SEQ_WAIT_FLAG 0x80\n"
                   "#define AW_SEQ_WAIT_MS(ms) (AW_SEQ_WAIT_FLAG | (((ms) * 24 + 32) / 64))\n"
                   "enum { AW_FF_EFFECT_CLICK, AW_FF_PRIMITIVE_NOOP, AW_FF_PRIMITIVE_QUICK_RISE };\n")
        self.compile_c(src + "const void *keep = aw_ff_effects;\n", prelude)

    def test_effect_stream_compiles(self):
        src = exports.export_effect_stream_c(self.proj())
        self.assertIn("PRIMITIVE_ID_MASK | 4", src)
        self.compile_c(src, "")

    def test_wav(self):
        data = exports.export_wav([0, 10, -10, 127], 24000)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(data)
            f.flush()
            with wavemod.open(f.name) as w:
                self.assertEqual((w.getframerate(), w.getnframes(), w.getsampwidth()), (24000, 4, 2))

    def test_traced_warning(self):
        p = self.proj()
        p["waves"][0]["provenance"] = "traced:px_click"
        self.assertIn("warning: click is traced:px_click", exports.export_kernel_table(p, upstream=True))

    def test_deterministic(self):
        p = self.proj()
        self.assertEqual(exports.export_ram_bin(p)[0], exports.export_ram_bin(self.proj())[0])
        self.assertEqual(exports.export_effect_stream_c(p), exports.export_effect_stream_c(self.proj()))


class Persist(unittest.TestCase):
    def test_canonical_and_roundtrip(self):
        p = project.new_project("empty", "x")
        q = dict(reversed(list(p.items())))
        self.assertEqual(project.canonical_json(p), project.canonical_json(q))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.hapkit.json")
            project.save(p, path)
            self.assertEqual(project.load(path), p)


if __name__ == "__main__":
    unittest.main()
