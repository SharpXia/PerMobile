from __future__ import annotations

import json
import logging
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from perfeval.gui.pages.settings_page import SettingsPage
from perfeval.gui.services.gui_logging import GuiLogService, PRIVACY_NOTICE
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService


class FakeTaskController(QObject):
    started = Signal(object)
    finished = Signal(object)
    launch_failed = Signal(object)

    def __init__(self, output_root: Path) -> None:
        super().__init__()
        self.current_task = SimpleNamespace(
            task_id="task-1",
            task_type=SimpleNamespace(value="run"),
            expected_output_root=output_root,
        )


def _settings(tmp_path: Path) -> SettingsService:
    backend = QSettings(str(tmp_path / "logging.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def test_gui_logging_records_exception_rotates_and_exports_bounded_diagnostics(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    service = GuiLogService(settings, workspace, max_bytes=350, backup_count=2)
    try:
        for index in range(40):
            service.logger.info("日志行 %s %s", index, "x" * 60)
        try:
            raise ValueError("诊断异常 🚀")
        except ValueError as exc:
            service.record_exception(type(exc), exc, exc.__traceback__, context="test")

        result = service.export_diagnostics(
            tmp_path / "exports" / "diagnostics bundle",
            extra_context={"deviceSerial": "emulator-5554"},
        )

        assert result.path.suffix == ".zip"
        assert result.path.is_file()
        assert PRIVACY_NOTICE in result.privacy_notice
        assert len(list(service.log_directory.glob("perfeval-gui.log*"))) <= 3
        with zipfile.ZipFile(result.path) as archive:
            assert "diagnostics.json" in archive.namelist()
            metadata = json.loads(archive.read("diagnostics.json"))
            assert metadata["workspace"] == str(tmp_path.resolve())
            assert metadata["context"]["deviceSerial"] == "emulator-5554"
            assert "本地文件路径" in metadata["privacyNotice"]
            log_text = "\n".join(
                archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.startswith("logs/")
            )
            assert "诊断异常" in log_text
            assert "Traceback" in log_text
    finally:
        service.shutdown()


def test_exception_hooks_are_installable_and_restorable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    service = GuiLogService(settings, workspace)
    previous = sys.excepthook

    service.install_global_exception_hook()
    assert sys.excepthook is not previous

    service.restore_global_exception_hook()
    assert sys.excepthook is previous
    service.shutdown()


def test_gui_logging_falls_back_when_configured_directory_is_unusable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    unusable = tmp_path / "not-a-directory"
    unusable.write_text("file", encoding="utf-8")
    fallback = tmp_path / "application-data" / "logs"
    settings.log_directory = str(unusable)
    monkeypatch.setattr(
        GuiLogService,
        "fallback_log_directory",
        staticmethod(lambda: fallback),
    )

    service = GuiLogService(settings, workspace)
    try:
        assert service.log_directory == fallback
        assert service.current_log_path.is_file()
        assert "using fallback" in service.current_log_path.read_text(encoding="utf-8")
    finally:
        service.shutdown()


def test_gui_logging_disables_file_handler_if_fallback_is_unusable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    unusable = tmp_path / "not-a-directory"
    unusable.write_text("file", encoding="utf-8")
    settings.log_directory = str(unusable)
    monkeypatch.setattr(
        GuiLogService,
        "fallback_log_directory",
        staticmethod(lambda: unusable),
    )

    service = GuiLogService(settings, workspace)
    try:
        assert isinstance(service._handler, logging.NullHandler)
        service.logger.info("logging remains non-fatal")
    finally:
        service.shutdown()


def test_gui_logging_binds_task_lifecycle_without_copying_task_output(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    service = GuiLogService(settings, workspace)
    controller = FakeTaskController(tmp_path / "runs")
    service.bind_task_controller(controller, source="run")

    controller.started.emit(SimpleNamespace(pid=1234))
    controller.finished.emit(
        SimpleNamespace(
            task_id="task-1",
            outcome=SimpleNamespace(value="completed"),
            exit_code=0,
            duration_seconds=1.25,
            results=(1, 2),
        )
    )
    service._handler.flush()
    text = service.current_log_path.read_text(encoding="utf-8")

    assert "GUI task started" in text
    assert "task_id=task-1" in text
    assert "GUI task finished" in text
    assert "duration=1.25" in text
    service.shutdown()


def test_settings_page_persists_tools_changes_log_directory_and_exports_with_warning(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    service = GuiLogService(settings, workspace)
    page = SettingsPage(
        settings,
        workspace,
        diagnostics_service=service,
    )
    qtbot.addWidget(page)
    cli = tmp_path / "perfeval-cli"
    adb = tmp_path / "adb"
    trace_processor = tmp_path / "tools" / "trace_processor_shell"
    cli.write_text("cli", encoding="utf-8")
    adb.write_text("adb", encoding="utf-8")
    trace_processor.parent.mkdir()
    trace_processor.write_text("tp", encoding="utf-8")
    page.cli_picker.set_path(str(cli))
    page.adb_picker.set_path(str(adb))
    page.trace_processor_picker.set_path(str(trace_processor))
    page.save_tools_button.click()
    assert settings.external_tool_path("cli") == str(cli)
    assert settings.external_tool_path("adb") == str(adb)
    assert settings.external_tool_path("trace_processor") == str(trace_processor)

    custom_logs = tmp_path / "自定义 GUI logs"
    page.log_directory_picker.set_path(str(custom_logs))
    page.apply_log_directory_button.click()
    assert settings.log_directory == str(custom_logs.resolve())
    assert service.log_directory == custom_logs.resolve()

    export_path = tmp_path / "diagnostics.zip"
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(export_path), "ZIP archive (*.zip)"),
    )

    page.export_diagnostics_button.click()

    assert export_path.is_file()
    assert "分享前请检查" in page.validation_banner.messages[0]
    service.shutdown()


def test_settings_page_trace_processor_picker_saves_and_resets(
    qtbot,
    tmp_path: Path,
) -> None:
    """TASK-025：设置页 trace_processor 路径保存持久化，恢复默认偏好时一并清空。"""
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = SettingsPage(settings, workspace)
    qtbot.addWidget(page)
    trace_processor = tmp_path / "trace_processor_shell"
    trace_processor.write_text("tp", encoding="utf-8")

    page.trace_processor_picker.set_path(str(trace_processor))
    page.save_tools_button.click()

    assert settings.external_tool_path("trace_processor") == str(trace_processor)

    page.reset_button.click()

    assert settings.external_tool_path("trace_processor") is None
    assert page.trace_processor_picker.path == ""
    # 既有设置项读写不受新增项影响
    assert page.cli_picker.path == ""
    assert page.adb_picker.path == ""
