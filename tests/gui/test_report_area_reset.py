"""ux(fix-2) 回归：重新运行开始时清空上一轮报告区并立即回收其占用空间。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings

from perfeval.gui.controllers.task_controller import TaskController
from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.models.preset import PresetTaskKind, TaskLaunchConfig
from perfeval.gui.models.result import ResultDescriptor, ResultType, ResultViewer, TaskOutcome, TaskResult
from perfeval.gui.models.task import TaskSpec, TaskType
from perfeval.gui.pages.run_page import RunPage
from perfeval.gui.pages.task_page import TaskPage
from perfeval.gui.services.command_builder import CommandBuilder
from perfeval.gui.services.process_runner import ProcessRunner
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def _settings(tmp_path: Path, name: str) -> SettingsService:
    backend = QSettings(str(tmp_path / f"{name}.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def _report_result(count: int, tmp_path: Path) -> TaskResult:
    return TaskResult(
        task_id="reset-task",
        task_type=TaskType.RUN,
        outcome=TaskOutcome.COMPLETED,
        exit_code=0,
        results=tuple(
            ResultDescriptor(
                type=ResultType.REPORT,
                label=f"HTML 报告 {index}",
                location=str(tmp_path / f"report-{index}.html"),
                exists=True,
                viewer=ResultViewer.REPORT,
            )
            for index in range(count)
        ),
    )


# --- TaskPage：再次运行（重复执行/新任务）恢复运行前高度分布 ---


def _task_page(qtbot, tmp_path: Path) -> tuple[TaskPage, TaskSessionController, TaskLaunchConfig]:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    controller = TaskController(runner=runner)
    session = TaskSessionController(controller=controller)
    page = TaskPage(session=session)
    qtbot.addWidget(page)
    page.resize(900, 620)
    page.show()
    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program=sys.executable,
        arguments=(
            str(FAKE_CLI),
            "exit",
            "--delay",
            "0",
            "--output-root",
            str(tmp_path / "output"),
            "--report-state",
            "exists",
        ),
        working_directory=tmp_path,
        display_command="fake-cli exit",
        expected_output_root=tmp_path / "output",
        metadata={"scenario_title": "空间回收测试"},
        task_id="reset-task",
    )
    launch = TaskLaunchConfig(
        preset_id="preset",
        title="页面流畅度",
        task_kind=PresetTaskKind.RUN,
        task=task,
        device_label="Fake_Pixel",
        package_name="com.example",
        repeat_count=1,
        quick_mode=True,
    )
    return page, session, launch


def test_task_page_relaunch_clears_report_and_reclaims_space(qtbot, tmp_path: Path) -> None:
    page, session, launch = _task_page(qtbot, tmp_path)

    session.launch_changed.emit(launch)
    qtbot.waitUntil(lambda: not page.log_group.isHidden())
    baseline_min_height = page.scroll_content.minimumSizeHint().height()

    session.controller.finished.emit(_report_result(8, tmp_path))
    qtbot.waitUntil(lambda: not page.summary.isHidden())
    qtbot.waitUntil(lambda: page.summary.result_layout.count() > 1)
    expanded_min_height = page.scroll_content.minimumSizeHint().height()
    assert expanded_min_height > baseline_min_height

    # 重新开始一次运行：报告区清空且其占用高度立即回收
    session.launch_changed.emit(launch)
    qtbot.waitUntil(lambda: page.summary.isHidden())
    qtbot.wait(20)  # 处理 deleteLater

    assert page.summary.result is None
    assert page.summary.outcome_label.text() == "尚未运行"
    assert page.summary.result_layout.count() == 1  # 仅剩尾部 stretch
    assert page.summary.result_container.sizeHint().height() == 0
    assert not page.action_stack.isHidden()
    assert page.scroll_content.minimumSizeHint().height() <= baseline_min_height


# --- RunPage：再次"开始"同样清空上一轮报告并回收空间 ---


def test_run_page_restart_clears_previous_report_rows(qtbot, tmp_path: Path) -> None:
    from tests.gui.test_run_page import FakeDeviceService, RecordingController

    settings = _settings(tmp_path, "reset-run")
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    controller = RecordingController()
    page = RunPage(
        settings=settings,
        workspace_service=workspace,
        device_service=FakeDeviceService(),
        controller=controller,
        builder_factory=lambda root: CommandBuilder(root, cli_program="perfeval", display_platform="posix"),
    )
    qtbot.addWidget(page)
    page.resize(1000, 860)
    page.show()

    scenario = tmp_path / "场景.yaml"
    scenario.write_text("scenario", encoding="utf-8")
    form = page.device_form
    form.scenario_picker.set_path(str(scenario))
    form.output_edit.setText("runs")
    form.serial_combo.setEditText("emulator-5554")

    task = page.start_task()
    assert task is not None

    result = _report_result(8, tmp_path)
    controller.publish_result(result)
    qtbot.waitUntil(lambda: page.task_summary.result is result)
    qtbot.waitUntil(lambda: page.task_summary.result_layout.count() > 1)
    expanded_hint = page.task_summary.result_container.sizeHint().height()
    assert expanded_hint > 0

    # 重新开始：_prepare_task 清空报告区，占用的 sizeHint 立即归零
    task_again = page.start_task()
    assert task_again is not None
    qtbot.wait(20)  # 处理 deleteLater

    assert page.task_summary.result is None
    assert page.task_summary.result_layout.count() == 1
    assert page.task_summary.result_container.sizeHint().height() == 0
