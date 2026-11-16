from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from perfeval.gui.main_window import MainWindow
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.theme import ThemeManager


class FakeCloseController:
    def __init__(self, *, active: bool) -> None:
        self.is_active = active
        self.shutdown_calls = 0

    def shutdown(self) -> bool:
        self.shutdown_calls += 1
        self.is_active = False
        return True


def _window(qtbot, tmp_path, *, confirm_close=None):
    backend = QSettings(str(tmp_path / "close.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    settings = SettingsService(backend)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    window = MainWindow(
        settings=settings,
        workspace_service=workspace,
        theme_manager=ThemeManager(application, settings),
        confirm_close=confirm_close,
    )
    qtbot.addWidget(window)
    return window, settings


def _install_controller(window: MainWindow, *, active: bool) -> FakeCloseController:
    controller = FakeCloseController(active=active)
    window.task_session = controller  # type: ignore[assignment]
    return controller


def test_validation_close_rejection_keeps_window_and_tasks_running(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    window, settings = _window(qtbot, tmp_path, confirm_close=lambda _count: False)
    controller = _install_controller(window, active=True)
    event = QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    assert controller.shutdown_calls == 0
    assert settings.window_geometry is None


def test_validation_close_confirmation_shuts_down_every_active_controller(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    window, settings = _window(qtbot, tmp_path, confirm_close=lambda count: count == 1)
    window.show()
    controller = _install_controller(window, active=True)
    event = QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    assert controller.shutdown_calls == 1
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=2000)
    assert settings.window_geometry is not None
    assert settings.window_state is not None


def test_validation_inactive_window_closes_without_prompt_and_persists_state(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    window, settings = _window(qtbot, tmp_path)
    controller = _install_controller(window, active=False)

    def unexpected_prompt(*_args, **_kwargs):
        raise AssertionError("inactive window must not prompt")

    monkeypatch.setattr(QMessageBox, "question", unexpected_prompt)
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted()
    assert controller.shutdown_calls == 0
    assert settings.window_geometry is not None
    assert settings.window_state is not None
