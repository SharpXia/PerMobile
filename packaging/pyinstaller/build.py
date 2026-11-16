from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from datetime import datetime, timezone
from importlib.metadata import distribution, version
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "packaging" / "pyinstaller" / "perfeval.spec"
ICON_SOURCE = ROOT / "packaging" / "pyinstaller" / "assets" / "perfeval-icon.svg"
ICON_SCRIPT = ROOT / "packaging" / "pyinstaller" / "generate_icons.py"
LOCAL_CODESIGN_IDENTITY = ROOT / ".perfeval" / "codesign-identity"

PINNED_BUILD_DEPENDENCIES = {
    "PyInstaller": "6.22.0",
    "PySide6": "6.11.1",
    "keyring": "25.6.0",
    "pytest": "9.1.1",
    "pytest-qt": "4.5.0",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build PerfEval Lite GUI + CLI sidecar with PyInstaller.")
    parser.add_argument("--clean", action="store_true", help="Clear PyInstaller caches before building.")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build" / "pyinstaller")
    parser.add_argument("--skip-smoke", action="store_true")
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument(
        "--codesign-identity",
        help="macOS codesign identity name or SHA-1 fingerprint; defaults to auto-discovery.",
    )
    signing.add_argument(
        "--adhoc-sign",
        action="store_true",
        help="Force ad-hoc signing even when a stable macOS identity is available.",
    )
    args = parser.parse_args()

    _check_build_dependencies()
    from perfeval import __version__

    codesign_identity = None
    if sys.platform == "darwin" and not args.adhoc_sign:
        codesign_identity = (
            str(args.codesign_identity or os.environ.get("PERFEVAL_CODESIGN_IDENTITY") or "").strip()
            or _discover_codesign_identity()
        )

    dist_dir = args.dist_dir.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    staging = work_dir / "packaging"
    staging.mkdir(parents=True, exist_ok=True)
    source_epoch = _source_date_epoch()
    configured_source_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    metadata_path = _write_version_metadata(
        staging / "version.json",
        version_value=__version__,
        source_epoch=source_epoch,
    )
    icon_suffix = ".icns" if sys.platform == "darwin" else ".ico" if sys.platform.startswith("win") else ".png"
    icon_path = staging / f"perfeval{icon_suffix}"
    subprocess.run(
        [sys.executable, str(ICON_SCRIPT), str(ICON_SOURCE), str(icon_path)],
        cwd=ROOT,
        check=True,
    )
    license_root = _stage_extra_licenses(staging / "extra-licenses")

    environment = os.environ.copy()
    environment.update(
        {
            "PERFEVAL_ICON_PATH": str(icon_path),
            "PERFEVAL_VERSION": __version__,
            "PERFEVAL_BUILD_VERSION": __version__,
            "PERFEVAL_VERSION_METADATA": str(metadata_path),
            "PERFEVAL_EXTRA_LICENSE_DIR": str(license_root),
            "PYINSTALLER_CONFIG_DIR": str(work_dir / "pyinstaller-config"),
        }
    )
    environment.pop("PERFEVAL_CODESIGN_IDENTITY", None)
    if configured_source_epoch:
        environment["SOURCE_DATE_EPOCH"] = configured_source_epoch
    else:
        environment.pop("SOURCE_DATE_EPOCH", None)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir / "work"),
    ]
    if args.clean:
        command.append("--clean")
    command.append(str(SPEC))
    subprocess.run(command, cwd=ROOT, env=environment, check=True)

    artifact = _artifact_path(dist_dir)
    if not artifact.exists():
        raise FileNotFoundError(f"PyInstaller artifact was not created: {artifact}")
    if sys.platform == "darwin" and codesign_identity:
        _sign_macos_bundle(artifact, codesign_identity)
    _normalize_tree_mtime(artifact, source_epoch=source_epoch)
    archive = dist_dir / _archive_name(__version__)
    _write_deterministic_zip(artifact, archive, source_epoch=source_epoch)
    record: dict[str, Any] = {
        "schemaVersion": "perfeval.packaging.build.v1",
        "executedAtUtc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sourceDateEpoch": source_epoch,
        "version": __version__,
        "commit": _git_value("rev-parse", "HEAD"),
        "dirty": bool(_git_value("status", "--porcelain")),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dependencies": {name: version(name) for name in PINNED_BUILD_DEPENDENCIES},
        "command": command,
        "artifact": str(artifact),
        "artifactTreeSha256": _tree_hash(artifact),
        "archive": str(archive),
        "archiveSha256": _sha256(archive),
        "archiveBytes": archive.stat().st_size,
        "signing": (
            "stable_identity"
            if sys.platform == "darwin" and codesign_identity
            else "adhoc"
            if sys.platform == "darwin"
            else "unsigned"
        ),
        "notarization": "not_run",
        "windowsSmoke": "not_run" if not sys.platform.startswith("win") else "pending",
    }
    record_path = staging / "build-record.json"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not args.skip_smoke:
        smoke_path = staging / "smoke-record.json"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "packaging" / "pyinstaller" / "smoke.py"),
                str(artifact),
                "--json-output",
                str(smoke_path),
            ],
            cwd=ROOT,
            check=True,
        )
        record["smokeRecord"] = str(smoke_path)
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def _discover_codesign_identity() -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    preferred = None
    try:
        preferred = LOCAL_CODESIGN_IDENTITY.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    selected = _select_codesign_identity(result.stdout, preferred=preferred)
    if selected and selected != preferred:
        try:
            LOCAL_CODESIGN_IDENTITY.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_CODESIGN_IDENTITY.write_text(selected + "\n", encoding="utf-8")
        except OSError:
            pass
    return selected


def _select_codesign_identity(output: str, *, preferred: str | None = None) -> str | None:
    identities = tuple(
        (fingerprint, name)
        for fingerprint, name in re.findall(
            r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"]+)"',
            output,
            flags=re.MULTILINE,
        )
    )
    normalized_preferred = str(preferred or "").strip().casefold()
    if normalized_preferred:
        cached = next(
            (
                fingerprint
                for fingerprint, _name in identities
                if fingerprint.casefold() == normalized_preferred
            ),
            None,
        )
        if cached:
            return cached
    for prefix in (
        "Developer ID Application:",
        "Apple Development:",
        "3rd Party Mac Developer Application:",
    ):
        match = next(
            (fingerprint for fingerprint, name in identities if name.startswith(prefix)),
            None,
        )
        if match:
            return match
    return identities[0][0] if identities else None


def _sign_macos_bundle(artifact: Path, identity: str) -> None:
    subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--deep",
            "--sign",
            identity,
            "--timestamp=none",
            "--options",
            "runtime",
            "--entitlements",
            str(ROOT / "packaging" / "macos" / "entitlements.plist"),
            str(artifact),
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(artifact)],
        cwd=ROOT,
        check=True,
    )


def _check_build_dependencies() -> None:
    mismatches = []
    for name, expected in PINNED_BUILD_DEPENDENCIES.items():
        actual = version(name)
        if actual != expected:
            mismatches.append(f"{name}=={actual} (expected {expected})")
    if sys.version_info[:2] != (3, 11):
        mismatches.append(f"Python {platform.python_version()} (expected 3.11.x)")
    if mismatches:
        raise RuntimeError("build dependency lock mismatch: " + "; ".join(mismatches))


def _write_version_metadata(path: Path, *, version_value: str, source_epoch: int) -> Path:
    timestamp = datetime.fromtimestamp(source_epoch, timezone.utc).replace(microsecond=0)
    payload = {
        "schemaVersion": "perfeval.version.v1",
        "version": version_value,
        "commit": _git_value("rev-parse", "HEAD"),
        "sourceDateUtc": timestamp.isoformat().replace("+00:00", "Z"),
        "python": platform.python_version(),
        "pyinstaller": version("PyInstaller"),
        "pyside6": version("PySide6"),
        "keyring": version("keyring"),
        "platform": sys.platform,
        "architecture": platform.machine(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _stage_extra_licenses(destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    pyinstaller_files = distribution("PyInstaller").files or ()
    copying = next((file for file in pyinstaller_files if str(file).endswith("licenses/COPYING.txt")), None)
    if copying is not None:
        source = Path(distribution("PyInstaller").locate_file(copying))
        target = destination / "PyInstaller" / "COPYING.txt"
        target.parent.mkdir(parents=True)
        shutil.copy2(source, target)
    python_license = _find_python_license()
    if python_license is not None:
        target = destination / "Python" / "LICENSE"
        target.parent.mkdir(parents=True)
        shutil.copy2(python_license, target)
    return destination


def _find_python_license() -> Path | None:
    prefix = Path(sysconfig.get_config_var("prefix")).resolve()
    candidates = [
        prefix / "LICENSE",
        Path(sys.base_prefix) / "LICENSE",
        *(
            parent / "LICENSE"
            for parent in (prefix, *prefix.parents)
        ),
    ]
    if sys.platform == "darwin":
        candidates.extend(Path("/opt/homebrew/Cellar/python@3.11").glob("*/LICENSE"))
    return next((path for path in candidates if path.is_file()), None)


def _artifact_path(dist_dir: Path) -> Path:
    if sys.platform == "darwin":
        return dist_dir / "PerfEval Lite.app"
    return dist_dir / "PerfEval-Lite"


def _archive_name(version_value: str) -> str:
    system = "macos" if sys.platform == "darwin" else "windows" if sys.platform.startswith("win") else sys.platform
    return f"PerfEval-Lite-{version_value}-{system}-{platform.machine()}.zip"


def _write_deterministic_zip(source: Path, destination: Path, *, source_epoch: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # ZIP has no timezone field. Store local wall-clock time so Finder does not
    # display an eight-hour offset for builds created in Asia/Shanghai.
    timestamp = datetime.fromtimestamp(max(source_epoch, 315532800))
    date_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    root_parent = source.parent
    # Recreate instead of truncating an older archive. On macOS, truncation
    # preserves the previous file creation date shown by Finder.
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted((source, *source.rglob("*")), key=lambda value: value.relative_to(root_parent).as_posix()):
            relative = path.relative_to(root_parent).as_posix()
            stat = path.lstat()
            info = zipfile.ZipInfo(relative + ("/" if path.is_dir() and not path.is_symlink() else ""), date_time)
            info.create_system = 3
            info.external_attr = (stat.st_mode & 0xFFFF) << 16
            if path.is_symlink():
                archive.writestr(info, os.readlink(path).encode("utf-8"))
            elif path.is_dir():
                archive.writestr(info, b"")
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.utime(destination, (source_epoch, source_epoch))


def _normalize_tree_mtime(root: Path, *, source_epoch: int) -> None:
    """Set real filesystem mtimes after PyInstaller signing/copying.

    PyInstaller may translate SOURCE_DATE_EPOCH through UTC wall-clock tuples on
    macOS. Applying the epoch as filesystem time prevents Finder and
    LaunchServices from seeing a stale or timezone-shifted application bundle.
    """

    paths = sorted(
        (root, *root.rglob("*")),
        key=lambda value: len(value.relative_to(root.parent).parts),
        reverse=True,
    )
    for path in paths:
        try:
            os.utime(path, (source_epoch, source_epoch), follow_symlinks=False)
        except (NotImplementedError, OSError):
            if not path.is_symlink():
                raise


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root, *root.rglob("*")), key=lambda value: value.relative_to(root.parent).as_posix()):
        relative = path.relative_to(root.parent).as_posix().encode("utf-8")
        digest.update(relative + b"\0")
        if path.is_symlink():
            digest.update(b"L" + os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"F")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"D")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_date_epoch() -> int:
    configured = os.environ.get("SOURCE_DATE_EPOCH")
    if configured:
        return int(configured)
    return int(datetime.now(timezone.utc).timestamp())


def _git_value(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


if __name__ == "__main__":
    raise SystemExit(main())
