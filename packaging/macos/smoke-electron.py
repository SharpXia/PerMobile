#!/usr/bin/env python3
"""Verify the desktop app inside the macOS disk image with a system-only PATH."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def verify_bundle(app: Path, notarized: bool) -> Path:
    contents = app / "Contents"
    resources = contents / "Resources"
    with (contents / "Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    executable = contents / "MacOS" / metadata["CFBundleExecutable"]
    backend = resources / "backend" / "perfeval-backend"
    for binary in (executable, backend):
        subprocess.run(["/usr/bin/lipo", "-verify_arch", "arm64", str(binary)], check=True)
    icon_path = resources / metadata["CFBundleIconFile"]
    with icon_path.open("rb") as stream:
        if icon_path.name == "electron.icns" or stream.read(4) != b"icns":
            raise RuntimeError("The desktop app did not pick up the project icon.")
    volume_icon = app.parent / ".VolumeIcon.icns"
    if volume_icon.read_bytes() != icon_path.read_bytes():
        raise RuntimeError("The disk image did not pick up the project icon.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    if notarized:
        subprocess.run(["/usr/bin/xcrun", "stapler", "validate", str(app)], check=True)
    return executable


def reserve_ports() -> tuple[int, int]:
    with socket.socket() as api, socket.socket() as inspector:
        for listener in (api, inspector):
            listener.bind(("127.0.0.1", 0))
        return api.getsockname()[1], inspector.getsockname()[1]


def quit_application(pid: int) -> None:
    script = """function run(args) {
      ObjC.import('AppKit');
      const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(Number(args[0]));
      if (!app.terminate) throw Error('Could not terminate the packaged app.');
    }"""
    subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, str(pid)], check=True, timeout=10)


def verify_runtime(executable: Path, scratch: Path, logfile: Path) -> None:
    port, inspector_port = reserve_ports()
    origin = f"http://127.0.0.1:{port}"
    inspector_url = f"http://127.0.0.1:{inspector_port}/json/list"
    opener = build_opener(ProxyHandler({}))

    def get(url: str) -> bytes:
        with opener.open(url, timeout=2) as response:
            return response.read()

    home = scratch / "home"
    home.mkdir()
    env = {
        "HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PERFEVAL_BACKEND_PORT": str(port), "PERFEVAL_RUNS_DIR": str(scratch / "runs"),
        "PERFEVAL_PROVIDER": "synthetic",
    }
    with logfile.open("wb") as log:
        proc = subprocess.Popen(
            [str(executable), f"--user-data-dir={scratch / 'profile'}", "--disable-gpu",
             "--enable-logging=stderr", f"--remote-debugging-port={inspector_port}",
             "--remote-debugging-address=127.0.0.1"],
            cwd=scratch, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"The packaged app exited during startup ({proc.returncode}).")
                try:
                    health = json.loads(get(origin + "/api/health"))
                    pages = json.loads(get(inspector_url))
                    if health["status"] == "ok" and any(page.get("url") == origin + "/" for page in pages):
                        break
                except (URLError, TimeoutError, ConnectionError):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("The frozen backend and desktop page did not start.")
                time.sleep(0.25)

            if health["providers"]["devices"] != "synthetic":
                raise RuntimeError("The packaged backend used the wrong provider in the smoke test.")
            frontend = get(origin + "/")
            entry = re.search(rb'src="(/assets/[^" ]+\.js)"', frontend)
            if not entry or not get(origin + entry[1].decode("ascii")):
                raise RuntimeError("The frozen backend did not serve the frontend build.")

            quit_application(proc.pid)
            if proc.wait(timeout=15) != 0:
                raise RuntimeError(f"Closing the desktop returned {proc.returncode}.")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
            except ConnectionRefusedError:
                pass
            else:
                raise RuntimeError("Closing the desktop left its backend running.")
        finally:
            # Reclaim only the process group created by this test if graceful shutdown failed.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dmg", type=Path)
    parser.add_argument("--notarized", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("The disk image smoke check must run on macOS.")

    dmg = args.dmg.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="perfeval-smoke-") as temp:
        scratch = Path(temp)
        mount = scratch / "mount"
        mount.mkdir()
        logfile = scratch / "desktop.log"
        subprocess.run(["/usr/bin/hdiutil", "attach", str(dmg), "-nobrowse", "-readonly",
                        "-mountpoint", str(mount)], check=True, timeout=120)
        try:
            executable = verify_bundle(mount / "PerfEval Desktop.app", args.notarized)
            verify_runtime(executable, scratch, logfile)
        except Exception:
            if logfile.is_file():
                print(logfile.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
            raise
        finally:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True, timeout=30)
    print("Verified mounted app, icon, arm64 sidecar, frontend and shutdown.")


if __name__ == "__main__":
    main()
