from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from perfeval.gui.models.result import ResultDescriptor, ResultType, ResultViewer


class OpenStatus(str, Enum):
    OPENED = "opened"
    VIEWER_STARTED = "viewer_started"
    CONFIRM_LOCAL_SERVER = "confirm_local_server"
    CONFIRM_EXTERNAL_SITE = "confirm_external_site"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True)
class OpenResult:
    status: OpenStatus
    message: str
    success: bool = False

    @property
    def confirmation_required(self) -> bool:
        return self.status in {
            OpenStatus.CONFIRM_LOCAL_SERVER,
            OpenStatus.CONFIRM_EXTERNAL_SITE,
        }


class PlatformOpenService(QObject):
    """Safely open result descriptors without reusing the task ProcessRunner."""

    viewer_started = Signal(object)
    viewer_finished = Signal(object, int)
    open_failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        cli_program: str,
        cli_prefix_arguments: Sequence[str] = (),
        working_directory: str | Path | None = None,
        allowed_roots: Sequence[str | Path] = (),
        serve_seconds: int = 300,
        process_factory: Callable[[QObject], QProcess] | None = None,
    ) -> None:
        super().__init__(parent)
        if not cli_program.strip():
            raise ValueError("cli_program must be non-empty")
        if not 30 <= serve_seconds <= 3600:
            raise ValueError("serve_seconds must be between 30 and 3600")
        self.cli_program = cli_program
        self.cli_prefix_arguments = tuple(str(value) for value in cli_prefix_arguments)
        self.working_directory = Path(working_directory or Path.cwd()).expanduser().resolve()
        self.allowed_roots = tuple(
            Path(value).expanduser().resolve(strict=False) for value in allowed_roots
        ) or (self.working_directory,)
        self.serve_seconds = serve_seconds
        self._process_factory = process_factory or (lambda owner: QProcess(owner))
        self._viewer_processes: set[QProcess] = set()
        self._retired_processes: set[QProcess] = set()

    @property
    def active_viewer_count(self) -> int:
        return len(self._viewer_processes)

    def configure_cli(
        self,
        *,
        program: str,
        prefix_arguments: Sequence[str] = (),
        working_directory: str | Path | None = None,
    ) -> None:
        if not program.strip():
            raise ValueError("program must be non-empty")
        self.cli_program = program
        self.cli_prefix_arguments = tuple(str(value) for value in prefix_arguments)
        if working_directory is not None:
            self.working_directory = Path(working_directory).expanduser().resolve()

    def set_allowed_roots(self, roots: Sequence[str | Path]) -> None:
        resolved = tuple(Path(value).expanduser().resolve(strict=False) for value in roots)
        if not resolved:
            raise ValueError("at least one allowed root is required")
        self.allowed_roots = resolved

    def open_descriptor(
        self,
        descriptor: ResultDescriptor,
        *,
        confirm_local_server: bool = False,
        confirm_external_site: bool = False,
    ) -> OpenResult:
        if descriptor.type == ResultType.URL:
            return self._open_controlled_url(descriptor.location)
        checked = self._checked_path(descriptor)
        if isinstance(checked, OpenResult):
            return checked

        if descriptor.viewer in {ResultViewer.SYSTEM, ResultViewer.DIRECTORY}:
            return self._open_local_path(checked)
        if descriptor.viewer == ResultViewer.REPORT:
            if checked.suffix.casefold() not in {".html", ".htm"}:
                return self._failed("完整报告查看器只接受 HTML 报告。")
            if not confirm_local_server:
                return OpenResult(
                    OpenStatus.CONFIRM_LOCAL_SERVER,
                    "完整报告查看器将在 127.0.0.1 启动限时本地服务；也可选择静态打开。",
                )
            return self._start_viewer(descriptor, checked, "report")
        if descriptor.viewer == ResultViewer.PERFETTO:
            if not confirm_external_site:
                return OpenResult(
                    OpenStatus.CONFIRM_EXTERNAL_SITE,
                    "Perfetto UI 会启动 127.0.0.1 限时服务并访问 https://ui.perfetto.dev；可改为打开本地目录离线处理。",
                )
            return self._start_viewer(descriptor, checked, "perfetto-ui")
        if descriptor.viewer == ResultViewer.ANDROID_STUDIO:
            return self._start_viewer(descriptor, checked, "android-studio")
        return self._failed("该结果没有可用的打开方式。")

    def open_static(self, descriptor: ResultDescriptor) -> OpenResult:
        """Open a local report/artifact without starting a localhost viewer."""

        checked = self._checked_path(descriptor)
        if isinstance(checked, OpenResult):
            return checked
        return self._open_local_path(checked)

    def open_parent_directory(self, descriptor: ResultDescriptor) -> OpenResult:
        checked = self._checked_path(descriptor)
        if isinstance(checked, OpenResult):
            return checked
        target = checked if checked.is_dir() else checked.parent
        return self._open_local_path(target)

    def shutdown(self, *, kill_after_ms: int = 1500) -> None:
        for process in tuple(self._viewer_processes):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
        QTimer.singleShot(max(0, kill_after_ms), self._kill_remaining)

    def _checked_path(self, descriptor: ResultDescriptor) -> Path | OpenResult:
        path = descriptor.path
        if path is None:
            return self._failed("结果不是本地路径。")
        resolved = path.expanduser().resolve(strict=False)
        if not any(_is_relative_to(resolved, root) for root in self.allowed_roots):
            return self._failed("拒绝打开 workspace/授权目录之外的路径。", rejected=True)
        if not resolved.exists():
            return self._failed(f"路径不存在：{resolved}")
        if descriptor.type == ResultType.DIRECTORY and not resolved.is_dir():
            return self._failed(f"结果目录无效：{resolved}")
        return resolved

    def _open_local_path(self, path: Path) -> OpenResult:
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            return OpenResult(OpenStatus.OPENED, f"已打开：{path}", success=True)
        return self._failed(f"系统未能打开：{path}")

    def _open_controlled_url(self, location: str) -> OpenResult:
        url = QUrl(location)
        if not url.isValid() or url.scheme() not in {"http", "https"}:
            return self._failed("拒绝打开不受支持的 URL scheme。", rejected=True)
        if url.host() not in {"127.0.0.1", "localhost", "::1"}:
            return self._failed("只允许打开由 PerfEval 启动的本地 viewer URL。", rejected=True)
        if QDesktopServices.openUrl(url):
            return OpenResult(OpenStatus.OPENED, f"已打开：{location}", success=True)
        return self._failed(f"系统未能打开：{location}")

    def _start_viewer(
        self,
        descriptor: ResultDescriptor,
        path: Path,
        viewer: str,
    ) -> OpenResult:
        process = self._process_factory(self)
        process.setProgram(self.cli_program)
        process.setArguments(
            [
                *self.cli_prefix_arguments,
                "open",
                str(path),
                "--viewer",
                viewer,
                "--serve-seconds",
                str(self.serve_seconds),
            ]
        )
        process.setWorkingDirectory(str(self.working_directory))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.started.connect(lambda value=descriptor: self.viewer_started.emit(value))
        process.errorOccurred.connect(
            lambda _error, value=process: self._viewer_error(value)
        )
        process.finished.connect(
            lambda exit_code, _status, value=process, item=descriptor: self._viewer_finished(
                value, item, int(exit_code)
            )
        )
        self._viewer_processes.add(process)
        process.start()
        return OpenResult(
            OpenStatus.VIEWER_STARTED,
            f"已启动 {viewer} viewer：{path}",
            success=True,
        )

    def _viewer_error(self, process: QProcess) -> None:
        message = process.errorString()
        self.open_failed.emit(message)
        if process.state() == QProcess.ProcessState.NotRunning:
            self._viewer_processes.discard(process)
            self._retired_processes.add(process)

    def _viewer_finished(
        self,
        process: QProcess,
        descriptor: ResultDescriptor,
        exit_code: int,
    ) -> None:
        self._viewer_processes.discard(process)
        self._retired_processes.add(process)
        self.viewer_finished.emit(descriptor, exit_code)

    def _kill_remaining(self) -> None:
        for process in tuple(self._viewer_processes):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()

    def _failed(self, message: str, *, rejected: bool = False) -> OpenResult:
        self.open_failed.emit(message)
        return OpenResult(
            OpenStatus.REJECTED if rejected else OpenStatus.FAILED,
            message,
            success=False,
        )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
