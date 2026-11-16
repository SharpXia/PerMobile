from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a frozen PerfEval Lite onedir artifact.")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    artifact = args.artifact.expanduser().resolve()
    gui, cli, resource_roots = _layout(artifact)
    checks: list[dict[str, Any]] = []
    _record(checks, "gui.exists", gui.is_file(), str(gui))
    _record(checks, "sidecar.exists", cli.is_file(), str(cli))
    metadata_path = next(
        (root / "version.json" for root in resource_roots if (root / "version.json").is_file()),
        None,
    )
    _record(checks, "version_metadata.exists", metadata_path is not None, str(metadata_path or ""))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path else {}
    preset_manifest = next(
        (
            candidate
            for root in resource_roots
            for candidate in (
                root / "perfeval" / "gui" / "resources" / "presets" / "manifest.json",
                root / "gui" / "resources" / "presets" / "manifest.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    _record(checks, "presets.manifest", preset_manifest is not None, str(preset_manifest or ""))
    if preset_manifest is not None:
        preset_root = preset_manifest.parent
        payload = json.loads(preset_manifest.read_text(encoding="utf-8"))
        scenario_files = [
            preset_root / str(value.get("scenario"))
            for value in payload.get("presets", [])
            if isinstance(value, dict) and value.get("scenario")
        ]
        _record(
            checks,
            "presets.scenarios",
            bool(scenario_files) and all(path.is_file() for path in scenario_files),
            f"count={len(scenario_files)}",
        )
    if artifact.suffix == ".app":
        info_plist = artifact / "Contents" / "Info.plist"
        plist_payload = plistlib.loads(info_plist.read_bytes()) if info_plist.is_file() else {}
        _record(
            checks,
            "macos.foreground_application",
            plist_payload.get("LSBackgroundOnly") is not True,
            f"LSBackgroundOnly={plist_payload.get('LSBackgroundOnly')!r}",
        )

    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PATH"] = "/usr/bin:/bin" if not platform.system().startswith("Windows") else os.environ.get("SystemRoot", r"C:\Windows") + r"\System32"
    version_result = subprocess.run(
        [str(cli), "--version"],
        cwd=artifact.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    expected_version = str(metadata.get("version") or "")
    _record(
        checks,
        "sidecar.version",
        version_result.returncode == 0 and version_result.stdout.strip() == f"perfeval {expected_version}",
        version_result.stdout.strip() or version_result.stderr.strip(),
    )

    with tempfile.TemporaryDirectory(prefix="PerfEval 冻结 smoke ") as raw_workspace:
        workspace = Path(raw_workspace)
        validate_result = subprocess.run(
            [
                str(cli),
                "validate",
                "synthetic",
                "--out",
                str(workspace / "运行 结果"),
                "--reports",
                str(workspace / "报告 结果"),
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        reports = tuple((workspace / "报告 结果").glob("*/report.html"))
        echarts = tuple((workspace / "报告 结果").rglob("echarts.min.js"))
        _record(checks, "synthetic.exit", validate_result.returncode == 0, validate_result.stderr.strip())
        # CM-001 新增 continuous_stream 后 validate synthetic 共 7 个用例，每例一份
        # report.html；用下限断言防止报告管线退化，同时避免加用例即失败。
        _record(checks, "synthetic.reports", len(reports) >= 7, f"reports={len(reports)}")
        _record(checks, "report.echarts_resource", bool(echarts), f"assets={len(echarts)}")

        gui_environment = environment.copy()
        gui_environment["QT_QPA_PLATFORM"] = "offscreen"
        gui_environment["PERFEVAL_GUI_SMOKE_EXIT_MS"] = "750"
        gui_result = subprocess.run(
            [str(gui)],
            cwd=workspace,
            env=gui_environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        _record(
            checks,
            "gui.startup",
            gui_result.returncode == 0,
            gui_result.stderr.strip() or gui_result.stdout.strip(),
        )
        if artifact.suffix == ".app":
            finder_home = workspace / "Finder Home"
            finder_home.mkdir()
            finder_environment = gui_environment.copy()
            finder_environment["HOME"] = str(finder_home)
            finder_result = subprocess.run(
                [str(gui)],
                cwd=Path("/"),
                env=finder_environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            _record(
                checks,
                "gui.finder_cwd_startup",
                finder_result.returncode == 0,
                finder_result.stderr.strip() or finder_result.stdout.strip(),
            )

    license_files = tuple(artifact.rglob("THIRD_PARTY_NOTICES.md"))
    qt_plugins = tuple(
        path for path in artifact.rglob("*")
        if path.is_file() and ("qcocoa" in path.name.casefold() or "qwindows" in path.name.casefold())
    )
    _record(checks, "licenses.present", bool(license_files), f"count={len(license_files)}")
    _record(checks, "qt.platform_plugin", bool(qt_plugins), f"count={len(qt_plugins)}")

    passed = all(check["passed"] for check in checks)
    record = {
        "schemaVersion": "perfeval.packaging.smoke.v1",
        "executedAtUtc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "artifact": str(artifact),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "passed": passed,
        "checks": checks,
        "cleanMachine": "not_run; PATH/PYTHONHOME/PYTHONPATH isolation only",
        "signing": _macos_signing_status(artifact) if platform.system() == "Darwin" else "not_run",
        "notarization": "not_run",
        "artifactTreeSha256": _tree_hash(artifact),
    }
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if passed else 1


def _layout(artifact: Path) -> tuple[Path, Path, tuple[Path, ...]]:
    if artifact.suffix == ".app":
        contents = artifact / "Contents"
        return (
            contents / "MacOS" / "PerfEval-Lite",
            contents / "MacOS" / "perfeval-cli",
            (contents / "Resources" / "resources", contents / "Resources"),
        )
    suffix = ".exe" if platform.system().startswith("Windows") else ""
    return (
        artifact / f"PerfEval-Lite{suffix}",
        artifact / f"perfeval-cli{suffix}",
        (artifact / "resources",),
    )


def _record(checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def _macos_signing_status(artifact: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "--verbose=4", str(artifact)],
        capture_output=True,
        text=True,
        check=False,
    )
    details = result.stderr
    if "Authority=Developer ID Application:" in details:
        return "developer_id"
    if "Authority=Apple Development:" in details:
        return "apple_development"
    if "Authority=3rd Party Mac Developer Application:" in details:
        return "mac_app_store"
    if "Signature=adhoc" in details:
        return "adhoc"
    return "unknown"


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root, *root.rglob("*")), key=lambda value: value.relative_to(root.parent).as_posix()):
        digest.update(path.relative_to(root.parent).as_posix().encode("utf-8") + b"\0")
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


if __name__ == "__main__":
    raise SystemExit(main())
