# hapkit

This is a tool to design, simulate and test haptic waveforms on the Fairphone 6, and output them in a format suitable for the Awinic AW86938 driver.  
Measurement of the FP6 LRA response and Q works well enough via the microphone that the external accelerometer measurement was ommitted in this repo. The measurements are used to predict and generate braking waveforms.  
The tool shows reference waveforms, mostly to get a better idea of what the often vague haptic effect descriptions are supposed to be. Waveforms from the pixel 3 and pixel 9 were used, but they are not included here due to the unclear license.

If you are interested in live testing:  
There are surprise absolute paths in various scripts, which will need to be changed.  
My test device gets wiped regularly, so install/test scripts weren't made with proper cleanup in mind.

### SLOP WARNING

This repo is almost entirely LLM generated, use at your own risk.

---

Waveform workbench for the Fairphone 6 haptics (Awinic AW86938, LRA f0 ≈ 232.4 Hz): design int8 @ 24 kHz waves
for Android effects and primitives, compare them with references, simulate the LRA, play them on the dev device,
export for the RAM route (`haptic_ram.bin` + kernel sequence table) or the stream route (QTI effect-stream C).

```
python3 run.py [--port 8765] [--project projects/fp6-primitives.hapkit.json]
python3 tools/mkproject.py     # rebuild projects/fp6-primitives.hapkit.json (starter designs + references)
python3 -m unittest discover -s tests
node tools/uitest.mjs http://127.0.0.1:8765/ out/ui   # headless UI smoke test, screenshots in out/ui
```

Requires Python 3.12 with numpy and scipy (nothing else). `hapkit/core` is stdlib-only and deterministic so it
can run as a build-time generator. Contract and data model: [docs/SPEC.md](docs/SPEC.md).

## Model

- **wave**: segments rendered with a running carrier phase. `tone` (length in cycles/ms/samples; frequency and
  signed amplitude breakpoints — negative amplitude is an anti-phase brake; sine/sharp/triangle shape; phase
  continue/absolute), `gap` (phase keeps running at f0), `pcm` (imported samples). Quantised to −127…127.
- **RAM**: ordered wave list → `haptic_ram.bin` (index = position + 1). The last RAM wave is the FF_CONSTANT
  `on()`/`setAmplitude` loop wave. Budget: 0x0800…0x1FFF (6144 B incl. table).
- **sequence**: AW8693x sequencer program, ≤ 8 slots of {RAM wave, loop register 0…14} or {wait ms} (64-sample
  units as programmed by the kernel series), plus MAINLOOP. `device.loop_extra_plays` (default 1: plays = loops+1).
- **effect**: AIDL Effect / CompositePrimitive → sequence (RAM route) or stream wave (stream route) + scale.
- **reference**: imported waveform for overlay/comparison only (Pixel CS40L26 wavetable, WAV, raw, C arrays).
- **provenance**: `original` | `stock` | `reference:<id>` | `traced:<id>`. Exports with *upstream* refuse
  `stock`/`reference:*` waves.
- **publishing**: references marked proprietary (e.g. imported Pixel wavetables) are saved to
  `<project>.refs.local.json` next to the project and merged on load; that file is gitignored, so
  `projects/fp6.hapkit.json` can be committed and rebuilds the same `haptic_ram.bin` anywhere.

## Analysis

Spectrogram (Hann, fixed 0 dB = full-scale sine) with half-cycle instantaneous-frequency points, spectrum with
the LRA acceleration response, and a resonator model of the LRA (`q_drive` while the output drives, `q_free`
after release; acc normalised to a steady full-scale sine at f0). The brake optimiser searches cycles/amplitude/
phase of a brake segment against that model. **Q values are assumptions until measured** — treat simulated
ringdown as relative.

## Device (live test)

Only `214d028e` (verified via `ro.serialno`; the daily driver serial is refused). Root via `adb root`; Windows
`adb.exe` from WSL (`HAPKIT_ADB` overrides). No kernel change needed:

| action | mechanism |
|---|---|
| upload RAM | push to `/data/local/tmp/hapkit/fw`, prepend that dir to `firmware_class.path` (Android's comma list), SELinux permissive, `ram_update`, wait for the driver's completion line, read the SRAM back and compare |
| play `ram` | WAVCFG1–8 and loops via `awrw`, MAINLOOP via `reg`, `gain`, start with LED `brightness` (RAM mode; `activate` would use the DT RAM-loop mode) |
| play `rtp` | render → RTP slot 4 (`haptic_rtp_auto_sin.bin`) → `rtp` (chip auto-brake stays on unless the `rtp_brake` test module is loaded) |
| play `hal` | `cmd vibrator_manager synced -f prebaked <id>` — uses the kernel's own effect table |
| capture | `tools/capture.py`: `hkcap` records the mic (or IMU) via `app_process`, the RAM play script is its trigger |
| restore | original firmware path, slot loops and MAINLOOP cleared, stock RAM reloaded, original SELinux mode |

**Persistent install (dev device, userdebug):** `tools/devinstall.sh [project]` exports `haptic_ram.bin` and the
kernel effect table, builds `haptic.ko` from a copy of the `haptic_hv` driver with the primitives series (`KWT`)
with its `aw_ff_effects` table replaced by the project's, puts both into the `adb remount` overlay and adds an init hook that swaps the module in `post-fs-data`, before the vibrator
HAL starts. The framework then uses the project for all effects and the 8 primitives (`CAP_COMPOSE_EFFECTS` via
`/sys/class/qcom-haptics/primitive_duration`). `tools/devinstall.sh --remove` goes back to stock.

RAM playback refuses to run when the device RAM differs from the project's RAM image (upload first). Verified on
`214d028e` (LOS 24.0, kernel with the series): upload + read-back identical, all three routes reach the driver,
restore returns firmware path, SELinux and sequencer registers to their original values.
`tools/android/hkprobe` (static aarch64) triggers a sysfs node and logs GLBRD5 state transitions (~1.3 ms per
poll) — used to measure the loop semantics: plays = loops + 1, MAINLOOP likewise.

## Measuring f0 / Q

`python3 tools/mkproject.py --measure` → `projects/fp6-measure.hapkit.json`: bursts of 20/60 cycles at f0 (plus
half level and ±10 % detuned) built from looped 1-cycle RAM waves (RAM mode has no chip auto-brake), and a 2 s
80–600 Hz sweep for the RTP route.

**On the device itself** (no external sensor): `python3 tools/capture.py [project] --seq m_burst20,m_half20 --reps 6`
uploads the RAM, records the phone's own microphone (48 kHz, `VOICE_RECOGNITION` = no AGC/NS) while each
sequence plays, restores the device, and prints mean ± std of f0 / Q_free / Q_drive / plateau level per sequence
(recordings and `summary.json` in `out/capture/`). Put the phone on cloth: on a hard table the table rings along
and the decay grows a long tail. The recorder is `tools/android/hkcap` (dex run through `app_process` as root;
`build.sh` uses the JDK/d8/android.jar prebuilts of the LineageOS tree). The FP6 IMU is no use here: it runs on
the ADSP sensor island (signed firmware), Android sees at most 500 Hz, and at that ODR its low-pass leaves 232 Hz
in the noise.

**External accelerometer**: import the log (`accelerometer csv`: time column auto-detected, axis = largest
variance unless set; or WAV), then *fit f0/Q* on the reference: free-decay log-linear fit from the end of the
drive plateau → `q_free`, rise fit → `q_drive`; apply to the project. On simulated recordings (4 kHz, noise) the
fit recovers Q_free within 1 % and Q_drive within ~7 %.

Measured on `214d028e` (mic, on cloth, gain 128, 2026-10-11): f0 231.5–232.0 Hz, **Q_free 25–27** at all levels
(1.0 / 0.5 / 0.25 FS). Device defaults: f0 231.7, q_drive 26, q_free 26 — q_drive from the brake measurements
below; the rise fit (22–23 at 0.25 FS) leaves every brake ~5 % too weak. Steady drive compresses above ~0.3–0.5 FS
(1.0 FS is only +1.4 dB over 0.5 FS) and its harmonics grow (HD2-8 −34 dB at ≤ 0.25 FS, −31 at 0.5, −23 at 1.0):
rattles. Short pulses (≤ 2 cycles at 1.0) stay below that.

## Checking brakes against the model

`python3 tools/capture.py <project> --seq all --compare <unbraked seq>` plays every sequence and fits a free decay
15–80 ms after each sequence ends (time-gated, so the drive pulse cannot leak in), relative to the reference
sequence, then does the same on the simulation. The ratio cancels the acoustic path at f0, so measured and model
numbers are directly comparable. The table also lists harmonic distortion (HD2-8) for sequences with ≥ 12 steady
cycles. `tools/mkproject.py --brakes` builds `projects/fp6-brakes.hapkit.json` (one click drive with brake
amplitude, phase and length varied, plus a tick series).

On `214d028e` (brake series plus a half-amplitude repeat, 23 variants): 19 within ±1.4 dB of the model with
q_drive 26, independent of level. The exceptions are designs the model predicts as a deep null (−40…−80 dB): on the
device those land at −25…−35 dB, because a null depends on f0 to ±0.3 % and q_drive to ±15 %, which is about how
well they are known.
So design brakes for the worst case over that spread, not for the nominal null. Brakes that end on a peak (1.25 /
1.75 cycles at phase 0) leave a step and brake badly. The re-tuned starters measured: click −35.7 dB, tick −34.8,
thud −33.7 (were −23, −32, −22 with brakes tuned for q_drive 22). The mic also shows ±40 Hz sidebands around f0 at
about −24 dB relative to unbraked ringing; they don't depend on the brake.

## References on disk (not in git)

- `ref/vendor/haptic_ram.stock.bin` — FP6 stock RAM library (Awinic/Fairphone).
- `ref/vendor/komodo/` — Pixel 9 Pro XL haptics firmware from TheMuppets (`cs40l26.bin` = RAM wavetable).
- `ref/aosp/crosshatch/` — Pixel 3 (CS40L20) wavetable WAV sources from AOSP `device/google/crosshatch`.
- `ref/vendor/aw8693x_ds_v1.4.txt`, `ref/vendor/awdrv/` — datasheet text, Awinic driver packages.
