"""ux(fix-1) 回归：完成后产出的报告区块必须位于操作按钮之下（页面最下方），
报告出现/刷新不得改变外层页面滚动位置。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QScrollArea

from perfeval.gui.controllers.task_controller import TaskController
from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.models.result import ResultDescriptor, ResultType, ResultViewer, TaskOutcome, TaskResult
from perfeval.gui.models.task import TaskSpec, TaskType
from perfeval.gui.pages.run_page import RunPage
from perfeval.gui.pages.task_page import TaskPage
from perfeval.gui.services.command_builder import CommandBuilder
from perfeval.gui.services.process_runner import ProcessRunner
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


# --- TaskPage：报告在操作按钮之下，外层滚动不被报告出现改变 ---


def _task_page(qtbot, tmp_path: Path) -> tuple[TaskPage, TaskSessionController, TaskSpec]:
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
        metadata={"scenario_title": "布局测试"},
        task_id="placement-task",
    )
    return page, session, task


def _report_result(count: int, tmp_path: Path) -> TaskResult:
    return TaskResult(
        task_id="placement-task",
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


def test_task_page_report_block_sits_below_action_buttons_after_finish(qtbot, tmp_path: Path) -> None:
    page, session, task = _task_page(qtbot, tmp_path)

    assert session.start(task)
    assert page.summary.isHidden()
    assert not page.action_stack.isHidden()

    with qtbot.waitSignal(session.controller.finished, timeout=5000):
        pass
    # 直接以构造好的结果驱动完成路径（含多条报告条目）
    session.controller.finished.emit(_report_result(3, tmp_path))
    qtbot.waitUntil(lambda: not page.summary.isHidden())

    # 报告区在两个操作按钮（开始新测试/再次运行）之下
    summary_y = page.summary.mapTo(page, QPoint(0, 0)).y()
    assert summary_y > page.new_test_button.mapTo(page, QPoint(0, 0)).y()
    assert summary_y > page.repeat_button.mapTo(page, QPoint(0, 0)).y()
    # 报告区是操作面板的最后一个区块；状态/进度区已收起
    panel_layout = page.action_panel.layout()
    assert panel_layout.indexOf(page.summary) == panel_layout.count() - 1
    assert page.action_stack.isHidden()


def test_task_page_outer_scroll_position_unchanged_when_report_appears(qtbot, tmp_path: Path) -> None:
    page, session, task = _task_page(qtbot, tmp_path)

    assert session.start(task)
    qtbot.waitUntil(lambda: page.scroll.verticalScrollBar().maximum() > 0)
    bar = page.scroll.verticalScrollBar()
    bar.setValue(min(40, bar.maximum()))
    value_before = bar.value()

    session.controller.finished.emit(_report_result(6, tmp_path))
    qtbot.waitUntil(lambda: not page.summary.isHidden())

    # 报告出现不改变外层页面滚动位置（任务日志自身滚动跟随不受此约束）
    assert page.scroll.verticalScrollBar().value() == value_before


# --- RunPage：报告 tab 始终位于操作按钮之下，报告刷新不挤动页面 ---


def _settings(tmp_path: Path, name: str) -> SettingsService:
    from PySide6.QtCore import QSettings

    backend = QSettings(str(tmp_path / f"{name}.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def _run_page(qtbot, tmp_path: Path) -> tuple[RunPage, object]:
    from tests.gui.test_run_page import FakeDeviceService, RecordingController

    settings = _settings(tmp_path, "placement-run")
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
    return page, controller


def test_run_page_report_tab_stays_below_action_buttons(qtbot, tmp_path: Path) -> None:
    page, controller = _run_page(qtbot, tmp_path)

    result = TaskResult(
        task_id="run-1",
        task_type=TaskType.RUN,
        outcome=TaskOutcome.COMPLETED,
        exit_code=0,
        results=(
            ResultDescriptor(
                type=ResultType.REPORT,
                label="HTML 报告",
                location=str(tmp_path / "report.html"),
                exists=True,
                viewer=ResultViewer.REPORT,
            ),
        ),
    )
    controller.publish_result(result)
    qtbot.waitUntil(lambda: page.task_summary.result is result)

    page_layout = page.layout
    # section_tabs（承载"结果"报告 tab）是页面最后一个区块，位于操作按钮行之下
    assert page_layout.indexOf(page.section_tabs) == page_layout.count() - 1
    assert page.start_button.mapTo(page, QPoint(0, 0)).y() < page.section_tabs.mapTo(page, QPoint(0, 0)).y()
    assert page.task_summary.mapTo(page, QPoint(0, 0)).y() > page.start_button.mapTo(page, QPoint(0, 0)).y()

    # 报告刷新（再次 finished、更多条目）不改变 section_tabs 的高度与页面几何
    tabs_height = page.section_tabs.height()
    refreshed = TaskResult(
        task_id="run-1",
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
            for index in range(8)
        ),
    )
    controller.publish_result(refreshed)
    qtbot.waitUntil(lambda: page.task_summary.result is refreshed)
    qtbot.waitUntil(lambda: page.task_summary.result_layout.count() > 1)

    assert page.section_tabs.height() == tabs_height
    assert page.task_summary.mapTo(page, QPoint(0, 0)).y() > page.start_button.mapTo(page, QPoint(0, 0)).y()


def test_run_page_config_scroll_not_scrolled_by_report(qtbot, tmp_path: Path) -> None:
    page, controller = _run_page(qtbot, tmp_path)

    result = TaskResult(
        task_id="run-1",
        task_type=TaskType.RUN,
        outcome=TaskOutcome.COMPLETED,
        exit_code=0,
        results=(
            ResultDescriptor(
                type=ResultType.REPORT,
                label="HTML 报告",
                location=str(tmp_path / "report.html"),
                exists=True,
                viewer=ResultViewer.REPORT,
            ),
        ),
    )
    config_scroll = page.findChild(QScrollArea, "runConfigScroll")
    assert config_scroll is not None

    controller.publish_result(result)
    qtbot.waitUntil(lambda: page.task_summary.result is result)

    # 外层（配置页）滚动位置不受报告出现影响
    assert config_scroll.verticalScrollBar().value() == 0
