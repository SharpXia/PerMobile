from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from perfeval.gui.models.task import ManualRequest, TaskState
from perfeval.gui.pages.diagnose_page import DiagnosePage
from perfeval.gui.services.command_builder import CommandBuilder
from perfeval.gui.services.platform_open import OpenResult, OpenStatus


class _FakeController(QObject):
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
        self.started_tasks = []
        self.continue_count = 0
        self.cancel_count = 0

    @property
    def is_active(self) -> bool:
        return self.state in {
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.WAITING_FOR_USER,
            TaskState.CANCELLING,
        }

    def start(self, task) -> bool:
        self.started_tasks.append(task)
        self.state = TaskState.RUNNING
        self.state_changed.emit(self.state)
        return True

    def continue_manual(self) -> bool:
        self.continue_count += 1
        self.state = TaskState.RUNNING
        self.state_changed.emit(self.state)
        return True

    def cancel(self) -> bool:
        self.cancel_count += 1
        self.state = TaskState.CANCELLING
        self.state_changed.emit(self.state)
        return True

    def reset(self) -> None:
        self.state = TaskState.IDLE
        self.state_changed.emit(self.state)

    def shutdown(self) -> bool:
        return True


class _FakePlatformOpen(QObject):
    open_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.opened = []

    def open_descriptor(self, descriptor, **_kwargs):
        self.opened.append(descriptor)
        return OpenResult(OpenStatus.OPENED, "opened", success=True)

    def open_static(self, descriptor):
        return self.open_descriptor(descriptor)

    def open_parent_directory(self, descriptor):
        return self.open_descriptor(descriptor)

    def shutdown(self) -> None:
        pass


def _page(qtbot, tmp_path: Path) -> tuple[DiagnosePage, _FakeController]:
    controller = _FakeController()
    builder = CommandBuilder(tmp_path, cli_program="perfeval-cli")
    page = DiagnosePage(
        workspace=tmp_path,
        command_builder=builder,
        controller=controller,
        platform_open=_FakePlatformOpen(),
    )
    qtbot.addWidget(page)
    page.show()
    return page, controller


def test_process_diagnose_page_maps_all_mvp_fields_and_preview(qtbot, tmp_path: Path) -> None:
    page, controller = _page(qtbot, tmp_path)
    page.package_edit.setText("com.example.app")
    page.serial_edit.setText("emulator-5554")
    page.output_picker.set_path("runs/诊断 输出")
    page.case_edit.setText("focus-case")
    page.mode_combo.setCurrentIndex(page.mode_combo.findData("full"))
    page.duration_spin.setValue(42)
    page.interactive_check.setChecked(True)
    page.memory_profile_combo.setCurrentIndex(page.memory_profile_combo.findData("heavy"))
    page.first_party_regex_edit.setText(r"^com\.example")

    assert not page.duration_spin.isHidden()
    assert "diagnose android" in page.command_preview.toPlainText()
    assert "--gui-events" in page.command_preview.toPlainText()
    page.start_button.click()

    assert len(controller.started_tasks) == 1
    task = controller.started_tasks[0]
    assert task.arguments == (
        "diagnose",
        "android",
        "--package-name",
        "com.example.app",
        "--out",
        str(tmp_path / "runs/诊断 输出"),
        "--case",
        "focus-case",
        "--mode",
        "full",
        "--duration",
        "42",
        "--memory-profile",
        "heavy",
        "--serial",
        "emulator-5554",
        "--interactive",
        "--first-party-regex",
        r"^com\.example",
        "--gui-events",
    )
    assert page.cancel_button.isEnabled()
    assert not page.start_button.isEnabled()


def test_process_diagnose_page_mode_visibility_and_validation(qtbot, tmp_path: Path) -> None:
    page, controller = _page(qtbot, tmp_path)

    assert page.mode == "frame"
    assert page.duration_spin.isHidden()
    page.mode_combo.setCurrentIndex(page.mode_combo.findData("cpu"))
    assert not page.duration_spin.isHidden()

    page.package_edit.setText("invalid package")
    page.start_button.click()
    assert controller.started_tasks == []
    assert "package_name" in page.validation_banner.messages[0]
    assert page.package_edit.text() == "invalid package"


def test_process_diagnose_page_manual_prompt_is_non_blocking_and_continues(qtbot, tmp_path: Path) -> None:
    page, controller = _page(qtbot, tmp_path)

    controller.state = TaskState.WAITING_FOR_USER
    controller.state_changed.emit(TaskState.WAITING_FOR_USER)
    controller.manual_requested.emit(
        ManualRequest("请完成聚焦操作后按 Enter", timeout_seconds=8, source="event")
    )

    assert page.task_console.manual_frame.isVisible()
    assert "8.0 秒" in page.task_console.manual_timeout_label.text()
    assert page.task_summary.manual_label.isVisible()
    page.task_console.manual_continue_button.click()

    assert controller.continue_count == 1
    assert not page.task_console.manual_frame.isVisible()
    assert not page.task_summary.manual_label.isVisible()

