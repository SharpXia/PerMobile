from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from perfeval import __version__

from .main_window import MainWindow
from .services.gui_logging import GuiLogService
from .services.packaging_paths import check_packaged_runtime, is_frozen_runtime
from .services.settings import SettingsService
from .services.workspace import WorkspaceService
from .theme import ThemeManager


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Return the process QApplication, creating it when necessary."""

    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        application = existing
    else:
        application = QApplication(list(argv) if argv is not None else sys.argv)
    application.setApplicationName("PerfEval Lite")
    application.setApplicationDisplayName("PerfEval Lite")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("PerfEval")
    application.setOrganizationDomain("perfeval.local")
    return application


def create_main_window(
    application: QApplication,
    *,
    settings: SettingsService | None = None,
    gui_log_service: GuiLogService | None = None,
    install_exception_hooks: bool = True,
) -> MainWindow:
    """Build the main window and its shared GUI services."""

    settings_service = settings or SettingsService()
    workspace_service = WorkspaceService(settings_service)
    log_service = gui_log_service or GuiLogService(settings_service, workspace_service)
    if install_exception_hooks:
        log_service.install_global_exception_hook()
    theme_manager = ThemeManager(application, settings_service)
    theme_manager.apply_saved_theme()
    window = MainWindow(
        settings=settings_service,
        workspace_service=workspace_service,
        theme_manager=theme_manager,
        gui_log_service=log_service,
    )
    log_service.bind_task_controller(window.task_session.controller, source="task")
    window.gui_log_service = log_service  # type: ignore[attr-defined]
    application.aboutToQuit.connect(log_service.shutdown)
    application.aboutToQuit.connect(window.interpretation_manager.shutdown)
    return window


def main(argv: Sequence[str] | None = None) -> int:
    application = create_application(argv)
    if is_frozen_runtime():
        runtime_check = check_packaged_runtime()
        if not runtime_check.ok:
            QMessageBox.critical(
                None,
                "PerfEval Lite 安装不完整",
                runtime_check.message,
            )
            return 2
    window = create_main_window(application)
    window.show()
    QTimer.singleShot(0, window.refresh_overview)
    smoke_exit_ms = _smoke_exit_delay_ms()
    if smoke_exit_ms is not None:
        QTimer.singleShot(smoke_exit_ms, application.quit)
    return application.exec()


def _smoke_exit_delay_ms() -> int | None:
    raw_value = os.environ.get("PERFEVAL_GUI_SMOKE_EXIT_MS")
    if raw_value is None:
        return None
    try:
        return max(0, min(30_000, int(raw_value)))
    except ValueError:
        return None
