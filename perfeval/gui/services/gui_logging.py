from __future__ import annotations

import json
import logging
import platform
import sys
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping

from PySide6.QtCore import QObject, QStandardPaths, Signal

from perfeval import __version__

from .settings import SettingsService
from .workspace import WorkspaceService


PRIVACY_NOTICE = (
    "诊断包可能包含本地文件路径、workspace 名称、外部工具路径、设备标识和错误堆栈；"
    "导出后请在分享前检查内容。"
)


@dataclass(frozen=True)
class DiagnosticExportResult:
    path: Path
    files: tuple[str, ...]
    privacy_notice: str = PRIVACY_NOTICE


class GuiLogService(QObject):
    """Rolling GUI log, global exception capture, and bounded diagnostic export."""

    log_directory_changed = Signal(str)
    exception_recorded = Signal(str)

    def __init__(
        self,
        settings: SettingsService,
        workspace_service: WorkspaceService,
        parent: QObject | None = None,
        *,
        max_bytes: int = 1_000_000,
        backup_count: int = 3,
    ) -> None:
        super().__init__(parent)
        if max_bytes < 1 or backup_count < 1:
            raise ValueError("rolling log limits must be positive")
        self._settings = settings
        self._workspace_service = workspace_service
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handler: logging.Handler | None = None
        self._hooks_installed = False
        self._previous_sys_hook = None
        self._previous_thread_hook = None
        self._sys_hook = None
        self._thread_hook = None
        self._bound_controllers: set[int] = set()
        self.logger = logging.getLogger("perfeval.gui")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self._log_directory = self._initial_log_directory()
        self._configure_initial_handler(self._log_directory)
        self._workspace_service.workspace_changed.connect(self._workspace_changed)
        self.logger.info(
            "GUI logging initialized version=%s platform=%s python=%s workspace=%s",
            __version__,
            platform.platform(),
            platform.python_version(),
            self._workspace_service.current,
        )

    @property
    def log_directory(self) -> Path:
        return self._log_directory

    @property
    def current_log_path(self) -> Path:
        return self._log_directory / "perfeval-gui.log"

    @staticmethod
    def default_log_directory(workspace: str | Path) -> Path:
        return Path(workspace).expanduser().resolve(strict=False) / ".perfeval" / "gui-logs"

    @staticmethod
    def fallback_log_directory() -> Path:
        location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        if location:
            return Path(location).expanduser().resolve(strict=False) / "logs"
        return Path.home().expanduser().resolve(strict=False) / ".perfeval" / "gui-logs"

    def set_log_directory(self, path: str | Path | None, *, persist: bool = True) -> Path:
        target = (
            Path(path).expanduser().resolve(strict=False)
            if path and str(path).strip()
            else self.default_log_directory(self._workspace_service.current)
        )
        self._configure_handler(target)
        if persist:
            default = self.default_log_directory(self._workspace_service.current)
            self._settings.log_directory = None if target == default else str(target)
        self.log_directory_changed.emit(str(target))
        self.logger.info("GUI log directory changed path=%s", target)
        return target

    def install_global_exception_hook(self) -> None:
        if self._hooks_installed:
            return
        self._previous_sys_hook = sys.excepthook
        self._previous_thread_hook = getattr(threading, "excepthook", None)

        def sys_hook(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_traceback: TracebackType | None,
        ) -> None:
            self.record_exception(exc_type, exc_value, exc_traceback, context="main_thread")
            previous = self._previous_sys_hook
            if previous is not None and previous is not sys_hook:
                previous(exc_type, exc_value, exc_traceback)

        def thread_hook(args: threading.ExceptHookArgs) -> None:
            self.record_exception(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                context=f"thread:{getattr(args.thread, 'name', 'unknown')}",
            )
            previous = self._previous_thread_hook
            if previous is not None and previous is not thread_hook:
                previous(args)

        self._sys_hook = sys_hook
        self._thread_hook = thread_hook
        sys.excepthook = sys_hook
        threading.excepthook = thread_hook
        self._hooks_installed = True

    def restore_global_exception_hook(self) -> None:
        if not self._hooks_installed:
            return
        if sys.excepthook is self._sys_hook and self._previous_sys_hook is not None:
            sys.excepthook = self._previous_sys_hook
        if (
            getattr(threading, "excepthook", None) is self._thread_hook
            and self._previous_thread_hook is not None
        ):
            threading.excepthook = self._previous_thread_hook
        self._hooks_installed = False

    def record_exception(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
        *,
        context: str,
    ) -> None:
        self.logger.critical(
            "Unhandled GUI exception context=%s",
            context,
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        self.exception_recorded.emit(context)

    def bind_task_controller(self, controller: object, *, source: str) -> None:
        """Record task lifecycle metadata without copying task output into the GUI log."""

        identity = id(controller)
        if identity in self._bound_controllers:
            return
        self._bound_controllers.add(identity)
        controller.started.connect(  # type: ignore[attr-defined]
            lambda info: self._log_task_started(controller, source, info)
        )
        controller.finished.connect(  # type: ignore[attr-defined]
            lambda result: self._log_task_finished(source, result)
        )
        controller.launch_failed.connect(  # type: ignore[attr-defined]
            lambda message: self.logger.error(
                "GUI task launch failed source=%s error=%s",
                source,
                message,
            )
        )

    def export_diagnostics(
        self,
        destination: str | Path,
        *,
        extra_context: Mapping[str, Any] | None = None,
    ) -> DiagnosticExportResult:
        target = Path(destination).expanduser().resolve(strict=False)
        if target.suffix.casefold() != ".zip":
            target = target.with_suffix(".zip")
        target.parent.mkdir(parents=True, exist_ok=True)
        if self._handler is not None:
            self._handler.flush()

        metadata = {
            "schemaVersion": "perfeval.gui.diagnostics.v1",
            "createdAtUtc": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "workspace": str(self._workspace_service.current),
            "logDirectory": str(self._log_directory),
            "theme": self._settings.theme,
            "externalTools": {
                name: self._settings.external_tool_path(name)
                for name in ("cli", "adb", "android_studio")
            },
            "privacyNotice": PRIVACY_NOTICE,
            "context": dict(extra_context or {}),
        }
        archived: list[str] = ["diagnostics.json"]
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "diagnostics.json",
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
            )
            for path in sorted(self._log_directory.glob("perfeval-gui.log*")):
                if path.is_symlink() or not path.is_file():
                    continue
                name = f"logs/{path.name}"
                archive.write(path, name)
                archived.append(name)
        self.logger.info("GUI diagnostics exported path=%s files=%s", target, archived)
        return DiagnosticExportResult(path=target, files=tuple(archived))

    def shutdown(self) -> None:
        self.restore_global_exception_hook()
        if self._handler is not None:
            self._handler.flush()
            self._handler.close()
            self.logger.removeHandler(self._handler)
            self._handler = None

    def _initial_log_directory(self) -> Path:
        configured = self._settings.log_directory
        if configured:
            return Path(configured).expanduser().resolve(strict=False)
        return self.default_log_directory(self._workspace_service.current)

    def _configure_handler(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "perfeval-gui.log",
            maxBytes=self._max_bytes,
            backupCount=self._backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
            )
        )
        previous = self._handler
        self.logger.addHandler(handler)
        self._handler = handler
        self._log_directory = directory
        if previous is not None:
            previous.flush()
            previous.close()
            self.logger.removeHandler(previous)

    def _configure_initial_handler(self, directory: Path) -> None:
        primary_error: OSError | None = None
        try:
            self._configure_handler(directory)
            return
        except OSError as exc:
            primary_error = exc
            fallback = self.fallback_log_directory()

        try:
            self._configure_handler(fallback)
        except OSError as fallback_error:
            handler = logging.NullHandler()
            self.logger.addHandler(handler)
            self._handler = handler
            self._log_directory = fallback
            print(
                "PerfEval Lite: GUI file logging disabled; "
                f"requested={directory!s} error={primary_error!s}; "
                f"fallback={fallback!s} error={fallback_error!s}",
                file=sys.stderr,
            )
            return

        self.logger.warning(
            "GUI log directory unavailable; using fallback requested=%s fallback=%s error=%s",
            directory,
            fallback,
            primary_error,
        )

    def _workspace_changed(self, workspace: str) -> None:
        if self._settings.log_directory:
            return
        self.set_log_directory(self.default_log_directory(workspace), persist=False)

    def _log_task_started(self, controller: object, source: str, info: object) -> None:
        task = getattr(controller, "current_task", None)
        self.logger.info(
            "GUI task started source=%s task_id=%s task_type=%s pid=%s output_root=%s",
            source,
            getattr(task, "task_id", None),
            getattr(getattr(task, "task_type", None), "value", None),
            getattr(info, "pid", None),
            getattr(task, "expected_output_root", None),
        )

    def _log_task_finished(self, source: str, result: object) -> None:
        self.logger.info(
            "GUI task finished source=%s task_id=%s outcome=%s exit_code=%s duration=%s results=%s",
            source,
            getattr(result, "task_id", None),
            getattr(getattr(result, "outcome", None), "value", None),
            getattr(result, "exit_code", None),
            getattr(result, "duration_seconds", None),
            len(getattr(result, "results", ()) or ()),
        )
