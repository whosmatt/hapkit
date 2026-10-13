"""HTTP JSON API + static web UI."""
from __future__ import annotations

import base64
import copy
import glob
import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from . import __version__
from .analysis import dsp, lra
from .core import exports, project as proj, ramfmt
from .core.render import _interp as _interp_amp, render_wave
from .core.sequence import render_effect, render_sequence
from .importers import import_data, make_id, ref_samples

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
WEB = os.path.join(ROOT, "web")
PROJECTS = os.path.join(ROOT, "projects")
OUT = os.path.join(ROOT, "out")


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _r(x, nd: int = 4) -> list:
    return np.round(np.asarray(x, dtype=np.float64), nd).tolist()


class App:
    def __init__(self, project_path: str | None = None):
        self.lock = threading.RLock()
        self.path = None
        self.dirty = False
        self.version = 0
        self.project = proj.new_project("empty", "untitled")
        self.log_lines: list[dict] = []
        self.log_seq = 0
        self._device = None
        if project_path:
            self.open(project_path)
        else:
            try:
                self.project = proj.new_project("stock", "stock")
            except FileNotFoundError:
                pass

    # ---- log -------------------------------------------------------------------------------
    def log(self, text: str, level: str = "info") -> None:
        with self.lock:
            self.log_seq += 1
            self.log_lines.append({"seq": self.log_seq, "t": time.time(), "level": level, "text": text})
            del self.log_lines[:-2000]

    # ---- project ---------------------------------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            return {"project": proj.strip_reference_data(self.project), "path": self.path, "dirty": self.dirty,
                    "version": self.version, "app_version": __version__,
                    "issues": proj.validate(self.project)}

    def set_project(self, p: dict) -> list[dict]:
        if not isinstance(p, dict) or p.get("format") != proj.FORMAT:
            raise ApiError("not a hapkit/1 project")
        with self.lock:
            old = {r["id"]: r for r in self.project.get("references", [])}
            p = copy.deepcopy(p)
            for r in p.get("references", []):
                if "data_b64" not in r:
                    if r.get("id") not in old:
                        raise ApiError(f"reference {r.get('id')!r} has no data")
                    r["data_b64"] = old[r["id"]]["data_b64"]
                r.pop("n", None)
            dev = dict(proj.DEFAULT_DEVICE)
            dev.update(p.get("device") or {})
            p["device"] = dev
            if proj.canonical_json(p) != proj.canonical_json(self.project):
                self.project = p
                self.dirty = True
                self.version += 1
            return proj.validate(self.project)

    def open(self, path: str) -> dict:
        path = self._project_path(path)
        p = proj.load(path)
        with self.lock:
            self.project, self.path, self.dirty = p, path, False
            self.version += 1
        self.log(f"opened {os.path.relpath(path, ROOT)}")
        return self.state()

    def save(self, path: str | None) -> str:
        with self.lock:
            target = self._project_path(path) if path else self.path
            if not target:
                target = self._project_path(make_id(self.project.get("name") or "untitled", set()))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            proj.save(self.project, target)
            self.path, self.dirty = target, False
        self.log(f"saved {os.path.relpath(target, ROOT)}")
        return target

    @staticmethod
    def _project_path(path: str) -> str:
        if not path.endswith(".hapkit.json"):
            path += ".hapkit.json"
        if os.path.isabs(path):
            return os.path.normpath(path)
        for base in (os.getcwd(), ROOT):
            cand = os.path.normpath(os.path.join(base, path))
            if os.path.exists(cand):
                return cand
        rel = path[len("projects/"):] if path.startswith("projects/") else path
        return os.path.normpath(os.path.join(PROJECTS, rel))

    def list_projects(self) -> list[dict]:
        out = []
        for f in sorted(glob.glob(os.path.join(PROJECTS, "*.hapkit.json"))):
            out.append({"path": os.path.relpath(f, PROJECTS), "name": os.path.basename(f)[:-12],
                        "mtime": os.path.getmtime(f)})
        return out

    # ---- lookup ----------------------------------------------------------------------------
    def find(self, coll: str, iid: str) -> dict:
        for item in self.project.get(coll, []):
            if item.get("id") == iid:
                return item
        raise ApiError(f"no {coll[:-1]} {iid!r}", 404)

    # ---- render ----------------------------------------------------------------------------
    def target_signal(self, target: dict) -> dict:
        p, dev = self.project, self.project["device"]
        fs = int(dev["fs"])
        t, iid = target.get("type"), target.get("id")
        try:
            if t == "wave":
                r = render_wave(self.find("waves", iid), dev)
                r["duration_ms"] = len(r["samples"]) * 1000.0 / fs
                r["kernel_ms"] = None
                order = p.get("ram", {}).get("waves", [])
                if iid in order:
                    lens = [len(render_wave(self.find("waves", w), dev)["samples"]) for w in order]
                    lay = ramfmt.layout(lens, int(dev["base_addr"]), int(dev["ram_end"]))
                    r["ram"] = {"index": order.index(iid) + 1, "bytes": len(r["samples"]), "used": lay["used"],
                                "capacity": lay["capacity"], "fits": lay["fits"]}
            elif t == "sequence":
                r = render_sequence(self.find("sequences", iid), p)
            elif t == "effect":
                r = render_effect(self.find("effects", iid), p, target.get("route", "ram"))
            elif t == "reference":
                ref = self.find("references", iid)
                x = dsp.resample(ref_samples(ref), float(ref["fs"]), fs)
                r = {"fs": fs, "float": x.tolist(), "samples": np.clip(np.round(x * 127), -127, 127).astype(int).tolist(),
                     "bounds": [], "clips": 0, "warnings": [], "native_fs": ref["fs"],
                     "duration_ms": len(x) * 1000.0 / fs, "kernel_ms": None}
            else:
                raise ApiError(f"unknown target type {t!r}")
        except ValueError as e:
            raise ApiError(str(e)) from e
        return r

    def overlay(self, spec: dict) -> dict | None:
        if not spec or not spec.get("ref"):
            return None
        ref = spec["ref"]
        kind, _, iid = ref.partition(":") if ":" in ref else ("reference", "", ref)
        sig = self.target_signal({"type": kind, "id": iid})
        dev = self.project["device"]
        x = np.asarray(sig["float"] if kind == "reference" else np.asarray(sig["samples"]) / 127.0)
        mode = spec.get("retarget", "none")
        if mode != "none":
            f_ref = float(spec.get("f_ref") or 0)
            if f_ref <= 0:
                raise ApiError("retarget needs f_ref")
            x = dsp.retarget(x, int(dev["fs"]), f_ref, float(dev["f0"]), mode)
        x = x * float(spec.get("gain", 1.0))
        return {"label": ref, "fs": int(dev["fs"]), "float": _r(x), "offset_ms": float(spec.get("offset_ms", 0.0))}

    def render(self, req: dict) -> dict:
        with self.lock:
            if "project" in req:
                self.set_project(req["project"])
            target = req.get("target") or {}
            opts = req.get("options") or {}
            sig = self.target_signal(target)
            dev = self.project["device"]
            ov = self.overlay(req.get("overlay"))
        fs = int(dev["fs"])
        x = np.asarray(sig["samples"], dtype=np.float64) / 127.0
        if target.get("type") == "reference":
            x = np.asarray(sig["float"])
        spec_o = {"win": 256, "hop": 8, "nfft": 2048, "fmax": 1000.0, "db_range": 60.0, **(opts.get("spec") or {})}
        sg = dsp.spectrogram(x, fs, int(spec_o["win"]), int(spec_o["hop"]), int(spec_o["nfft"]),
                             float(spec_o["fmax"]), float(spec_o["db_range"]))
        out = {k: sig.get(k) for k in ("fs", "samples", "bounds", "slot_bounds", "clips", "warnings",
                                       "duration_ms", "kernel_ms", "ram", "registers", "stream_ms", "native_fs")}
        out.update({"target": target, "n": len(x), "env": _r(dsp.envelope(x, fs)),
                    "ifreq": [[round(t, 3), round(f, 2)] for t, f in dsp.inst_freq(x, fs)],
                    "spectrum": {k: _r(v, 2) for k, v in dsp.spectrum(x, fs, fmax=spec_o["fmax"]).items()},
                    "spectrogram": {**{k: v for k, v in sg.items() if k != "data"},
                                    "data_b64": base64.b64encode(sg["data"].tobytes()).decode()},
                    "overlay": ov})
        if target.get("type") == "reference":
            out["float"] = _r(x)
        sim_o = {"enabled": True, "tail_ms": 60.0, **(opts.get("sim") or {})}
        if sim_o["enabled"] and len(x):
            sim = lra.simulate(x, fs, float(dev["f0"]), float(dev["q_drive"]), float(dev["q_free"]),
                               tail_ms=float(sim_o["tail_ms"]))
            out["sim"] = {"acc": _r(sim["acc"]), "disp": _r(sim["disp"]), "n": len(sim["acc"]),
                          "metrics": lra.metrics(sim, fs)}
            if sim_o.get("robustness"):
                out["sim"]["robustness"] = lra.robustness(x, fs, float(dev["f0"]), float(dev["q_drive"]),
                                                          float(dev["q_free"]))
        return out

    def lengths(self, req: dict) -> dict:
        with self.lock:
            if "project" in req:
                self.set_project(req["project"])
            dev = self.project["device"]
            out = {}
            for w in self.project.get("waves", []):
                try:
                    out[w["id"]] = len(render_wave(w, dev)["samples"])
                except ValueError:
                    out[w["id"]] = None
            return {"lengths": out, "issues": proj.validate(self.project)}

    def reference_to_wave(self, req: dict) -> dict:
        from .importers import wave_from_samples
        with self.lock:
            if "project" in req:
                self.set_project(req["project"])
            ref = self.find("references", req.get("id"))
            dev = self.project["device"]
            fs = int(dev["fs"])
            x = dsp.resample(ref_samples(ref), float(ref["fs"]), fs)
            mode = req.get("retarget", "none")
            if mode != "none":
                f_ref = float(req.get("f_ref") or 0)
                if f_ref <= 0:
                    raise ApiError("retarget needs f_ref")
                x = dsp.retarget(x, fs, f_ref, float(dev["f0"]), mode)
            taken = {i["id"] for c in ("waves", "sequences", "effects", "references") for i in self.project[c]}
            wid = make_id(f"{ref['id']}_w", taken)
            w = wave_from_samples(wid, f"{ref.get('name', ref['id'])} ({mode})", x, fs, fs, f"reference:{ref['id']}")
            self.project["waves"].append(w)
            self.dirty = True
            self.version += 1
            return {**self.state(), "wave": wid}

    def fit(self, req: dict) -> dict:
        with self.lock:
            ref = self.find("references", req.get("ref"))
            x = ref_samples(ref)
            fs = int(round(float(ref["fs"])))
        try:
            return lra.fit_q(x, fs, req.get("f0_hint") or None)
        except ValueError as e:
            raise ApiError(str(e)) from e

    # ---- brake -----------------------------------------------------------------------------
    def brake_optimize(self, req: dict) -> dict:
        """Optimise a brake segment (selected, or appended) against the LRA model.

        soft = {ramp_in, ramp_out, taper} in cycles: the brake's amplitude rises from 0 and falls back to 0 with
        cosine ramps, and the end of the preceding tone segment fades out over `taper` cycles, so the drive never
        reverses abruptly (less broadband click, longer brake). robust averages the residual over f0 +-0.3 % and
        q_drive +-15 %. sweep = list of ramp_in values: optimise each and return all as candidates."""
        with self.lock:
            wave = copy.deepcopy(self.find("waves", req.get("wave")))
            dev = dict(self.project["device"])
        segs = wave.get("segments") or []
        idx = req.get("segment")
        if idx is None:
            seg = {"type": "tone", "length": {"unit": "cycles", "value": 1.0},
                   "freq": {"unit": "f0", "points": [[0, 1.0], [1, 1.0]], "interp": "linear"},
                   "amp": {"points": [[0, -0.5], [1, -0.5]], "interp": "linear"},
                   "shape": {"kind": "sine", "sharp": 0.0}, "phase": {"mode": "continue", "deg": 0.0},
                   "label": "brake"}
            segs.append(seg)
            idx = len(segs) - 1
        else:
            idx = int(idx)
            if not 0 <= idx < len(segs) or segs[idx].get("type") != "tone":
                raise ApiError("segment must be a tone segment")
        base = copy.deepcopy(wave)
        base["segments"] = [x for i, x in enumerate(segs) if i != idx]
        fs, f0 = int(dev["fs"]), float(dev["f0"])
        x0 = np.asarray(render_wave(base, dev)["samples"], dtype=np.float64) / 127.0
        before = lra.metrics(lra.simulate(x0, fs, f0, float(dev["q_drive"]), float(dev["q_free"])), fs)
        before["click_db"] = lra.click_db(x0, fs, f0, max(0, len(x0) - int(round(fs / f0))), len(x0))
        soft = req.get("soft") or {}
        objective = {**(req.get("objective") or {}), "robust": bool(req.get("robust", False))}
        sweep = req.get("sweep")
        try:
            if sweep:
                cands = [self._brake_one(wave, idx, dev, {**soft, "ramp_in": float(r)}, req.get("bounds"), objective)
                         for r in sweep]
            else:
                cands = [self._brake_one(wave, idx, dev, soft, req.get("bounds"), objective)]
        except ValueError as e:
            raise ApiError(str(e)) from e
        best = cands[0]
        return {"wave": wave["id"], "index": idx, "append": req.get("segment") is None, **best,
                "before": before, "candidates": cands if sweep else None}

    @staticmethod
    def _brake_one(wave: dict, idx: int, dev: dict, soft: dict, bounds: dict | None, objective: dict) -> dict:
        fs, f0 = int(dev["fs"]), float(dev["f0"])
        ri, ro, taper = (max(0.0, float(soft.get(k) or 0.0)) for k in ("ramp_in", "ramp_out", "taper"))
        segs = wave["segments"]
        work = copy.deepcopy(wave)
        prev = idx - 1 if idx > 0 and segs[idx - 1].get("type") == "tone" else None
        if taper > 0 and prev is not None:
            b = render_wave(wave, dev)["bounds"][prev]
            cyc = (b["end"] - b["start"]) * f0 / fs
            u0 = max(0.0, 1.0 - taper / cyc) if cyc > 0 else 0.0
            ps = work["segments"][prev]
            amp = ps.get("amp") or {}
            pts = sorted(([float(u), float(v)] for u, v in (amp.get("points") or [[0, 1.0], [1, 1.0]])), key=lambda q: q[0])
            v0 = _interp_amp(pts, u0, amp.get("interp", "linear"))
            ps["amp"] = {"points": [q for q in pts if q[0] < u0] + [[round(u0, 4), round(v0, 4)], [1.0, 0.0]],
                         "interp": amp.get("interp", "linear")}
        cur = segs[idx]
        if ri > 0 or ro > 0:
            shape, interp = None, "cos"
            cands = (bounds or {}).get("cycles") or [ri + ro + k for k in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)]
        else:
            pts = cur.get("amp", {}).get("points") or [[0, -0.5], [1, -0.5]]
            a0 = abs(pts[0][1]) or 1.0
            shape, interp = [[u, abs(v) / a0] for u, v in pts], cur.get("amp", {}).get("interp", "linear")
            cands = (bounds or {}).get("cycles") or [0.5, 1.0, 1.5, 2.0]

        def apply(params: dict) -> dict:
            w2 = copy.deepcopy(work)
            s = w2["segments"][idx]
            L = float(params["cycles"])
            if shape is None:
                a = min(ri / L, 1.0)
                bb = max(1.0 - ro / L, a)
                rel = ([[0.0, 0.0]] if ri > 0 else [[0.0, 1.0]]) + ([[a, 1.0]] if ri > 0 else []) + \
                      ([[bb, 1.0]] if ro > 0 and bb > a else []) + ([[1.0, 0.0]] if ro > 0 else [[1.0, 1.0]])
                uniq = []
                for u, v in rel:
                    if not uniq or u > uniq[-1][0]:
                        uniq.append([u, v])
                rel = uniq
            else:
                rel = shape
            s["length"] = {"unit": "cycles", "value": L}
            s["amp"] = {"points": [[round(u, 4), -round(params["amp"] * r, 4)] for u, r in rel], "interp": interp}
            s["phase"] = {"mode": "continue", "deg": float(params["phase_deg"])}
            s.setdefault("label", "brake")
            return w2

        def build(params: dict):
            return np.asarray(render_wave(apply(params), dev)["samples"], dtype=np.float64) / 127.0

        res = lra.optimize_brake(build, fs, f0, float(dev["q_drive"]), float(dev["q_free"]),
                                 {**(bounds or {}), "cycles": cands}, objective)
        out = apply(res["params"])
        w = build(res["params"])
        bnd = render_wave(out, dev)["bounds"]
        c0 = bnd[prev]["start"] if taper > 0 and prev is not None else bnd[idx]["start"]
        res["metrics"]["click_db"] = lra.click_db(w, fs, f0, c0, bnd[idx]["end"])
        return {"segment": out["segments"][idx], "prev_index": prev if taper > 0 else None,
                "prev_segment": out["segments"][prev] if taper > 0 and prev is not None else None,
                "params": res["params"], "after": {**res["metrics"], "length_ms": round(len(w) * 1000.0 / fs, 2)},
                "soft": {"ramp_in": ri, "ramp_out": ro, "taper": taper}, "evaluations": res["evaluations"]}

    # ---- import / export -------------------------------------------------------------------
    def do_import(self, req: dict) -> dict:
        data = base64.b64decode(req.get("data_b64", ""))
        opts = req.get("options") or {}
        with self.lock:
            try:
                res = import_data(req.get("kind"), req.get("filename", ""), data, opts, self.project)
            except (ValueError, NotImplementedError) as e:
                raise ApiError(str(e)) from e
            self.project["references"] += res.get("references", [])
            self.project["waves"] += res.get("waves", [])
            if res.get("ram_order") and opts.get("ram", "none") in ("replace", "append"):
                order = self.project["ram"]["waves"]
                self.project["ram"]["waves"] = res["ram_order"] if opts["ram"] == "replace" else order + res["ram_order"]
            if res.get("device"):
                self.project["device"].update(res["device"])
            self.dirty = True
            self.version += 1
        self.log(f"import {req.get('kind')} {req.get('filename')}: {res.get('report', '')}")
        return {**self.state(), "added": {"waves": [w["id"] for w in res.get("waves", [])],
                                          "references": [r["id"] for r in res.get("references", [])]},
                "report": res.get("report", "")}

    def do_export(self, req: dict) -> dict:
        kind = req.get("kind")
        opts = req.get("options") or {}
        up = bool(opts.get("upstream"))
        with self.lock:
            p = copy.deepcopy(self.project)
        name = make_id(p.get("name") or "project", set())
        try:
            if kind == "ram_bin":
                data, rep = exports.export_ram_bin(p, up)
                fn, mime, text = "haptic_ram.bin", "application/octet-stream", None
                report = json.dumps(rep, indent=1)
            elif kind == "kernel_table":
                text, fn, mime = exports.export_kernel_table(p, up), f"{name}_aw_ff_effects.c", "text/x-c"
                data, report = text.encode(), ""
            elif kind == "effect_stream_c":
                text, fn, mime = exports.export_effect_stream_c(p, up), f"{name}_effect.cpp", "text/x-c"
                data, report = text.encode(), ""
            elif kind in ("raw", "wav"):
                with self.lock:
                    sig = self.target_signal(req.get("target") or {})
                tid = make_id((req.get("target") or {}).get("id", "target"), set())
                if kind == "raw":
                    data, fn, mime = exports.export_raw(sig["samples"]), f"{tid}.pcm", "application/octet-stream"
                else:
                    data, fn, mime = exports.export_wav(sig["samples"], sig["fs"]), f"{tid}.wav", "audio/wav"
                text, report = None, f"{len(sig['samples'])} samples"
            elif kind == "project":
                text = json.dumps(p, sort_keys=True, indent=1)
                data, fn, mime, report = text.encode(), f"{name}.hapkit.json", "application/json", ""
            else:
                raise ApiError(f"unknown export kind {kind!r}")
        except ValueError as e:
            raise ApiError(str(e)) from e
        os.makedirs(OUT, exist_ok=True)
        path = os.path.join(OUT, fn)
        with open(path, "wb") as f:
            f.write(data)
        self.log(f"export {kind} -> out/{fn} ({len(data)} B)")
        return {"filename": fn, "mime": mime, "data_b64": base64.b64encode(data).decode(), "text": text,
                "report": report, "path": os.path.relpath(path, ROOT)}

    # ---- device ----------------------------------------------------------------------------
    @property
    def device(self):
        if self._device is None:
            from .device.aw import DeviceManager
            self._device = DeviceManager(self.log)
        return self._device

    def device_call(self, name: str, req: dict) -> dict:
        dm = self.device
        if name == "status":
            return dm.status()
        if name == "connect":
            return dm.connect()
        if name == "stop":
            return dm.stop()
        if name == "restore":
            return dm.restore()
        with self.lock:
            p = copy.deepcopy(self.project)
        if name == "ram_upload":
            if req.get("source") == "stock":
                return dm.upload_ram(None, "stock")
            blob, rep = exports.export_ram_bin(p)
            return dm.upload_ram(blob, rep["sha256"][:12])
        if name == "play":
            return dm.play(p, req)
        raise ApiError(f"unknown device call {name!r}", 404)

    def log_since(self, since: int) -> dict:
        with self.lock:
            lines = [ln for ln in self.log_lines if ln["seq"] > since]
            return {"lines": lines[-500:], "next": self.log_seq}


ROUTES_GET = {
    "/api/state": lambda app, q, b: app.state(),
    "/api/projects": lambda app, q, b: {"files": app.list_projects()},
    "/api/device": lambda app, q, b: app.device_call("status", {}),
    "/api/device/log": lambda app, q, b: app.log_since(int((q.get("since") or ["0"])[0])),
}
ROUTES_POST = {
    "/api/project/new": lambda app, q, b: (app.set_project(proj.new_project(b.get("template", "empty"), b.get("name"))),
                                           setattr(app, "path", None), app.state())[-1],
    "/api/project/open": lambda app, q, b: app.open(b["path"]),
    "/api/project/save": lambda app, q, b: {"path": os.path.relpath(app.save(b.get("path")), ROOT)},
    "/api/render": lambda app, q, b: app.render(b),
    "/api/import": lambda app, q, b: app.do_import(b),
    "/api/export": lambda app, q, b: app.do_export(b),
    "/api/brake/optimize": lambda app, q, b: app.brake_optimize(b),
    "/api/lengths": lambda app, q, b: app.lengths(b),
    "/api/fit": lambda app, q, b: app.fit(b),
    "/api/reference/to_wave": lambda app, q, b: app.reference_to_wave(b),
    "/api/device/connect": lambda app, q, b: app.device_call("connect", b),
    "/api/device/ram/upload": lambda app, q, b: app.device_call("ram_upload", b),
    "/api/device/play": lambda app, q, b: app.device_call("play", b),
    "/api/device/stop": lambda app, q, b: app.device_call("stop", b),
    "/api/device/restore": lambda app, q, b: app.device_call("restore", b),
}
ROUTES_PUT = {
    "/api/project": lambda app, q, b: {"ok": True, "issues": app.set_project(b.get("project")), "dirty": app.dirty,
                                       "version": app.version},
}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"hapkit/{__version__}"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n))

        def _dispatch(self, routes: dict) -> None:
            url = urlparse(self.path)
            fn = routes.get(url.path)
            if fn is None:
                return self._json(404, {"error": f"no route {url.path}"})
            try:
                body = self._body()
                self._json(200, fn(app, parse_qs(url.query), body))
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                app.log(f"{url.path}: {e!r}", "error")
                self._json(500, {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                return self._dispatch(ROUTES_GET)
            rel = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            if rel.startswith("web/"):
                rel = rel[4:]
            path = os.path.normpath(os.path.join(WEB, rel))
            if not path.startswith(WEB) or not os.path.isfile(path):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith(".js") or path.endswith(".mjs"):
                ctype = "text/javascript"
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)

        def do_POST(self):
            self._dispatch(ROUTES_POST)

        def do_PUT(self):
            self._dispatch(ROUTES_PUT)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, project_path: str | None = None) -> None:
    app = App(project_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    print(f"hapkit {__version__} on http://{host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
