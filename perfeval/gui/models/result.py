from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .task import TaskState, TaskType


class TaskOutcome(str, Enum):
    COMPLETED = "completed"
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    PARTIAL = "partial"
    TOOL_ERROR = "tool_error"
    LAUNCH_ERROR = "launch_error"
    UNEXPECTED_ERROR = "unexpected_error"
    CANCELLED = "cancelled"


class ResultType(str, Enum):
    REPORT = "report"
    INTERPRETATION = "interpretation"
    RESULT = "result"
    SUMMARY = "summary"
    COMPARE = "compare"
    ARTIFACT = "artifact"
    DIRECTORY = "directory"
    LOG = "log"
    URL = "url"


class ResultViewer(str, Enum):
    SYSTEM = "system"
    DIRECTORY = "directory"
    REPORT = "report"
    PERFETTO = "perfetto"
    ANDROID_STUDIO = "android_studio"
    NONE = "none"


@dataclass(frozen=True)
class ResultDescriptor:
    type: ResultType
    label: str
    location: str
    exists: bool
    viewer: ResultViewer = ResultViewer.SYSTEM
    related_task_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def path(self) -> Path | None:
        if self.type == ResultType.URL:
            return None
        return Path(self.location)


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    task_type: TaskType
    outcome: TaskOutcome
    final_state: TaskState = TaskState.COMPLETED
    exit_code: int | None = None
    process_error: str | None = None
    stdout_log_path: Path | None = None
    stderr_log_path: Path | None = None
    results: tuple[ResultDescriptor, ...] = ()
    started_at: datetime | None = None
    ended_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user_cancelled: bool = False
    structured_status: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.final_state != TaskState.COMPLETED:
            raise ValueError("TaskResult.final_state must be completed")
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, (self.ended_at - self.started_at).total_seconds())


_STATUS_OUTCOMES = {
    outcome.value: outcome
    for outcome in (
        TaskOutcome.COMPLETED,
        TaskOutcome.PASS,
        TaskOutcome.WARN,
        TaskOutcome.FAIL,
        TaskOutcome.UNKNOWN,
        TaskOutcome.INVALID,
        TaskOutcome.PARTIAL,
        TaskOutcome.TOOL_ERROR,
        TaskOutcome.CANCELLED,
    )
}


def classify_outcome(
    exit_code: int | None,
    *,
    structured_status: str | None = None,
    launch_error: bool = False,
    user_cancelled: bool = False,
) -> TaskOutcome:
    """Combine the current CLI exit contract with an optional result status."""

    if launch_error:
        return TaskOutcome.LAUNCH_ERROR
    if user_cancelled or exit_code == 130:
        return TaskOutcome.CANCELLED
    if exit_code == 1:
        return TaskOutcome.FAIL
    if exit_code == 4:
        return TaskOutcome.INVALID
    if exit_code == 10:
        return TaskOutcome.TOOL_ERROR
    if exit_code is None:
        return TaskOutcome.UNEXPECTED_ERROR
    if exit_code != 0:
        return TaskOutcome.UNEXPECTED_ERROR
    normalized = (structured_status or "").strip().lower()
    return _STATUS_OUTCOMES.get(normalized, TaskOutcome.COMPLETED)


_OUTCOME_SUGGESTIONS: Mapping[TaskOutcome, str] = {
    TaskOutcome.COMPLETED: "命令已完成；请查看结果文件确认业务状态。",
    TaskOutcome.PASS: "性能检查通过。",
    TaskOutcome.WARN: "存在警告项；建议打开报告查看接近阈值的指标。",
    TaskOutcome.FAIL: "性能判定未通过；建议查看 compare/report 中的失败指标。",
    TaskOutcome.UNKNOWN: "证据不足以形成结论；建议增加重复次数并检查采集完整性。",
    TaskOutcome.INVALID: "结果不可比较；请检查 baseline、candidate 和场景配置是否匹配。",
    TaskOutcome.PARTIAL: "任务只生成了部分结果；请查看 stderr 和诊断日志。",
    TaskOutcome.TOOL_ERROR: "工具执行失败；请检查环境、参数和完整日志后重试。",
    TaskOutcome.LAUNCH_ERROR: "进程无法启动；请检查 CLI 路径、权限和安装版本。",
    TaskOutcome.UNEXPECTED_ERROR: "进程异常结束；请保留退出码和日志用于排查。",
    TaskOutcome.CANCELLED: "任务已取消；已生成的日志和部分产物会保留。",
}


def outcome_suggestion(outcome: TaskOutcome) -> str:
    return _OUTCOME_SUGGESTIONS[outcome]
