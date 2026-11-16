from __future__ import annotations

import sys
from pathlib import Path

from perfeval.gui.controllers import TaskController
from perfeval.gui.models.result import TaskOutcome
from perfeval.gui.models.task import ManualCapability, TaskSpec, TaskState, TaskType
from perfeval.gui.services.process_runner import ProcessRunner


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def _manual_task(tmp_path: Path) -> TaskSpec:
    output = tmp_path / "controller-output"
    return TaskSpec.create(
        task_type=TaskType.DIAGNOSE,
        program=sys.executable,
        arguments=(str(FAKE_CLI), "manual", "--output-root", str(output)),
        working_directory=tmp_path,
        display_command="fake manual",
        expected_output_root=output,
        manual_capability=ManualCapability.EVENT_AND_TEXT,
        task_id="controller-manual",
    )


def test_process_controller_runs_strict_manual_lifecycle(qtbot, tmp_path: Path) -> None:
    controller = TaskController(runner=ProcessRunner(log_directory=tmp_path / "logs"))
    states = []
    controller.state_changed.connect(states.append)

    with qtbot.waitSignal(controller.manual_requested, timeout=5000):
        assert controller.start(_manual_task(tmp_path))
    assert controller.state == TaskState.WAITING_FOR_USER

    with qtbot.waitSignal(controller.finished, timeout=5000) as blocker:
        assert controller.continue_manual()

    assert blocker.args[0].outcome == TaskOutcome.COMPLETED
    assert controller.state == TaskState.COMPLETED
    assert states[:5] == [
        TaskState.VALIDATING,
        TaskState.READY,
        TaskState.STARTING,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_USER,
    ]
    assert states[-2:] == [TaskState.RUNNING, TaskState.COMPLETED]


def test_process_controller_keeps_invalid_input_out_of_runner(qtbot, tmp_path: Path) -> None:
    controller = TaskController(runner=ProcessRunner(log_directory=tmp_path / "logs"))
    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program=sys.executable,
        arguments=(str(FAKE_CLI), "exit"),
        working_directory=tmp_path / "missing-workspace",
        display_command="fake exit",
        expected_output_root=tmp_path / "output",
    )

    with qtbot.waitSignal(controller.validation_failed, timeout=1000) as blocker:
        assert not controller.start(task)

    assert controller.state == TaskState.INVALID_INPUT
    assert "workspace 不存在" in blocker.args[0][0]
    assert not controller.runner.is_active

