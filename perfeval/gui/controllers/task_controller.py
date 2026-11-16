from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from perfeval.gui.models.result import TaskOutcome, TaskResult
from perfeval.gui.models.task import (
    InvalidTaskTransition,
    LogChunk,
    ManualRequest,
    ProcessStartInfo,
    TaskSpec,
    TaskPhaseEvent,
    TaskState,
    TaskStateMachine,
)
from perfeval.gui.services.process_runner import ProcessRunner

try:
    from PySide6.QtCore import QObject, QTimer, Signal
except ImportError:  # Keep non-GUI imports available without the optional extra.
    QObject = object  # type: ignore[assignment,misc]
    QTimer = None  # type: ignore[assignment,misc]

    class Signal:  # type: ignore[no-redef]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass


class TaskController(QObject):
    """Own the strict GUI lifecycle and adapt ProcessRunner signals for pages."""

    state_changed = Signal(object)
    validation_failed = Signal(object)
    started = Signal(object)
    log_chunk = Signal(object)
    manual_requested = Signal(object)
    phase_changed = Signal(object)
    elapsed_changed = Signal(float)
    launch_failed = Signal(object)
    finished = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        if QTimer is None:
            raise RuntimeError("PySide6 is required to use TaskController")
        super().__init__(parent)
        self.runner = runner or ProcessRunner(self)
        self._machine = TaskStateMachine()
        self._task: TaskSpec | None = None
        self._result: TaskResult | None = None
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._emit_elapsed)

        self.runner.state_changed.connect(self._on_runner_state)
        self.runner.started.connect(self._on_started)
        self.runner.stdout_chunk.connect(self._on_log_chunk)
        self.runner.stderr_chunk.connect(self._on_log_chunk)
        self.runner.manual_requested.connect(self._on_manual_requested)
        self.runner.phase_changed.connect(self._on_phase_changed)
        self.runner.launch_failed.connect(self.launch_failed.emit)
        self.runner.finished.connect(self._on_finished)

    @property
    def state(self) -> TaskState:
        return self._machine.state

    @property
    def current_task(self) -> TaskSpec | None:
        return self._task

    @property
    def result(self) -> TaskResult | None:
        return self._result

    @property
    def is_active(self) -> bool:
        return self.state in {
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.WAITING_FOR_USER,
            TaskState.CANCELLING,
        }

    def validate(self, task: TaskSpec) -> tuple[str, ...]:
        issues: list[str] = []
        if not task.working_directory.is_dir():
            issues.append(f"workspace 不存在: {task.working_directory}")
        if not task.program.strip():
            issues.append("CLI program 不能为空")
        if not task.arguments:
            issues.append("CLI arguments 不能为空")
        output_parent = next(
            (candidate for candidate in (task.expected_output_root, *task.expected_output_root.parents) if candidate.exists()),
            None,
        )
        if output_parent is None or not output_parent.is_dir() or not os.access(output_parent, os.W_OK):
            issues.append(f"输出目录不可创建: {task.expected_output_root}")
        return tuple(issues)

    def start(self, task: TaskSpec) -> bool:
        if self.is_active:
            raise RuntimeError("a task is already active")
        if self.state in {TaskState.COMPLETED, TaskState.INVALID_INPUT, TaskState.READY}:
            self.reset()
        if self.state != TaskState.IDLE:
            raise InvalidTaskTransition(f"cannot start from {self.state.value}")

        self._task = task
        self._result = None
        self._transition(TaskState.VALIDATING)
        issues = self.validate(task)
        if issues:
            self._transition(TaskState.INVALID_INPUT)
            self.validation_failed.emit(issues)
            return False
        self._transition(TaskState.READY)
        self._transition(TaskState.STARTING)
        try:
            self.runner.start(task)
        except Exception as exc:
            self._transition(TaskState.COMPLETED)
            self.launch_failed.emit(str(exc))
            result = TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                outcome=TaskOutcome.LAUNCH_ERROR,
                process_error=str(exc),
            )
            self._result = result
            self.finished.emit(result)
            return False
        return True

    def continue_manual(self) -> bool:
        if self.state != TaskState.WAITING_FOR_USER:
            return False
        return self.runner.continue_manual()

    def cancel(self) -> bool:
        if self.state not in {
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.WAITING_FOR_USER,
        }:
            return False
        return self.runner.cancel()

    def shutdown(self) -> bool:
        """Begin the same bounded cancel flow used by the Cancel button."""

        return self.cancel() if self.is_active else True

    def reset(self) -> None:
        if self.is_active:
            raise InvalidTaskTransition("cannot reset an active task")
        if self.state != TaskState.IDLE:
            self._transition(TaskState.IDLE)
        self._task = None
        self._result = None
        self._elapsed_timer.stop()
        self.elapsed_changed.emit(0.0)

    def _on_runner_state(self, state: TaskState) -> None:
        if state == self.state:
            return
        try:
            self._transition(state)
        except InvalidTaskTransition:
            # Runner launch errors may complete before QProcess ever reaches RUNNING.
            if state == TaskState.COMPLETED and self.state == TaskState.STARTING:
                self._transition(TaskState.COMPLETED)
            else:
                raise
        if state == TaskState.COMPLETED:
            self._elapsed_timer.stop()

    def _on_started(self, info: ProcessStartInfo) -> None:
        self._elapsed_timer.start()
        self.started.emit(info)

    def _on_log_chunk(self, chunk: LogChunk) -> None:
        self.log_chunk.emit(chunk)

    def _on_manual_requested(self, request: ManualRequest) -> None:
        self.manual_requested.emit(request)

    def _on_phase_changed(self, event: TaskPhaseEvent) -> None:
        self.phase_changed.emit(event)

    def _on_finished(self, result: TaskResult) -> None:
        self._result = result
        self._elapsed_timer.stop()
        self._emit_elapsed()
        self.finished.emit(result)

    def _emit_elapsed(self) -> None:
        result = self._result
        if result is not None and result.duration_seconds is not None:
            self.elapsed_changed.emit(result.duration_seconds)
            return
        task_started = self.runner.started_at
        if task_started is None:
            self.elapsed_changed.emit(0.0)
            return
        from datetime import datetime, timezone

        self.elapsed_changed.emit(max(0.0, (datetime.now(timezone.utc) - task_started).total_seconds()))

    def _transition(self, state: TaskState) -> None:
        self._machine.transition(state)
        self.state_changed.emit(state)
