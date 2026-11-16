from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .task import TaskSpec


class PresetSource(StrEnum):
    BUILTIN = "builtin"
    WORKSPACE = "workspace"


class PresetTaskKind(StrEnum):
    RUN = "run"
    AB = "ab"
    DIAGNOSE = "diagnose"


class PresetCategory(StrEnum):
    STARTUP = "startup"
    SMOOTHNESS = "smoothness"
    MEMORY = "memory"
    AB = "ab"
    DIAGNOSE = "diagnose"
    PROJECT = "project"


@dataclass(frozen=True)
class PresetScenario:
    preset_id: str
    title: str
    description: str
    task_kind: PresetTaskKind
    category: PresetCategory
    source: PresetSource
    scenario_path: Path | None = None
    default_repeat: int = 3
    default_package: str | None = None
    default_activity: str | None = None
    default_mode: str | None = None
    interactive: bool = False
    valid: bool = True
    issues: tuple[str, ...] = ()
    relative_path: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def source_label(self) -> str:
        return "内置" if self.source is PresetSource.BUILTIN else "项目"


@dataclass(frozen=True)
class TaskLaunchConfig:
    preset_id: str
    title: str
    task_kind: PresetTaskKind
    task: TaskSpec
    device_serial: str | None = None
    device_label: str | None = None
    package_name: str | None = None
    repeat_count: int | None = None
    source: PresetSource | None = None
    quick_mode: bool = True
    phase_ids: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_task(cls, task: TaskSpec) -> TaskLaunchConfig:
        metadata = task.metadata
        raw_kind = str(metadata.get("preset_task_kind") or task.task_type.value)
        try:
            kind = PresetTaskKind(raw_kind)
        except ValueError:
            kind = PresetTaskKind.RUN
        phases = metadata.get("phase_ids")
        phase_values = tuple(
            (str(value[0]), str(value[1]))
            for value in phases
            if isinstance(value, (tuple, list)) and len(value) == 2
        ) if isinstance(phases, (tuple, list)) else ()
        return cls(
            preset_id=str(metadata.get("preset_id") or task.task_id),
            title=str(metadata.get("scenario_title") or _task_title(kind)),
            task_kind=kind,
            task=task,
            device_serial=_optional_text(metadata.get("device_serial")),
            device_label=_optional_text(metadata.get("device_label")),
            package_name=_optional_text(metadata.get("package_name")),
            repeat_count=_optional_int(metadata.get("repeat_count")),
            quick_mode=bool(metadata.get("quick_mode", False)),
            phase_ids=phase_values,
        )


def _optional_text(value: object) -> str | None:
    return str(value) if value not in {None, ""} else None


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _task_title(kind: PresetTaskKind) -> str:
    return {
        PresetTaskKind.RUN: "性能采集",
        PresetTaskKind.AB: "多版本 A/B",
        PresetTaskKind.DIAGNOSE: "深度诊断",
    }[kind]
