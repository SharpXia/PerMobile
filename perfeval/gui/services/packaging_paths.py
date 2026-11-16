from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from perfeval import __version__


@dataclass(frozen=True)
class RuntimePaths:
    frozen: bool
    platform: str
    executable: Path
    application_root: Path
    resource_root: Path
    sidecar: Path | None
    version_metadata: Path | None


@dataclass(frozen=True)
class CliLaunch:
    program: str
    prefix_arguments: tuple[str, ...] = ()
    source: str = "development"


@dataclass(frozen=True)
class PackagedRuntimeCheck:
    ok: bool
    message: str
    gui_version: str
    cli_version: str | None = None
    metadata: Mapping[str, Any] | None = None


class PackagedRuntimeError(RuntimeError):
    pass


def is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_paths(
    *,
    executable: str | Path | None = None,
    platform: str | None = None,
    frozen: bool | None = None,
    meipass: str | Path | None = None,
) -> RuntimePaths:
    target_platform = platform or sys.platform
    target_frozen = is_frozen_runtime() if frozen is None else frozen
    executable_path = Path(executable or sys.executable).expanduser().resolve(strict=False)

    if not target_frozen:
        repository_root = Path(__file__).resolve().parents[3]
        return RuntimePaths(
            frozen=False,
            platform=target_platform,
            executable=executable_path,
            application_root=repository_root,
            resource_root=repository_root / "perfeval",
            sidecar=None,
            version_metadata=None,
        )

    if target_platform == "darwin" and executable_path.parent.name == "MacOS":
        contents = executable_path.parent.parent
        resource_candidates = (
            contents / "Resources" / "resources",
            contents / "Resources",
        )
        sidecar = executable_path.parent / "perfeval-cli"
        application_root = contents
    else:
        application_root = executable_path.parent
        resource_candidates = (application_root / "resources",)
        suffix = ".exe" if target_platform.startswith("win") else ""
        sidecar = application_root / f"perfeval-cli{suffix}"

    if meipass is None:
        raw_meipass = getattr(sys, "_MEIPASS", None)
        meipass_path = Path(raw_meipass).resolve(strict=False) if raw_meipass else None
    else:
        meipass_path = Path(meipass).expanduser().resolve(strict=False)
    if meipass_path is not None:
        resource_candidates = (
            *resource_candidates,
            meipass_path / "resources",
            meipass_path,
        )
    resource_root = next(
        (candidate for candidate in resource_candidates if candidate.exists()),
        resource_candidates[0],
    )
    metadata_candidates = tuple(candidate / "version.json" for candidate in resource_candidates)
    version_metadata = next(
        (candidate for candidate in metadata_candidates if candidate.is_file()),
        metadata_candidates[0],
    )
    return RuntimePaths(
        frozen=True,
        platform=target_platform,
        executable=executable_path,
        application_root=application_root,
        resource_root=resource_root,
        sidecar=sidecar,
        version_metadata=version_metadata,
    )


def resolve_cli_launch(
    configured_cli: str | Path | None = None,
    *,
    paths: RuntimePaths | None = None,
) -> CliLaunch:
    current = paths or runtime_paths()
    if current.frozen:
        sidecar = current.sidecar
        if sidecar is None or not sidecar.is_file():
            raise PackagedRuntimeError(
                "PerfEval CLI sidecar 缺失；请重新安装完整的 PerfEval Lite 应用包。"
            )
        return CliLaunch(str(sidecar), (), "packaged-sidecar")

    if configured_cli:
        candidate = Path(configured_cli).expanduser().resolve(strict=False)
        if candidate.is_file():
            return CliLaunch(str(candidate), (), "configured")
    installed = shutil.which("perfeval")
    if installed:
        return CliLaunch(installed, (), "path")
    return CliLaunch(sys.executable, ("-m", "perfeval.cli.main"), "python-module")


def parse_cli_version(output: str) -> str | None:
    text = output.strip()
    if not text:
        return None
    parts = text.split()
    if len(parts) == 2 and parts[0].casefold() == "perfeval":
        return parts[1]
    return None


def cli_version_matches(output: str, *, expected: str = __version__) -> bool:
    return parse_cli_version(output) == expected


def load_version_metadata(paths: RuntimePaths | None = None) -> Mapping[str, Any] | None:
    current = paths or runtime_paths()
    metadata_path = current.version_metadata
    if metadata_path is None or not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def check_packaged_runtime(
    *,
    paths: RuntimePaths | None = None,
    expected_version: str = __version__,
    timeout_seconds: float = 5.0,
) -> PackagedRuntimeCheck:
    current = paths or runtime_paths()
    if not current.frozen:
        return PackagedRuntimeCheck(True, "development runtime", expected_version)
    try:
        launch = resolve_cli_launch(paths=current)
    except PackagedRuntimeError as exc:
        return PackagedRuntimeCheck(False, str(exc), expected_version)
    metadata = load_version_metadata(current)
    if metadata is None:
        return PackagedRuntimeCheck(
            False,
            "应用版本元数据缺失或损坏；请重新安装完整应用包。",
            expected_version,
        )
    metadata_version = str(metadata.get("version") or "")
    if metadata_version != expected_version:
        return PackagedRuntimeCheck(
            False,
            f"GUI 元数据版本 {metadata_version or 'unknown'} 与代码版本 {expected_version} 不一致。",
            expected_version,
            metadata=metadata,
        )
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    try:
        completed = subprocess.run(
            [launch.program, *launch.prefix_arguments, "--version"],
            cwd=str(current.application_root),
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PackagedRuntimeCheck(
            False,
            f"CLI sidecar 版本检查失败：{exc}",
            expected_version,
            metadata=metadata,
        )
    cli_version = parse_cli_version(completed.stdout)
    if completed.returncode != 0 or cli_version != expected_version:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"退出码 {completed.returncode}"
        return PackagedRuntimeCheck(
            False,
            f"GUI/CLI 版本不匹配或 sidecar 无法运行：{detail}",
            expected_version,
            cli_version=cli_version,
            metadata=metadata,
        )
    return PackagedRuntimeCheck(
        True,
        f"GUI/CLI 版本一致：{expected_version}",
        expected_version,
        cli_version=cli_version,
        metadata=metadata,
    )

