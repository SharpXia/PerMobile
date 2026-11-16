from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.models.preset import TaskLaunchConfig
from perfeval.gui.models.result import ResultDescriptor, TaskOutcome, TaskResult
from perfeval.gui.models.task import (
    LogChunk,
    ManualRequest,
    TaskPhaseEvent,
    TaskPhaseState,
    TaskState,
)
from perfeval.gui.widgets.task_summary import TaskSummary
from perfeval.gui.services.llm_artifacts import source_from_task
from perfeval.gui.services.llm_jobs import InterpretationJobManager
from perfeval.gui.services.llm_profiles import LlmProfileService

from .base_page import BasePage


_PHASE_PANEL_HEIGHT = 480
_LOG_PREVIEW_HEIGHT = 190


class PhaseRow(QFrame):
    def __init__(self, phase_id: str, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.phase_id = phase_id
        self.setObjectName("phaseRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        self.icon_label = QLabel("○", self)
        self.icon_label.setObjectName("phaseIcon")
        self.icon_label.setFixedWidth(24)
        self.label = QLabel(label, self)
        self.label.setObjectName("phaseLabel")
        self.detail = QLabel("", self)
        self.detail.setObjectName("mutedText")
        self.detail.setWordWrap(True)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.label)
        text.addWidget(self.detail)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text, 1)
        self.set_state(None)

    def set_state(self, state: TaskPhaseState | None, *, detail: str | None = None) -> None:
        self.setProperty("phaseState", state.value if state else "pending")
        self.icon_label.setText(
            {
                TaskPhaseState.STARTED: "▶",
                TaskPhaseState.COMPLETED: "✓",
                TaskPhaseState.SKIPPED: "—",
                TaskPhaseState.FAILED: "!",
                None: "○",
            }[state]
        )
        self.detail.setText(detail or ("当前步骤" if state is TaskPhaseState.STARTED else ""))
        self.style().unpolish(self)
        self.style().polish(self)


class TaskPage(BasePage):
    open_result_requested = Signal(object)
    open_log_requested = Signal(str)
    navigation_requested = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        session: TaskSessionController,
        llm_profiles: LlmProfileService | None = None,
        interpretation_manager: InterpretationJobManager | None = None,
    ) -> None:
        super().__init__("任务", "查看当前测试进度，并在需要时完成设备上的操作。", parent)
        header_item = self.layout.takeAt(0)
        header = header_item.widget() if header_item is not None else None
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("taskPageScroll")
        self.scroll.setAccessibleName("任务页面内容")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_content = QWidget(self.scroll)
        self.scroll_content.setObjectName("taskPageScrollContent")
        self.content_layout = QVBoxLayout(self.scroll_content)
        self.content_layout.setContentsMargins(32, 26, 32, 26)
        self.content_layout.setSpacing(18)
        self.content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        if header is not None:
            header.setParent(self.scroll_content)
            self.content_layout.addWidget(header)
        self.scroll.setWidget(self.scroll_content)
        self.layout.addWidget(self.scroll, 1)
        self.session = session
        self._launch: TaskLaunchConfig | None = None
        self._phase_rows: OrderedDict[str, PhaseRow] = OrderedDict()

        self.context_label = QLabel("当前没有运行中的任务。", self.scroll_content)
        self.context_label.setObjectName("taskContextLine")
        self.context_label.setWordWrap(True)
        self.content_layout.addWidget(self.context_label)

        self.body = QFrame(self.scroll_content)
        self.body.setObjectName("taskBody")
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 8, 0, 0)
        body_layout.setSpacing(22)
        body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.phase_panel = QFrame(self.body)
        self.phase_panel.setObjectName("phasePanel")
        self.phase_panel.setMinimumWidth(220)
        self.phase_panel.setMaximumWidth(285)
        self.phase_panel.setFixedHeight(_PHASE_PANEL_HEIGHT)
        self.phase_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        phase_layout = QVBoxLayout(self.phase_panel)
        phase_layout.setContentsMargins(20, 18, 20, 18)
        phase_kicker = QLabel("测试进度", self.phase_panel)
        phase_kicker.setObjectName("phaseKicker")
        phase_layout.addWidget(phase_kicker)
        phase_title = QLabel("测试进度", self.phase_panel)
        phase_title.setObjectName("sectionTitle")
        phase_layout.addWidget(phase_title)
        self.phase_host = QWidget(self.phase_panel)
        self.phase_rows_layout = QVBoxLayout(self.phase_host)
        self.phase_rows_layout.setContentsMargins(0, 8, 0, 0)
        self.phase_rows_layout.setSpacing(5)
        phase_layout.addWidget(self.phase_host)
        phase_layout.addStretch(1)
        self.phase_fallback = QLabel("启动任务后将显示 CLI 提供的可靠阶段。", self.phase_panel)
        self.phase_fallback.setObjectName("mutedText")
        self.phase_fallback.setWordWrap(True)
        phase_layout.addWidget(self.phase_fallback)
        body_layout.addWidget(self.phase_panel, 0, Qt.AlignmentFlag.AlignTop)

        self.action_panel = QFrame(self.body)
        self.action_panel.setObjectName("taskActionPanel")
        self.action_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        action_layout = QVBoxLayout(self.action_panel)
        action_layout.setContentsMargins(28, 24, 28, 22)
        action_layout.setSpacing(14)

        self.action_stack = QStackedWidget(self.action_panel)
        self.action_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        active = QWidget(self.action_stack)
        active_layout = QVBoxLayout(active)
        active_layout.setContentsMargins(0, 0, 0, 0)
        active_layout.setSpacing(12)
        self.state_title = QLabel("尚未开始测试", active)
        self.state_title.setObjectName("taskStateTitle")
        self.state_title.setWordWrap(True)
        self.instruction_label = QLabel("请前往“开始测试”选择预置场景。", active)
        self.instruction_label.setObjectName("taskInstruction")
        self.instruction_label.setWordWrap(True)
        self.supporting_label = QLabel("", active)
        self.supporting_label.setObjectName("pageDescription")
        self.supporting_label.setWordWrap(True)
        active_layout.addWidget(self.state_title)
        active_layout.addWidget(self.instruction_label)
        active_layout.addWidget(self.supporting_label)

        divider = QFrame(active)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("subtleDivider")
        active_layout.addWidget(divider)

        metadata = QGridLayout()
        metadata.setHorizontalSpacing(28)
        metadata.setVerticalSpacing(12)
        self.scenario_value = QLabel("—", active)
        self.device_value = QLabel("—", active)
        self.package_value = QLabel("—", active)
        self.output_value = QLabel("—", active)
        for row, (label, value) in enumerate(
            (
                ("场景", self.scenario_value),
                ("设备", self.device_value),
                ("应用", self.package_value),
                ("输出", self.output_value),
            )
        ):
            caption = QLabel(label, active)
            caption.setObjectName("taskMetaCaption")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            metadata.addWidget(caption, row, 0)
            metadata.addWidget(value, row, 1)
        active_layout.addLayout(metadata)

        self.manual_actions = QVBoxLayout()
        self.continue_button = QPushButton("已准备好，继续", active)
        self.continue_button.setObjectName("primaryActionButton")
        self.continue_button.setMinimumHeight(48)
        self.continue_button.clicked.connect(self.session.continue_manual)
        self.cancel_button = QPushButton("取消测试", active)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self.session.cancel)
        self.manual_actions.addWidget(self.continue_button)
        self.manual_actions.addWidget(self.cancel_button)
        active_layout.addLayout(self.manual_actions)
        active_layout.addStretch(1)

        self.summary = TaskSummary(
            self.action_panel,
            llm_profiles=llm_profiles,
            interpretation_manager=interpretation_manager,
            expand_results=True,
        )
        self.summary.bind_controller(session.controller)
        self.summary.open_result_requested.connect(self.open_result_requested)
        self.summary.settings_requested.connect(lambda: self.navigation_requested.emit("settings"))
        self.action_stack.addWidget(active)
        action_layout.addWidget(self.action_stack)

        self.log_group = QGroupBox("实时活动与日志", self.action_panel)
        self.log_group.setObjectName("taskLogGroup")
        self.log_group.setCheckable(True)
        self.log_group.setChecked(True)
        log_layout = QVBoxLayout(self.log_group)
        self.log_output = QPlainTextEdit(self.log_group)
        self.log_output.setObjectName("taskLogPreview")
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(1000)
        self.log_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_output.setFixedHeight(_LOG_PREVIEW_HEIGHT)
        self.log_output.setPlaceholderText("任务启动后将在这里显示状态、阶段、stdout 和 stderr。")
        log_actions = QHBoxLayout()
        log_actions.addStretch(1)
        self.open_log_button = QPushButton("打开完整日志", self.log_group)
        self.open_log_button.setEnabled(False)
        self.open_log_button.clicked.connect(self._open_log)
        log_actions.addWidget(self.open_log_button)
        log_layout.addWidget(self.log_output)
        log_layout.addLayout(log_actions)
        self.log_group.toggled.connect(self.log_output.setVisible)
        self.log_group.toggled.connect(self.open_log_button.setVisible)
        action_layout.addWidget(self.log_group)

        # ux(fix-1)：报告区块固定在操作按钮之下、页面最下方——不得插在
        # 实时进度（阶段面板/实时日志）与操作按钮之间。
        bottom_actions = QHBoxLayout()
        self.repeat_button = QPushButton("再次运行", self.action_panel)
        self.repeat_button.clicked.connect(self._repeat)
        self.new_test_button = QPushButton("开始新测试", self.action_panel)
        self.new_test_button.clicked.connect(lambda: self.navigation_requested.emit("start"))
        bottom_actions.addStretch(1)
        bottom_actions.addWidget(self.new_test_button)
        bottom_actions.addWidget(self.repeat_button)
        self.repeat_button.hide()
        self.new_test_button.hide()
        action_layout.addLayout(bottom_actions)

        action_layout.addWidget(self.summary)
        self.summary.hide()

        body_layout.addWidget(self.action_panel, 1, Qt.AlignmentFlag.AlignTop)
        self.content_layout.addWidget(self.body)

        session.launch_changed.connect(self._on_launch)
        session.phases_reset.connect(self._reset_phases)
        session.phase_changed.connect(self._on_phase)
        session.controller.state_changed.connect(self._on_state)
        session.controller.manual_requested.connect(self._on_manual)
        session.controller.log_chunk.connect(self._on_log)
        session.controller.finished.connect(self._on_finished)
        self._set_idle()

    @property
    def is_active(self) -> bool:
        return self.session.is_active

    def shutdown(self) -> bool:
        return self.session.shutdown()

    def _on_launch(self, launch: TaskLaunchConfig) -> None:
        self._launch = launch
        self.title_label.setText(f"{launch.title}测试")
        self.title_label.setAccessibleName(f"当前页面：{launch.title}测试")
        self.action_stack.show()
        # ux(fix-2)：重新运行开始即清空上一轮报告并回收其占用的布局空间，
        # 页面高度分布恢复到运行前；残留行会让 QStackedWidget 的
        # max(sizeHint) 撑高整页且不会被后续 set_result 自动收回。
        self.summary.clear()
        self.summary.hide()
        self.log_output.clear()
        self.log_group.show()
        self.log_group.setChecked(True)
        self.repeat_button.hide()
        self.new_test_button.hide()
        self.context_label.setText(self._context(launch))
        self.scenario_value.setText(launch.title)
        self.device_value.setText(launch.device_label or launch.device_serial or "自动选择")
        self.package_value.setText(launch.package_name or "场景默认")
        self.output_value.setText(_relative_output(launch.task.working_directory, launch.task.expected_output_root))
        self.state_title.setText("正在启动测试")
        self.instruction_label.setText("正在校验任务并启动 PerfEval CLI。")
        self.supporting_label.setText("任务进度将使用 CLI 提供的真实阶段更新。")
        self.continue_button.hide()
        self.cancel_button.show()
        self.summary.set_interpretation_source(None)
        self._append_activity("系统", f"已提交“{launch.title}”任务，正在启动 PerfEval CLI。")

    def _reset_phases(self, values: object) -> None:
        while self.phase_rows_layout.count():
            item = self.phase_rows_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._phase_rows.clear()
        for phase_id, label in values if isinstance(values, (tuple, list)) else ():
            row = PhaseRow(str(phase_id), str(label), self.phase_host)
            self._phase_rows[str(phase_id)] = row
            self.phase_rows_layout.addWidget(row)
        self.phase_fallback.setVisible(not bool(self._phase_rows))

    def _on_phase(self, event: TaskPhaseEvent) -> None:
        row = self._phase_rows.get(event.phase_id)
        if row is None:
            row = PhaseRow(event.phase_id, event.label, self.phase_host)
            self._phase_rows[event.phase_id] = row
            self.phase_rows_layout.addWidget(row)
        detail_parts = []
        if event.variant_id:
            detail_parts.append(event.variant_id)
        if event.repeat_index and event.repeat_count:
            detail_parts.append(f"第 {event.repeat_index}/{event.repeat_count} 次")
        if event.detail:
            detail_parts.append(event.detail)
        row.set_state(event.state, detail=" · ".join(detail_parts) or None)
        self.phase_fallback.hide()
        detail = " · ".join(detail_parts)
        state_label = {
            TaskPhaseState.STARTED: "开始",
            TaskPhaseState.COMPLETED: "完成",
            TaskPhaseState.SKIPPED: "跳过",
            TaskPhaseState.FAILED: "失败",
        }[event.state]
        self._append_activity("阶段", f"{event.label}：{state_label}" + (f" · {detail}" if detail else ""))
        if event.state is TaskPhaseState.STARTED and self.session.state is not TaskState.WAITING_FOR_USER:
            self.state_title.setText(event.label)
            self.instruction_label.setText(event.detail or f"正在{event.label}，请保持设备连接。")
            self.supporting_label.setText("只展示 CLI 已确认的阶段，不估算百分比或剩余时间。")

    def _on_state(self, state: TaskState) -> None:
        if self._launch is not None:
            self._append_activity(
                "状态",
                {
                    TaskState.STARTING: "正在启动",
                    TaskState.RUNNING: "运行中",
                    TaskState.WAITING_FOR_USER: "等待人工操作",
                    TaskState.CANCELLING: "正在取消",
                    TaskState.COMPLETED: "任务进程已结束",
                }.get(state, state.value),
            )
        self.cancel_button.setEnabled(state in {TaskState.STARTING, TaskState.RUNNING, TaskState.WAITING_FOR_USER})
        if state is TaskState.CANCELLING:
            self.state_title.setText("正在取消测试")
            self.instruction_label.setText("正在等待采集进程安全结束，已生成的日志和部分产物会保留。")
            self.continue_button.hide()
        elif state is TaskState.RUNNING:
            self.continue_button.hide()
        elif state is TaskState.COMPLETED and not self.session.phase_events_received:
            self.phase_fallback.setText("当前 CLI 未提供阶段事件；最终结果和日志仍然可靠。")
            self.phase_fallback.show()

    def _on_manual(self, request: ManualRequest) -> None:
        self._append_activity("人工操作", request.prompt)
        self.action_stack.show()
        self.state_title.setText("等待你的操作")
        self.instruction_label.setText(request.prompt)
        self.supporting_label.setText(
            f"CLI 最长等待 {request.timeout_seconds:g} 秒；超时行为由 CLI 控制。"
            if request.timeout_seconds is not None
            else "完成设备上的操作后点击继续；当前没有可伪造的倒计时。"
        )
        self.continue_button.show()
        self.continue_button.setFocus()

    def _on_log(self, chunk: LogChunk) -> None:
        text = chunk.text.rstrip("\r\n")
        if not text:
            return
        lines = text.splitlines() or [text]
        for line in lines:
            self.log_output.appendPlainText(
                f"[{chunk.sequence:06d}][{chunk.stream.value}] {line}"
            )
        self.log_output.ensureCursorVisible()

    def _on_finished(self, result: TaskResult) -> None:
        # ux(fix-1)：结束后报告区在操作按钮之下展开；状态区收起，避免报告
        # 出现在实时进度与操作按钮之间。
        self.action_stack.hide()
        self.summary.show()
        self.summary.updateGeometry()
        self.repeat_button.setVisible(self.session.last_launch is not None)
        self.new_test_button.show()
        preferred_paths = (
            (result.stderr_log_path, result.stdout_log_path)
            if result.outcome in {
                TaskOutcome.FAIL,
                TaskOutcome.INVALID,
                TaskOutcome.PARTIAL,
                TaskOutcome.TOOL_ERROR,
                TaskOutcome.LAUNCH_ERROR,
                TaskOutcome.UNEXPECTED_ERROR,
            }
            else (result.stdout_log_path, result.stderr_log_path)
        )
        paths = [path for path in preferred_paths if path is not None and path.exists()]
        self.open_log_button.setProperty("paths", [str(path) for path in paths])
        self.open_log_button.setEnabled(bool(paths))
        self._append_activity("结果", f"{result.outcome.value}，退出码：{result.exit_code if result.exit_code is not None else '—'}")
        if result.process_error:
            self._append_activity("错误", result.process_error)
        launch = self.session.last_launch
        if launch is not None:
            try:
                self.summary.set_interpretation_source(source_from_task(launch, result))
            except Exception:
                self.summary.set_interpretation_source(None)

    def _repeat(self) -> None:
        try:
            self.session.repeat_last()
        except Exception as exc:
            self.action_stack.show()
            self.summary.hide()
            self.state_title.setText("无法再次运行")
            self.instruction_label.setText(str(exc))

    def _open_log(self) -> None:
        values = self.open_log_button.property("paths") or []
        if values:
            self.open_log_requested.emit(str(values[0]))

    def _set_idle(self) -> None:
        self.continue_button.hide()
        self.cancel_button.hide()
        self.log_group.hide()

    def _append_activity(self, category: str, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        self.log_output.appendPlainText(f"[{category}] {text}")
        self.log_output.ensureCursorVisible()

    @staticmethod
    def _context(launch: TaskLaunchConfig) -> str:
        values = [launch.device_label or launch.device_serial, launch.package_name]
        if launch.repeat_count:
            values.append(f"{launch.repeat_count} 次")
        return " · ".join(value for value in values if value) or launch.title


def _relative_output(workspace: Path, output: Path) -> str:
    try:
        return f"当前工作区 / {output.relative_to(workspace)}"
    except ValueError:
        return str(output)


__all__ = ["PhaseRow", "TaskPage"]
