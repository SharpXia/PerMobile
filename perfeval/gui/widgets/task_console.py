from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from perfeval.gui.models.task import LogChunk, LogStream, ManualRequest, TaskState


_STATE_LABELS = {
    TaskState.IDLE: "空闲",
    TaskState.VALIDATING: "正在校验",
    TaskState.INVALID_INPUT: "输入无效",
    TaskState.READY: "已就绪",
    TaskState.STARTING: "正在启动",
    TaskState.RUNNING: "运行中",
    TaskState.WAITING_FOR_USER: "等待人工操作",
    TaskState.CANCELLING: "正在取消",
    TaskState.COMPLETED: "已结束",
}


class TaskConsole(QWidget):
    """Bounded, sequence-preserving merged task log."""

    open_log_requested = Signal(str)
    manual_continue_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, max_lines: int = 2000) -> None:
        super().__init__(parent)
        if max_lines < 1:
            raise ValueError("max_lines must be positive")
        self._log_paths: tuple[Path, ...] = ()
        self._command_preview = ""
        self._manual_deadline: float | None = None

        self.status_label = QLabel(_STATE_LABELS[TaskState.IDLE], self)
        self.status_label.setObjectName("taskConsoleStatus")
        self.status_label.setAccessibleName("任务状态")
        self.elapsed_label = QLabel("00:00", self)
        self.elapsed_label.setAccessibleName("任务耗时")

        self.output = QPlainTextEdit(self)
        self.output.setObjectName("taskConsoleOutput")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.output.document().setMaximumBlockCount(max_lines)
        self.output.setAccessibleName("任务实时日志")
        self.output.setPlaceholderText("任务启动后将在这里显示 stdout 和 stderr。")

        self.manual_frame = QFrame(self)
        self.manual_frame.setObjectName("manualRequestFrame")
        self.manual_frame.setAccessibleName("人工步骤提示")
        manual_layout = QVBoxLayout(self.manual_frame)
        manual_layout.setContentsMargins(12, 10, 12, 10)
        self.manual_prompt_label = QLabel("", self.manual_frame)
        self.manual_prompt_label.setWordWrap(True)
        self.manual_timeout_label = QLabel("", self.manual_frame)
        self.manual_timeout_label.setObjectName("manualTimeoutLabel")
        manual_actions = QHBoxLayout()
        manual_actions.addStretch(1)
        self.manual_continue_button = QPushButton("已完成，继续", self.manual_frame)
        self.manual_continue_button.setAccessibleName("继续人工步骤")
        self.manual_continue_button.clicked.connect(self.manual_continue_requested.emit)
        manual_actions.addWidget(self.manual_continue_button)
        manual_layout.addWidget(self.manual_prompt_label)
        manual_layout.addWidget(self.manual_timeout_label)
        manual_layout.addLayout(manual_actions)
        self.manual_frame.hide()
        self._manual_timer = QTimer(self)
        self._manual_timer.setInterval(250)
        self._manual_timer.timeout.connect(self._update_manual_timeout)

        self.auto_scroll = QCheckBox("自动滚动", self)
        self.auto_scroll.setChecked(True)
        self.copy_button = QPushButton("复制可见日志", self)
        self.copy_button.clicked.connect(self.copy_visible)
        self.copy_command_button = QPushButton("复制命令", self)
        self.copy_command_button.setEnabled(False)
        self.copy_command_button.clicked.connect(self.copy_command)
        self.clear_button = QPushButton("清空可见区", self)
        self.clear_button.clicked.connect(self.clear_visible)
        self.open_log_button = QPushButton("打开完整日志", self)
        self.open_log_button.setEnabled(False)
        self.open_log_button.clicked.connect(self._request_open_log)

        header = QHBoxLayout()
        header.addWidget(QLabel("状态：", self))
        header.addWidget(self.status_label)
        header.addSpacing(16)
        header.addWidget(QLabel("耗时：", self))
        header.addWidget(self.elapsed_label)
        header.addStretch(1)

        actions = QGridLayout()
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(6)
        actions.addWidget(self.auto_scroll, 0, 0)
        actions.setColumnStretch(1, 1)
        actions.addWidget(self.copy_command_button, 0, 2)
        actions.addWidget(self.copy_button, 0, 3)
        actions.addWidget(self.clear_button, 1, 2)
        actions.addWidget(self.open_log_button, 1, 3)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.manual_frame)
        layout.addWidget(self.output, 1)
        layout.addLayout(actions)

    @property
    def max_lines(self) -> int:
        return self.output.document().maximumBlockCount()

    def bind_controller(self, controller: object) -> None:
        controller.state_changed.connect(self.set_state)  # type: ignore[attr-defined]
        controller.elapsed_changed.connect(self.set_elapsed)  # type: ignore[attr-defined]
        controller.log_chunk.connect(self.append_chunk)  # type: ignore[attr-defined]
        controller.manual_requested.connect(self.show_manual_request)  # type: ignore[attr-defined]
        controller.finished.connect(self._on_finished)  # type: ignore[attr-defined]
        self.manual_continue_requested.connect(controller.continue_manual)  # type: ignore[attr-defined]

    def set_state(self, state: TaskState) -> None:
        self.status_label.setText(_STATE_LABELS[state])
        self.status_label.setProperty("taskState", state.value)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        if state in {TaskState.RUNNING, TaskState.CANCELLING, TaskState.COMPLETED, TaskState.IDLE}:
            self.hide_manual_request()

    def show_manual_request(self, request: ManualRequest) -> None:
        self.manual_prompt_label.setText(request.prompt)
        self.manual_continue_button.setEnabled(True)
        if request.timeout_seconds is not None:
            self._manual_deadline = time.monotonic() + request.timeout_seconds
            self._manual_timer.start()
            self._update_manual_timeout()
        else:
            self._manual_deadline = None
            self._manual_timer.stop()
            self.manual_timeout_label.setText("等待时间由 CLI 控制；当前没有可靠倒计时。")
        self.manual_frame.show()
        self.manual_continue_button.setFocus()

    def hide_manual_request(self) -> None:
        self._manual_deadline = None
        self._manual_timer.stop()
        self.manual_frame.hide()

    def _update_manual_timeout(self) -> None:
        if self._manual_deadline is None:
            return
        remaining = max(0.0, self._manual_deadline - time.monotonic())
        if remaining <= 0:
            self.manual_timeout_label.setText("倒计时已到，等待 CLI 输出超时诊断并自动继续。")
            self._manual_timer.stop()
            return
        self.manual_timeout_label.setText(f"CLI 最长等待：{remaining:.1f} 秒")

    def set_elapsed(self, seconds: float) -> None:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds_value = divmod(remainder, 60)
        self.elapsed_label.setText(
            f"{hours:02d}:{minutes:02d}:{seconds_value:02d}"
            if hours
            else f"{minutes:02d}:{seconds_value:02d}"
        )

    def set_command_preview(self, command: str) -> None:
        self._command_preview = command
        self.copy_command_button.setEnabled(bool(command))
        self.copy_command_button.setToolTip(command)

    def set_log_paths(self, *paths: str | Path | None) -> None:
        self._log_paths = tuple(Path(path) for path in paths if path is not None)
        self.open_log_button.setEnabled(any(path.exists() for path in self._log_paths))

    def append_chunk(self, chunk: LogChunk) -> None:
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        prefix_format = QTextCharFormat()
        prefix_format.setForeground(self.palette().color(QPalette.ColorRole.Mid))
        cursor.insertText(f"[{chunk.sequence:06d}][{chunk.stream.value}] ", prefix_format)

        text_format = QTextCharFormat()
        if chunk.stream == LogStream.STDERR:
            text_format.setForeground(self.palette().color(QPalette.ColorRole.BrightText))
        cursor.insertText(chunk.text, text_format)
        if not chunk.text.endswith(("\n", "\r")):
            cursor.insertText("\n", text_format)
        self.output.setTextCursor(cursor)
        if self.auto_scroll.isChecked():
            self.output.ensureCursorVisible()

    def copy_visible(self) -> None:
        QGuiApplication.clipboard().setText(self.output.toPlainText())

    def copy_command(self) -> None:
        if self._command_preview:
            QGuiApplication.clipboard().setText(self._command_preview)

    def clear_visible(self) -> None:
        self.output.clear()

    def _request_open_log(self) -> None:
        existing = next((path for path in self._log_paths if path.exists()), None)
        if existing is not None:
            self.open_log_requested.emit(str(existing))

    def _on_finished(self, result: object) -> None:
        self.set_log_paths(
            getattr(result, "stdout_log_path", None),
            getattr(result, "stderr_log_path", None),
        )
