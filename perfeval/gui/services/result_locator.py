from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from perfeval.gui.models.result import ResultDescriptor, ResultType, ResultViewer
from perfeval.gui.models.task import TaskSpec


_TEXT_PATH_PATTERN = re.compile(
    r"(?im)^(?P<label>report|summary|artifact|compare(?: json)?)\s*:\s*(?P<path>.+?)\s*$"
    r"|^(?P<diagnosis>android diagnosis written to)\s+(?P<diagnosis_path>.+?)\s*$"
)
_KNOWN_FILENAMES = {
    "report.html": (ResultType.REPORT, ResultViewer.REPORT),
    "interpretation.html": (ResultType.INTERPRETATION, ResultViewer.REPORT),
    "summary.json": (ResultType.SUMMARY, ResultViewer.SYSTEM),
    "compare.json": (ResultType.COMPARE, ResultViewer.SYSTEM),
    "diagnosis.json": (ResultType.RESULT, ResultViewer.SYSTEM),
    "manifest.json": (ResultType.RESULT, ResultViewer.SYSTEM),
}
_PATH_KEYS = {
    "report": (ResultType.REPORT, ResultViewer.REPORT),
    "reportHtml": (ResultType.REPORT, ResultViewer.REPORT),
    "summary": (ResultType.SUMMARY, ResultViewer.SYSTEM),
    "summaryPath": (ResultType.SUMMARY, ResultViewer.SYSTEM),
    "compare": (ResultType.COMPARE, ResultViewer.SYSTEM),
    "comparePath": (ResultType.COMPARE, ResultViewer.SYSTEM),
    "result": (ResultType.RESULT, ResultViewer.SYSTEM),
    "resultPath": (ResultType.RESULT, ResultViewer.SYSTEM),
    "diagnosisPath": (ResultType.RESULT, ResultViewer.SYSTEM),
    "diagnosis": (ResultType.RESULT, ResultViewer.SYSTEM),
    "root": (ResultType.DIRECTORY, ResultViewer.DIRECTORY),
    "outputRoot": (ResultType.DIRECTORY, ResultViewer.DIRECTORY),
    "runDir": (ResultType.DIRECTORY, ResultViewer.DIRECTORY),
    "artifact": (ResultType.ARTIFACT, ResultViewer.SYSTEM),
}


@dataclass(frozen=True)
class LocatedResults:
    descriptors: tuple[ResultDescriptor, ...]
    result_not_found: bool
    scanned_entries: int = 0


class ResultLocator:
    """Find current-task outputs without following symlinks or scanning globally."""

    def __init__(
        self,
        *,
        max_entries: int = 2048,
        max_depth: int = 5,
        clock_skew_seconds: float = 2.0,
    ) -> None:
        if max_entries < 1 or max_depth < 0:
            raise ValueError("max_entries must be positive and max_depth must be non-negative")
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.clock_skew_seconds = max(0.0, clock_skew_seconds)

    def locate(
        self,
        task: TaskSpec,
        *,
        envelope: Mapping[str, Any] | None = None,
        stdout_text: str = "",
        started_at: datetime | float | None = None,
    ) -> LocatedResults:
        descriptors: list[ResultDescriptor] = []
        seen: set[tuple[ResultType, str]] = set()

        if envelope:
            self._from_envelope(task, envelope, descriptors, seen)
        if stdout_text:
            self._from_text(task, stdout_text, descriptors, seen)

        cutoff = self._cutoff_timestamp(started_at)
        scanned_entries = self._scan_expected_root(task, cutoff, descriptors, seen)
        has_primary_result = any(
            item.exists and item.type not in {ResultType.DIRECTORY, ResultType.LOG}
            for item in descriptors
        )
        output_root = task.expected_output_root
        self._append_path(
            task,
            output_root,
            ResultType.DIRECTORY,
            ResultViewer.DIRECTORY,
            "输出目录",
            descriptors,
            seen,
            allow_missing=True,
        )
        return LocatedResults(
            descriptors=tuple(descriptors),
            result_not_found=not has_primary_result,
            scanned_entries=scanned_entries,
        )

    def parse_jsonl_events(self, text: str) -> tuple[Mapping[str, Any], ...]:
        events: list[Mapping[str, Any]] = []
        for line in text.splitlines():
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, Mapping) and payload.get("schemaVersion") == "perfeval.cli.event.v1":
                events.append(payload)
        return tuple(events)

    def _from_envelope(
        self,
        task: TaskSpec,
        envelope: Mapping[str, Any],
        descriptors: list[ResultDescriptor],
        seen: set[tuple[ResultType, str]],
    ) -> None:
        payload: Mapping[str, Any] = envelope
        if envelope.get("schemaVersion") == "perfeval.cli.event.v1" and isinstance(envelope.get("data"), Mapping):
            payload = envelope["data"]
        self._walk_envelope(task, payload, descriptors, seen)

    def _walk_envelope(
        self,
        task: TaskSpec,
        payload: Mapping[str, Any],
        descriptors: list[ResultDescriptor],
        seen: set[tuple[ResultType, str]],
    ) -> None:
        for key, value in payload.items():
            if key in _PATH_KEYS:
                result_type, viewer = _PATH_KEYS[key]
                for location in _path_values(value):
                    self._append_location(
                        task,
                        location,
                        result_type,
                        viewer,
                        _label_for(result_type),
                        descriptors,
                        seen,
                        allow_missing=True,
                    )
                continue
            if key in {"artifacts", "artifactPaths"}:
                for location in _path_values(value):
                    artifact_path = Path(location)
                    self._append_location(
                        task,
                        location,
                        ResultType.ARTIFACT,
                        _viewer_for_path(artifact_path),
                        _artifact_label(artifact_path),
                        descriptors,
                        seen,
                        allow_missing=True,
                    )
                continue
            if isinstance(value, Mapping):
                self._walk_envelope(task, value, descriptors, seen)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    if isinstance(item, Mapping):
                        self._walk_envelope(task, item, descriptors, seen)

    def _from_text(
        self,
        task: TaskSpec,
        text: str,
        descriptors: list[ResultDescriptor],
        seen: set[tuple[ResultType, str]],
    ) -> None:
        for match in _TEXT_PATH_PATTERN.finditer(text):
            diagnosis_path = match.group("diagnosis_path")
            if diagnosis_path is not None:
                self._append_location(
                    task,
                    diagnosis_path.strip().strip("'\""),
                    ResultType.DIRECTORY,
                    ResultViewer.DIRECTORY,
                    "诊断目录",
                    descriptors,
                    seen,
                    allow_missing=True,
                )
                continue
            label = str(match.group("label")).lower()
            raw_path = str(match.group("path")).strip().strip("'\"")
            if label.startswith("report"):
                result_type, viewer = ResultType.REPORT, ResultViewer.REPORT
            elif label.startswith("summary"):
                result_type, viewer = ResultType.SUMMARY, ResultViewer.SYSTEM
            elif label.startswith("compare"):
                result_type, viewer = ResultType.COMPARE, ResultViewer.SYSTEM
            elif label.startswith("artifact"):
                result_type, viewer = ResultType.ARTIFACT, _viewer_for_path(Path(raw_path))
            else:
                result_type, viewer = ResultType.DIRECTORY, ResultViewer.DIRECTORY
            display_label = (
                _artifact_label(Path(raw_path))
                if result_type == ResultType.ARTIFACT
                else _label_for(result_type)
            )
            self._append_location(
                task,
                raw_path,
                result_type,
                viewer,
                display_label,
                descriptors,
                seen,
                allow_missing=True,
            )

    def _scan_expected_root(
        self,
        task: TaskSpec,
        cutoff: float | None,
        descriptors: list[ResultDescriptor],
        seen: set[tuple[ResultType, str]],
    ) -> int:
        root = task.expected_output_root
        if not root.is_dir() or root.is_symlink():
            return 0
        count = 0
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and count < self.max_entries:
            directory, depth = stack.pop()
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for child in children:
                if count >= self.max_entries:
                    break
                count += 1
                if child.is_symlink():
                    continue
                try:
                    stat = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if child.is_dir():
                    if depth < self.max_depth:
                        stack.append((child, depth + 1))
                    continue
                if cutoff is not None and stat.st_mtime < cutoff:
                    continue
                known = _KNOWN_FILENAMES.get(child.name.casefold())
                if known is not None:
                    result_type, viewer = known
                    self._append_path(
                        task,
                        child,
                        result_type,
                        viewer,
                        _label_for(result_type),
                        descriptors,
                        seen,
                    )
                elif _looks_like_artifact(child):
                    self._append_path(
                        task,
                        child,
                        ResultType.ARTIFACT,
                        _viewer_for_path(child),
                        _artifact_label(child),
                        descriptors,
                        seen,
                    )
        return count

    def _append_location(
        self,
        task: TaskSpec,
        location: str,
        result_type: ResultType,
        viewer: ResultViewer,
        label: str,
        descriptors: list[ResultDescriptor],
        seen: set[tuple[ResultType, str]],
        *,
        allow_missing: bool,
    ) -> None:
        if "://" in location:
            if _is_controlled_local_url(location):
                key = (ResultType.URL, location)
                if key not in seen:
                    seen.add(key)
                    descriptors.append(
                        ResultDescriptor(
                            type=ResultType.URL,
                            label=label,
                            location=location,
                            exists=True,
                            viewer=viewer,
                            related_task_id=task.task_id,
                        )
                    )
            return
        path = Path(location).expanduser()
        if not path.is_absolute():
            path = task.working_directory / path
        self._append_path(
            task,
            path.resolve(strict=False),
            result_type,
            viewer,
            label,
            descriptors,
            seen,
            allow_missing=allow_missing,
        )

    def _append_path(
        self,
        task: TaskSpec,
        path: Path,
        result_type: ResultType,
        viewer: ResultViewer,
        label: str,
        descriptors: list[ResultDescriptor],
        seen: set[tuple[ResultType, str]],
        *,
        allow_missing: bool = False,
    ) -> None:
        resolved = path.expanduser().resolve(strict=False)
        if not self._is_allowed(task, resolved):
            return
        if resolved.is_symlink():
            return
        exists = _path_matches_result_type(resolved, result_type)
        if not exists and not allow_missing:
            return
        key = (result_type, os.path.normcase(str(resolved)))
        if key in seen:
            return
        seen.add(key)
        descriptors.append(
            ResultDescriptor(
                type=result_type,
                label=label,
                location=str(resolved),
                exists=exists,
                viewer=viewer,
                related_task_id=task.task_id,
            )
        )

    @staticmethod
    def _is_allowed(task: TaskSpec, path: Path) -> bool:
        for root in (task.working_directory, task.expected_output_root):
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    def _cutoff_timestamp(self, value: datetime | float | None) -> float | None:
        if value is None:
            return None
        timestamp = value.timestamp() if isinstance(value, datetime) else float(value)
        return timestamp - self.clock_skew_seconds


def _path_values(value: Any) -> Iterable[str]:
    if isinstance(value, (str, Path)):
        yield str(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, (str, Path)):
                yield str(item)


def _path_matches_result_type(path: Path, result_type: ResultType) -> bool:
    if result_type == ResultType.DIRECTORY:
        return path.is_dir()
    if result_type == ResultType.URL:
        return False
    return path.is_file()


def _is_controlled_local_url(location: str) -> bool:
    try:
        parsed = urlparse(location)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def _label_for(result_type: ResultType) -> str:
    return {
        ResultType.REPORT: "报告",
        ResultType.INTERPRETATION: "LLM 解读报告",
        ResultType.SUMMARY: "Summary",
        ResultType.COMPARE: "Compare",
        ResultType.RESULT: "结果",
        ResultType.ARTIFACT: "Artifact",
        ResultType.DIRECTORY: "输出目录",
        ResultType.LOG: "日志",
        ResultType.URL: "链接",
    }[result_type]


def _viewer_for_path(path: Path) -> ResultViewer:
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name == "report.html" or suffix in {".html", ".htm"}:
        return ResultViewer.REPORT
    if "perfetto" in name or suffix in {".pftrace", ".perfetto-trace"}:
        return ResultViewer.PERFETTO
    if "simpleperf" in name or suffix == ".data":
        return ResultViewer.ANDROID_STUDIO
    return ResultViewer.SYSTEM


def _artifact_label(path: Path) -> str:
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if "perfetto" in name or suffix in {".pftrace", ".perfetto-trace"}:
        return "Perfetto trace"
    if "simpleperf" in name or suffix == ".data":
        return "simpleperf artifact"
    return "Artifact"


def _looks_like_artifact(path: Path) -> bool:
    name = path.name.casefold()
    return (
        "perfetto" in name
        or "simpleperf" in name
        or path.suffix.casefold() in {".trace", ".pftrace", ".perfetto-trace", ".data"}
    )
