from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.models.preset import PresetTaskKind, TaskLaunchConfig
from perfeval.gui.models.task import TaskPhaseEvent, TaskPhaseState, TaskSpec, TaskState, TaskType


class FakeController(QObject):
    state_changed = Signal(object)
    phase_changed = Signal(object)
    finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = TaskState.IDLE
        self.current_task = None
        self.started = []

    @property
    def is_active(self) -> bool:
        return self.state is TaskState.RUNNING

    def start(self, task) -> bool:
        self.current_task = task
        self.started.append(task)
        self.state = TaskState.RUNNING
        self.state_changed.emit(self.state)
        return True

    def cancel(self): return True
    def continue_manual(self): return True
    def reset(self): self.state = TaskState.IDLE
    def shutdown(self): return True


def test_session_enforces_single_task_and_collects_phases(tmp_path: Path) -> None:
    controller = FakeController()
    session = TaskSessionController(controller=controller)  # type: ignore[arg-type]
    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program="perfeval",
        arguments=("run", "scenario.yaml"),
        working_directory=tmp_path,
        display_command="perfeval run scenario.yaml",
        expected_output_root=tmp_path / "runs",
    )
    launch = TaskLaunchConfig("p", "页面流畅度", PresetTaskKind.RUN, task)

    assert session.start(launch)
    phase = TaskPhaseEvent("collection", TaskPhaseState.STARTED, "采集性能数据")
    controller.phase_changed.emit(phase)
    assert session.phases == (phase,)

    try:
        session.start(launch)
    except RuntimeError as exc:
        assert "已有任务" in str(exc)
    else:
        raise AssertionError("concurrent start must be rejected")


def test_repeat_last_uses_a_fresh_task_id(tmp_path: Path) -> None:
    controller = FakeController()
    session = TaskSessionController(controller=controller)  # type: ignore[arg-type]
    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program="perfeval",
        arguments=("run", "scenario.yaml"),
        working_directory=tmp_path,
        display_command="perfeval run scenario.yaml",
        expected_output_root=tmp_path / "runs",
    )
    launch = TaskLaunchConfig("p", "页面流畅度", PresetTaskKind.RUN, task)
    assert session.start(launch)
    controller.state = TaskState.COMPLETED

    assert session.repeat_last()
    assert controller.started[0].task_id != controller.started[1].task_id
