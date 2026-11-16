"""ux(fix-4b) 回归：GUI 人工步骤提示复用 scenario 文案原文（含步骤序号上下文）。

CLI 经 manual 事件把 scenario 的 prompt 原文透传给 GUI；文案中的
【人工步骤 k/N】即终端与 GUI 共用的步骤上下文，不得被 GUI 截断或改写。
"""

from __future__ import annotations

from pathlib import Path

from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.models.preset import PresetTaskKind, TaskLaunchConfig
from perfeval.gui.models.task import ManualRequest, TaskSpec, TaskType
from perfeval.gui.pages.run_page import RunPage
from perfeval.gui.pages.task_page import TaskPage
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from tests.gui.test_run_page import FakeDeviceService, RecordingController


PROMPT = (
    "【人工步骤 1/2】应用已由工具自动启动，采样正在后台记录。"
    "请在设备上手动导航到目标页面并停留；完成后回来按 Enter——"
    "工具将清空帧统计（gfxinfo reset）并进入滑动窗口。"
)


def _settings(tmp_path: Path) -> SettingsService:
    from PySide6.QtCore import QSettings

    backend = QSettings(str(tmp_path / "manual-prompt.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def test_run_page_manual_banner_shows_full_prompt_with_step_marker(qtbot, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    controller = RecordingController()
    page = RunPage(
        settings=settings,
        workspace_service=workspace,
        device_service=FakeDeviceService(),
        controller=controller,
    )
    qtbot.addWidget(page)
    page.resize(1000, 860)
    page.show()

    controller.manual_requested.emit(ManualRequest(prompt=PROMPT, timeout_seconds=60.0))

    assert page.manual_host.isVisible()
    # 文案原文透传（含【人工步骤 1/2】标记），只附加超时说明
    banner_text = " ".join(page.manual_banner.messages)
    assert PROMPT in banner_text
    assert "60" in banner_text


def test_task_page_manual_instruction_shows_full_prompt_with_step_marker(qtbot, tmp_path: Path) -> None:
    session = TaskSessionController()
    page = TaskPage(session=session)
    qtbot.addWidget(page)
    page.resize(900, 620)
    page.show()

    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program="perfeval",
        arguments=("run",),
        working_directory=tmp_path,
        display_command="perfeval run",
        expected_output_root=tmp_path,
        metadata={"scenario_title": "t"},
        task_id="manual-prompt-task",
    )
    launch = TaskLaunchConfig(
        preset_id="preset",
        title="页面流畅度",
        task_kind=PresetTaskKind.RUN,
        task=task,
        quick_mode=True,
    )
    session.launch_changed.emit(launch)
    session.controller.manual_requested.emit(ManualRequest(prompt=PROMPT, timeout_seconds=None))

    assert not page.action_stack.isHidden()
    assert PROMPT in page.instruction_label.text()
    assert "【人工步骤 1/2】" in page.log_output.toPlainText()
