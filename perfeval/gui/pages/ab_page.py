from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from perfeval.gui.controllers.task_controller import TaskController
from perfeval.gui.models.result import ResultDescriptor, ResultType, TaskResult
from perfeval.gui.models.task import ManualRequest, TaskSpec, TaskState
from perfeval.gui.services.command_builder import CommandBuilder, CommandValidationError
from perfeval.gui.services.platform_open import OpenResult, PlatformOpenService
from perfeval.gui.widgets.candidate_editor import CandidateEditor
from perfeval.gui.widgets.path_picker import PathPicker, PathPickerMode
from perfeval.gui.widgets.task_console import TaskConsole
from perfeval.gui.widgets.task_summary import TaskSummary
from perfeval.gui.widgets.validation_banner import ValidationBanner, ValidationSeverity

from .base_page import BasePage


_ACTIVE_STATES = {
    TaskState.STARTING,
    TaskState.RUNNING,
    TaskState.WAITING_FOR_USER,
    TaskState.CANCELLING,
}


class AbPage(BasePage):
    """Baseline plus ordered multi-candidate A/B workflow."""

    result_open_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        workspace: str | Path | None = None,
        command_builder: CommandBuilder | None = None,
        controller: TaskController | None = None,
        platform_open: PlatformOpenService | None = None,
        open_result: Callable[[ResultDescriptor], bool] | None = None,
    ) -> None:
        super().__init__(
            "A/B 对比",
            "配置一个固定 baseline 和 1–4 个有序 candidate，顺序采集后生成对比总览。",
            parent,
        )
        self._workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        self.command_builder = command_builder or CommandBuilder(self._workspace)
        self._workspace = self.command_builder.workspace
        self.controller = controller or TaskController(self)
        self.platform_open = platform_open or PlatformOpenService(
            self,
            cli_program=self.command_builder.program,
            cli_prefix_arguments=self.command_builder.prefix_arguments,
            working_directory=self._workspace,
            allowed_roots=(self._workspace,),
        )
        self._open_result_handler = open_result
        self._last_task: TaskSpec | None = None
        self._last_result: TaskResult | None = None

        self._build_form()
        self._bind_controller()
        self._connect_form_signals()
        self._refresh_preview(show_errors=False)

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def is_active(self) -> bool:
        return self.controller.is_active

    def shutdown(self) -> bool:
        self.platform_open.shutdown()
        return self.controller.shutdown()

    def set_workspace(
        self,
        workspace: str | Path,
        *,
        command_builder: CommandBuilder | None = None,
    ) -> bool:
        target = Path(workspace).expanduser().resolve()
        if target == self._workspace and command_builder is None:
            return True
        if self.controller.is_active:
            self.validation_banner.set_messages(
                "任务运行期间不能切换 workspace；请先取消或等待任务完成。",
                severity=ValidationSeverity.WARNING,
            )
            return False
        builder = command_builder or CommandBuilder(target)
        self.command_builder = builder
        self._workspace = builder.workspace
        self.platform_open.configure_cli(
            program=builder.program,
            prefix_arguments=builder.prefix_arguments,
            working_directory=self._workspace,
        )
        self.platform_open.set_allowed_roots((self._workspace,))
        self._last_task = None
        self._last_result = None
        self._refresh_preview(show_errors=False)
        return True

    @property
    def last_task(self) -> TaskSpec | None:
        return self._last_task

    def build_task(self) -> TaskSpec:
        return self.command_builder.build_ab(
            scenario=self.scenario_picker.path,
            baseline_apk=self.baseline_apk_picker.path,
            candidates=self.candidate_editor.candidate_inputs(),
            output=self.output_picker.path,
            baseline_id=self.baseline_id_edit.text(),
            repeat=self.repeat_spin.value(),
            serial=self.serial_edit.text().strip() or None,
            package_name=self.package_edit.text().strip() or None,
            activity=self.activity_edit.text().strip() or None,
            clear_data=self.clear_data_combo.currentData(),
            allow_uninstall=self.allow_uninstall_checkbox.isChecked(),
            perfetto=self.perfetto_checkbox.isChecked(),
            post_launch_wait=self.post_launch_wait_spin.value(),
            interactive=self.interactive_checkbox.isChecked(),
            metadata={
                "variant_order": (
                    self.baseline_id_edit.text(),
                    *(row.candidate_id for row in self.candidate_editor.rows),
                )
            },
        )

    def refresh_preview(self) -> TaskSpec | None:
        return self._refresh_preview(show_errors=True)

    def _build_form(self) -> None:
        scroll = QScrollArea(self)
        scroll.setObjectName("abPageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 12, 0)
        content_layout.setSpacing(14)

        common_group = QGroupBox("任务参数", content)
        common_group.setObjectName("abCommonGroup")
        common_form = QFormLayout(common_group)
        common_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.scenario_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 A/B Scenario",
            file_filter="Scenario (*.yaml *.yml *.json);;所有文件 (*)",
            placeholder="选择 scenario YAML/JSON",
            parent=common_group,
        )
        self.scenario_picker.setObjectName("abScenarioPicker")
        common_form.addRow("Scenario *", self.scenario_picker)

        self.output_picker = PathPicker(
            mode=PathPickerMode.DIRECTORY,
            caption="选择 A/B 输出目录",
            placeholder="输出目录，可直接输入尚未创建的路径",
            parent=common_group,
        )
        self.output_picker.setObjectName("abOutputPicker")
        self.output_picker.set_path("runs/ab")
        common_form.addRow("输出目录 *", self.output_picker)

        self.repeat_spin = QSpinBox(common_group)
        self.repeat_spin.setObjectName("abRepeatSpin")
        self.repeat_spin.setRange(1, 100)
        self.repeat_spin.setValue(5)
        self.repeat_spin.setAccessibleName("每个版本重复次数")
        common_form.addRow("重复次数", self.repeat_spin)

        self.serial_edit = QLineEdit(common_group)
        self.serial_edit.setObjectName("abSerialEdit")
        self.serial_edit.setPlaceholderText("可选；留空时由 CLI 选择唯一在线设备")
        self.serial_edit.setAccessibleName("Android 设备 serial")
        common_form.addRow("设备 Serial", self.serial_edit)

        self.package_edit = QLineEdit(common_group)
        self.package_edit.setObjectName("abPackageEdit")
        self.package_edit.setPlaceholderText("可选；可从 scenario apps[] 读取")
        self.package_edit.setAccessibleName("Android 包名")
        common_form.addRow("包名", self.package_edit)

        self.activity_edit = QLineEdit(common_group)
        self.activity_edit.setObjectName("abActivityEdit")
        self.activity_edit.setPlaceholderText("例如 .MainActivity")
        self.activity_edit.setAccessibleName("Android Activity")
        common_form.addRow("Activity", self.activity_edit)
        content_layout.addWidget(common_group)
        self.common_group = common_group

        baseline_group = QGroupBox("Baseline（固定一项）", content)
        baseline_group.setObjectName("baselineGroup")
        baseline_form = QFormLayout(baseline_group)
        baseline_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.baseline_id_edit = QLineEdit("baseline", baseline_group)
        self.baseline_id_edit.setObjectName("baselineIdEdit")
        self.baseline_id_edit.setAccessibleName("Baseline ID")
        baseline_form.addRow("Baseline ID *", self.baseline_id_edit)
        self.baseline_apk_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 Baseline APK",
            file_filter="Android APK (*.apk);;所有文件 (*)",
            placeholder="选择 baseline APK",
            parent=baseline_group,
        )
        self.baseline_apk_picker.setObjectName("baselineApkPicker")
        baseline_form.addRow("Baseline APK *", self.baseline_apk_picker)
        content_layout.addWidget(baseline_group)
        self.baseline_group = baseline_group

        candidate_group = QGroupBox("Candidates（按此顺序执行，1–4 项）", content)
        candidate_group.setObjectName("candidateGroup")
        candidate_layout = QVBoxLayout(candidate_group)
        candidate_note = QLabel(
            "ID 会用于 --candidate-apk ID=PATH 和输出目录；可用上移/下移调整顺序。",
            candidate_group,
        )
        candidate_note.setWordWrap(True)
        candidate_layout.addWidget(candidate_note)
        self.candidate_editor = CandidateEditor(
            baseline_id=self.baseline_id_edit.text(),
            parent=candidate_group,
        )
        self.candidate_editor.setObjectName("candidateEditor")
        candidate_layout.addWidget(self.candidate_editor)
        content_layout.addWidget(candidate_group)
        self.candidate_group = candidate_group

        self.advanced_toggle = QToolButton(content)
        self.advanced_toggle.setObjectName("abAdvancedToggle")
        self.advanced_toggle.setText("显示高级选项")
        self.advanced_toggle.setCheckable(True)
        content_layout.addWidget(self.advanced_toggle)

        advanced_group = QGroupBox("高级选项", content)
        advanced_group.setObjectName("abAdvancedGroup")
        advanced_form = QFormLayout(advanced_group)
        advanced_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.clear_data_combo = QComboBox(advanced_group)
        self.clear_data_combo.setObjectName("abClearDataCombo")
        self.clear_data_combo.addItem("使用 scenario 默认", None)
        self.clear_data_combo.addItem("每轮清理数据", True)
        self.clear_data_combo.addItem("保留应用数据", False)
        self.clear_data_combo.setAccessibleName("A/B clear data 策略")
        advanced_form.addRow("Clear data", self.clear_data_combo)

        self.allow_uninstall_checkbox = QCheckBox(
            "安装冲突时允许卸载后重试（会清除应用数据）",
            advanced_group,
        )
        self.allow_uninstall_checkbox.setObjectName("abAllowUninstallCheckbox")
        advanced_form.addRow("安装恢复", self.allow_uninstall_checkbox)

        self.perfetto_checkbox = QCheckBox("每轮采集 Perfetto", advanced_group)
        self.perfetto_checkbox.setObjectName("abPerfettoCheckbox")
        advanced_form.addRow("Perfetto", self.perfetto_checkbox)

        self.post_launch_wait_spin = QDoubleSpinBox(advanced_group)
        self.post_launch_wait_spin.setObjectName("abPostLaunchWaitSpin")
        self.post_launch_wait_spin.setRange(0.0, 3600.0)
        self.post_launch_wait_spin.setDecimals(1)
        self.post_launch_wait_spin.setSingleStep(0.5)
        self.post_launch_wait_spin.setValue(2.0)
        self.post_launch_wait_spin.setSuffix(" 秒")
        self.post_launch_wait_spin.setAccessibleName("启动后等待时间")
        advanced_form.addRow("启动后等待", self.post_launch_wait_spin)

        self.interactive_checkbox = QCheckBox("启用 scenario manual 步骤", advanced_group)
        self.interactive_checkbox.setObjectName("abInteractiveCheckbox")
        advanced_form.addRow("人工步骤", self.interactive_checkbox)
        advanced_group.hide()
        self.advanced_toggle.toggled.connect(advanced_group.setVisible)
        self.advanced_toggle.toggled.connect(
            lambda checked: self.advanced_toggle.setText(
                "隐藏高级选项" if checked else "显示高级选项"
            )
        )
        content_layout.addWidget(advanced_group)
        self.advanced_group = advanced_group

        self.validation_banner = ValidationBanner(content)
        self.validation_banner.setObjectName("abValidationBanner")
        content_layout.addWidget(self.validation_banner)

        preview_group = QGroupBox("等价 CLI 命令", content)
        preview_group.setObjectName("abCommandPreviewGroup")
        preview_layout = QVBoxLayout(preview_group)
        preview_actions = QHBoxLayout()
        self.preview_toggle = QToolButton(preview_group)
        self.preview_toggle.setText("展开命令预览")
        self.preview_toggle.setCheckable(True)
        self.copy_preview_button = QPushButton("复制命令", preview_group)
        self.copy_preview_button.setEnabled(False)
        preview_actions.addWidget(self.preview_toggle)
        preview_actions.addStretch(1)
        preview_actions.addWidget(self.copy_preview_button)
        preview_layout.addLayout(preview_actions)
        self.command_preview = QPlainTextEdit(preview_group)
        self.command_preview.setObjectName("abCommandPreview")
        self.command_preview.setReadOnly(True)
        self.command_preview.setMaximumHeight(110)
        self.command_preview.setAccessibleName("A/B 等价 CLI 命令")
        self.command_preview.hide()
        self.preview_toggle.toggled.connect(self.command_preview.setVisible)
        self.preview_toggle.toggled.connect(
            lambda checked: self.preview_toggle.setText(
                "收起命令预览" if checked else "展开命令预览"
            )
        )
        self.copy_preview_button.clicked.connect(self._copy_command_preview)
        preview_layout.addWidget(self.command_preview)
        content_layout.addWidget(preview_group)

        execution_group = QGroupBox("执行与可靠状态", content)
        execution_group.setObjectName("abExecutionGroup")
        execution_layout = QVBoxLayout(execution_group)
        self.variant_status_label = QLabel(execution_group)
        self.variant_status_label.setObjectName("abVariantStatus")
        self.variant_status_label.setWordWrap(True)
        self.variant_status_label.setAccessibleName("A/B Variant 状态")
        execution_layout.addWidget(self.variant_status_label)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("开始 A/B", execution_group)
        self.start_button.setObjectName("abStartButton")
        self.cancel_button = QPushButton("取消任务", execution_group)
        self.cancel_button.setObjectName("abCancelButton")
        self.cancel_button.setEnabled(False)
        self.clear_button = QPushButton("清空表单", execution_group)
        self.clear_button.setObjectName("abClearButton")
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.clear_button)
        button_row.addStretch(1)
        execution_layout.addLayout(button_row)

        self.manual_frame = QFrame(execution_group)
        self.manual_frame.setObjectName("abManualFrame")
        self.manual_frame.setFrameShape(QFrame.Shape.StyledPanel)
        manual_layout = QHBoxLayout(self.manual_frame)
        self.manual_label = QLabel(self.manual_frame)
        self.manual_label.setWordWrap(True)
        self.manual_label.setAccessibleName("人工步骤提示")
        self.continue_button = QPushButton("已完成，继续", self.manual_frame)
        self.continue_button.setObjectName("abContinueButton")
        manual_layout.addWidget(self.manual_label, 1)
        manual_layout.addWidget(self.continue_button)
        self.manual_frame.hide()
        execution_layout.addWidget(self.manual_frame)
        content_layout.addWidget(execution_group)

        task_tabs = QTabWidget(content)
        task_tabs.setObjectName("abTaskTabs")
        self.console = TaskConsole(task_tabs)
        self.console.setObjectName("abTaskConsole")
        self.summary = TaskSummary(task_tabs)
        self.summary.setObjectName("abTaskSummary")
        task_tabs.addTab(self.console, "实时日志")
        task_tabs.addTab(self.summary, "结果")
        task_tabs.setMinimumHeight(300)
        content_layout.addWidget(task_tabs)
        self.task_tabs = task_tabs

        scroll.setWidget(content)
        self.layout.addWidget(scroll, 1)
        self._set_variant_idle_text()

    def _connect_form_signals(self) -> None:
        for picker in (self.scenario_picker, self.output_picker, self.baseline_apk_picker):
            picker.path_changed.connect(self._on_form_changed)
        for edit in (
            self.baseline_id_edit,
            self.serial_edit,
            self.package_edit,
            self.activity_edit,
        ):
            edit.textChanged.connect(self._on_form_changed)
        self.baseline_id_edit.textChanged.connect(self.candidate_editor.set_baseline_id)
        self.repeat_spin.valueChanged.connect(self._on_form_changed)
        self.candidate_editor.changed.connect(self._on_form_changed)
        self.clear_data_combo.currentIndexChanged.connect(self._on_form_changed)
        self.allow_uninstall_checkbox.toggled.connect(self._on_form_changed)
        self.perfetto_checkbox.toggled.connect(self._on_form_changed)
        self.post_launch_wait_spin.valueChanged.connect(self._on_form_changed)
        self.interactive_checkbox.toggled.connect(self._on_form_changed)

        self.start_button.clicked.connect(self._start_task)
        self.cancel_button.clicked.connect(self._cancel_task)
        self.clear_button.clicked.connect(self._clear_form)
        self.continue_button.clicked.connect(self._continue_manual)
        self.summary.open_result_requested.connect(self._open_result)
        self.console.open_log_requested.connect(self._open_log)

    def _bind_controller(self) -> None:
        self.console.bind_controller(self.controller)
        self.summary.bind_controller(self.controller)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.validation_failed.connect(self._on_controller_validation_failed)
        self.controller.manual_requested.connect(self._on_manual_requested)
        self.controller.launch_failed.connect(self._on_launch_failed)
        self.controller.finished.connect(self._on_finished)

    def _on_form_changed(self, *_args: object) -> None:
        self._refresh_preview(show_errors=True)
        if self.controller.state not in _ACTIVE_STATES:
            self._set_variant_idle_text()

    def _refresh_preview(self, *, show_errors: bool) -> TaskSpec | None:
        messages = list(self.candidate_editor.validation_messages())
        task: TaskSpec | None = None
        try:
            task = self.build_task()
        except CommandValidationError as exc:
            messages.extend(f"{issue.field}: {issue.message}" for issue in exc.issues)
        except (OSError, ValueError) as exc:
            messages.append(str(exc))

        unique_messages = tuple(dict.fromkeys(message for message in messages if message))
        if unique_messages:
            task = None
            self._last_task = None
            self.command_preview.clear()
            self.copy_preview_button.setEnabled(False)
            self.console.set_command_preview("")
            self.summary.set_command_preview("")
            if show_errors:
                self.validation_banner.set_messages(
                    unique_messages,
                    severity=ValidationSeverity.ERROR,
                )
            else:
                self.validation_banner.clear()
            return None

        assert task is not None
        self._last_task = task
        self.command_preview.setPlainText(task.display_command)
        self.copy_preview_button.setEnabled(True)
        self.console.set_command_preview(task.display_command)
        self.summary.set_command_preview(task.display_command)
        self.validation_banner.clear()
        return task

    def _start_task(self) -> None:
        task = self._refresh_preview(show_errors=True)
        if task is None:
            return
        self.console.clear_visible()
        self.summary.clear()
        self._last_result = None
        self.variant_status_label.setText(
            "已提交执行顺序："
            + " → ".join(str(value) for value in task.metadata.get("variant_order", ()))
            + "。CLI 尚未提供可靠的当前 variant 进度事件。"
        )
        started = self.controller.start(task)
        if not started and self.controller.state == TaskState.INVALID_INPUT:
            return

    def _cancel_task(self) -> None:
        if not self.controller.cancel():
            self.validation_banner.set_messages(
                "当前没有可取消的 A/B 任务。",
                severity=ValidationSeverity.INFO,
            )

    def _continue_manual(self) -> None:
        if self.controller.continue_manual():
            self.manual_frame.hide()
            self.variant_status_label.setText(
                "人工步骤已继续；CLI 尚未提供可靠的当前 variant 标识。"
            )

    def _on_state_changed(self, state: TaskState) -> None:
        active = state in _ACTIVE_STATES
        for group in (self.common_group, self.baseline_group, self.candidate_group, self.advanced_group):
            group.setEnabled(not active)
        self.advanced_toggle.setEnabled(not active)
        self.start_button.setEnabled(not active)
        self.cancel_button.setEnabled(
            state in {TaskState.STARTING, TaskState.RUNNING, TaskState.WAITING_FOR_USER}
        )
        self.clear_button.setEnabled(not active)

        if state == TaskState.STARTING:
            self.variant_status_label.setText(
                "A/B 任务正在启动；执行顺序已冻结，当前 variant 尚不可可靠判断。"
            )
        elif state == TaskState.RUNNING:
            self.variant_status_label.setText(
                "A/B 任务正在按顺序执行；CLI 未提供可靠的当前 variant 进度事件。"
            )
        elif state == TaskState.WAITING_FOR_USER:
            self.variant_status_label.setText(
                "等待人工步骤；当前 variant 未由 CLI 可靠标识。"
            )
        elif state == TaskState.CANCELLING:
            self.variant_status_label.setText("正在取消 A/B 任务并等待进程收敛。")

    def _on_manual_requested(self, request: ManualRequest) -> None:
        timeout = (
            f"（CLI 提供最长等待 {request.timeout_seconds:g} 秒）"
            if request.timeout_seconds is not None
            else "（CLI 未提供可靠的超时时间）"
        )
        self.manual_label.setText(f"{request.prompt}\n{timeout}")
        self.manual_frame.show()
        self.variant_status_label.setText(
            "等待人工步骤；当前 variant 未由 CLI 可靠标识。"
        )

    def _on_finished(self, result: TaskResult) -> None:
        self._last_result = result
        self.manual_frame.hide()
        self.task_tabs.setCurrentWidget(self.summary)
        statuses = _candidate_statuses(result)
        if statuses:
            self.variant_status_label.setText(
                "可靠的最终 candidate 状态："
                + "；".join(f"{candidate_id}={status}" for candidate_id, status in statuses)
            )
        else:
            self.variant_status_label.setText(
                f"A/B 任务已结束：{result.outcome.value}。当前 CLI 未提供逐 candidate 状态时，"
                "请以总览和单 candidate 报告为准。"
            )

    def _on_controller_validation_failed(self, issues: tuple[str, ...]) -> None:
        self.validation_banner.set_messages(issues, severity=ValidationSeverity.ERROR)

    def _on_launch_failed(self, message: str) -> None:
        self.validation_banner.set_messages(
            ["A/B 进程无法启动。", str(message), "请检查 CLI 路径、权限和安装版本。"],
            severity=ValidationSeverity.ERROR,
        )

    def _copy_command_preview(self) -> None:
        command = self.command_preview.toPlainText()
        if command:
            QGuiApplication.clipboard().setText(command)

    def _clear_form(self) -> None:
        if self.controller.state in _ACTIVE_STATES:
            return
        self.scenario_picker.clear()
        self.output_picker.set_path("runs/ab")
        self.repeat_spin.setValue(5)
        self.serial_edit.clear()
        self.package_edit.clear()
        self.activity_edit.clear()
        self.baseline_id_edit.setText("baseline")
        self.baseline_apk_picker.clear()
        self.candidate_editor.clear()
        self.clear_data_combo.setCurrentIndex(0)
        self.allow_uninstall_checkbox.setChecked(False)
        self.perfetto_checkbox.setChecked(False)
        self.post_launch_wait_spin.setValue(2.0)
        self.interactive_checkbox.setChecked(False)
        self.console.clear_visible()
        self.summary.clear()
        self.validation_banner.clear()
        self._last_task = None
        self._last_result = None
        self._refresh_preview(show_errors=False)
        self._set_variant_idle_text()

    def _set_variant_idle_text(self) -> None:
        order = [
            self.baseline_id_edit.text() or "baseline",
            *(row.candidate_id or f"candidate-{index + 1}" for index, row in enumerate(self.candidate_editor.rows)),
        ]
        self.variant_status_label.setText(
            "计划执行顺序："
            + " → ".join(order)
            + "。运行期间仅展示 CLI 可可靠提供的状态，不推测当前 variant 或百分比。"
        )

    def _open_result(self, descriptor: ResultDescriptor) -> None:
        self.result_open_requested.emit(descriptor)
        if self._open_result_handler is not None:
            self._open_result_handler(descriptor)
            return
        if descriptor.type == ResultType.REPORT:
            result = self.platform_open.open_static(descriptor)
        else:
            result = self.platform_open.open_descriptor(descriptor)
        self._show_open_result(result)

    def _open_log(self, path: str) -> None:
        resolved = Path(path).expanduser().resolve(strict=False)
        descriptor = ResultDescriptor(
            type=ResultType.LOG,
            label="任务日志",
            location=str(resolved),
            exists=resolved.exists(),
        )
        self._show_open_result(self.platform_open.open_descriptor(descriptor))

    def _show_open_result(self, result: OpenResult) -> None:
        if result.success:
            self.validation_banner.set_messages(
                result.message,
                severity=ValidationSeverity.INFO,
            )
        else:
            self.validation_banner.set_messages(
                result.message,
                severity=(
                    ValidationSeverity.WARNING
                    if result.confirmation_required
                    else ValidationSeverity.ERROR
                ),
            )


def _candidate_statuses(result: TaskResult) -> tuple[tuple[str, str], ...]:
    envelope = result.metadata.get("result_envelope")
    if not isinstance(envelope, Mapping):
        return ()
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        return ()
    comparisons = data.get("comparisons")
    if not isinstance(comparisons, Mapping):
        return ()
    values: list[tuple[str, str]] = []
    for candidate_id, payload in comparisons.items():
        if isinstance(payload, Mapping) and isinstance(payload.get("status"), str):
            values.append((str(candidate_id), str(payload["status"])))
    return tuple(values)
