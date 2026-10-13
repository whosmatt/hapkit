"""Live tests on the dev device through the haptic_hv sysfs interface (no kernel changes).

RAM: the image is pushed to FW_DIR, firmware_class.path points there (SELinux permissive while a session is
active: the kernel reads it from /data), ram_update reloads it. Playback programs WAVCFG1-13 and starts RAM mode
through the LED brightness node (the 'activate' node always uses the DT mode, RAM-loop). RTP: the PCM is
installed as RTP slot 4 (haptic_rtp_auto_sin.bin, unused by the framework) and played via 'rtp'.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time

from ..core import exports, project as proj
from ..core.sequence import effect_sequence, render_effect, render_sequence, seq_registers
from ..core.render import render_wave
from .adb import Adb, AdbError, DEV_SERIAL, q

SYS = "/sys/class/leds/aw_vibrator"
FWPATH = "/sys/module/firmware_class/parameters/path"
WORK = "/data/local/tmp/hapkit"
FW_DIR = f"{WORK}/fw"
RTP_SLOT, RTP_NAME = 4, "haptic_rtp_auto_sin.bin"
REG_WAVCFG1, REG_WAVCFG9, REG_WAVCFG13, REG_GLBRD5 = 0x0A, 0x12, 0x16, 0x3F


class DeviceError(RuntimeError):
    pass


class DeviceManager:
    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.adb: Adb | None = None
        self.st: dict = {"connected": False, "serial": DEV_SERIAL}
        self.ram_tag: str | None = None
        self.fw_session = False
        self.fw_orig = ""
        self.se_orig = ""

    # ---- helpers ----------------------------------------------------------------------------
    def _sh(self, script: str, timeout: float = 30.0) -> str:
        assert self.adb is not None
        return self.adb.shell(script, timeout=timeout)

    def _ensure(self) -> None:
        if not self.adb or not self.st.get("connected"):
            self._connect()
        self.adb.verify()

    def _error(self, e: Exception) -> dict:
        self.st["error"] = str(e)
        self.log(f"device: {e}", "error")
        return {"ok": False, "report": str(e), "status": self.status()}

    def status(self) -> dict:
        return {**self.st, "ram_tag": self.ram_tag, "fw_session": self.fw_session}

    # ---- connect ----------------------------------------------------------------------------
    def connect(self) -> dict:
        with self.lock:
            try:
                self._connect()
                return self.status()
            except (AdbError, DeviceError) as e:
                self.st = {"connected": False, "serial": DEV_SERIAL, "error": str(e)}
                self.log(f"device: {e}", "error")
                return self.status()

    def _connect(self) -> None:
        adb = Adb()
        devs = adb.devices()
        if devs.get(adb.serial) != "device":
            raise DeviceError(f"{adb.serial} not attached (adb devices: {devs or 'none'})")
        adb.verify()
        if not adb.root():
            raise DeviceError("adb root failed (enable rooted debugging)")
        self.adb = adb
        info = self._sh(
            "printf 'sn=%s\\n' \"$(getprop ro.serialno)\"; printf 'lin=%s\\n' \"$(getprop ro.lineage.version)\"; "
            "printf 'kern=%s\\n' \"$(uname -r)\"; printf 'se=%s\\n' \"$(getenforce)\"; "
            f"printf 'fwp=%s\\n' \"$(cat {FWPATH})\"; printf 'ram=%s\\n' \"$(cat {SYS}/ram_num 2>&1)\"; "
            "printf 'brk=%s\\n' \"$(cat /sys/module/haptic/parameters/rtp_brake 2>/dev/null)\"; "
            "printf 'hal=%s\\n' \"$(pidof vendor.qti.hardware.vibrator.service)\"; "
            f"ls {SYS}/brightness {SYS}/reg {SYS}/awrw {SYS}/rtp >/dev/null 2>&1 && echo sys=ok || echo sys=missing")
        kv = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
        if kv.get("sys") != "ok":
            raise DeviceError(f"{SYS} nodes missing")
        fwp = kv.get("fwp", "")
        self.fw_session = fwp.startswith(FW_DIR)
        if self.fw_session:
            self.fw_orig = fwp[len(FW_DIR):].lstrip(",")
            self.se_orig = self.st.get("se_orig") or "Enforcing"
        else:
            self.fw_orig, self.se_orig = fwp, kv.get("se", "")
        self.st = {"connected": True, "serial": kv.get("sn"), "root": True, "selinux": kv.get("se"),
                   "lineage": kv.get("lin"), "kernel": kv.get("kern"), "fw_path": fwp, "se_orig": self.se_orig,
                   "ram_num": kv.get("ram", "").replace("ram_num = ", ""),
                   "rtp_brake": kv.get("brk") or None, "hal_pid": kv.get("hal") or None}
        self.log(f"device {self.st['serial']} {self.st['lineage']} {self.st['kernel']} selinux={self.st['selinux']} "
                 f"ram_num={self.st['ram_num']} rtp_brake={self.st['rtp_brake']} fw_path={fwp!r}")

    def _fw_session(self) -> None:
        if not self.fw_session:
            path = FW_DIR + ("," + self.fw_orig if self.fw_orig else "")
            self._sh(f"mkdir -p {FW_DIR} && chmod 755 {WORK} {FW_DIR} && setenforce 0 && printf '%s' {q(path)} > {FWPATH}")
            self.fw_session = True
            self.st["selinux"] = "Permissive"
            self.log(f"firmware path -> {path!r}, SELinux permissive (restore reverts)")

    def _push_bytes(self, data: bytes, remote: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            self.adb.push(tmp, remote + ".tmp")
            self._sh(f"mv {q(remote + '.tmp')} {q(remote)} && chmod 644 {q(remote)}")
        finally:
            os.unlink(tmp)

    def _wait_kmsg(self, marker: str, done: tuple[str, ...], timeout: float = 8.0) -> str:
        t0 = time.time()
        out = ""
        while time.time() - t0 < timeout:
            out = self._sh(f"dmesg | sed -n '/{marker}/,$p' | grep -E 'haptic|{marker}'")
            if any(d in out for d in done):
                return out
            time.sleep(0.15)
        raise DeviceError(f"timeout waiting for driver ({marker})")

    # ---- RAM --------------------------------------------------------------------------------
    def upload_ram(self, blob: bytes | None, tag: str) -> dict:
        with self.lock:
            try:
                self._ensure()
                marker = f"hapkit-ram-{int(time.time() * 1000)}"
                if blob is None:
                    self._sh(f"rm -f {FW_DIR}/haptic_ram.bin; echo {marker} > /dev/kmsg; echo 1 > {SYS}/ram_update")
                else:
                    self._fw_session()
                    self._push_bytes(blob, f"{FW_DIR}/haptic_ram.bin")
                    self._sh(f"echo {marker} > /dev/kmsg; echo 1 > {SYS}/ram_update")
                out = self._wait_kmsg(marker, ("update complete", "check sum err", "failed to read", "update failed"))
                if "update complete" not in out:
                    raise DeviceError("RAM load failed: " + " | ".join(ln.split("]", 1)[-1].strip() for ln in out.splitlines()[-4:]))
                time.sleep(0.05)
                out = self._sh(f"dmesg | sed -n '/{marker}/,$p' | grep -E 'ram_num|wave [0-9]+:|check sum'")
                waves = re.findall(r"wave (\d+): (0x[0-9a-f]+)-(0x[0-9a-f]+), (\d+) samples", out)
                ram_num = re.findall(r"ram_num = (\d+)", out)
                self.ram_tag = hashlib.sha256(blob).hexdigest()[:12] if blob is not None else self._stock_tag()
                rep = f"RAM {self.ram_tag}: ram_num {ram_num[-1] if ram_num else '?'}, " + \
                      ", ".join(f"w{i} {n}" for i, _, _, n in waves)
                self.st["ram_num"] = ram_num[-1] if ram_num else self.st.get("ram_num")
                if blob is not None:
                    rep += "; " + self._verify_ram(blob)
                self.log(rep)
                return {"ok": True, "report": rep, "status": self.status()}
            except (AdbError, DeviceError) as e:
                return self._error(e)

    def _verify_ram(self, blob: bytes) -> str:
        """Read the SRAM image back (ram_update show dumps it to the kernel log) and compare."""
        marker = f"hapkit-dump-{int(time.time() * 1000)}"
        self._sh(f"echo {marker} > /dev/kmsg; cat {SYS}/ram_update >/dev/null")
        out = self._sh(f"dmesg | sed -n '/{marker}/,$p' | grep ram_update_show")
        got = bytes(int(h, 16) for h in re.findall(r"0x([0-9a-f]{2}),", out))
        want = blob[4:]
        if got == want:
            return f"read-back {len(got)} B identical"
        n = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
        raise DeviceError(f"RAM read-back differs at offset {n} ({len(got)} B read, {len(want)} B expected)")

    # ---- play -------------------------------------------------------------------------------
    def _render_target(self, project: dict, target: dict, route: str) -> list[int]:
        t, iid = target.get("type"), target.get("id")
        coll = {"wave": "waves", "sequence": "sequences", "effect": "effects"}.get(t)
        if not coll:
            raise DeviceError(f"cannot play a {t}")
        item = next((x for x in project.get(coll, []) if x["id"] == iid), None)
        if item is None:
            raise DeviceError(f"no {t} {iid!r}")
        if t == "wave":
            return render_wave(item, project["device"])["samples"]
        if t == "sequence":
            return render_sequence(item, project)["samples"]
        return render_effect(item, project, "stream" if route == "rtp" else "ram")["samples"]

    def _registers(self, project: dict, target: dict) -> dict:
        t, iid = target.get("type"), target.get("id")
        if t == "wave":
            seq = {"slots": [{"kind": "wave", "wave": iid, "loops": 0}], "mainloop": 0}
        elif t == "sequence":
            seq = next((s for s in project["sequences"] if s["id"] == iid), None)
        elif t == "effect":
            e = next((x for x in project["effects"] if x["id"] == iid), None)
            seq = effect_sequence(e, project) if e else None
        else:
            seq = None
        if seq is None:
            raise DeviceError(f"{t} {iid!r} has no RAM sequence")
        return seq_registers(seq, project)

    def play(self, project: dict, req: dict) -> dict:
        route = req.get("route", "ram")
        target = req.get("target") or {}
        gain = max(0, min(255, int(req.get("gain", 128))))
        with self.lock:
            try:
                self._ensure()
                if route == "ram":
                    return self._play_ram(project, target, gain, bool(req.get("force")))
                if route == "rtp":
                    return self._play_rtp(project, target, gain)
                if route == "hal":
                    return self._play_hal(project, target)
                raise DeviceError(f"unknown route {route!r}")
            except (AdbError, DeviceError, ValueError) as e:
                return self._error(e)

    @staticmethod
    def _stock_tag() -> str:
        try:
            with open(proj.STOCK_BIN, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:12]
        except OSError:
            return "stock"

    def ram_play_script(self, project: dict, target: dict, gain: int, force: bool = False) -> tuple[str, str]:
        """Shell script that programs the sequencer for target and starts RAM playback, plus a report line."""
        blob, _ = exports.export_ram_bin(project)
        want = hashlib.sha256(blob).hexdigest()[:12]
        if self.ram_tag != want and not force:
            raise DeviceError(f"device RAM is {self.ram_tag or 'unknown'}, project RAM is {want}: upload RAM first")
        regs = self._registers(project, target)
        wav = " ".join(f"0x{v:02x}" for v in regs["wavcfg"])
        lp = regs["loops"]
        loops = " ".join(f"0x{(lp[2 * i] << 4) | lp[2 * i + 1]:02x}" for i in range(4))
        script = (f"echo {gain} > {SYS}/gain; "
                  f"echo '0x00 0x08 0x{REG_WAVCFG1:02x} {wav}' > {SYS}/awrw; "
                  f"echo '0x00 0x04 0x{REG_WAVCFG9:02x} {loops}' > {SYS}/awrw; "
                  f"echo '1 1 {REG_WAVCFG13:x}' > {SYS}/awrw; v=$(cat {SYS}/awrw); "
                  f"echo {REG_WAVCFG13:x} $(printf %x $(( (v & 0xf0) | {regs['mainloop']} ))) > {SYS}/reg; "
                  f"echo 1 > {SYS}/brightness")
        rep = f"ram {target.get('type')} {target.get('id')}: WAVCFG {wav} loops {loops} main {regs['mainloop']} gain {gain}"
        return script, rep

    def _play_ram(self, project: dict, target: dict, gain: int, force: bool) -> dict:
        script, rep = self.ram_play_script(project, target, gain, force)
        self._sh(script)
        self.log(rep)
        return {"ok": True, "report": rep}

    def _play_rtp(self, project: dict, target: dict, gain: int) -> dict:
        samples = self._render_target(project, target, "rtp")
        if not samples:
            raise DeviceError("empty signal")
        self._fw_session()
        self._push_bytes(exports.export_raw(samples), f"{FW_DIR}/{RTP_NAME}")
        self._sh(f"echo {gain} > {SYS}/gain; echo {RTP_SLOT} > {SYS}/rtp")
        rep = f"rtp {target.get('type')} {target.get('id')}: {len(samples)} samples ({len(samples) / 24.0:.1f} ms) gain {gain}"
        self.log(rep)
        return {"ok": True, "report": rep}

    def _play_hal(self, project: dict, target: dict) -> dict:
        if target.get("type") != "effect":
            raise DeviceError("hal route plays effects only")
        e = next((x for x in project["effects"] if x["id"] == target.get("id")), None)
        if e is None:
            raise DeviceError("no such effect")
        if e["kind"] == "effect":
            cmd = f"cmd vibrator_manager synced -f prebaked {e['aidl']}"
        else:
            cmd = f"cmd vibrator_manager synced -f primitives {e['aidl']}"
        out = self._sh(cmd + " 2>&1").strip()
        rep = f"hal {e['kind']} {e['name']}: {cmd}" + (f" -> {out}" if out else "")
        self.log(rep)
        return {"ok": True, "report": rep}

    def stop(self) -> dict:
        with self.lock:
            try:
                self._ensure()
                self._sh(f"echo 0 > {SYS}/brightness; echo 0 > {SYS}/rtp")
                return {"ok": True, "report": "stopped"}
            except (AdbError, DeviceError) as e:
                return self._error(e)

    def restore(self) -> dict:
        with self.lock:
            try:
                self._ensure()
                marker = f"hapkit-restore-{int(time.time() * 1000)}"
                fw = f"printf '%s' {q(self.fw_orig)}" if self.fw_orig else "printf '\\0'"
                # MAINLOOP and slot loops persist in the chip and the kernel's effect path does not reset them
                self._sh(f"echo 0 > {SYS}/brightness; {fw} > {FWPATH}; rm -rf {FW_DIR}; "
                         f"echo '0x00 0x04 0x{REG_WAVCFG9:02x} 0x00 0x00 0x00 0x00' > {SYS}/awrw; "
                         f"echo '1 1 {REG_WAVCFG13:x}' > {SYS}/awrw; v=$(cat {SYS}/awrw); "
                         f"echo {REG_WAVCFG13:x} $(printf %x $(( v & 0xf0 ))) > {SYS}/reg; "
                         f"echo {marker} > /dev/kmsg; echo 1 > {SYS}/ram_update")
                self._wait_kmsg(marker, ("update complete", "check sum err", "failed to read"))
                if self.se_orig == "Enforcing":
                    self._sh("setenforce 1")
                self.fw_session, self.ram_tag = False, self._stock_tag()
                self.st["selinux"] = self._sh("getenforce").strip()
                self.st["fw_path"] = self._sh(f"cat {FWPATH}")
                self.log(f"restored: firmware path {self.st['fw_path']!r}, stock RAM reloaded, SELinux {self.st['selinux']}")
                return {"ok": True, "report": "restored", "status": self.status()}
            except (AdbError, DeviceError) as e:
                return self._error(e)
