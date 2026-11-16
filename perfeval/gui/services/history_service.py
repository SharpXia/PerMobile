from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from perfeval.gui.models.history import (
    HistoryEntry,
    HistoryHealth,
    HistoryScanResult,
    HistorySource,
    HistoryTaskKind,
)
from perfeval.gui.services.llm_artifacts import read_snapshot


_SKIP_DIRECTORIES = frozenset({"artifacts", "report", "compare", "repeats", ".perfeval"})
_KNOWN_STATUSES = frozenset(
    {
        "completed",
        "pass",
        "warn",
        "fail",
        "unknown",
        "invalid",
        "partial",
        "tool_error",
        "launch_error",
        "unexpected_error",
        "cancelled",
    }
)


@dataclass(frozen=True)
class HistoryDeleteResult:
    deleted: tuple[Path, ...]
    errors: tuple[str, ...]


class HistoryService:
    """Bounded history discovery limited to a single workspace."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        max_nodes: int = 2000,
        max_entries: int = 500,
        max_depth: int = 6,
        max_json_bytes: int = 1_000_000,
    ) -> None:
        if max_nodes < 1 or max_entries < 1 or max_depth < 1 or max_json_bytes < 1:
            raise ValueError("history scan limits must be positive")
        self.max_nodes = max_nodes
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.max_json_bytes = max_json_bytes
        self.set_workspace(workspace)

    @property
    def workspace(self) -> Path:
        return self._workspace

    def set_workspace(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser().resolve(strict=False)

    def delete_entries(self, entries: Sequence[HistoryEntry]) -> HistoryDeleteResult:
        targets: list[Path] = []
        errors: list[str] = []
        for entry in entries:
            if entry.status == "running":
                errors.append(f"{entry.name}：任务仍在运行")
                continue
            target, error = self._validated_delete_target(entry)
            if error is not None:
                errors.append(f"{entry.name}：{error}")
                continue
            if target is not None:
                targets.append(target)

        top_level_targets: list[Path] = []
        for target in sorted(set(targets), key=lambda value: len(value.parts)):
            if any(_is_relative_to(target, parent) for parent in top_level_targets):
                continue
            top_level_targets.append(target)

        deleted: list[Path] = []
        for target in top_level_targets:
            try:
                shutil.rmtree(target)
            except OSError as exc:
                errors.append(f"{target.name}：删除失败：{exc}")
            else:
                deleted.append(target)
        return HistoryDeleteResult(tuple(deleted), tuple(errors))

    def scan(self) -> HistoryScanResult:
        entries: list[HistoryEntry] = []
        warnings: list[str] = []
        scanned_nodes = 0
        truncated = False
        claimed_reports: set[Path] = set()

        history_roots = (
            ("experiments", self._workspace / ".perfeval" / "experiments"),
            ("runs", self._workspace / "runs"),
            ("reports", self._workspace / "reports"),
        )
        for root_name, root in history_roots:
            if root.is_symlink():
                warnings.append(f"已跳过符号链接历史根目录：{root}")
                continue
            if not root.is_dir():
                continue
            stack: list[tuple[Path, int]] = [(root, 0)]
            while stack:
                directory, depth = stack.pop()
                try:
                    iterator = os.scandir(directory)
                except OSError as exc:
                    warnings.append(f"无法读取历史目录 {directory}：{exc}")
                    continue
                with iterator:
                    for child in iterator:
                        scanned_nodes += 1
                        if scanned_nodes > self.max_nodes:
                            truncated = True
                            break
                        try:
                            if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        child_path = Path(child.path)
                        if not _is_relative_to(child_path.resolve(strict=False), self._workspace):
                            warnings.append(f"已跳过 workspace 外部目录：{child_path}")
                            continue
                        entry = self._build_entry(child_path, root_name, claimed_reports)
                        if entry is not None:
                            entries.append(entry)
                            if entry.report_path is not None:
                                claimed_reports.add(entry.report_path.resolve(strict=False))
                            if len(entries) >= self.max_entries:
                                truncated = True
                                break
                        if (
                            depth < self.max_depth
                            and child.name.casefold() not in _SKIP_DIRECTORIES
                            and not (root_name == "experiments" and entry is not None)
                        ):
                            stack.append((child_path, depth + 1))
                    if truncated:
                        break
            if truncated:
                break

        entries.sort(key=lambda entry: entry.updated_at, reverse=True)
        if truncated:
            warnings.append("历史扫描达到安全上限，仅展示本次有界扫描发现的结果。")
        return HistoryScanResult(
            entries=tuple(entries),
            scanned_nodes=min(scanned_nodes, self.max_nodes),
            truncated=truncated,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _build_entry(
        self,
        directory: Path,
        source_root: str,
        claimed_reports: set[Path],
    ) -> HistoryEntry | None:
        if source_root == "experiments":
            return self._build_experiment_entry(directory)
        summary_path = directory / "summary.json"
        manifest_path = directory / "manifest.json"
        compare_path = directory / "compare.json"
        if not self._is_safe_file(compare_path):
            compare_path = directory / "compare" / "compare.json"
        report_path = directory / "report.html"
        if not self._is_safe_file(report_path):
            report_path = directory / "report" / "report.html"

        known_paths = (summary_path, manifest_path, compare_path, report_path)
        if not any(self._is_safe_file(path) for path in known_paths):
            return None
        report_exists = self._is_safe_file(report_path)
        resolved_report = report_path.resolve(strict=False) if report_exists else None
        if resolved_report is not None and resolved_report in claimed_reports:
            return None

        summary, summary_error = self._read_json(summary_path)
        manifest, manifest_error = self._read_json(manifest_path)
        compare, compare_error = self._read_json(compare_path)
        errors = tuple(
            message
            for message in (summary_error, manifest_error, compare_error)
            if message is not None
        )
        payload = summary or compare or manifest or {}
        status = _history_status(summary, compare, manifest)
        health = HistoryHealth.COMPLETE
        if errors or not report_exists:
            health = HistoryHealth.PARTIAL
        elif not payload:
            health = HistoryHealth.UNKNOWN

        name = _history_name(directory, summary, compare, manifest)
        task_kind = _history_task_kind(
            source_root=source_root,
            summary_path=summary_path,
            manifest_path=manifest_path,
            compare_path=compare_path,
            is_safe_file=self._is_safe_file,
        )
        platform = _history_platform(summary, manifest)
        scenario_id = _history_scenario_id(summary, compare, manifest)
        package_name = _history_package_name(summary, manifest)
        safe_paths = tuple(path for path in known_paths if self._is_safe_file(path))
        interpretation = read_snapshot(directory)
        interpretation_path = (
            interpretation.html_path
            if interpretation.html_path is not None and self._is_safe_file(interpretation.html_path)
            else None
        )
        updated_at = datetime.fromtimestamp(_latest_mtime(safe_paths, directory), tz=timezone.utc)
        return HistoryEntry(
            entry_id=str(directory.resolve(strict=False)),
            name=name,
            task_kind=task_kind,
            status=status,
            health=health,
            updated_at=updated_at,
            directory=directory.resolve(strict=False),
            report_path=resolved_report,
            summary_path=(
                summary_path.resolve(strict=False)
                if self._is_safe_file(summary_path)
                else compare_path.resolve(strict=False)
                if self._is_safe_file(compare_path)
                else None
            ),
            interpretation_path=interpretation_path,
            interpretation_state=interpretation.state,
            platform=platform,
            source=HistorySource.LEGACY,
            scenario_id=scenario_id,
            package_name=package_name,
            summary=payload,
            warnings=errors,
        )

    def _build_experiment_entry(self, directory: Path) -> HistoryEntry | None:
        manifest_path = directory / "experiment.json"
        manifest, manifest_error = self._read_json(manifest_path)
        if not manifest and manifest_error is None:
            return None
        if manifest.get("schemaVersion") != "perfeval.gui.experiment.v1":
            return None

        report_path = self._manifest_file(directory, manifest.get("reportPath"))
        summary_path = self._manifest_file(directory, manifest.get("summaryPath"))
        compare_path = self._manifest_file(directory, manifest.get("comparePath"))
        summary, summary_error = self._read_json(summary_path or Path()) if summary_path else ({}, None)
        compare, compare_error = self._read_json(compare_path or Path()) if compare_path else ({}, None)
        errors = tuple(
            message
            for message in (manifest_error, summary_error, compare_error)
            if message is not None
        )
        status = str(manifest.get("status") or "unknown").strip().lower()
        if status not in _KNOWN_STATUSES and status != "running":
            status = "unknown"
        health = (
            HistoryHealth.COMPLETE
            if report_path is not None and not errors
            else HistoryHealth.PARTIAL
        )
        payload = summary or compare or manifest
        raw_task_kind = str(manifest.get("taskKind") or "").strip().lower()
        task_kind = (
            HistoryTaskKind.COMPARE
            if raw_task_kind == "ab"
            else HistoryTaskKind.RUN
            if raw_task_kind in {"run", "diagnose"}
            else HistoryTaskKind.UNKNOWN
        )
        updated_at = _manifest_datetime(manifest) or datetime.fromtimestamp(
            _latest_mtime(
                tuple(
                    path
                    for path in (manifest_path, summary_path, compare_path, report_path)
                    if path is not None
                ),
                directory,
            ),
            tz=timezone.utc,
        )
        platform = "android" if manifest.get("packageName") else _history_platform(summary, manifest)
        interpretation = read_snapshot(directory)
        interpretation_path = (
            interpretation.html_path
            if interpretation.html_path is not None and self._is_safe_file(interpretation.html_path)
            else None
        )
        return HistoryEntry(
            entry_id=str(directory.resolve(strict=False)),
            name=str(manifest.get("title") or manifest.get("experimentId") or directory.name),
            task_kind=task_kind,
            status=status,
            health=health,
            updated_at=updated_at,
            directory=directory.resolve(strict=False),
            report_path=report_path,
            summary_path=summary_path or compare_path,
            interpretation_path=interpretation_path,
            interpretation_state=interpretation.state,
            platform=platform,
            source=HistorySource.QUICK_START,
            preset_id=_optional_text(manifest.get("presetId")),
            scenario_id=_history_scenario_id(summary, compare, manifest),
            package_name=_optional_text(manifest.get("packageName"))
            or _history_package_name(summary, manifest),
            summary=payload,
            warnings=errors,
        )

    def _manifest_file(self, directory: Path, value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = (directory / value).resolve(strict=False)
        if not _is_relative_to(path, directory.resolve(strict=False)):
            return None
        return path if self._is_safe_file(path) else None

    def _read_json(self, path: Path) -> tuple[Mapping[str, Any], str | None]:
        if not self._is_safe_file(path):
            return {}, None
        try:
            if path.stat().st_size > self.max_json_bytes:
                return {}, f"{path.name} 超过读取上限"
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {}, f"{path.name} 无法解析：{exc}"
        if not isinstance(value, Mapping):
            return {}, f"{path.name} 顶层不是 JSON object"
        return value, None

    def _is_safe_file(self, path: Path) -> bool:
        try:
            return (
                not path.is_symlink()
                and path.is_file()
                and _is_relative_to(path.resolve(strict=False), self._workspace)
            )
        except OSError:
            return False

    def _validated_delete_target(
        self,
        entry: HistoryEntry,
    ) -> tuple[Path | None, str | None]:
        directory = entry.directory.expanduser()
        try:
            if directory.is_symlink():
                return None, "拒绝删除符号链接"
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            return None, f"结果目录不可用：{exc}"
        if not resolved.is_dir() or not _is_relative_to(resolved, self._workspace):
            return None, "结果目录不在当前 workspace 内"

        if entry.source is HistorySource.QUICK_START:
            allowed_root = (self._workspace / ".perfeval" / "experiments").resolve(
                strict=False
            )
            if (
                resolved == allowed_root
                or not _is_relative_to(resolved, allowed_root)
                or _path_uses_symlink(resolved, allowed_root)
                or not self._is_safe_file(resolved / "experiment.json")
            ):
                return None, "不是合法的快速启动实验目录"
            return resolved, None

        allowed_root = next(
            (
                root.resolve(strict=False)
                for root in (self._workspace / "runs", self._workspace / "reports")
                if _is_relative_to(resolved, root.resolve(strict=False))
            ),
            None,
        )
        if (
            allowed_root is None
            or resolved == allowed_root
            or _path_uses_symlink(resolved, allowed_root)
        ):
            return None, "不是合法的旧结果目录"
        known_paths = (
            resolved / "summary.json",
            resolved / "manifest.json",
            resolved / "compare.json",
            resolved / "report.html",
            resolved / "compare" / "compare.json",
            resolved / "report" / "report.html",
        )
        if not any(self._is_safe_file(path) for path in known_paths):
            return None, "目录中没有可识别的历史结果"
        return resolved, None


def _history_status(
    summary: Mapping[str, Any],
    compare: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> str:
    value = compare.get("status") or summary.get("status")
    if value is None:
        run = manifest.get("run")
        value = run.get("status") if isinstance(run, Mapping) else None
    normalized = str(value or "unknown").strip().lower()
    return normalized if normalized in _KNOWN_STATUSES else "unknown"


def _history_task_kind(
    *,
    source_root: str,
    summary_path: Path,
    manifest_path: Path,
    compare_path: Path,
    is_safe_file,
) -> HistoryTaskKind:
    if is_safe_file(compare_path):
        return HistoryTaskKind.COMPARE
    if source_root == "runs" or is_safe_file(summary_path) or is_safe_file(manifest_path):
        return HistoryTaskKind.RUN
    return HistoryTaskKind.UNKNOWN


def _history_name(
    directory: Path,
    summary: Mapping[str, Any],
    compare: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> str:
    scenario = summary.get("scenario")
    manifest_scenario = manifest.get("scenario")
    values = (
        summary.get("scenarioId"),
        scenario.get("scenarioId") if isinstance(scenario, Mapping) else None,
        summary.get("runId"),
        compare.get("compareId"),
        manifest.get("runId"),
        manifest_scenario.get("scenarioId") if isinstance(manifest_scenario, Mapping) else None,
    )
    return next((str(value) for value in values if value), directory.name)


def _history_platform(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    for payload in (summary, manifest):
        for key in ("app", "device"):
            value = payload.get(key)
            if isinstance(value, Mapping) and value.get("platform"):
                return str(value["platform"])
    return "unknown"


def _history_scenario_id(
    summary: Mapping[str, Any],
    compare: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> str | None:
    for payload in (summary, compare, manifest):
        direct = _optional_text(payload.get("scenarioId"))
        if direct:
            return direct
        scenario = payload.get("scenario")
        if isinstance(scenario, Mapping):
            nested = _optional_text(scenario.get("scenarioId") or scenario.get("id"))
            if nested:
                return nested
    return None


def _history_package_name(
    summary: Mapping[str, Any], manifest: Mapping[str, Any]
) -> str | None:
    for payload in (summary, manifest):
        direct = _optional_text(payload.get("packageName"))
        if direct:
            return direct
        app = payload.get("app")
        if isinstance(app, Mapping):
            nested = _optional_text(app.get("packageName"))
            if nested:
                return nested
    return None


def _optional_text(value: object) -> str | None:
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _latest_mtime(paths: tuple[Path, ...], directory: Path) -> float:
    values: list[float] = []
    for path in paths:
        try:
            if path.exists():
                values.append(path.stat().st_mtime)
        except OSError:
            continue
    if values:
        return max(values)
    try:
        return directory.stat().st_mtime
    except OSError:
        return 0.0


def _manifest_datetime(manifest: Mapping[str, Any]) -> datetime | None:
    for key in ("endedAtUtc", "startedAtUtc", "createdAtUtc"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_uses_symlink(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return root.is_symlink()
