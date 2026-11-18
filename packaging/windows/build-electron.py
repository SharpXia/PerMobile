#!/usr/bin/env python3
"""Build and test the self-contained PerfEval Desktop Windows x64 installer."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FRONTEND = ROOT / "services" / "frontend"
DESKTOP = ROOT / "services" / "desktop"
WORK = ROOT / "build" / "windows-electron"
STAGING = WORK / "staging"
OUTPUT = DESKTOP / "dist" / "windows-x64"
PYTHON_VERSION = (3, 14, 10)
NODE_VERSION = "v24.21.0"


def run(*args: str | Path, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True)


def main() -> None:
    if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"}:
        raise SystemExit("Run the release build on Windows x64 with x64 Python.")
    if sys.version_info[:3] != PYTHON_VERSION or platform.architecture()[0] != "64bit":
        raise SystemExit(f"Install x64 CPython {'.'.join(map(str, PYTHON_VERSION))} for the build.")
    node = shutil.which("node.exe")
    npm = shutil.which("npm.cmd")
    if not node or not npm or subprocess.check_output([node, "--version"], text=True).strip() != NODE_VERSION:
        raise SystemExit(f"Install x64 Node.js {NODE_VERSION[1:]} with npm for the build.")
    if subprocess.check_output([node, "-p", "process.arch"], text=True).strip() != "x64":
        raise SystemExit("Use x64 Node.js for the release build.")

    version = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))["version"]
    expected_tag = os.environ.get("PERFEVAL_DESKTOP_RELEASE_TAG")
    if expected_tag and expected_tag != f"desktop-v{version}":
        raise SystemExit(f"Tag {expected_tag} must match the desktop package version {version}.")

    venv = WORK / "venv"
    run(sys.executable, "-m", "venv", "--clear", venv)
    python = venv / "Scripts" / "python.exe"
    run(python, "-m", "pip", "install", "--only-binary=:all:", "-r", ROOT / "packaging/pyinstaller/electron-requirements.txt")
    run(python, "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", ".")
    run(python, "-m", "pip", "check")

    run(npm, "ci", cwd=FRONTEND)
    frontend_dist = WORK / "frontend"
    run(npm, "run", "build", "--", "--outDir", "../../build/windows-electron/frontend", "--emptyOutDir", cwd=FRONTEND)

    frozen = WORK / "frozen"
    run(
        python, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", frozen, "--workpath", WORK / "pyinstaller",
        ROOT / "packaging" / "pyinstaller" / "perfeval-backend.spec",
        env={**os.environ, "PERFEVAL_FRONTEND_DIST": str(frontend_dist)},
    )
    STAGING.mkdir(parents=True, exist_ok=True)
    for resource in ("backend", "platform-tools"):
        path = STAGING / resource
        if path.exists():
            shutil.rmtree(path)
    shutil.move(frozen / "perfeval-backend", STAGING / "backend")
    run(python, HERE / "stage-adb.py", "--output", STAGING / "platform-tools")

    run(npm, "ci", cwd=DESKTOP)
    run(npm, "run", "dist", "--", "--win", "nsis", "--x64", "--publish", "never",
        "--config.directories.output=dist/windows-x64", cwd=DESKTOP)

    installer = OUTPUT / f"PerfEval-Desktop-{version}-windows-x64-setup.exe"
    run(python, HERE / "smoke-electron.py", OUTPUT / "win-unpacked")
    with installer.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    installer.with_suffix(".exe.sha256").write_text(f"{digest}  {installer.name}\n", encoding="ascii", newline="\n")
    print(installer)


if __name__ == "__main__":
    main()
