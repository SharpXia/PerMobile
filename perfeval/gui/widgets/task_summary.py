from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from perfeval.gui.models.result import (
    ResultDescriptor,
    TaskOutcome,
    ResultType,
    ResultViewer,
    TaskResult,
    outcome_suggestion,
)
from perfeval.gui.models.task import ManualRequest, TaskState
from perfeval.gui.models.llm import InterpretationSource
from perfeval.gui.services.llm_jobs import InterpretationJobManager
from perfeval.gui.services.llm_profiles import LlmProfileService
from .llm_interpretation_panel import LlmInterpretationPanel


class TaskSummary(QWidget):
    """Shared task outcome and result-entry panel."""

    open_result_requested = Signal(object)
    settings_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        llm_profiles: LlmProfileService | None = None,
        interpretation_manager: InterpretationJobManager | None = None,
        expand_results: bool = False,
    ) -> None:
        super().__init__(parent)
        self._result: TaskResult | None = None
        self._command_preview = ""
        self._failure_log_descriptor: ResultDescriptor | None = None
        self.llm_panel: LlmInterpretationPanel | None = None

        self.outcome_label = QLabel("尚未运行", self)
        self.outcome_label.setObjectName("taskSummaryOutcome")
        self.outcome_label.setAccessibleName("任务结果")
        self.exit_code_label = QLabel("—", self)
        self.duration_label = QLabel("—", self)
        self.suggestion_label = QLabel("", self)
        self.suggestion_label.setWordWrap(True)
        self.suggestion_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.suggestion_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.manual_label = QLabel("", self)
        self.manual_label.setObjectName("taskSummaryManual")
        self.manual_label.setWordWrap(True)
        self.manual_label.hide()

        self.summary_form = QFormLayout()
        self.summary_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.summary_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.summary_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.summary_form.setHorizontalSpacing(16)
        self.summary_form.setVerticalSpacing(10)
        self.summary_form.addRow("结果", self.outcome_label)
        self.summary_form.addRow("退出码", self.exit_code_label)
        self.summary_form.addRow("耗时", self.duration_label)
        self.summary_form.addRow("建议", self.suggestion_label)

        self.copy_command_button = QPushButton("复制等价命令", self)
        self.copy_command_button.setEnabled(False)
        self.copy_command_button.clicked.connect(self.copy_command)

        self.failure_frame = QFrame(self)
        self.failure_frame.setObjectName("taskFailureDetails")
        failure_layout = QVBoxLayout(self.failure_frame)
        failure_layout.setContentsMargins(14, 12, 14, 12)
        failure_layout.setSpacing(8)
        failure_title = QLabel("失败原因与报错日志", self.failure_frame)
        failure_title.setObjectName("sectionTitle")
        self.failure_output = QPlainTextEdit(self.failure_frame)
        self.failure_output.setObjectName("taskFailureOutput")
        self.failure_output.setAccessibleName("任务失败原因与报错日志")
        self.failure_output.setReadOnly(True)
        self.failure_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.failure_output.setFixedHeight(190)
        failure_actions = QHBoxLayout()
        failure_actions.addStretch(1)
        self.copy_failure_button = QPushButton("复制错误详情", self.failure_frame)
        self.copy_failure_button.clicked.connect(self.copy_failure_details)
        self.open_failure_log_button = QPushButton("打开完整错误日志", self.failure_frame)
        self.open_failure_log_button.clicked.connect(self._open_failure_log)
        failure_actions.addWidget(self.copy_failure_button)
        failure_actions.addWidget(self.open_failure_log_button)
        failure_layout.addWidget(failure_title)
        failure_layout.addWidget(self.failure_output)
        failure_layout.addLayout(failure_actions)
        self.failure_frame.hide()

        self.result_container = QWidget(self)
        self.result_layout = QVBoxLayout(self.result_container)
        self.result_layout.setContentsMargins(0, 0, 0, 0)
        self.result_layout.setSpacing(8)
        self.result_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.result_layout.addStretch(1)

        self.results_scroll: QScrollArea | None = None
        if not expand_results:
            self.results_scroll = QScrollArea(self)
            self.results_scroll.setWidgetResizable(True)
            self.results_scroll.setFrameShape(QFrame.NoFrame)
            self.results_scroll.setWidget(self.result_container)
            self.results_scroll.setAccessibleName("任务结果入口")

        layout = QVBoxLayout(self)
        layout.addLayout(self.summary_form)
        layout.addWidget(self.manual_label)
        layout.addWidget(self.copy_command_button, 0)
        layout.addWidget(self.failure_frame)
        if llm_profiles is not None and interpretation_manager is not None:
            self.llm_panel = LlmInterpretationPanel(
                llm_profiles,
                interpretation_manager,
                self,
            )
            self.llm_panel.settings_requested.connect(self.settings_requested)
            self.llm_panel.open_result_requested.connect(self.open_result_requested)
            layout.addWidget(self.llm_panel)
        if self.results_scroll is not None:
            layout.addWidget(self.results_scroll, 1)
        else:
            layout.addWidget(self.result_container)

    @property
    def result(self) -> TaskResult | None:
        return self._result

    def bind_controller(self, controller: object) -> None:
        controller.manual_requested.connect(self.show_manual_request)  # type: ignore[attr-defined]
        controller.state_changed.connect(self._on_state_changed)  # type: ignore[attr-defined]
        controller.finished.connect(self.set_result)  # type: ignore[attr-defined]

    def show_manual_request(self, request: ManualRequest) -> None:
        timeout = (
            f"（最长等待 {request.timeout_seconds:g} 秒）"
            if request.timeout_seconds is not None
            else "（无可靠倒计时）"
        )
        self.manual_label.setText(f"等待人工步骤：{request.prompt} {timeout}")
        self.manual_label.show()

    def _on_state_changed(self, state: TaskState) -> None:
        if state != TaskState.WAITING_FOR_USER:
            self.manual_label.hide()

    def set_command_preview(self, command: str) -> None:
        self._command_preview = command
        self.copy_command_button.setEnabled(bool(command))
        self.copy_command_button.setToolTip(command)

    def set_result(self, result: TaskResult) -> None:
        self._result = result
        self.manual_label.hide()
        self.outcome_label.setText(result.outcome.value)
        self.outcome_label.setProperty("taskOutcome", result.outcome.value)
        self.outcome_label.style().unpolish(self.outcome_label)
        self.outcome_label.style().polish(self.outcome_label)
        self.exit_code_label.setText("—" if result.exit_code is None else str(result.exit_code))
        self.duration_label.setText(_format_duration(result.duration_seconds))
        suggestion = outcome_suggestion(result.outcome)
        if result.metadata.get("result_not_found"):
            suggestion += " 未定位到本次任务的报告，可先打开输出目录。"
        self.suggestion_label.setText(suggestion)
        self.suggestion_label.updateGeometry()
        self.summary_form.invalidate()
        self._set_failure_details(result)
        self._replace_descriptors(result.results)

    def clear(self) -> None:
        self._result = None
        self.outcome_label.setText("尚未运行")
        self.outcome_label.setProperty("taskOutcome", "idle")
        self.outcome_label.style().unpolish(self.outcome_label)
        self.outcome_label.style().polish(self.outcome_label)
        self.exit_code_label.setText("—")
        self.duration_label.setText("—")
        self.suggestion_label.clear()
        self.manual_label.hide()
        self.failure_frame.hide()
        self.failure_output.clear()
        self._failure_log_descriptor = None
        self._replace_descriptors(())
        self.set_interpretation_source(None)

    def set_interpretation_source(self, source: InterpretationSource | None) -> None:
        if self.llm_panel is not None:
            self.llm_panel.set_source(source)

    def copy_command(self) -> None:
        if self._command_preview:
            QGuiApplication.clipboard().setText(self._command_preview)

    def copy_failure_details(self) -> None:
        text = self.failure_output.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)

    def _open_failure_log(self) -> None:
        if self._failure_log_descriptor is not None:
            self.open_result_requested.emit(self._failure_log_descriptor)

    def _set_failure_details(self, result: TaskResult) -> None:
        if result.outcome not in _FAILURE_OUTCOMES:
            self.failure_frame.hide()
            self.failure_output.clear()
            self._failure_log_descriptor = None
            return
        details, descriptor = _failure_details(result)
        self.failure_output.setPlainText(details)
        self.failure_output.verticalScrollBar().setValue(0)
        self.failure_output.horizontalScrollBar().setValue(0)
        self._failure_log_descriptor = descriptor
        self.copy_failure_button.setEnabled(bool(details))
        self.open_failure_log_button.setEnabled(descriptor is not None)
        self.failure_frame.show()

    def _replace_descriptors(self, descriptors: tuple[ResultDescriptor, ...]) -> None:
        while self.result_layout.count() > 1:
            item = self.result_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if descriptors:
            grouped: dict[str, list[ResultDescriptor]] = {}
            for descriptor in descriptors:
                grouped.setdefault(_descriptor_group(descriptor), []).append(descriptor)
            for group_name in (
                "报告",
                "LLM 解读",
                "Perfetto",
                "simpleperf / Android Studio",
                "结果与摘要",
                "原始目录",
                "日志",
                "其他 artifact",
            ):
                values = grouped.get(group_name, ())
                if not values:
                    continue
                header = QLabel(group_name, self.result_container)
                header.setObjectName("resultGroupTitle")
                self.result_layout.insertWidget(self.result_layout.count() - 1, header)
                for descriptor in values:
                    self._insert_descriptor_row(descriptor)
        else:
            # ux(fix-2)：清空时立即回收行占用的 sizeHint，不让已移除的
            # 报告行继续撑开外层布局。
            self.result_container.updateGeometry()
            self.updateGeometry()

    def _insert_descriptor_row(self, descriptor: ResultDescriptor) -> None:
        row = QWidget(self.result_container)
        row.setMinimumHeight(38)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        label = QLabel(
            f"{descriptor.label}: {descriptor.location}"
            + ("" if descriptor.exists else "（不存在）"),
            row,
        )
        label.setTextInteractionFlags(label.textInteractionFlags())
        label.setWordWrap(True)
        button = QPushButton("打开", row)
        button.setMinimumHeight(32)
        button.setEnabled(descriptor.exists)
        button.clicked.connect(
            lambda _checked=False, value=descriptor: self.open_result_requested.emit(value)
        )
        row_layout.addWidget(label, 1)
        row_layout.addWidget(button)
        self.result_layout.insertWidget(self.result_layout.count() - 1, row)
        self.result_container.updateGeometry()
        self.updateGeometry()


_FAILURE_OUTCOMES = {
    TaskOutcome.FAIL,
    TaskOutcome.INVALID,
    TaskOutcome.PARTIAL,
    TaskOutcome.TOOL_ERROR,
    TaskOutcome.LAUNCH_ERROR,
    TaskOutcome.UNEXPECTED_ERROR,
}


def _failure_details(result: TaskResult) -> tuple[str, ResultDescriptor | None]:
    sections: list[str] = []
    if result.process_error:
        sections.append(f"进程错误\n{result.process_error.strip()}")

    envelope = result.metadata.get("result_envelope")
    if isinstance(envelope, dict):
        data = envelope.get("data")
        if isinstance(data, dict):
            messages = [
                str(data[key]).strip()
                for key in ("error", "message", "reason", "detail")
                if isinstance(data.get(key), str) and str(data[key]).strip()
            ]
            if messages:
                sections.append("CLI 返回信息\n" + "\n".join(dict.fromkeys(messages)))

    descriptor: ResultDescriptor | None = None
    stderr_tail = _read_log_tail(result.stderr_log_path)
    if stderr_tail:
        sections.append("stderr（末尾）\n" + stderr_tail)
        descriptor = _log_descriptor(result, result.stderr_log_path, "stderr 完整日志")
    else:
        stdout_tail = _read_log_tail(result.stdout_log_path)
        if stdout_tail:
            sections.append("stdout（末尾）\n" + stdout_tail)
            descriptor = _log_descriptor(result, result.stdout_log_path, "stdout 完整日志")

    if not sections:
        sections.append("未捕获到可显示的错误文本。请打开结果报告或输出目录检查失败项。")
    return "\n\n".join(sections), descriptor


def _log_descriptor(
    result: TaskResult,
    path: object,
    label: str,
) -> ResultDescriptor | None:
    if path is None:
        return None
    from pathlib import Path

    resolved = Path(path)
    if not resolved.is_file():
        return None
    return ResultDescriptor(
        type=ResultType.LOG,
        label=label,
        location=str(resolved),
        exists=True,
        viewer=ResultViewer.SYSTEM,
        related_task_id=result.task_id,
    )


def _read_log_tail(path: object, *, max_bytes: int = 64 * 1024, max_lines: int = 120) -> str:
    if path is None:
        return ""
    from pathlib import Path

    value = Path(path)
    try:
        if not value.is_file():
            return ""
        with value.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            payload = handle.read(max_bytes)
    except OSError:
        return ""
    text = payload.decode("utf-8", errors="replace")
    if start:
        _, separator, remainder = text.partition("\n")
        text = remainder if separator else text
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        return "…（仅显示最后 120 行）\n" + "\n".join(lines).strip()
    prefix = "…（仅显示日志末尾）\n" if start else ""
    return prefix + "\n".join(lines).strip()


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds_value = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds_value:02d}"
    return f"{minutes:02d}:{seconds_value:02d}"


def _descriptor_group(descriptor: ResultDescriptor) -> str:
    if descriptor.type == ResultType.INTERPRETATION:
        return "LLM 解读"
    if descriptor.type == ResultType.REPORT:
        return "报告"
    if descriptor.viewer == ResultViewer.PERFETTO:
        return "Perfetto"
    if descriptor.viewer == ResultViewer.ANDROID_STUDIO:
        return "simpleperf / Android Studio"
    if descriptor.type in {ResultType.RESULT, ResultType.SUMMARY, ResultType.COMPARE}:
        return "结果与摘要"
    if descriptor.type == ResultType.DIRECTORY:
        return "原始目录"
    if descriptor.type == ResultType.LOG:
        return "日志"
    return "其他 artifact"
