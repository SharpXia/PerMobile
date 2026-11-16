from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication

from perfeval.gui.models.result import (
    ResultDescriptor,
    ResultType,
    ResultViewer,
    TaskOutcome,
    TaskResult,
)
from perfeval.gui.models.task import ManualRequest, TaskSpec, TaskState, TaskType
from perfeval.gui.pages.ab_page import AbPage
from perfeval.gui.services.command_builder import CommandBuilder


class FakeController(QObject):
    state_changed = Signal(object)
    validation_failed = Signal(object)
    started = Signal(object)
    log_chunk = Signal(object)
    manual_requested = Signal(object)
    elapsed_changed = Signal(float)
    launch_failed = Signal(object)
    finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = TaskState.IDLE
        self.started_tasks: list[TaskSpec] = []
        self.cancel_calls = 0
        self.continue_calls = 0

    def start(self, task: TaskSpec) -> bool:
        self.started_tasks.append(task)
        self.set_state(TaskState.STARTING)
        return True

    def cancel(self) -> bool:
        if self.state not in {TaskState.STARTING, TaskState.RUNNING, TaskState.WAITING_FOR_USER}:
            return False
        self.cancel_calls += 1
        self.set_state(TaskState.CANCELLING)
        return True

    def continue_manual(self) -> bool:
        if self.state != TaskState.WAITING_FOR_USER:
            return False
        self.continue_calls += 1
        self.set_state(TaskState.RUNNING)
        return True

    def set_state(self, state: TaskState) -> None:
        self.state = state
        self.state_changed.emit(state)

    def finish(self, result: TaskResult) -> None:
        self.set_state(TaskState.COMPLETED)
        self.finished.emit(result)


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    workspace = tmp_path / "工作 空间"
    workspace.mkdir()
    scenario = workspace / "ab 场景.yaml"
    scenario.write_text("scenarioId: gui_ab\n", encoding="utf-8")
    baseline = workspace / "baseline.apk"
    first = workspace / "候选 一.apk"
    second = workspace / "候选 二.apk"
    for path in (baseline, first, second):
        path.write_bytes(b"apk")
    return workspace, scenario, baseline, first, second


def _configured_page(qtbot, tmp_path: Path) -> tuple[AbPage, FakeController, tuple[Path, ...]]:
    workspace, scenario, baseline, first, second = _workspace(tmp_path)
    controller = FakeController()
    builder = CommandBuilder(workspace, cli_program="perfeval-cli")
    page = AbPage(
        workspace=workspace,
        command_builder=builder,
        controller=controller,
        open_result=lambda _descriptor: True,
    )
    qtbot.addWidget(page)
    page.scenario_picker.set_path(str(scenario))
    page.output_picker.set_path("runs/对比 输出")
    page.repeat_spin.setValue(7)
    page.serial_edit.setText("emulator-5554")
    page.package_edit.setText("com.example.app")
    page.activity_edit.setText(".MainActivity")
    page.baseline_id_edit.setText("baseline")
    page.baseline_apk_picker.set_path(str(baseline))
    page.candidate_editor.rows[0].id_edit.setText("v2")
    page.candidate_editor.rows[0].apk_picker.set_path(str(first))
    page.candidate_editor.add_candidate("v3", str(second))
    return page, controller, (workspace, scenario, baseline, first, second)


def test_ab_page_builds_all_mvp_arguments_and_copyable_preview(qtbot, tmp_path: Path) -> None:
    page, _controller, paths = _configured_page(qtbot, tmp_path)
    workspace, scenario, baseline, first, second = paths
    page.advanced_toggle.setChecked(True)
    page.clear_data_combo.setCurrentIndex(2)
    page.allow_uninstall_checkbox.setChecked(True)
    page.perfetto_checkbox.setChecked(True)
    page.post_launch_wait_spin.setValue(3.5)
    page.interactive_checkbox.setChecked(True)

    task = page.refresh_preview()

    assert task is not None
    assert task.task_type == TaskType.AB
    assert task.working_directory == workspace
    assert task.expected_output_root == workspace / "runs" / "对比 输出"
    assert task.arguments[:2] == ("ab", str(scenario))
    assert ("--baseline-apk", str(baseline)) == _argument_pair(task, "--baseline-apk")
    assert _argument_values(task, "--candidate-apk") == [f"v2={first}", f"v3={second}"]
    assert _argument_pair(task, "--repeat") == ("--repeat", "7")
    assert _argument_pair(task, "--serial") == ("--serial", "emulator-5554")
    assert _argument_pair(task, "--package-name") == ("--package-name", "com.example.app")
    assert _argument_pair(task, "--activity") == ("--activity", ".MainActivity")
    assert "--no-clear-data" in task.arguments
    assert "--allow-uninstall" in task.arguments
    assert "--perfetto" in task.arguments
    assert _argument_pair(task, "--post-launch-wait") == ("--post-launch-wait", "3.5")
    assert "--interactive" in task.arguments
    assert "--gui-events" in task.arguments
    assert page.command_preview.toPlainText() == task.display_command
    assert page.copy_preview_button.isEnabled()
    assert not page.validation_banner.messages
    page.copy_preview_button.click()
    assert QGuiApplication.clipboard().text() == task.display_command


def test_ab_page_refuses_duplicate_ids_and_preserves_input(qtbot, tmp_path: Path) -> None:
    page, controller, _paths = _configured_page(qtbot, tmp_path)
    page.candidate_editor.rows[1].id_edit.setText("V2")

    task = page.refresh_preview()
    page.start_button.click()

    assert task is None
    assert controller.started_tasks == []
    assert any("重复" in message for message in page.validation_banner.messages)
    assert page.candidate_editor.rows[0].candidate_id == "v2"
    assert page.candidate_editor.rows[1].candidate_id == "V2"
    assert page.command_preview.toPlainText() == ""


def test_ab_page_refuses_missing_files_and_external_output(qtbot, tmp_path: Path) -> None:
    page, controller, paths = _configured_page(qtbot, tmp_path)
    workspace = paths[0]
    page.baseline_apk_picker.set_path(str(workspace / "missing.apk"))

    assert page.refresh_preview() is None
    page.start_button.click()
    assert controller.started_tasks == []
    assert any("文件不存在" in message for message in page.validation_banner.messages)

    page.baseline_apk_picker.set_path(str(paths[2]))
    page.output_picker.set_path(str(tmp_path / "outside"))
    assert page.refresh_preview() is None
    assert any("workspace" in message for message in page.validation_banner.messages)


def test_ab_page_executes_cancels_and_handles_manual_without_fake_progress(qtbot, tmp_path: Path) -> None:
    page, controller, _paths = _configured_page(qtbot, tmp_path)
    page.interactive_checkbox.setChecked(True)
    assert page.refresh_preview() is not None

    page.start_button.click()
    assert len(controller.started_tasks) == 1
    assert not page.start_button.isEnabled()
    assert page.cancel_button.isEnabled()
    assert "尚不可可靠判断" in page.variant_status_label.text()

    controller.set_state(TaskState.RUNNING)
    assert "未提供可靠的当前 variant" in page.variant_status_label.text()
    controller.set_state(TaskState.WAITING_FOR_USER)
    controller.manual_requested.emit(
        ManualRequest("请完成 v2 的手动步骤", timeout_seconds=12.0, source="event")
    )
    assert page.manual_frame.isVisibleTo(page)
    assert "12" in page.manual_label.text()
    assert "当前 variant 未由 CLI 可靠标识" in page.variant_status_label.text()

    page.continue_button.click()
    assert controller.continue_calls == 1
    assert controller.state == TaskState.RUNNING

    page.cancel_button.click()
    assert controller.cancel_calls == 1
    assert controller.state == TaskState.CANCELLING
    assert not page.cancel_button.isEnabled()


def test_ab_page_shows_reliable_final_candidate_status_and_result_entry(qtbot, tmp_path: Path) -> None:
    page, controller, paths = _configured_page(qtbot, tmp_path)
    workspace = paths[0]
    report = workspace / "runs" / "对比 输出" / "report" / "report.html"
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")
    descriptor = ResultDescriptor(
        type=ResultType.REPORT,
        label="A/B 总览",
        location=str(report),
        exists=True,
        viewer=ResultViewer.REPORT,
    )
    now = datetime.now(timezone.utc)
    result = TaskResult(
        task_id="ab-task",
        task_type=TaskType.AB,
        outcome=TaskOutcome.FAIL,
        exit_code=1,
        results=(descriptor,),
        started_at=now,
        ended_at=now,
        metadata={
            "result_envelope": {
                "schemaVersion": "perfeval.cli.event.v1",
                "type": "result",
                "command": "ab",
                "data": {
                    "status": "fail",
                    "comparisons": {
                        "v2": {"status": "warn"},
                        "v3": {"status": "fail"},
                    },
                },
            }
        },
    )
    opened: list[ResultDescriptor] = []
    page.result_open_requested.connect(opened.append)

    controller.finish(result)
    page.summary.open_result_requested.emit(descriptor)

    assert page.summary.result is result
    assert page.summary.outcome_label.text() == "fail"
    assert "v2=warn" in page.variant_status_label.text()
    assert "v3=fail" in page.variant_status_label.text()
    assert opened == [descriptor]
    assert page.start_button.isEnabled()


def test_ab_page_clear_restores_fixed_baseline_and_single_candidate(qtbot, tmp_path: Path) -> None:
    page, _controller, _paths = _configured_page(qtbot, tmp_path)

    page.clear_button.click()

    assert page.baseline_id_edit.text() == "baseline"
    assert page.baseline_apk_picker.path == ""
    assert page.candidate_editor.count == 1
    assert page.candidate_editor.rows[0].candidate_id == "candidate"
    assert page.output_picker.path == "runs/ab"
    assert "不推测当前 variant" in page.variant_status_label.text()


def _argument_pair(task: TaskSpec, flag: str) -> tuple[str, str]:
    index = task.arguments.index(flag)
    return task.arguments[index], task.arguments[index + 1]


def _argument_values(task: TaskSpec, flag: str) -> list[str]:
    return [
        task.arguments[index + 1]
        for index, value in enumerate(task.arguments)
        if value == flag
    ]
