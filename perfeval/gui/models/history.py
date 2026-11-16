from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .result import ResultDescriptor, ResultType, ResultViewer
from .llm import InterpretationState


class HistoryHealth(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class HistoryTaskKind(str, Enum):
    RUN = "run"
    COMPARE = "compare"
    UNKNOWN = "unknown"


class HistorySource(str, Enum):
    QUICK_START = "quick_start"
    LEGACY = "legacy"


class HistoryArtifactKind(str, Enum):
    REPORT = "report"
    INTERPRETATION = "interpretation"
    SUMMARY = "summary"
    DIRECTORY = "directory"


@dataclass(frozen=True)
class HistoryQuery:
    label: str
    preset_id: str | None = None
    scenario_id: str | None = None
    package_name: str | None = None
    task_kind: HistoryTaskKind | None = None

    def __post_init__(self) -> None:
        if self.task_kind is not None:
            try:
                task_kind = HistoryTaskKind(self.task_kind)
            except ValueError:
                task_kind = None
            object.__setattr__(self, "task_kind", task_kind)


@dataclass(frozen=True)
class HistoryEntry:
    entry_id: str
    name: str
    task_kind: HistoryTaskKind
    status: str
    health: HistoryHealth
    updated_at: datetime
    directory: Path
    report_path: Path | None = None
    summary_path: Path | None = None
    interpretation_path: Path | None = None
    interpretation_state: InterpretationState = InterpretationState.NOT_GENERATED
    platform: str = "unknown"
    source: HistorySource = HistorySource.LEGACY
    preset_id: str | None = None
    scenario_id: str | None = None
    package_name: str | None = None
    summary: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            task_kind = HistoryTaskKind(self.task_kind)
        except ValueError:
            task_kind = HistoryTaskKind.UNKNOWN
        try:
            source = HistorySource(self.source)
        except ValueError:
            source = HistorySource.LEGACY
        object.__setattr__(self, "task_kind", task_kind)
        object.__setattr__(self, "source", source)
        try:
            interpretation_state = InterpretationState(self.interpretation_state)
        except ValueError:
            interpretation_state = InterpretationState.NOT_GENERATED
        object.__setattr__(self, "interpretation_state", interpretation_state)
        object.__setattr__(self, "summary", MappingProxyType(dict(self.summary)))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def kind(self) -> str:
        """Compatibility alias for callers that still expect the old string field."""

        return self.task_kind.value

    @property
    def artifact_kinds(self) -> frozenset[HistoryArtifactKind]:
        values = {HistoryArtifactKind.DIRECTORY}
        if self.report_path is not None and self.report_path.is_file():
            values.add(HistoryArtifactKind.REPORT)
        if self.summary_path is not None and self.summary_path.is_file():
            values.add(HistoryArtifactKind.SUMMARY)
        if self.interpretation_path is not None and self.interpretation_path.is_file():
            values.add(HistoryArtifactKind.INTERPRETATION)
        return frozenset(values)

    @property
    def has_report(self) -> bool:
        return HistoryArtifactKind.REPORT in self.artifact_kinds

    def has_artifact(self, artifact: HistoryArtifactKind | str) -> bool:
        try:
            normalized = HistoryArtifactKind(artifact)
        except ValueError:
            return False
        return normalized in self.artifact_kinds

    @property
    def report_descriptor(self) -> ResultDescriptor | None:
        if self.report_path is None:
            return None
        return ResultDescriptor(
            type=ResultType.REPORT,
            label="报告",
            location=str(self.report_path),
            exists=self.report_path.is_file(),
            viewer=ResultViewer.REPORT,
            related_task_id=self.entry_id,
        )

    @property
    def directory_descriptor(self) -> ResultDescriptor:
        return ResultDescriptor(
            type=ResultType.DIRECTORY,
            label="结果目录",
            location=str(self.directory),
            exists=self.directory.is_dir(),
            viewer=ResultViewer.DIRECTORY,
            related_task_id=self.entry_id,
        )

    @property
    def summary_descriptor(self) -> ResultDescriptor | None:
        if self.summary_path is None:
            return None
        return ResultDescriptor(
            type=ResultType.SUMMARY,
            label="Summary",
            location=str(self.summary_path),
            exists=self.summary_path.is_file(),
            viewer=ResultViewer.SYSTEM,
            related_task_id=self.entry_id,
        )

    @property
    def interpretation_descriptor(self) -> ResultDescriptor | None:
        if self.interpretation_path is None:
            return None
        return ResultDescriptor(
            type=ResultType.INTERPRETATION,
            label="LLM 解读报告",
            location=str(self.interpretation_path),
            exists=self.interpretation_path.is_file(),
            viewer=ResultViewer.REPORT,
            related_task_id=self.entry_id,
        )


@dataclass(frozen=True)
class HistoryScanResult:
    entries: tuple[HistoryEntry, ...]
    scanned_nodes: int
    truncated: bool = False
    warnings: tuple[str, ...] = ()
