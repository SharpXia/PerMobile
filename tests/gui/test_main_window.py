from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from perfeval.gui.main_window import MainWindow
from perfeval.gui.pages.history_page import HistoryPage
from perfeval.gui.pages.settings_page import SettingsPage
from perfeval.gui.pages.start_page import StartPage
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.theme import ThemeManager, ThemeMode


class QuietDeviceService(QObject):
    refresh_started = Signal()
    snapshot_ready = Signal(object)
    refresh_failed = Signal(str)
    refresh_finished = Signal()

    def __init__(self, _settings, _workspace, parent=None) -> None:
        super().__init__(parent)

    @property
    def is_refreshing(self) -> bool:
        return False

    def refresh(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _quiet_device_service(monkeypatch):
    monkeypatch.setattr("perfeval.gui.main_window.DeviceService", QuietDeviceService)


def _window(qtbot, tmp_path: Path) -> tuple[MainWindow, SettingsService]:
    backend = QSettings(str(tmp_path / "window-settings.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    settings = SettingsService(backend)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    theme = ThemeManager(application, settings)
    theme.apply(ThemeMode.SYSTEM)
    window = MainWindow(settings=settings, workspace_service=workspace, theme_manager=theme)
    qtbot.addWidget(window)
    window.show()
    return window, settings


def test_main_window_reaches_every_mvp_page_and_marks_current_page(qtbot, tmp_path: Path) -> None:
    window, _settings = _window(qtbot, tmp_path)

    assert window.page_ids == ("start", "task", "history", "environment", "settings")
    for page_id in window.page_ids:
        window.select_page(page_id)
        assert window.current_page_id == page_id
        assert window.page_stack.currentWidget() is window.page(page_id)
        assert "当前页面" in window.navigation_list.accessibleDescription()


def test_history_scans_once_at_startup_and_supports_manual_refresh(qtbot, tmp_path: Path) -> None:
    window, _settings = _window(qtbot, tmp_path)
    history = window.page("history")
    assert isinstance(history, HistoryPage)
    scan_started = QSignalSpy(history.scan_started)

    qtbot.waitUntil(
        lambda: scan_started.count() == 1 and not history.is_scanning,
        timeout=5000,
    )

    window.select_page("history")
    window.select_page("start")
    window.select_page("history")
    window.refresh_overview()
    window.task_session.result_changed.emit(None)
    QApplication.processEvents()

    assert scan_started.count() == 1
    assert not history.is_scanning

    with qtbot.waitSignal(history.scan_finished, timeout=5000):
        history.refresh_button.click()
        assert scan_started.count() == 2
        assert history.is_scanning
        assert history.loading_overlay.is_active

    assert not history.is_scanning


def test_start_page_recent_results_navigates_and_runs_related_history_query(
    qtbot, tmp_path: Path
) -> None:
    window, _settings = _window(qtbot, tmp_path)
    start = window.page("start")
    history = window.page("history")
    assert isinstance(start, StartPage)
    assert isinstance(history, HistoryPage)

    with qtbot.waitSignal(history.scan_finished, timeout=5000):
        start.quick_panel.recent_button.click()

    assert window.current_page_id == "history"
    assert history.related_query is not None
    assert history.related_query.preset_id == "builtin.smoothness"
    assert history.related_filter_bar.isVisibleTo(history)
    assert "未找到" in history.validation_banner.messages[0]


def test_workspace_and_theme_changes_update_shell_and_persist(qtbot, tmp_path: Path) -> None:
    window, settings = _window(qtbot, tmp_path)
    page = window.page("settings")
    assert isinstance(page, SettingsPage)
    target = tmp_path / "新的工作区"
    page.workspace_picker.set_path(str(target))

    page.apply_workspace_button.click()
    page.theme_combo.setCurrentIndex(page.theme_combo.findData(ThemeMode.DARK.value))

    assert target.is_dir()
    assert window.workspace_label.text() == str(target.resolve())
    assert settings.workspace == str(target.resolve())
    assert settings.theme == ThemeMode.DARK.value
    assert page.validation_banner.messages == ("workspace 已创建并可用。",)


def test_window_geometry_is_saved_on_close(qtbot, tmp_path: Path) -> None:
    window, settings = _window(qtbot, tmp_path)
    window.resize(960, 640)

    window.close()

    assert settings.window_geometry is not None
    assert settings.window_state is not None
