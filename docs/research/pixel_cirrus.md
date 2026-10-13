# Pixel haptics references (CS40L26) — what the files contain

Decoded 2026-10-09 from `ref/vendor/komodo/cs40l26.bin` (Pixel 9 Pro XL, TheMuppets
`proprietary_vendor_google_komodo` @ lineage-23.2, 1344 B, file comment "Date: 2024-04-02"). Format from the Pixel
kernel driver (`cl_dsp.c` `cl_dsp_read_wt`, `cs40l26.c` `cs40l26_owt_get_wlength`) and the Pixel HAL
(`hardware/google/pixel/vibrator/cs40l26`, android15-release: `Vibrator.cpp` WaveformIndex, `DspMemChunk.cpp/.h`
PWLE packing). Parser: `hapkit/importers/cirrus.py`.

## Container and table

- WMDR coefficient file: 16-byte header, blocks `{le16 offset, le16 type, le32 alg_id, le32 alg_ver, le32 sr, le32
  len}`, 4-byte aligned. Blocks here: name text "Speakerphone"; **XM block (type 5) for alg 0x400BD, 1016 B** = the
  RAM wavetable; two small XM blocks for algs 0x4F21B/0x4F200 (not decoded); date/comment text.
- XM data = 24-bit words in big-endian u32. Table entries `{flags:16 type:8}{offset:24}{size:24}` (words),
  terminator 0xFFFFFF; 14 entries, bodies right after the header. Flags bit 10 = metadata after the body
  (SVC braking time, word/8 = ms); bit 15 = "buzz" feature flag (`PWLE_FTR_BUZZ_BIT << 8`).
- Body word 0 = length in samples @ 8 kHz (bit 23 calculated, bit 22 indefinite).
- PCM (type 8): word 1 = a Q10.14 frequency field (192/220/190/200 Hz or 0; meaning unconfirmed — read as the
  design f0 used for F0 compensation), then int8 samples, 3 per word, MSB first, **8 kHz**.
- PWLE (type 12): `repeat:8 wait:12 nsections:8`, sections `delay:16 (0.25 ms) level:12 (signed /2048)
  freq:12 (/4 Hz) flags:8` — flags = `(chirp 8 | brake 4 | amp_reg 2 | 1) << 4`, +24-bit vbemf target if amp_reg.

## Content (index names from the HAL's WaveformIndex)

| # | name | kind | length | content |
|---|---|---|---|---|
| 0 | LONG_VIBRATION | PWLE, indefinite | 50 ms ramp, then hold | level 0 → 0.18 over 50 ms; frequency field outside 30–300 Hz (f0 tracking assumed); SVC brake 50 ms |
| 1 | RESERVED_1 | PCM | 7.9 ms | f-field 0, peak 0.82 |
| 2 | **CLICK** | PCM | 8.25 ms | f-field 192 Hz, peak 0.36; one broad positive push (~3.7 ms) then a faster tail; **SVC brake 20 ms** after it |
| 3 | SHORT_VIBRATION | PWLE, indefinite | — | level 0.18, frequency field outside range; SVC brake 50 ms |
| 4 | **THUD** | PWLE | 300 ms | 0.447 @ 75 Hz → 0 @ 45 Hz, chirp (decaying low sweep) |
| 5 | **SPIN** | PWLE | 132 ms | 0.224 @ 105 Hz → 0.447 @ 45 Hz (78 ms, chirp) → 0.224 @ 75 Hz (54 ms, chirp) |
| 6 | **QUICK_RISE** | PWLE | 150 ms | 0 @ 45 Hz → 0.447 @ 105 Hz, chirp |
| 7 | **SLOW_RISE** | PWLE | 500 ms | 0 @ 45 Hz → 0.447 @ 105 Hz, chirp |
| 8 | **QUICK_FALL** | PWLE | 101 ms | 0.224 @ 255 Hz → 0.447 @ 185 Hz (51 ms) → 0 @ 130 Hz (50 ms), chirp |
| 9 | **LIGHT_TICK** | PCM | 4.5 ms | f-field 220 Hz, peak 0.73, essentially one pulse |
| 10 | **LOW_TICK** | PCM | 11.25 ms | f-field 190 Hz, peak 0.59, no zero crossing above 5 % (unipolar-ish pulse) |
| 11 | RESERVED_MFG_1 | PWLE | — | like #0 |
| 12 | RESERVED_MFG_2 | PCM | 10.9 ms | f-field 200 Hz |
| 13 | RESERVED_MFG_3 | PCM | 4.5 ms | identical to #9 |

## What this means for FP6

- Pixel clicks/ticks are **short pulses** whose stopping is done by the DSP's closed-loop braking (SVC, 20 ms on
  CLICK), not by samples. Played open-loop on our LRA they ring (hapkit sim: CLICK rings ~60 ms with the default
  Q). The AW86938 has no SVC, so our clicks need the brake in the waveform.
- Pixel rises/falls/spin/thud are **PWLE chirps between 45 and 255 Hz** at ≤ 0.447 level — i.e. well below a
  232 Hz LRA's resonance for most of their span. On a narrowband LRA those frequencies are strongly attenuated;
  what transfers is the amplitude envelope and the durations (rise 150/500 ms, fall 101 ms, spin 132 ms, thud
  300 ms). Retargeting by f0 ratio does not fix this (still sub-resonant).
- The f-field values (190–220 Hz) and `RESONANT_FREQUENCY_DEFAULT = 145` in the HAL do not pin down the Pixel 9 Pro
  XL actuator f0; use the overlay retarget with an explicit `f_ref`.

## Provenance

The komodo files are Google proprietary firmware: local reference only (`proprietary: true`), never copied into
designs that go upstream. The Pixel 3 (CS40L20) sources under `ref/aosp/crosshatch/` come from AOSP
(`device/google/crosshatch/vibrator/cs40l20`, generated into `cs40l20.bin` by `pdata2bin.py` per its README): click
levels 23/39/55/75 % (9.4 ms), tick "11-2 (-3)" (12.4 ms), POP (221 ms), THUD (263 ms), all 8 kHz 16-bit.

## Not determined

1. Exact meaning of the PCM word-1 field (assumed design f0 in Q10.14) and of the out-of-range PWLE frequency
   values on the buzz entries.
2. The two non-wavetable XM blocks (algs 0x4F21B, 0x4F200) and `cs40l26-svc.bin` / `-dvl.bin` (braking and voltage
   limiting tuning) — not decoded.
3. Per-effect volumes the Pixel HAL applies (tick/click volume levels) — not modelled.
