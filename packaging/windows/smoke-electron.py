#!/usr/bin/env python3
"""Exercise an unpacked Windows app, or install and exercise an NSIS build on a clean machine."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def free_ports() -> tuple[int, int]:
    with socket.socket() as backend, socket.socket() as adb:
        for listener in (backend, adb):
            listener.bind(("127.0.0.1", 0))
        return backend.getsockname()[1], adb.getsockname()[1]


def find_window(pid: int) -> int | None:
    user32 = ctypes.WinDLL("user32")
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    found = None

    @callback_type
    def inspect(hwnd, _):
        nonlocal found
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found = hwnd
            return False
        return True

    user32.EnumWindows(inspect, 0)
    return found


def close_window(hwnd: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    if not user32.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
        raise ctypes.WinError(ctypes.get_last_error())


def ports_closed(*ports: int) -> bool:
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return False
        except OSError:
            pass
    return True


def verify_app(app_dir: Path, scratch: Path, log: Path) -> None:
    exe = app_dir / "PerfEval Desktop.exe"
    adb = app_dir / "resources" / "platform-tools" / "adb.exe"
    backend = app_dir / "resources" / "backend" / "perfeval-backend.exe"
    for binary in (exe, adb, backend):
        if not binary.is_file():
            raise FileNotFoundError(binary)

    port, adb_port = free_ports()
    origin = f"http://127.0.0.1:{port}"
    system_root = os.environ["SystemRoot"]
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("PERFEVAL_", "PD_", "ANDROID_", "ADB_", "PYTHON", "NODE_", "ELECTRON_"))
    }
    env.update({
        "PATH": os.pathsep.join((str(Path(system_root) / "System32"), system_root)),
        "APPDATA": str(scratch / "Roaming"),
        "LOCALAPPDATA": str(scratch / "Local"),
        "PERFEVAL_BACKEND_PORT": str(port),
        "PERFEVAL_ADB_SERVER_PORT": str(adb_port),
        "PERFEVAL_RUNS_DIR": str(scratch / "runs"),
        "ANDROID_USER_HOME": str(scratch / "android"),
    })
    for directory in ("android", "Roaming", "Local"):
        (scratch / directory).mkdir()
    opener = build_opener(ProxyHandler({}))

    def get(path: str, timeout: int = 3) -> bytes:
        with opener.open(origin + path, timeout=timeout) as response:
            return response.read()

    with log.open("wb") as output:
        proc = subprocess.Popen(
            [str(exe), f"--user-data-dir={scratch / 'profile'}", "--disable-gpu", "--enable-logging=stderr"],
            cwd=app_dir, env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 55
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"PerfEval Desktop exited before becoming ready ({proc.returncode})")
                try:
                    health = json.loads(get("/api/health"))
                except (URLError, TimeoutError):
                    health = {}
                hwnd = find_window(proc.pid)
                if hwnd and health.get("status") == "ok":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Desktop window or backend did not start (health: {health})")
                time.sleep(0.25)

            if health["providers"]["devices"] != "real":
                raise RuntimeError(f"Expected packaged Android provider, got {health}")
            frontend = get("/")
            entry = re.search(rb'src="(/assets/[^" ]+\.js)"', frontend)
            if not entry or not get(entry[1].decode("ascii")):
                raise RuntimeError("The frozen backend did not serve the frontend build.")
            if json.loads(get("/api/devices", timeout=15))["mode"] != "real":
                raise RuntimeError("The packaged backend did not serve the Android device endpoint.")
            # Only the packaged sidecar starts ADB. These clients cannot start a detached daemon.
            with socket.create_connection(("127.0.0.1", adb_port), timeout=4):
                pass
            adb_env = {**env, "ADB_SERVER_SOCKET": f"tcp:127.0.0.1:{adb_port}"}
            for command, expected in (("version", "Android Debug Bridge"), ("devices", "List of devices attached")):
                response = subprocess.check_output([str(adb), command], env=adb_env, text=True, timeout=15)
                if expected not in response:
                    raise RuntimeError(f"Bundled ADB {command} returned unexpected output: {response}")

            close_window(hwnd)
            if proc.wait(timeout=25) != 0:
                raise RuntimeError(f"Closing the desktop returned {proc.returncode}.")
            if not ports_closed(port, adb_port):
                raise RuntimeError("Closing the desktop left the backend or bundled ADB running.")
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not ports_closed(port, adb_port):
                    time.sleep(0.25)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="win-unpacked directory, or installer .exe on a clean Windows runner")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("The installer smoke check must run on Windows.")
    source = args.path.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="perfeval-smoke-") as temp:
        scratch = Path(temp)
        app_dir = source
        if source.is_file():
            app_dir = scratch / "PerfEval 安装 测试"
            # NSIS treats everything after /D= as a directory. /D= is last and unquoted.
            command = f'"{source}" /S /D={app_dir}'
            subprocess.run(command, executable=str(source), timeout=120, check=True)

        log = scratch / "desktop.log"
        try:
            verify_app(app_dir, scratch, log)
        except Exception:
            if log.is_file():
                print(log.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
            raise
    print("Verified desktop startup, frozen frontend/backend, bundled ADB, and shutdown.")


if __name__ == "__main__":
    main()
