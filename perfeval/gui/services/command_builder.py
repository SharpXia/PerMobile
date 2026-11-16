from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from perfeval.core.config import sanitize_slug
from perfeval.core.memory_profile import MEMORY_PROFILES
from perfeval.gui.models.task import ManualCapability, TaskSpec, TaskType


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str


class CommandValidationError(ValueError):
    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.field}: {issue.message}" for issue in self.issues))

    @classmethod
    def from_message(cls, field: str, message: str) -> CommandValidationError:
        return cls((ValidationIssue(field, message),))


@dataclass(frozen=True)
class CandidateInput:
    candidate_id: str
    apk: Path

    @property
    def output_slug(self) -> str:
        return sanitize_slug(self.candidate_id)


class CommandBuilder:
    """Build immutable ``TaskSpec`` values without invoking a shell."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        cli_program: str | Path | None = None,
        cli_prefix_arguments: Sequence[str] = (),
        authorized_output_roots: Sequence[str | Path] = (),
        environment: Mapping[str, str] | None = None,
        display_platform: str | None = None,
        enable_gui_events: bool = True,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise CommandValidationError(
                [ValidationIssue("workspace", f"目录不存在或不可访问: {self.workspace}")]
            )
        self.program, default_prefix = self._resolve_cli(cli_program)
        self.prefix_arguments = tuple(default_prefix) + tuple(str(value) for value in cli_prefix_arguments)
        self.authorized_output_roots = tuple(
            Path(value).expanduser().resolve(strict=False) for value in authorized_output_roots
        )
        self.environment = dict(environment or {})
        self.display_platform = display_platform or sys.platform
        self.enable_gui_events = enable_gui_events

    def build_run_device(
        self,
        *,
        scenario: str | Path,
        output: str | Path = "runs",
        repeat: int | None = None,
        package_name: str | None = None,
        serial: str | None = None,
        apk: str | Path | None = None,
        activity: str | None = None,
        clear_data: bool | None = None,
        reinstall: bool | None = None,
        perfetto: bool = False,
        post_launch_wait: float | None = None,
        interactive: bool = False,
        sampling_off: bool = False,
        sampling_interval_ms: int | None = None,
        screen_record: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskSpec:
        scenario_path = self._input_file("scenario", scenario)
        output_root = self._output_path("output", output)
        apk_path = self._input_file("apk", apk) if apk is not None else None
        self._positive_int("repeat", repeat, maximum=100)
        self._non_negative_number("post_launch_wait", post_launch_wait, maximum=3600)
        self._positive_int("sampling_interval_ms", sampling_interval_ms, maximum=3_600_000)

        arguments = ["run", str(scenario_path), "--out", str(output_root), "--android-device-run"]
        self._add_value(arguments, "--repeat", repeat)
        self._add_text(arguments, "--package-name", package_name)
        self._add_text(arguments, "--serial", serial)
        self._add_value(arguments, "--apk", apk_path)
        self._add_text(arguments, "--activity", activity)
        if clear_data is False:
            arguments.append("--no-clear-data")
        if reinstall is False:
            arguments.append("--no-reinstall")
        if perfetto:
            arguments.append("--perfetto")
        self._add_value(arguments, "--post-launch-wait", post_launch_wait)
        if sampling_off:
            arguments.append("--sampling-off")
        self._add_value(arguments, "--sampling-interval-ms", sampling_interval_ms)
        if screen_record:
            arguments.append("--screen-record")
        if interactive:
            arguments.append("--interactive")
        self._add_gui_events(arguments)
        return self.build(
            task_type=TaskType.RUN,
            arguments=arguments,
            expected_output_root=output_root,
            manual_capability=(
                ManualCapability.EVENT_AND_TEXT if interactive else ManualCapability.NONE
            ),
            metadata={"mode": "android_device", **dict(metadata or {})},
        )

    def build_run_offline(
        self,
        *,
        scenario: str | Path,
        android_artifacts: str | Path,
        output: str | Path = "runs",
        repeat: int | None = None,
        package_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskSpec:
        scenario_path = self._input_file("scenario", scenario)
        artifact_path = self._input_directory("android_artifacts", android_artifacts)
        output_root = self._output_path("output", output)
        self._positive_int("repeat", repeat, maximum=100)
        arguments = [
            "run",
            str(scenario_path),
            "--out",
            str(output_root),
            "--android-artifacts",
            str(artifact_path),
        ]
        self._add_value(arguments, "--repeat", repeat)
        self._add_text(arguments, "--package-name", package_name)
        self._add_gui_events(arguments)
        return self.build(
            task_type=TaskType.OFFLINE,
            arguments=arguments,
            expected_output_root=output_root,
            metadata={"mode": "android_artifacts", **dict(metadata or {})},
        )

    def build_ab(
        self,
        *,
        scenario: str | Path,
        baseline_apk: str | Path,
        candidates: Sequence[CandidateInput],
        output: str | Path = "runs/ab",
        baseline_id: str = "baseline",
        repeat: int | None = None,
        serial: str | None = None,
        package_name: str | None = None,
        activity: str | None = None,
        clear_data: bool | None = None,
        allow_uninstall: bool = False,
        perfetto: bool = False,
        post_launch_wait: float | None = None,
        interactive: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskSpec:
        scenario_path = self._input_file("scenario", scenario)
        baseline_path = self._input_file("baseline_apk", baseline_apk)
        output_root = self._output_path("output", output)
        self._positive_int("repeat", repeat, maximum=100)
        self._non_negative_number("post_launch_wait", post_launch_wait, maximum=3600)
        if not candidates:
            raise CommandValidationError(
                [ValidationIssue("candidates", "至少需要一个 candidate")]
            )
        if len(candidates) > 4:
            raise CommandValidationError(
                [ValidationIssue("candidates", "MVP 最多支持四个 candidate")]
            )

        baseline_slug = self._validate_slug("baseline_id", baseline_id)
        candidate_values: list[tuple[str, str, Path]] = []
        seen_ids = {unicodedata.normalize("NFKC", baseline_id).casefold()}
        seen_slugs = {baseline_slug.casefold()}
        issues: list[ValidationIssue] = []
        for index, candidate in enumerate(candidates):
            field = f"candidates[{index}]"
            normalized_id = unicodedata.normalize("NFKC", candidate.candidate_id).casefold()
            try:
                slug = self._validate_slug(f"{field}.id", candidate.candidate_id)
                apk_path = self._input_file(f"{field}.apk", candidate.apk)
            except CommandValidationError as exc:
                issues.extend(exc.issues)
                continue
            if normalized_id in seen_ids:
                issues.append(ValidationIssue(f"{field}.id", "ID 与 baseline/candidate 重复"))
            if slug.casefold() in seen_slugs:
                issues.append(ValidationIssue(f"{field}.id", f"清洗后的输出名冲突: {slug}"))
            seen_ids.add(normalized_id)
            seen_slugs.add(slug.casefold())
            candidate_values.append((candidate.candidate_id, slug, apk_path))
        if issues:
            raise CommandValidationError(issues)

        arguments = [
            "ab",
            str(scenario_path),
            "--out",
            str(output_root),
            "--android-device-run",
            "--baseline-apk",
            str(baseline_path),
            "--baseline-id",
            baseline_id,
        ]
        for candidate_id, _slug, apk_path in candidate_values:
            arguments.extend(["--candidate-apk", f"{candidate_id}={apk_path}"])
        self._add_value(arguments, "--repeat", repeat)
        self._add_text(arguments, "--serial", serial)
        self._add_text(arguments, "--package-name", package_name)
        self._add_text(arguments, "--activity", activity)
        if clear_data is not None:
            arguments.append("--clear-data" if clear_data else "--no-clear-data")
        if allow_uninstall:
            arguments.append("--allow-uninstall")
        if perfetto:
            arguments.append("--perfetto")
        self._add_value(arguments, "--post-launch-wait", post_launch_wait)
        if interactive:
            arguments.append("--interactive")
        self._add_gui_events(arguments)
        return self.build(
            task_type=TaskType.AB,
            arguments=arguments,
            expected_output_root=output_root,
            manual_capability=(
                ManualCapability.EVENT_AND_TEXT if interactive else ManualCapability.NONE
            ),
            metadata={
                "baseline_id": baseline_id,
                "candidate_ids": tuple(value[0] for value in candidate_values),
                "candidate_output_slugs": tuple(value[1] for value in candidate_values),
                **dict(metadata or {}),
            },
        )

    def build_diagnose(
        self,
        *,
        package_name: str,
        output: str | Path = "runs/android-diagnose",
        case_id: str = "android-diagnose",
        mode: str = "frame",
        duration: int = 30,
        serial: str | None = None,
        interactive: bool = False,
        memory_profile: str = "mainstream",
        first_party_regex: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskSpec:
        if not package_name.strip():
            raise CommandValidationError([ValidationIssue("package_name", "不能为空")])
        if mode not in {"frame", "cpu", "trace", "full"}:
            raise CommandValidationError(
                [ValidationIssue("mode", "必须是 frame、cpu、trace 或 full")]
            )
        if memory_profile not in MEMORY_PROFILES:
            raise CommandValidationError(
                [ValidationIssue("memory_profile", f"必须是 {', '.join(MEMORY_PROFILES)} 之一")]
            )
        self._positive_int("duration", duration, maximum=3600)
        output_root = self._output_path("output", output)
        output_slug = self._validate_slug("case_id", case_id)
        arguments = [
            "diagnose",
            "android",
            "--package-name",
            package_name.strip(),
            "--out",
            str(output_root),
            "--case",
            case_id,
            "--mode",
            mode,
            "--duration",
            str(duration),
            "--memory-profile",
            memory_profile,
        ]
        self._add_text(arguments, "--serial", serial)
        if interactive:
            arguments.append("--interactive")
        self._add_text(arguments, "--first-party-regex", first_party_regex)
        self._add_gui_events(arguments)
        return self.build(
            task_type=TaskType.DIAGNOSE,
            arguments=arguments,
            expected_output_root=output_root / output_slug,
            manual_capability=(
                ManualCapability.EVENT_AND_TEXT if interactive else ManualCapability.NONE
            ),
            metadata={"mode": mode, "case_id": case_id, "case_output_slug": output_slug, **dict(metadata or {})},
        )

    def build_validate(
        self,
        *,
        suite: str = "synthetic",
        case: str | None = None,
        output: str | Path = "runs/synthetic-validation",
        reports: str | Path = "reports/synthetic-validation",
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskSpec:
        if suite not in {"synthetic", "android-parsers"}:
            raise CommandValidationError(
                [ValidationIssue("suite", "必须是 synthetic 或 android-parsers")]
            )
        if suite == "android-parsers" and case is not None:
            raise CommandValidationError(
                [ValidationIssue("case", "android-parsers 不支持单 case")]
            )
        output_root = self._output_path("output", output)
        reports_root = self._output_path("reports", reports)
        arguments = [
            "validate",
            suite,
            "--out",
            str(output_root),
            "--reports",
            str(reports_root),
        ]
        self._add_text(arguments, "--case", case)
        return self.build(
            task_type=TaskType.VALIDATE,
            arguments=arguments,
            expected_output_root=reports_root if suite == "synthetic" else output_root,
            metadata={"suite": suite, "output_roots": (output_root, reports_root), **dict(metadata or {})},
        )

    def build(
        self,
        *,
        task_type: TaskType,
        arguments: Sequence[str],
        expected_output_root: str | Path,
        manual_capability: ManualCapability = ManualCapability.NONE,
        metadata: Mapping[str, Any] | None = None,
        redacted_metadata: Mapping[str, Any] | None = None,
        task_id: str | None = None,
    ) -> TaskSpec:
        actual_arguments = (*self.prefix_arguments, *(str(value) for value in arguments))
        display_command = format_display_command(
            self.program,
            actual_arguments,
            platform=self.display_platform,
        )
        return TaskSpec.create(
            task_type=task_type,
            program=self.program,
            arguments=actual_arguments,
            working_directory=self.workspace,
            display_command=display_command,
            expected_output_root=Path(expected_output_root),
            manual_capability=manual_capability,
            metadata=metadata,
            redacted_metadata=redacted_metadata or metadata,
            environment=self.environment,
            task_id=task_id,
        )

    def _resolve_cli(self, cli_program: str | Path | None) -> tuple[str, tuple[str, ...]]:
        if cli_program is not None:
            program = str(cli_program)
            if not program.strip():
                raise CommandValidationError([ValidationIssue("cli_program", "不能为空")])
            return program, ()
        installed = shutil.which("perfeval")
        if installed:
            return installed, ()
        return sys.executable, ("-m", "perfeval.cli.main")

    def _workspace_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve(strict=False)

    def _input_file(self, field: str, value: str | Path | None) -> Path:
        if value is None or not str(value).strip():
            raise CommandValidationError([ValidationIssue(field, "不能为空")])
        path = self._workspace_path(value)
        if not path.is_file():
            raise CommandValidationError([ValidationIssue(field, f"文件不存在: {path}")])
        return path

    def _input_directory(self, field: str, value: str | Path) -> Path:
        path = self._workspace_path(value)
        if not path.is_dir():
            raise CommandValidationError([ValidationIssue(field, f"目录不存在: {path}")])
        return path

    def _output_path(self, field: str, value: str | Path) -> Path:
        if not str(value).strip():
            raise CommandValidationError([ValidationIssue(field, "不能为空")])
        path = self._workspace_path(value)
        allowed_roots = (self.workspace, *self.authorized_output_roots)
        if not any(_is_relative_to(path, root) for root in allowed_roots):
            raise CommandValidationError(
                [ValidationIssue(field, "输出目录必须位于 workspace 或已授权的外部目录")]
            )
        if path.exists() and not path.is_dir():
            raise CommandValidationError([ValidationIssue(field, f"输出路径不是目录: {path}")])
        parent = next((candidate for candidate in (path, *path.parents) if candidate.exists()), None)
        if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK):
            raise CommandValidationError([ValidationIssue(field, f"输出目录不可创建: {path}")])
        return path

    @staticmethod
    def _positive_int(field: str, value: int | None, *, maximum: int) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise CommandValidationError(
                [ValidationIssue(field, f"必须是 1 到 {maximum} 的整数")]
            )

    @staticmethod
    def _non_negative_number(field: str, value: float | None, *, maximum: float) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= maximum:
            raise CommandValidationError(
                [ValidationIssue(field, f"必须是 0 到 {maximum:g} 的数字")]
            )

    @staticmethod
    def _validate_slug(field: str, value: str) -> str:
        if not value or not value.strip():
            raise CommandValidationError([ValidationIssue(field, "不能为空")])
        if value != value.rstrip(" ."):
            raise CommandValidationError([ValidationIssue(field, "不能以空格或点结尾")])
        normalized = unicodedata.normalize("NFKC", value.strip())
        basename = normalized.split(".", 1)[0].upper()
        slug = sanitize_slug(normalized)
        slug_basename = slug.split(".", 1)[0].upper()
        if basename in _WINDOWS_RESERVED_NAMES or slug_basename in _WINDOWS_RESERVED_NAMES:
            raise CommandValidationError([ValidationIssue(field, "不能使用 Windows 保留名称")])
        if any(ord(char) < 32 for char in value) or any(char in '<>:"/\\|?*' for char in value):
            raise CommandValidationError([ValidationIssue(field, "包含跨平台非法字符")])
        return slug

    @staticmethod
    def _add_value(arguments: list[str], flag: str, value: object | None) -> None:
        if value is not None:
            arguments.extend([flag, str(value)])

    @staticmethod
    def _add_text(arguments: list[str], flag: str, value: str | None) -> None:
        if value is not None and value.strip():
            arguments.extend([flag, value.strip()])

    def _add_gui_events(self, arguments: list[str]) -> None:
        if self.enable_gui_events:
            arguments.append("--gui-events")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def format_display_command(program: str, arguments: Sequence[str], *, platform: str | None = None) -> str:
    """Format a copy-only command without changing the execution values."""

    values = [program, *(str(value) for value in arguments)]
    target = platform or sys.platform
    if target.startswith("win"):
        return subprocess.list2cmdline(values)
    return shlex.join(values)
