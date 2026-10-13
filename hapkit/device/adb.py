"""adb access to the dev device from WSL (Windows adb.exe), pinned to one serial."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess

DEFAULT_ADB = "/mnt/c/Users/matt/adb-fastboot/platform-tools/adb.exe"
DEV_SERIAL = "214d028e"          # development device; nothing else is ever driven
FORBIDDEN = {"3c439602"}         # daily driver


class AdbError(RuntimeError):
    pass


def adb_path() -> str:
    p = os.environ.get("HAPKIT_ADB") or DEFAULT_ADB
    if os.path.exists(p):
        return p
    found = shutil.which("adb")
    if not found:
        raise AdbError(f"adb not found ({p})")
    return found


def host_path(path: str, adb: str) -> str:
    if adb.endswith(".exe"):
        return subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, check=True).stdout.strip()
    return path


class Adb:
    def __init__(self, serial: str | None = None):
        self.serial = serial or os.environ.get("HAPKIT_SERIAL") or DEV_SERIAL
        if self.serial in FORBIDDEN:
            raise AdbError(f"refusing serial {self.serial}")
        self.adb = adb_path()
        self.verified = False

    def run(self, *args: str, timeout: float = 30.0, check: bool = True) -> str:
        cmd = [self.adb, "-s", self.serial, *args]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"timeout: adb {' '.join(args)[:80]}") from e
        out = r.stdout.decode(errors="replace").replace("\r", "")
        err = r.stderr.decode(errors="replace").replace("\r", "").strip()
        if check and r.returncode != 0:
            raise AdbError(err or out.strip() or f"adb exit {r.returncode}")
        return out

    def devices(self) -> dict[str, str]:
        r = subprocess.run([self.adb, "devices"], capture_output=True, timeout=15)
        out = {}
        for line in r.stdout.decode(errors="replace").replace("\r", "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
        return out

    def shell(self, script: str, timeout: float = 30.0, check: bool = True) -> str:
        return self.run("shell", script, timeout=timeout, check=check)

    def verify(self) -> str:
        sn = self.shell("getprop ro.serialno").strip()
        if sn in FORBIDDEN or sn != DEV_SERIAL:
            self.verified = False
            raise AdbError(f"device reports ro.serialno {sn!r}; only {DEV_SERIAL} is allowed")
        self.verified = True
        return sn

    def root(self) -> bool:
        if self.shell("id -u").strip() == "0":
            return True
        self.run("root", timeout=20, check=False)
        self.run("wait-for-device", timeout=30, check=False)
        return self.shell("id -u").strip() == "0"

    def push(self, local: str, remote: str) -> None:
        self.run("push", host_path(local, self.adb), remote, timeout=60)

    def pull(self, remote: str, local: str) -> None:
        self.run("pull", remote, host_path(os.path.abspath(local), self.adb), timeout=60)


def q(s: str) -> str:
    return shlex.quote(s)
