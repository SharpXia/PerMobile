from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .command_builder import CommandBuilder
from .packaging_paths import resolve_cli_launch
from .settings import SettingsService


# ADR-0001：GUI 配置的 trace_processor 经子进程环境变量传给 CLI（CLI 侧发现链
# 第一步消费同名常量 perfeval.collectors.android.perfetto_trace.
# TRACE_PROCESSOR_ENV_VAR；测试层做一致性守护，GUI 服务层不 import collectors）。
TRACE_PROCESSOR_ENV_VAR = "PERFEVAL_TRACE_PROCESSOR"


@dataclass(frozen=True)
class ToolPathResolution:
    adb_program: str | None
    configured_adb: str | None
    adb_source: str | None
    android_studio_program: str | None
    configured_android_studio: str | None
    android_studio_source: str | None
    trace_processor_program: str | None
    configured_trace_processor: str | None
    environment: dict[str, str]
    warnings: tuple[str, ...]


def resolve_tool_paths(
    settings: SettingsService,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> ToolPathResolution:
    """Resolve Android tools without assuming a login shell initialized PATH."""

    environment_values = dict(os.environ if environ is None else environ)
    home_directory = (home or Path.home()).expanduser().resolve(strict=False)
    system = (platform_name or platform.system()).casefold()
    find_program = which or shutil.which
    warnings: list[str] = []
    child_environment: dict[str, str] = {}

    configured_adb = settings.external_tool_path("adb")
    adb_program, adb_source = _resolve_adb(
        configured_adb,
        environ=environment_values,
        home=home_directory,
        system=system,
        which=find_program,
    )
    if configured_adb and adb_source != "configured":
        candidate = Path(configured_adb).expanduser().resolve(strict=False)
        warnings.append(f"配置的 adb 不存在或不是文件：{candidate}；已回退自动发现。")
    if adb_program and adb_source in {"configured", "environment", "default"}:
        inherited_path = environment_values.get("PATH", "")
        adb_parent = str(Path(adb_program).parent)
        child_environment["PATH"] = (
            adb_parent
            if not inherited_path
            else f"{adb_parent}{os.pathsep}{inherited_path}"
        )

    configured_studio = settings.external_tool_path("android_studio")
    studio_program, studio_source = _resolve_android_studio(
        configured_studio,
        environ=environment_values,
        home=home_directory,
        system=system,
        which=find_program,
    )
    if configured_studio and studio_source != "configured":
        candidate = Path(configured_studio).expanduser().resolve(strict=False)
        warnings.append(
            f"配置的 Android Studio 不存在或无法启动：{candidate}；已回退自动发现。"
        )

    # ADR-0001 发现链：GUI 设置是 CLI 子进程看不见的持久化值，经
    # PERFEVAL_TRACE_PROCESSOR 环境变量进入 CLI 的发现链第一步；
    # 未配置或配置无效时留空/告警，由 CLI 继续按 PATH → tools/ 发现。
    configured_trace_processor = settings.external_tool_path("trace_processor")
    trace_processor_program = _resolve_trace_processor(configured_trace_processor)
    if configured_trace_processor and trace_processor_program is None:
        candidate = Path(configured_trace_processor).expanduser().resolve(strict=False)
        warnings.append(
            f"配置的 trace_processor 不存在或不是文件：{candidate}；"
            "已回退 PATH 与 tools/ 自动发现。"
        )
    if trace_processor_program is not None:
        child_environment[TRACE_PROCESSOR_ENV_VAR] = trace_processor_program

    return ToolPathResolution(
        adb_program=adb_program,
        configured_adb=configured_adb,
        adb_source=adb_source,
        android_studio_program=studio_program,
        configured_android_studio=configured_studio,
        android_studio_source=studio_source,
        trace_processor_program=trace_processor_program,
        configured_trace_processor=configured_trace_processor,
        environment=child_environment,
        warnings=tuple(warnings),
    )


def _resolve_trace_processor(configured: str | None) -> str | None:
    if not configured:
        return None
    candidate = Path(configured).expanduser().resolve(strict=False)
    if candidate.is_file():
        return str(candidate)
    return None


def _resolve_adb(
    configured: str | None,
    *,
    environ: Mapping[str, str],
    home: Path,
    system: str,
    which: Callable[[str], str | None],
) -> tuple[str | None, str | None]:
    if configured:
        candidate = Path(configured).expanduser().resolve(strict=False)
        if candidate.is_file():
            return str(candidate), "configured"

    for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = environ.get(variable)
        if not root:
            continue
        candidate = Path(root).expanduser() / "platform-tools" / _adb_filename(system)
        if candidate.is_file():
            return str(candidate.resolve()), "environment"

    path_program = which(_adb_filename(system)) or which("adb")
    if path_program:
        return str(Path(path_program).expanduser().resolve(strict=False)), "path"

    for candidate in _default_adb_candidates(home, system, environ):
        if candidate.is_file():
            return str(candidate.resolve()), "default"
    return None, None


def _resolve_android_studio(
    configured: str | None,
    *,
    environ: Mapping[str, str],
    home: Path,
    system: str,
    which: Callable[[str], str | None],
) -> tuple[str | None, str | None]:
    if configured:
        program = _normalize_android_studio_program(Path(configured))
        if program is not None:
            return str(program), "configured"

    studio_home = environ.get("ANDROID_STUDIO_HOME")
    if studio_home:
        program = _normalize_android_studio_program(Path(studio_home))
        if program is not None:
            return str(program), "environment"

    for name in _studio_path_names(system):
        path_program = which(name)
        if path_program:
            program = _normalize_android_studio_program(Path(path_program))
            if program is not None:
                return str(program), "path"

    for candidate in _default_android_studio_candidates(home, system, environ):
        program = _normalize_android_studio_program(candidate)
        if program is not None:
            return str(program), "default"
    return None, None


def _normalize_android_studio_program(path: Path) -> Path | None:
    candidate = path.expanduser().resolve(strict=False)
    if candidate.is_file():
        return candidate
    for relative in (
        Path("Contents/MacOS/studio"),
        Path("bin/studio64.exe"),
        Path("bin/studio.exe"),
        Path("bin/studio.sh"),
        Path("bin/studio"),
    ):
        program = candidate / relative
        if program.is_file():
            return program.resolve()
    return None


def _adb_filename(system: str) -> str:
    return "adb.exe" if system.startswith("win") else "adb"


def _default_adb_candidates(
    home: Path,
    system: str,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    if system == "darwin":
        return (home / "Library/Android/sdk/platform-tools/adb",)
    if system.startswith("win"):
        local_app_data = Path(environ.get("LOCALAPPDATA") or home / "AppData/Local")
        return (local_app_data / "Android/Sdk/platform-tools/adb.exe",)
    return (
        home / "Android/Sdk/platform-tools/adb",
        home / "Android/sdk/platform-tools/adb",
    )


def _studio_path_names(system: str) -> tuple[str, ...]:
    if system.startswith("win"):
        return ("studio64.exe", "studio.exe")
    return ("studio", "studio.sh")


def _default_android_studio_candidates(
    home: Path,
    system: str,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    if system == "darwin":
        return (
            Path("/Applications/Android Studio.app"),
            home / "Applications/Android Studio.app",
        )
    if system.startswith("win"):
        program_files = Path(environ.get("ProgramFiles") or "C:/Program Files")
        local_app_data = Path(environ.get("LOCALAPPDATA") or home / "AppData/Local")
        return (
            program_files / "Android/Android Studio",
            local_app_data / "Programs/Android Studio",
        )
    return (
        Path("/opt/android-studio"),
        home / "android-studio",
        home / ".local/share/JetBrains/Toolbox/apps/AndroidStudio",
    )


def command_builder_from_settings(
    settings: SettingsService,
    workspace: str | Path,
) -> CommandBuilder:
    resolution = resolve_tool_paths(settings)
    launch = resolve_cli_launch(settings.external_tool_path("cli"))
    return CommandBuilder(
        workspace,
        cli_program=launch.program,
        cli_prefix_arguments=launch.prefix_arguments,
        environment=resolution.environment,
    )
