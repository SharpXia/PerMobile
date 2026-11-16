from __future__ import annotations

from datetime import datetime, timezone

from perfeval.gui.models.result import ResultType, TaskOutcome, TaskResult
from perfeval.gui.models.task import LogChunk, LogStream, ManualRequest, TaskState, TaskType
from perfeval.gui.widgets.task_console import TaskConsole
from perfeval.gui.widgets.task_summary import TaskSummary


def test_process_console_bounds_visible_lines_and_preserves_sequence(qtbot) -> None:
    console = TaskConsole(max_lines=3)
    qtbot.addWidget(console)
    for sequence in range(1, 6):
        console.append_chunk(
            LogChunk(sequence, LogStream.STDERR if sequence == 5 else LogStream.STDOUT, f"line {sequence}", sequence)
        )

    text = console.output.toPlainText()
    assert console.output.blockCount() <= 3
    assert "[000005][stderr] line 5" in text
    assert "line 1" not in text


def test_process_summary_displays_textual_outcome_and_suggestion(qtbot) -> None:
    summary = TaskSummary()
    qtbot.addWidget(summary)
    now = datetime.now(timezone.utc)
    result = TaskResult(
        task_id="summary-task",
        task_type=TaskType.AB,
        outcome=TaskOutcome.FAIL,
        exit_code=1,
        started_at=now,
        ended_at=now,
    )

    summary.set_result(result)

    assert summary.outcome_label.text() == "fail"
    assert summary.exit_code_label.text() == "1"
    assert "性能判定未通过" in summary.suggestion_label.text()


def test_process_summary_suggestion_uses_available_width_without_overlap(qtbot) -> None:
    summary = TaskSummary()
    qtbot.addWidget(summary)
    summary.resize(520, 360)
    summary.show()
    now = datetime.now(timezone.utc)
    result = TaskResult(
        task_id="summary-layout-task",
        task_type=TaskType.RUN,
        outcome=TaskOutcome.COMPLETED,
        exit_code=0,
        started_at=now,
        ended_at=now,
        metadata={"result_not_found": True},
    )

    summary.set_result(result)
    qtbot.waitUntil(
        lambda: summary.suggestion_label.height()
        >= summary.suggestion_label.heightForWidth(summary.suggestion_label.width())
    )

    assert summary.summary_form.fieldGrowthPolicy() == (
        summary.summary_form.FieldGrowthPolicy.AllNonFixedFieldsGrow
    )
    assert summary.suggestion_label.width() > summary.width() // 2
    assert summary.suggestion_label.geometry().bottom() < summary.copy_command_button.y()


def test_process_widgets_show_and_clear_manual_request(qtbot) -> None:
    console = TaskConsole()
    summary = TaskSummary()
    qtbot.addWidget(console)
    qtbot.addWidget(summary)
    console.show()
    summary.show()
    request = ManualRequest("请滑动页面后按 Enter", timeout_seconds=12)

    console.show_manual_request(request)
    summary.show_manual_request(request)

    assert console.manual_frame.isVisible()
    assert "12.0 秒" in console.manual_timeout_label.text()
    assert summary.manual_label.isVisible()
    summary._on_state_changed(TaskState.RUNNING)
    console.set_state(TaskState.RUNNING)
    assert not console.manual_frame.isVisible()
    assert not summary.manual_label.isVisible()


def test_process_summary_shows_bounded_failure_log_and_opens_full_log(
    qtbot,
    tmp_path,
) -> None:
    stderr = tmp_path / "failed.stderr.log"
    stderr.write_text(
        "准备采集\nperfeval: error: adb device unauthorized\n",
        encoding="utf-8",
    )
    summary = TaskSummary()
    qtbot.addWidget(summary)
    summary.show()
    result = TaskResult(
        task_id="failed-task",
        task_type=TaskType.RUN,
        outcome=TaskOutcome.TOOL_ERROR,
        exit_code=10,
        stderr_log_path=stderr,
    )

    summary.set_result(result)

    assert summary.failure_frame.isVisible()
    assert "adb device unauthorized" in summary.failure_output.toPlainText()
    assert summary.failure_output.minimumHeight() == summary.failure_output.maximumHeight()
    assert summary.open_failure_log_button.isEnabled()
    with qtbot.waitSignal(summary.open_result_requested) as blocker:
        summary.open_failure_log_button.click()
    descriptor = blocker.args[0]
    assert descriptor.type == ResultType.LOG
    assert descriptor.location == str(stderr)
