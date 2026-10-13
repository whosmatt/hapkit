# hapkit — specification (v1)

Waveform workbench for the Fairphone 6 haptics (Awinic AW86938, LRA f0 ≈ 232.4 Hz). Designs int8 @ 24 kHz
waves for Android effects and primitives, compares them with references, simulates the LRA, live-tests on the
dev device, and exports for two upstream routes:

- **RAM route**: custom `haptic_ram.bin` + kernel effect/primitive sequence table (sequencer slots, loops, waits).
- **Stream route**: QTI `libqtivibratoreffect` effect streams (`struct effect_stream`, int8 PCM) as C source.

This document is the contract between modules. Research inputs: `docs/research/*.md`.

## 1. Constraints

- Python 3.12. Available third-party: **numpy 1.26, scipy 1.11 only** (no Flask/FastAPI/pytest). HTTP server =
  stdlib `http.server.ThreadingHTTPServer`. Tests = stdlib `unittest` (`python3 -m unittest discover -s tests`).
- `hapkit/core/**` is **stdlib-only** (no numpy): it must be usable as a deterministic build-time generator
  (Soong genrule) upstream. Same project → byte-identical exports.
- Rounding everywhere in core: round half away from zero: `qround(x) = int(math.copysign(math.floor(abs(x)+0.5), x))`.
- Frontend: vanilla ES modules + `<canvas>`, no framework, no CDN, no build step, works offline.
  Technical UI: labels, units and numbers only; no explanatory prose, no marketing text, no onboarding.
- Device safety: device operations only on `ro.serialno == 214d028e` (configurable constant, default that value);
  verify the serial before every state-changing batch; refuse anything else. Never touch serial `3c439602`.
- Vendor material (`ref/vendor/`, stock waves, Pixel data) is local-only. Exports with `upstream: true` refuse
  waves whose provenance is not `original` (see §3.2).
- Python style: type hints, small functions, terse docstrings, no comments restating code. JS: ES2022 modules.

## 2. Repository layout

```
run.py                     python3 run.py [--host 127.0.0.1] [--port 8765] [--project PATH]
hapkit/__init__.py         __version__ = "0.1.0"
hapkit/core/ids.py         AIDL Effect / CompositePrimitive ids and names
hapkit/core/ramfmt.py      haptic_ram.bin parse/build (stdlib)
hapkit/core/project.py     project schema: new/load/save/validate/canonical json/templates (stdlib)
hapkit/core/render.py      wave segment renderer (stdlib)
hapkit/core/sequence.py    sequencer model: slots/loops/waits -> registers, duration, render (stdlib)
hapkit/core/exports.py     ram bin, kernel table C, effect-stream C, raw, wav (stdlib)
hapkit/analysis/dsp.py     spectrogram, spectrum, envelope, inst. frequency, resample, retarget (numpy/scipy)
hapkit/analysis/lra.py     LRA simulation, metrics, brake optimizer (numpy/scipy)
hapkit/importers/carray.py C/C++ int8 array sources (OSOM effect.cpp, QTI VibratorPatterns.cpp)
hapkit/importers/wavfile.py WAV and raw PCM
hapkit/importers/cirrus.py Cirrus CS40L26/CS40L25 wavetable (.bin, WMDR) — phase 2
hapkit/device/adb.py       persistent adb root shell, push/pull (Windows adb.exe from WSL) — phase 2
hapkit/device/aw.py        AW8693x live-test protocol over sysfs — phase 2
hapkit/server.py           HTTP JSON API + static files
web/index.html, web/css/app.css, web/js/*.js
tools/mock_api.py          stdlib mock of the HTTP API for UI development
tools/uitest.mjs           headless-Chromium UI smoke test (Node 22, CDP over built-in WebSocket)
tests/                     unittest suites
projects/                  *.hapkit.json
out/                       exports (gitignored)
ref/vendor/                vendor material (gitignored): haptic_ram.stock.bin, datasheet text, awdrv/
```

## 3. Data model — project JSON (`format: "hapkit/1"`)

```json
{
  "format": "hapkit/1",
  "name": "fp6-primitives",
  "device": {
    "fs": 24000, "f0": 232.4, "q_drive": 6.0, "q_free": 20.0, "vmax_mv": 8500,
    "base_addr": 2048, "ram_end": 8191, "ram_version": 133, "max_waves": 16,
    "wait_unit": 64, "loop_extra_plays": 1
  },
  "ram": { "waves": ["click", "tick"] },
  "waves": [ <wave> ], "sequences": [ <sequence> ], "effects": [ <effect> ], "references": [ <reference> ]
}
```

- `device.f0` Hz, `q_drive` loaded Q while the output stage drives (including 0-valued samples inside a wave),
  `q_free` Q after playback ends (output released). `ram_end` = last SRAM address (0x1FFF). `max_waves` = driver
  `AW_RAM_WAVE_MAX`. `wait_unit` = samples per wait step (WAVCFG13, series sets 64). `loop_extra_plays`: a slot with
  loop register value `n` plays `n + loop_extra_plays` times; **measured on 214d028e (2026-10-09): 1** — loop 0/1/2/
  5/14 played 1/2/3/6/15 times, MAINLOOP 1/2 repeated the program 2/3 times, a 3-unit wait added 8.0 ms.
- `ram.waves`: ordered wave ids; RAM index = position + 1. A wave may exist without being in RAM (stream-only).
- Ids: `^[a-z0-9_]{1,40}$`, unique per collection.

### 3.1 Wave

```json
{ "id": "click", "name": "click v1", "provenance": "original",
  "segments": [ <segment> ], "post": { "gain": 1.0 }, "notes": "" }
```

Segments are rendered in order with a running carrier phase `phi` (radians) and sample clock.

**tone** — carrier with amplitude/frequency breakpoints:

```json
{ "type": "tone",
  "length": { "unit": "cycles", "value": 2.0 },
  "freq": { "unit": "f0", "points": [[0, 1.0], [1, 1.0]], "interp": "linear" },
  "amp":  { "points": [[0, 1.0], [1, 1.0]], "interp": "linear" },
  "shape": { "kind": "sine", "sharp": 0.0 },
  "phase": { "mode": "continue", "deg": 0.0 },
  "label": "drive" }
```

- `length.unit`: `cycles` | `ms` | `samples`. For `cycles` the breakpoint coordinate `u` is the elapsed fraction
  of the segment's phase (`u = (phi - phi_start) / (2*pi*cycles)`), for `ms`/`samples` the elapsed fraction of time.
  A `cycles` segment ends at the first sample whose phase would reach the target; the sub-sample phase residue
  carries into the next segment (phase continuity). Minimum length 1 sample.
- `freq.unit`: `f0` (multiplier of `device.f0`) or `hz`. `interp`: `linear` | `cos` (raised-cosine ease) |
  `exp` (geometric; for freq only, values > 0).
- `amp.points` values are **signed**, 1.0 = full scale (127). Negative = inverted carrier = **brake**. `interp`:
  `linear` | `cos` | `exp` (dB-linear; falls back to linear if the two points differ in sign or one is |v| < 1e-4).
- `shape.kind`: `sine` → `sin(phi)` if `sharp == 0`, else `tanh(g*sin(phi))/tanh(g)` with `g = 1 + 19*sharp`
  (`sharp` 0..1, 1 ≈ square). `triangle` → `(2/pi)*asin(sin(phi))`.
- `phase.mode`: `continue` → start at previous end phase + `deg`; `absolute` → start at `deg`.
- Phase advances per sample by `2*pi*f(u)/fs`; sample value = `amp(u) * shape(phi)` evaluated at the sample's
  phase *before* the increment.

**gap** — zeros: `{ "type": "gap", "length": {...}, "label": "" }`. Phase keeps advancing at `device.f0`
(the LRA keeps ringing at f0), so a following `continue` tone stays coherent with the free oscillation.
`cycles` length = cycles of f0.

**pcm** — raw samples: `{ "type": "pcm", "fs": 24000, "encoding": "int8" | "f32", "data_b64": "...",
"gain": 1.0, "trim": [start, end] | null, "label": "" }`. `int8` values map v/127. If `fs != device.fs`
core resamples by linear interpolation and warns (importers resample properly with scipy before creating pcm).
After a pcm segment the phase is reset to 0 (a following `continue` tone starts at `deg`).

`post.gain` multiplies the float signal; then quantize: `clamp(qround(v*127), -127, 127)` (never -128),
counting clipped samples.

### 3.2 Provenance

`original` (designed here) | `stock` (Fairphone/Awinic RAM) | `reference:<ref id>` (samples copied from a
reference) | `traced:<ref id>` (parametric, fitted to a reference). Export with `upstream: true`: error for
`stock`/`reference:*`, warning for `traced:*`. Every generated file carries a header listing provenance.

### 3.3 Sequence (RAM route)

```json
{ "id": "seq_click", "name": "", "slots": [
    { "kind": "wave", "wave": "click", "loops": 0 },
    { "kind": "wait", "ms": 100.0 } ],
  "mainloop": 0, "notes": "" }
```

- ≤ 8 slots. Wave slots must reference waves in `ram.waves`; register value = RAM index (1..127, < 0x80).
- `loops` = loop register value 0..14 (15 = infinite, rejected). Plays = `loops + device.loop_extra_plays`.
- Wait slot register = `0x80 | n`, `n = clamp(qround(ms*fs/1000/wait_unit), 1, 127)`; actual wait `n*wait_unit`
  samples; the effective ms is reported. Loops on wait slots are ignored by the chip (always written 0).
- `mainloop` = MAINLOOP register value (0..14); semantics as `loops` for the whole program (parameterised the same
  way; confirm by research).
- Render = concatenation of slot renders (wave renders repeated, waits as zeros), all repeated per mainloop.
  Duration ms reported both exact and as the kernel computes it (`DIV_ROUND_UP(us, 1000)` per §4.2 of the series).

### 3.4 Effect

```json
{ "id": "prim_click", "kind": "primitive", "aidl": 1, "name": "CLICK",
  "sequence": "seq_click", "stream_wave": null, "scale": 1.0, "enabled": true, "notes": "" }
```

- `kind`: `effect` (android.hardware.vibrator.Effect) or `primitive` (CompositePrimitive). `aidl` = enum value.
- RAM route uses `sequence`; stream route renders `stream_wave` if set, else the rendered `sequence`.
- `scale` = design-level amplitude multiplier for previews and stream export (RAM route: reported in the kernel
  table as a comment/gain field).
- `hapkit/core/ids.py` defines: Effect CLICK 0, DOUBLE_CLICK 1, TICK 2, THUD 3, POP 4, HEAVY_CLICK 5,
  RINGTONE_1..15 = 6..20, TEXTURE_TICK 21; CompositePrimitive NOOP 0, CLICK 1, THUD 2, SPIN 3, QUICK_RISE 4,
  SLOW_RISE 5, QUICK_FALL 6, LIGHT_TICK 7, LOW_TICK 8.

### 3.5 Reference

```json
{ "id": "px9_click", "name": "komodo #2 CLICK", "source": "cirrus:cs40l26.bin#2", "fs": 8000,
  "data_b64": "<float32 little-endian, -1..1>", "proprietary": true, "meta": {} }
```

References are never exported into waves except via explicit "copy to wave" (provenance `reference:<id>`).

### 3.6 Templates

- `empty`: device defaults, no waves.
- `stock`: the four stock RAM waves as `pcm` waves (ids `stock_w1..stock_w4`, provenance `stock`, from
  `ref/vendor/haptic_ram.stock.bin`; template fails with a clear error if the file is missing), `ram.waves` in
  stock order, sequences/effects equal to the submitted kernel series: CLICK→w1, DOUBLE_CLICK→w1 + 100 ms + w1,
  TICK→w3, THUD→w2, POP→w3, HEAVY_CLICK→w1; the 8 primitives present but `sequence: null`, `enabled: false`.
  Export of this template's RAM must be byte-identical to the stock file.

## 4. Core API (stdlib)

### 4.1 `ramfmt`

```python
@dataclass
class RamWave: start: int; end: int; data: bytes          # inclusive SRAM addresses
@dataclass
class RamImage: base_addr: int; version: int; waves: list[RamWave]; checksum: int; checksum_ok: bool; extra: bytes
def checksum(blob: bytes) -> int                         # sum(blob[2:]) & 0xffff
def parse(blob: bytes) -> RamImage                       # ValueError on malformed tables
def build(waves: list[bytes], base_addr=0x800, version=0x85, ram_end=0x1FFF) -> bytes   # contiguous after table
def layout(waves_lens: list[int], base_addr, ram_end) -> dict   # addresses, bytes used/free, fits
def int8_to_bytes(samples: list[int]) -> bytes; def bytes_to_int8(b: bytes) -> list[int]
```

File layout: `[0:2]` checksum BE, `[2:4]` base_addr BE, `[4:]` written to SRAM from base_addr: version byte,
N×(start BE16, end BE16), wave data. First wave starts at `base_addr + 1 + 4N`.

### 4.2 `render` / `sequence`

```python
def render_wave(wave: dict, device: dict) -> dict
# -> {"fs", "samples": list[int], "float": list[float], "bounds": [{"start","end","label","index"}],
#     "clips": int, "warnings": list[str]}
def render_sequence(seq: dict, project: dict) -> dict      # same keys + "slot_bounds", "duration_ms",
                                                           # "kernel_ms" (DIV_ROUND_UP(us,1000) as the series)
def render_effect(effect: dict, project: dict, route: str) -> dict   # route "ram" | "stream"
def seq_registers(seq: dict, project: dict) -> dict        # {"wavcfg": [8], "loops": [8], "mainloop", "wait_unit"}
def wait_slot(ms: float, device: dict) -> tuple[int, float]           # (register value, effective ms)
```

### 4.3 `exports`

```python
def export_ram_bin(project, upstream=False) -> tuple[bytes, dict]      # dict = layout report incl. per-wave addresses
def export_kernel_table(project, upstream=False) -> str               # C initializers (see below)
def export_effect_stream_c(project, upstream=False) -> str            # libqtivibratoreffect-style C++ source
def export_raw(samples: list[int]) -> bytes
def export_wav(samples: list[int], fs: int) -> bytes                  # 16-bit mono, value*256
```

Kernel table format (RAM route; struct to be finalised with the kernel patch):

```c
/* hapkit 0.1.0, project "<name>", sha256 <first 16 hex of canonical project json> */
/* provenance: click=original, ... */
static const struct aw_ff_effect aw_ff_effects[] = {
	{ AW_FF_EFFECT_CLICK,		{ 1 },			{ 0 } },
	{ AW_FF_EFFECT_DOUBLE_CLICK,	{ 1, 0x80 | 38, 1 },	{ 0, 0, 0 } },
	{ AW_FF_PRIMITIVE_CLICK,	{ 5, 6 },		{ 2, 0 } },
};
```

Effect-stream C (stream route): one `static const int8_t <id>_data[] = { ... };` per enabled effect, 16 values per
line, and a `struct effect_stream` table + `get_effect_stream()` in the style of the QTI/OSOM sources (primitive id
encoding to be filled from `docs/research/android_primitives.md`; until then a `TODO` constant
`PRIMITIVE_ID_BASE`). Header comment with generator version, project hash, per-array provenance and the recipe
(segment parameters) of each wave so reviewers can read the design.

### 4.4 `project`

```python
def new_project(template="empty") -> dict
def load(path) -> dict; def save(project, path) -> None     # pretty JSON, sorted keys, stable
def validate(project) -> list[dict]                         # [{"level": "error"|"warn", "path": "waves[2].segments[0]", "msg"}]
def canonical_json(project) -> bytes                        # sorted keys, no whitespace; hash input
def project_from_ram_bin(blob: bytes, name: str) -> dict    # pcm waves + ram list
def waves_from_ram_bin(blob: bytes, prefix: str = "ram", provenance: str = "stock") -> tuple[list[dict], list[str], str]
    # (pcm waves with ids <prefix>_w1.., ram order, report) for importing into an existing project
```

## 5. Analysis API (numpy/scipy)

```python
dsp.spectrogram(x, fs, win=256, hop=8, nfft=2048, fmax=1000, db_range=60) -> dict
    # {"t0_ms","dt_ms","df_hz","nt","nf","db_min","db_max","data": uint8 ndarray (nt, nf)}; Hann window,
    # zero-padded edges so short waves (5 ms) still give >= 8 frames
dsp.spectrum(x, fs, nfft=8192, fmax=1000) -> {"f": [...], "db": [...]}   # 0 dB = full-scale sine
dsp.envelope(x, fs) -> ndarray            # |analytic signal|, lightly smoothed
dsp.half_cycle_peaks(x) -> list[(index, value)]
dsp.inst_freq(x, fs, min_amp=0.02) -> list[(t_ms, hz)]   # from zero crossings (half periods), amplitude-gated
dsp.resample(x, fs_in, fs_out) -> ndarray                # resample_poly
dsp.retarget(x, fs, f_ref, f_target, mode)               # "timescale" | "envelope" (resynthesize at f_target)
lra.simulate(x, fs, f0, q_drive, q_free, active_len=None, tail_ms=60) -> {"acc","disp","vel"}  # float arrays
    # 2nd-order resonator driven by x; Q switches from q_drive to q_free at active_len (default len(x));
    # acc normalised so a full-scale steady sine at f0 gives |acc| peak 1.0
lra.metrics(sim, fs, active_len) -> {"peak_acc","t_peak_ms","rise_ms","ring_ms","residual_ratio","energy"}
    # ring_ms: time after active_len until |acc| envelope < 10 % (-20 dB) of peak; residual_ratio: energy after
    # active_len / total
lra.optimize_brake(build, x_drive_len, fs, f0, q_drive, q_free, bounds, objective) -> dict
    # build(params: dict) -> float ndarray (full wave); searches params within bounds minimising
    # residual energy + len_penalty * extra length; returns {"params", "metrics", "evaluations"}
```

## 6. HTTP API

JSON over HTTP, `Content-Type: application/json`. Errors: HTTP 4xx/5xx with `{"error": "msg"}`. The server
holds the authoritative project in memory (+ path, dirty flag). Static: `GET /` → `web/index.html`,
`GET /web/...`.

| method/path | request | response |
|---|---|---|
| `GET /api/state` | — | `{project, path, dirty, version}`; references without `data_b64` (add `n`) |
| `PUT /api/project` | `{project}` | `{ok, issues, dirty}`; references lacking `data_b64` keep the server copy |
| `POST /api/project/new` | `{template}` | `{project, path: null}` |
| `GET /api/projects` | — | `{files: [{path, name, mtime}]}` (projects/*.hapkit.json) |
| `POST /api/project/open` | `{path}` | `{project, path}` |
| `POST /api/project/save` | `{path?}` | `{path}` |
| `POST /api/render` | `{project?, target, overlay?, options?}` | render result (below) |
| `POST /api/import` | `{kind, filename, data_b64, options}` | `{project, added: {waves, references}, report}` |
| `POST /api/export` | `{kind, target?, options}` | `{filename, mime, data_b64?, text?, report, path}` |
| `POST /api/brake/optimize` | `{wave, segment?, bounds?, objective?}` | `{segment, before, after, evaluations}` |
| `GET /api/device` | — | device status |
| `POST /api/device/connect` | — | device status |
| `POST /api/device/ram/upload` | `{source: "project"|"stock"}` | `{ok, report, status}` |
| `POST /api/device/play` | `{target, route, gain?, repeat?, interval_ms?}` | `{ok, report}` |
| `POST /api/device/stop` | — | `{ok}` |
| `POST /api/device/restore` | — | `{ok, report, status}` |
| `GET /api/device/log?since=N` | — | `{lines: [{seq, t, level, text}], next}` |

- `target`: `{"type": "wave"|"sequence"|"effect"|"reference", "id": "...", "route"?: "ram"|"stream"}`.
- `import.kind`: `ram_bin` | `carray` | `wav` | `raw` | `cirrus`; `options.as`: `reference` (default) | `wave`;
  raw: `options.fs`, `options.encoding` (`int8`|`int16le`|`f32`).
- `export.kind`: `ram_bin` | `kernel_table` | `effect_stream_c` | `raw` | `wav` | `project`; `options.upstream`.
  Files are also written to `out/`.
- `options` for render: `{"spec": {"win","hop","nfft","fmax","db_range"}, "sim": {"enabled": true, "tail_ms": 60},
  "strength_gain": 1.0}`.

Render result:

```json
{ "target": {...}, "fs": 24000, "n": 716, "duration_ms": 29.83, "kernel_ms": 30,
  "samples": [0, 7, ...], "bounds": [{"start": 0, "end": 102, "label": "drive", "index": 0}],
  "slot_bounds": [...], "clips": 0, "warnings": [],
  "env": [...], "ifreq": [[t_ms, hz], ...],
  "spectrum": {"f": [...], "db": [...]},
  "spectrogram": {"t0_ms": 0, "dt_ms": 0.333, "df_hz": 11.7, "nt": 90, "nf": 86,
                  "db_min": -60, "db_max": 0, "data_b64": "<uint8 nt*nf, time-major>"},
  "sim": {"acc": [...], "disp": [...], "n": 2156, "metrics": {...}},
  "overlay": {"label": "...", "fs": 24000, "float": [...], "offset_ms": 0} | null,
  "ram": {"bytes": 716, "fits": true} }
```

`overlay` request: `{"ref": "<reference id>" | "wave:<id>" | "sequence:<id>", "offset_ms": 0, "gain": 1.0,
"retarget": "none"|"timescale"|"envelope", "f_ref": 150.0}` → resampled to `device.fs`.

## 7. Device protocol (phase 2; details from `docs/research/driver_livetest.md`)

- adb: Windows `adb.exe` (`/mnt/c/Users/matt/adb-fastboot/platform-tools/adb.exe`, overridable via
  `HAPKIT_ADB`), `-s 214d028e`; strip `\r`; host paths for push/pull via `wslpath -w`.
- Persistent root shell (`adb root` once, then one long-lived `adb shell` with sentinel-delimited commands) to keep
  per-action latency low; fallback one-shot `adb shell`.
- Working dir `/data/local/tmp/hapkit/`, firmware dir `/data/local/tmp/hapkit/fw/`.
- RAM upload: export bin → push → prepend the work dir to `firmware_class.path` (on this kernel it is Android's
  comma-separated list `/vendor/firmware_mnt/image,…,/vendor/firmware,`; never clear it) → permissive SELinux for
  the session → `ram_update` → wait for the driver's completion line → read the SRAM back (`cat ram_update` dumps it
  to the kernel log) and compare. Restore: original firmware path, loops/MAINLOOP cleared (they persist in the chip
  and the kernel's effect path does not reset MAINLOOP), stock reload, original SELinux mode.
- Play routes: `ram` (program sequencer slots/loops/waits + gain, start), `rtp` (render → PCM file in an RTP slot →
  `rtp` index), `hal` (`cmd vibrator_manager synced prebaked <id>` / composition when supported).
- Every operation is logged to the device log (`GET /api/device/log`).

## 8. Web UI

Dark, dense, technical. Monospace for numbers. Colours as CSS variables. No prose.

```
┌ top bar: hapkit · project ▾ (new/open/save/save as) · import ▾ · export ▾ · undo/redo ·
│          fs · f0 · q_drive · q_free (editable) · device pill (serial · root · selinux · ram hash) ┐
├ left (library) ──────┬ center (plots, shared time axis) ───────────────┬ right (inspector) ─────┤
│ RAM  n/16  used/free │ waveform: int8 samples, envelope, segment      │ selected item fields   │
│  1 click   716  29.8 │   bounds, sim acc (own scale), overlay ghost   │ segment table (tone/   │
│  …  (drag reorder)   │ spectrogram: heatmap + inst-freq trace + f0    │  gap/pcm rows, inline  │
│ WAVES                │   line; frequency axis 0..fmax                 │  numeric edit, add/del/│
│ SEQUENCES            │ spectrum (right of waveform) + metrics strip   │  move, duplicate)      │
│ EFFECTS (table:      │                                                │ brake: optimize, params│
│  id, seq, ms, route) │                                                │ metrics                │
│ REFERENCES           │                                                │ device: route ▾, gain, │
│                      │                                                │  play, stop, upload RAM│
├ bottom: log (device ops, warnings, validation issues) ─────────────────────────────────────────┤
```

- Plots: wheel = zoom x around cursor; drag = pan; double-click = fit; shift+wheel = zoom y; hover readout
  (t ms, sample index, value, env, inst. freq, sim acc). Time axis shared by waveform and spectrogram. Segment
  boundaries as vertical lines with labels; RAM sequence slots shaded.
- Direct manipulation on the waveform plot for tone segments: drag amplitude breakpoint handles vertically; drag a
  segment's end boundary horizontally (updates `length.value` in its unit, cycles snap to 0.5 with shift).
- Brake optimiser (brake ⚙): optimises the selected brake segment, else a trailing brake, else appends one. Soft
  options in cycles: ramp in / ramp out (cosine amplitude ramps of the brake from/to 0) and taper (fade of the
  preceding tone segment's end), so the drive does not reverse abruptly; robust averages the residual over f0
  ±0.3 % and q_drive ±15 %; "sweep in" optimises ramp-in 0…2 and lists length, click (energy above 2.5 f0 around
  the brake) and residual per candidate; a row is picked and applied.
- Overlay picker (reference or another wave/sequence) with offset, gain, retarget mode.
- Sim toggle and tail length; spectrogram window/fmax/dB range controls.
- Keyboard: Space or middle mouse button play (current route) · Esc stop · Ctrl+S save · Ctrl+Z / Ctrl+Shift+Z undo/redo ·
  Del delete selected segment · F fit.
- Edits update the server via `PUT /api/project` or `POST /api/render {project}` (debounced, latest-wins);
  undo/redo client-side (project snapshots without reference data).
- Effects table shows per effect: aidl id, name, sequence, duration (kernel ms), route availability, enabled.
- Validation issues and render warnings show in the log and inline (red marker on the item).
