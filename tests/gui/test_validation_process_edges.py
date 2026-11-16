from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer

from perfeval.gui.controllers.task_controller import TaskController
from perfeval.gui.models.result import ResultType, TaskOutcome
from perfeval.gui.models.task import TaskSpec, TaskType
from perfeval.gui.services.process_runner import ProcessRunner
from perfeval.gui.widgets.task_console import TaskConsole


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def _task(tmp_path: Path, scenario: str, *extra: str) -> TaskSpec:
    output = tmp_path / "Unicode 空格 输出"
    return TaskSpec.create(
        task_type=TaskType.RUN,
        program=sys.executable,
        arguments=(
            str(FAKE_CLI),
            scenario,
            "--output-root",
            str(output),
            *extra,
        ),
        working_directory=tmp_path,
        display_command=f"fake {scenario}",
        expected_output_root=output,
        task_id=f"validation-{scenario}",
    )


def test_validation_long_log_keeps_event_loop_responsive_and_ui_bounded(qtbot, tmp_path: Path) -> None:
    controller = TaskController(runner=ProcessRunner(log_directory=tmp_path / "logs"))
    console = TaskConsole(max_lines=120)
    qtbot.addWidget(console)
    console.bind_controller(controller)
    heartbeat = {"ticks": 0}
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: heartbeat.__setitem__("ticks", heartbeat["ticks"] + 1))
    timer.start()

    with qtbot.waitSignal(controller.finished, timeout=10000) as blocker:
        assert controller.start(
            _task(
                tmp_path,
                "long-log",
                "--line-count",
                "2500",
                "--delay",
                "0.0002",
            )
        )

    timer.stop()
    result = blocker.args[0]
    assert heartbeat["ticks"] >= 10
    assert console.output.blockCount() <= 120
    assert "line 02499" in console.output.toPlainText()
    assert "line 02498" in result.stdout_log_path.read_text(encoding="utf-8")
    assert "line 02499" in result.stderr_log_path.read_text(encoding="utf-8")
    assert result.outcome == TaskOutcome.COMPLETED


@pytest.mark.parametrize(
    ("exit_code", "status", "outcome"),
    [
        (7, "completed", TaskOutcome.UNEXPECTED_ERROR),
        (10, "tool_error", TaskOutcome.TOOL_ERROR),
    ],
)
def test_validation_abnormal_exit_finishes_without_gui_crash(
    qtbot,
    tmp_path: Path,
    exit_code: int,
    status: str,
    outcome: TaskOutcome,
) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    with qtbot.waitSignal(runner.finished, timeout=5000) as blocker:
        runner.start(
            _task(
                tmp_path,
                "exit",
                "--exit-code",
                str(exit_code),
                "--status",
                status,
            )
        )

    result = blocker.args[0]
    assert result.outcome == outcome
    assert result.exit_code == exit_code
    assert result.final_state.value == "completed"


@pytest.mark.parametrize("report_state", ["missing", "damaged"])
def test_validation_missing_or_damaged_report_is_not_a_primary_result(
    qtbot,
    tmp_path: Path,
    report_state: str,
) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    with qtbot.waitSignal(runner.finished, timeout=5000) as blocker:
        runner.start(
            _task(
                tmp_path,
                "exit",
                "--status",
                "partial",
                "--report-state",
                report_state,
            )
        )

    result = blocker.args[0]
    reports = [item for item in result.results if item.type == ResultType.REPORT]
    assert not any(item.exists for item in reports)
    if report_state == "damaged":
        assert reports
    assert result.metadata["result_not_found"] is True


def test_validation_cancel_escalates_to_kill_when_process_ignores_terminate(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs", cancel_grace_ms=75)
    with qtbot.waitSignal(runner.started, timeout=5000):
        runner.start(_task(tmp_path, "ignore-terminate", "--delay", "0.01"))

    with qtbot.waitSignal(runner.finished, timeout=5000) as blocker:
        assert runner.cancel()

    result = blocker.args[0]
    assert result.outcome == TaskOutcome.CANCELLED
    assert result.user_cancelled
    assert result.final_state.value == "completed"


@pytest.mark.skipif(
    os.environ.get("PERFEVAL_RUN_30S_GUI") != "1",
    reason="set PERFEVAL_RUN_30S_GUI=1 for the explicit GUI-AC-012 soak",
)
def test_validation_30_second_fake_task_keeps_qt_event_loop_responsive(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs", cancel_grace_ms=200)
    heartbeat = {"ticks": 0}
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(lambda: heartbeat.__setitem__("ticks", heartbeat["ticks"] + 1))

    with qtbot.waitSignal(runner.started, timeout=5000):
        runner.start(_task(tmp_path, "long", "--delay", "0.05"))
    timer.start()
    qtbot.wait(30_000)
    timer.stop()

    with qtbot.waitSignal(runner.finished, timeout=5000) as blocker:
        assert runner.cancel()

    assert heartbeat["ticks"] >= 400
    assert blocker.args[0].outcome == TaskOutcome.CANCELLED
