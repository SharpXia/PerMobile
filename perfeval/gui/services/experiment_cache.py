from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, Mapping

from perfeval.core.config import sanitize_slug
from perfeval.gui.models.preset import TaskLaunchConfig
from perfeval.gui.models.result import ResultDescriptor, ResultType, TaskResult

from .command_builder import format_display_command


EXPERIMENT_SCHEMA_VERSION = "perfeval.gui.experiment.v1"


@dataclass(frozen=True)
class ExperimentReservation:
    experiment_id: str
    root: Path
    data_directory: Path


class ExperimentCacheService:
    """Persist quick-test inputs, outputs, logs and report references per experiment."""

    @classmethod
    def reserve(
        cls,
        workspace: str | Path,
        title: str,
        *,
        now: datetime | None = None,
        suffix: str | None = None,
    ) -> ExperimentReservation:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        title_slug = sanitize_slug(title).strip("-_") or "experiment"
        experiment_id = (
            f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-"
            f"{title_slug}-{suffix or token_hex(3)}"
        )
        root = (
            Path(workspace).expanduser().resolve(strict=False)
            / ".perfeval"
            / "experiments"
            / timestamp.strftime("%Y")
            / timestamp.strftime("%m")
            / experiment_id
        )
        return ExperimentReservation(experiment_id, root, root / "data")

    def begin(self, launch: TaskLaunchConfig) -> None:
        reservation = self._reservation_from_launch(launch)
        if reservation is None:
            return
        reservation.root.mkdir(parents=True, exist_ok=True)
        payload = self._base_manifest(launch, reservation)
        payload.update(
            {
                "status": "running",
                "startedAtUtc": _utc_now(),
                "dataDirectory": "data",
                "results": [],
            }
        )
        self._write_manifest(reservation.root, payload)

    def finish(self, launch: TaskLaunchConfig, result: TaskResult) -> None:
        reservation = self._reservation_from_launch(launch)
        if reservation is None:
            return
        reservation.root.mkdir(parents=True, exist_ok=True)
        payload = self._read_manifest(reservation.root) or self._base_manifest(
            launch, reservation
        )
        result_values = self._result_values(reservation.root, result.results)
        report_path = self._preferred_path(
            reservation.root,
            result.results,
            ResultType.REPORT,
            fallback_name="report.html",
        )
        summary_path = self._preferred_path(
            reservation.root,
            result.results,
            ResultType.SUMMARY,
            fallback_name="summary.json",
        )
        compare_path = self._preferred_path(
            reservation.root,
            result.results,
            ResultType.COMPARE,
            fallback_name="compare.json",
        )
        payload.update(
            {
                "status": result.outcome.value,
                "endedAtUtc": result.ended_at.astimezone(timezone.utc).isoformat(),
                "durationSeconds": result.duration_seconds,
                "exitCode": result.exit_code,
                "structuredStatus": result.structured_status,
                "userCancelled": result.user_cancelled,
                "reportPath": report_path,
                "summaryPath": summary_path,
                "comparePath": compare_path,
                "results": result_values,
                "logs": {
                    "stdout": self._relative_file(reservation.root, result.stdout_log_path),
                    "stderr": self._relative_file(reservation.root, result.stderr_log_path),
                },
            }
        )
        self._write_manifest(reservation.root, payload)

    def fresh_launch(self, launch: TaskLaunchConfig) -> TaskLaunchConfig:
        """Give a repeated quick task a new cache root instead of overwriting history."""

        if not launch.quick_mode or "experiment_root" not in launch.task.metadata:
            return launch
        reservation = self.reserve(launch.task.working_directory, launch.title)
        arguments = list(launch.task.arguments)
        try:
            output_index = arguments.index("--out") + 1
        except ValueError:
            return launch
        if output_index >= len(arguments):
            return launch
        arguments[output_index] = str(reservation.data_directory)
        metadata = {
            **dict(launch.task.metadata),
            "experiment_id": reservation.experiment_id,
            "experiment_root": str(reservation.root),
        }
        expected_output = reservation.data_directory
        case_slug = metadata.get("case_output_slug")
        if launch.task.task_type.value == "diagnose" and isinstance(case_slug, str):
            expected_output /= case_slug
        task = replace(
            launch.task,
            arguments=tuple(arguments),
            expected_output_root=expected_output,
            display_command=format_display_command(
                launch.task.program,
                arguments,
            ),
            metadata=metadata,
            redacted_metadata={**dict(launch.task.redacted_metadata), **metadata},
        )
        return replace(launch, task=task)

    @staticmethod
    def log_directory(task) -> Path | None:
        raw_root = task.metadata.get("experiment_root")
        if not isinstance(raw_root, str) or not raw_root.strip():
            return None
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            root = task.working_directory / root
        root = root.resolve(strict=False)
        allowed_root = (
            task.working_directory / ".perfeval" / "experiments"
        ).resolve(strict=False)
        if not _is_relative_to(root, allowed_root):
            return None
        return root / "logs"

    def _reservation_from_launch(
        self, launch: TaskLaunchConfig
    ) -> ExperimentReservation | None:
        experiment_id = launch.task.metadata.get("experiment_id")
        raw_root = launch.task.metadata.get("experiment_root")
        if not isinstance(experiment_id, str) or not isinstance(raw_root, str):
            return None
        root = Path(raw_root).expanduser().resolve(strict=False)
        allowed_root = (
            launch.task.working_directory / ".perfeval" / "experiments"
        ).resolve(strict=False)
        if not _is_relative_to(root, allowed_root):
            return None
        return ExperimentReservation(experiment_id, root, root / "data")

    @staticmethod
    def _base_manifest(
        launch: TaskLaunchConfig,
        reservation: ExperimentReservation,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": EXPERIMENT_SCHEMA_VERSION,
            "experimentId": reservation.experiment_id,
            "createdAtUtc": _utc_now(),
            "title": launch.title,
            "presetId": launch.preset_id,
            "taskKind": launch.task_kind.value,
            "quickMode": launch.quick_mode,
            "packageName": launch.package_name,
            "deviceSerial": launch.device_serial,
            "deviceLabel": launch.device_label,
            "repeatCount": launch.repeat_count,
            "taskId": launch.task.task_id,
        }

    @staticmethod
    def _result_values(
        root: Path,
        descriptors: tuple[ResultDescriptor, ...],
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for descriptor in descriptors:
            path = descriptor.path
            relative = ExperimentCacheService._relative_file(root, path)
            if relative is None:
                continue
            values.append(
                {
                    "type": descriptor.type.value,
                    "label": descriptor.label,
                    "path": relative,
                    "viewer": descriptor.viewer.value,
                    "exists": descriptor.exists,
                }
            )
        return values

    @classmethod
    def _preferred_path(
        cls,
        root: Path,
        descriptors: tuple[ResultDescriptor, ...],
        result_type: ResultType,
        *,
        fallback_name: str,
    ) -> str | None:
        for descriptor in descriptors:
            if descriptor.type is result_type and descriptor.exists:
                relative = cls._relative_file(root, descriptor.path)
                if relative:
                    return relative
        for path in _bounded_files(root, fallback_name):
            relative = cls._relative_file(root, path)
            if relative:
                return relative
        return None

    @staticmethod
    def _relative_file(root: Path, path: Path | None) -> str | None:
        if path is None:
            return None
        resolved = path.expanduser().resolve(strict=False)
        if not _is_relative_to(resolved, root) or resolved.is_symlink():
            return None
        return resolved.relative_to(root).as_posix()

    @staticmethod
    def _read_manifest(root: Path) -> dict[str, Any] | None:
        try:
            value = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return dict(value) if isinstance(value, Mapping) else None

    @staticmethod
    def _write_manifest(root: Path, payload: Mapping[str, Any]) -> None:
        target = root / "experiment.json"
        temporary = root / ".experiment.json.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(target)


def _bounded_files(root: Path, filename: str, *, limit: int = 4096) -> tuple[Path, ...]:
    matches: list[Path] = []
    count = 0
    stack = [root]
    while stack and count < limit:
        directory = stack.pop()
        try:
            children = tuple(directory.iterdir())
        except OSError:
            continue
        for child in children:
            count += 1
            if count > limit or child.is_symlink():
                break
            if child.is_dir():
                stack.append(child)
            elif child.name == filename:
                matches.append(child)
    return tuple(sorted(matches))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
