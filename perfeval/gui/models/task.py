from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import uuid4


class TaskType(str, Enum):
    """Business operation executed by the GUI process boundary."""

    RUN = "run"
    OFFLINE = "offline"
    AB = "ab"
    DIAGNOSE = "diagnose"
    VALIDATE = "validate"


class TaskState(str, Enum):
    """GUI lifecycle state; it deliberately does not encode the outcome."""

    IDLE = "idle"
    VALIDATING = "validating"
    INVALID_INPUT = "invalid_input"
    READY = "ready"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    CANCELLING = "cancelling"
    COMPLETED = "completed"


class ManualCapability(str, Enum):
    NONE = "none"
    TEXT_FALLBACK = "text_fallback"
    EVENT_AND_TEXT = "event_and_text"


class LogStream(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"


class TaskPhaseState(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.IDLE: frozenset({TaskState.VALIDATING}),
    TaskState.VALIDATING: frozenset({TaskState.INVALID_INPUT, TaskState.READY}),
    TaskState.INVALID_INPUT: frozenset({TaskState.IDLE}),
    TaskState.READY: frozenset({TaskState.STARTING, TaskState.IDLE}),
    TaskState.STARTING: frozenset({TaskState.RUNNING, TaskState.CANCELLING, TaskState.COMPLETED}),
    TaskState.RUNNING: frozenset(
        {TaskState.WAITING_FOR_USER, TaskState.CANCELLING, TaskState.COMPLETED}
    ),
    TaskState.WAITING_FOR_USER: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLING, TaskState.COMPLETED}
    ),
    TaskState.CANCELLING: frozenset({TaskState.COMPLETED}),
    TaskState.COMPLETED: frozenset({TaskState.IDLE}),
}


class InvalidTaskTransition(ValueError):
    pass


@dataclass
class TaskStateMachine:
    state: TaskState = TaskState.IDLE

    def can_transition(self, target: TaskState) -> bool:
        return target in _ALLOWED_TRANSITIONS[self.state]

    def transition(self, target: TaskState) -> TaskState:
        if target == self.state:
            return self.state
        if not self.can_transition(target):
            raise InvalidTaskTransition(f"invalid task transition: {self.state.value} -> {target.value}")
        self.state = target
        return self.state

    def reset(self) -> TaskState:
        if self.state == TaskState.IDLE:
            return self.state
        return self.transition(TaskState.IDLE)


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class TaskSpec:
    """Immutable, shell-free process specification.

    ``display_command`` is presentation-only.  Runners must use ``program`` and
    ``arguments`` separately.
    """

    task_type: TaskType
    program: str
    arguments: tuple[str, ...]
    working_directory: Path
    display_command: str
    expected_output_root: Path
    manual_capability: ManualCapability = ManualCapability.NONE
    metadata: Mapping[str, Any] = field(default_factory=dict)
    redacted_metadata: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.program or "\x00" in self.program:
            raise ValueError("program must be a non-empty executable path or name")
        if not self.task_id or "\x00" in self.task_id:
            raise ValueError("task_id must be non-empty")
        arguments = tuple(str(value) for value in self.arguments)
        if any("\x00" in value for value in arguments):
            raise ValueError("arguments must not contain NUL bytes")
        working_directory = Path(self.working_directory).expanduser().resolve()
        expected_output_root = Path(self.expected_output_root).expanduser()
        if not expected_output_root.is_absolute():
            expected_output_root = working_directory / expected_output_root
        expected_output_root = expected_output_root.resolve(strict=False)
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(self, "expected_output_root", expected_output_root)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))
        object.__setattr__(self, "redacted_metadata", _freeze_mapping(self.redacted_metadata))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @classmethod
    def create(
        cls,
        *,
        task_type: TaskType,
        program: str,
        arguments: Sequence[str],
        working_directory: Path,
        display_command: str,
        expected_output_root: Path,
        manual_capability: ManualCapability = ManualCapability.NONE,
        metadata: Mapping[str, Any] | None = None,
        redacted_metadata: Mapping[str, Any] | None = None,
        environment: Mapping[str, str] | None = None,
        task_id: str | None = None,
    ) -> TaskSpec:
        values: dict[str, Any] = {
            "task_type": task_type,
            "program": program,
            "arguments": tuple(arguments),
            "working_directory": working_directory,
            "display_command": display_command,
            "expected_output_root": expected_output_root,
            "manual_capability": manual_capability,
            "metadata": metadata or {},
            "redacted_metadata": redacted_metadata or {},
            "environment": environment or {},
        }
        if task_id is not None:
            values["task_id"] = task_id
        return cls(**values)


# CLI_INTEGRATION names this process boundary value CommandSpec.  Keep a single
# concrete model so pages cannot accidentally execute a divergent display spec.
CommandSpec = TaskSpec


@dataclass(frozen=True)
class ProcessStartInfo:
    pid: int
    started_at: datetime

    @classmethod
    def now(cls, pid: int) -> ProcessStartInfo:
        return cls(pid=pid, started_at=datetime.now(timezone.utc))


@dataclass(frozen=True)
class LogChunk:
    sequence: int
    stream: LogStream
    text: str
    monotonic_ns: int
    replacement_count: int = 0


@dataclass(frozen=True)
class ManualRequest:
    prompt: str
    timeout_seconds: float | None = None
    source: str = "text"
    event_id: str | None = None


@dataclass(frozen=True)
class TaskPhaseEvent:
    phase_id: str
    state: TaskPhaseState
    label: str
    repeat_index: int | None = None
    repeat_count: int | None = None
    variant_id: str | None = None
    detail: str | None = None
