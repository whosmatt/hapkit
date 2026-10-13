import os
import struct
import unittest

import numpy as np

from hapkit.core import project
from hapkit.importers import carray, import_data, ref_samples, wavfile

QTI = "/home/matt/lineage/vendor/qcom/opensource/vibrator/effect/effect.cpp"

SNIPPET = r"""
#define RATE 24000
/* click */
static const int8_t click_data[] = { 0, 12, -127, 0x7f, // c
    -0x10, 5, };
static const int8_t spin[] = {1, 2, 3};
static const uint8_t pattern[] = { 0x1F, 0x3F };
static const struct effect_stream effects[] = {
    { .effect_id = 0, .length = sizeof(click_data), .play_rate_hz = RATE, .data = click_data },
    { .effect_id = 0x8000 | 3, .length = sizeof(spin), .play_rate_hz = 8000, .data = spin },
};
static const struct effect_stream more[] = { { 2, sizeof(click_data), 24000, click_data } };
"""


class CArray(unittest.TestCase):
    def test_snippet(self):
        arrs = {a["name"]: a for a in carray.extract(SNIPPET)}
        self.assertEqual(set(arrs), {"click_data", "spin"})
        self.assertEqual(arrs["click_data"]["values"], [0, 12, -127, 127, -16, 5])
        self.assertEqual(arrs["spin"]["kind"], "primitive")
        self.assertEqual((arrs["spin"]["aidl"], arrs["spin"]["fs"]), (3, 8000))
        self.assertEqual(arrs["click_data"]["label"], "TICK")   # last table wins: positional {2, ...}

    @unittest.skipUnless(os.path.exists(QTI), "QTI effect.cpp missing")
    def test_qti(self):
        with open(QTI) as f:
            arrs = carray.extract(f.read())
        names = {a["name"]: a for a in arrs}
        self.assertEqual(len(arrs), 5)
        self.assertEqual(names["primitive_1"]["kind"], "primitive")
        self.assertEqual(names["effect_0"]["fs"], 8000)
        self.assertEqual(len(names["effect_0"]["values"]), 47)

    def test_import_dispatch(self):
        p = project.new_project("empty")
        r = import_data("carray", "dev/effect.cpp", SNIPPET.encode(), {}, p)
        self.assertEqual(len(r["references"]), 2)
        ref = r["references"][0]
        self.assertTrue(np.allclose(ref_samples(ref)[:3], [0, 12 / 127, -1.0], atol=1e-6))
        r2 = import_data("carray", "dev/effect.cpp", SNIPPET.encode(), {"as": "wave"}, p)
        self.assertEqual(r2["waves"][0]["provenance"].split(":")[0], "reference")
        self.assertEqual(project.validate({**p, "waves": r2["waves"]}), [])


def wav_bytes(fmt_tag, bits, ch, fs, frames: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", fmt_tag, ch, fs, fs * ch * bits // 8, ch * bits // 8, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
    return b"RIFF" + struct.pack("<I", len(body)) + body


class Wav(unittest.TestCase):
    def test_formats(self):
        x = np.array([0.0, 0.5, -0.5, 0.25])
        cases = {
            (1, 16): np.round(x * 32767).astype("<i2").tobytes(),
            (3, 32): x.astype("<f4").tobytes(),
            (1, 32): np.round(x * 2147483647).astype("<i4").tobytes(),
            (1, 8): (np.round(x * 127) + 128).astype(np.uint8).tobytes(),
        }
        for (tag, bits), frames in cases.items():
            y, fs, meta = wavfile.read_wav(wav_bytes(tag, bits, 1, 8000, frames))
            self.assertEqual(fs, 8000)
            self.assertTrue(np.allclose(y, x, atol=0.01), (tag, bits, y))
        v = np.round(x * 8388607).astype(np.int32)
        b24 = b"".join(int(i & 0xFFFFFF).to_bytes(3, "little") for i in v)
        y, _, _ = wavfile.read_wav(wav_bytes(1, 24, 1, 8000, b24))
        self.assertTrue(np.allclose(y, x, atol=1e-3))

    def test_stereo_first(self):
        frames = np.array([[100, -5], [200, -5]], dtype="<i2").tobytes()
        y, _, meta = wavfile.read_wav(wav_bytes(1, 16, 2, 8000, frames))
        self.assertEqual(meta["channels"], 2)
        self.assertTrue(np.allclose(y * 32768, [100, 200]))

    def test_import_wave_resampled(self):
        p = project.new_project("empty")
        x = 0.8 * np.sin(2 * np.pi * 200 * np.arange(80) / 8000)
        data = wav_bytes(1, 16, 1, 8000, np.round(x * 32767).astype("<i2").tobytes())
        r = import_data("wav", "a.wav", data, {"as": "wave"}, p)
        from hapkit.core.render import render_wave
        self.assertEqual(len(render_wave(r["waves"][0], p["device"])["samples"]), 240)

    def test_raw(self):
        p = project.new_project("empty")
        r = import_data("raw", "x.bin", bytes([0, 127, 129]), {"fs": 24000}, p)
        self.assertTrue(np.allclose(ref_samples(r["references"][0]), [0, 1, -1], atol=1e-6))


if __name__ == "__main__":
    unittest.main()
