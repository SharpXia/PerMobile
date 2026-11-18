#!/usr/bin/env python3
"""Build and test the self-contained PerfEval Desktop macOS arm64 disk image."""

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
PYINSTALLER = ROOT / "packaging" / "pyinstaller"
FRONTEND = ROOT / "services" / "frontend"
DESKTOP = ROOT / "services" / "desktop"
WORK = ROOT / "build" / "macos-electron"
OUTPUT = DESKTOP / "dist" / "mac-release"
PYTHON_VERSION = (3, 14, 10)
NODE_VERSION = "v24.21.0"


def run(*args: str | Path, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True)


def main() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Run the release build with native Apple Silicon Python on macOS.")
    if sys.version_info[:3] != PYTHON_VERSION:
        raise SystemExit(f"Install arm64 CPython {'.'.join(map(str, PYTHON_VERSION))} for the build.")
    node = shutil.which("node")
    npm = shutil.which("npm")
    required_node = f"{NODE_VERSION} arm64"
    if not node or not npm or subprocess.check_output(
        [node, "-p", "process.version + ' ' + process.arch"], text=True
    ).strip() != required_node:
        raise SystemExit(f"Install arm64 Node.js {NODE_VERSION[1:]} with npm for the build.")

    version = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))["version"]
    tag = os.environ.get("PERFEVAL_DESKTOP_RELEASE_TAG")
    if tag and tag != f"desktop-v{version}":
        raise SystemExit(f"Tag {tag} must match the desktop package version {version}.")

    signed = bool(os.environ.get("CSC_LINK"))
    apple_credentials = ("APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID")
    if signed and any(not os.environ.get(name) for name in apple_credentials):
        raise SystemExit("Developer ID builds require APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD and APPLE_TEAM_ID.")

    venv = WORK / "venv"
    run(sys.executable, "-m", "venv", "--clear", venv)
    python = venv / "bin" / "python"
    run(python, "-m", "pip", "install", "--only-binary=:all:", "-r", PYINSTALLER / "electron-requirements.txt")
    run(python, "-m", "pip", "check")

    run(npm, "ci", cwd=FRONTEND)
    frontend_dist = WORK / "frontend"
    run(npm, "run", "build", "--", "--outDir", frontend_dist, "--emptyOutDir", cwd=FRONTEND)
    frozen = WORK / "frozen"
    run(
        python, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", frozen, "--workpath", WORK / "pyinstaller", PYINSTALLER / "perfeval-backend.spec",
        env={**os.environ, "PERFEVAL_FRONTEND_DIST": str(frontend_dist)},
    )

    # The object override replaces the normal local staging array in electron-builder.yml.
    backend = frozen / "perfeval-backend"
    identity = ("--config.mac.identity=", "--config.forceCodeSigning=true") if signed else (
        "--config.mac.identity=-", "--config.mac.hardenedRuntime=false", "--config.mac.notarize=false",
    )
    run(npm, "ci", cwd=DESKTOP)
    run(
        npm, "run", "dist", "--", "--mac", "dmg", "--arm64", "--publish", "never",
        "--config.directories.output=dist/mac-release",
        "--config.dmg.artifactName=PerfEval-Desktop-${version}-macos-${arch}.${ext}",
        f"--config.mac.extraResources.from={backend}", "--config.mac.extraResources.to=backend",
        *identity, cwd=DESKTOP,
        env={**os.environ, "CSC_IDENTITY_AUTO_DISCOVERY": "true" if signed else "false"},
    )
    dmg = OUTPUT / f"PerfEval-Desktop-{version}-macos-arm64.dmg"
    run(python, HERE / "smoke-electron.py", dmg, *(["--notarized"] if signed else []))
    with dmg.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    dmg.with_suffix(".dmg.sha256").write_text(f"{digest}  {dmg.name}\n", encoding="ascii")
    print(dmg)


if __name__ == "__main__":
    main()
