from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from perfeval.core.memory_profile import MEMORY_PROFILES
from perfeval.gui.controllers.task_controller import TaskController
from perfeval.gui.models.result import ResultDescriptor, ResultViewer, TaskResult
from perfeval.gui.models.task import TaskSpec, TaskState
from perfeval.gui.services.command_builder import CommandBuilder, CommandValidationError
from perfeval.gui.services.platform_open import OpenResult, PlatformOpenService
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.tool_paths import command_builder_from_settings
from perfeval.gui.widgets.path_picker import PathPicker, PathPickerMode
from perfeval.gui.widgets.task_console import TaskConsole
from perfeval.gui.widgets.task_summary import TaskSummary
from perfeval.gui.widgets.validation_banner import ValidationBanner, ValidationSeverity

from .base_page import BasePage


_PACKAGE_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


class DiagnosePage(BasePage):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: SettingsService | None = None,
        workspace: str | Path | None = None,
        command_builder: CommandBuilder | None = None,
        controller: TaskController | None = None,
        platform_open: PlatformOpenService | None = None,
    ) -> None:
        super().__init__(
            "聚焦诊断",
            "针对 frame、cpu、trace 或 full 模式采集聚焦诊断证据。",
            parent,
        )
        self._settings = settings
        self._workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        self._injected_builder = command_builder
        self._builder: CommandBuilder | None = command_builder
        self.controller = controller or TaskController(self)

        initial_builder = self._command_builder()
        self.platform_open = platform_open or PlatformOpenService(
            self,
            cli_program=initial_builder.program,
            cli_prefix_arguments=initial_builder.prefix_arguments,
            working_directory=self._workspace,
            allowed_roots=(self._workspace,),
        )

        scroll = QScrollArea(self)
        scroll.setObjectName("diagnosePageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(0, 0, 8, 0)
        self.content_layout.setSpacing(16)
        scroll.setWidget(content)
        self.layout.addWidget(scroll, 1)

        self._build_form()
        self._build_command_and_actions()
        self._build_task_widgets()
        self._connect_signals()
        self._update_mode_fields()
        self._update_manual_notice()
        self._update_command_preview()

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def is_active(self) -> bool:
        return self.controller.is_active

    def shutdown(self) -> bool:
        self.platform_open.shutdown()
        return self.controller.shutdown()

    def set_workspace(self, workspace: str | Path) -> None:
        target = Path(workspace).expanduser().resolve()
        if target == self._workspace:
            return
        if self.controller.is_active:
            self.validation_banner.set_messages(
                "任务运行期间不能切换 workspace；请先取消或等待任务完成。",
                severity=ValidationSeverity.WARNING,
            )
            return
        self._workspace = target
        self.workspace_value.setText(str(target))
        self.workspace_value.setToolTip(str(target))
        if self._injected_builder is None:
            self._builder = None
        builder = self._command_builder()
        if hasattr(self.platform_open, "configure_cli"):
            self.platform_open.configure_cli(
                program=builder.program,
                prefix_arguments=builder.prefix_arguments,
                working_directory=target,
            )
        if hasattr(self.platform_open, "set_allowed_roots"):
            self.platform_open.set_allowed_roots((target,))
        self._update_command_preview()

    def _build_form(self) -> None:
        group = QGroupBox("诊断参数", self)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.workspace_value = QLineEdit(str(self._workspace), group)
        self.workspace_value.setObjectName("diagnoseWorkspace")
        self.workspace_value.setReadOnly(True)
        self.workspace_value.setAccessibleName("当前 workspace")
        self.workspace_value.setToolTip(str(self._workspace))
        form.addRow("Workspace", self.workspace_value)

        self.serial_edit = QLineEdit(group)
        self.serial_edit.setObjectName("diagnoseSerial")
        self.serial_edit.setPlaceholderText("可选；多设备时建议填写")
        serial_label = QLabel("设备 serial", group)
        serial_label.setBuddy(self.serial_edit)
        form.addRow(serial_label, self.serial_edit)

        self.package_edit = QLineEdit(group)
        self.package_edit.setObjectName("diagnosePackageName")
        self.package_edit.setPlaceholderText("例如 com.example.app")
        self.package_edit.setAccessibleName("Android 包名，必填")
        package_label = QLabel("包名 *", group)
        package_label.setBuddy(self.package_edit)
        form.addRow(package_label, self.package_edit)

        self.output_picker = PathPicker(
            mode=PathPickerMode.DIRECTORY,
            caption="选择诊断输出目录",
            placeholder="相对 workspace 或已授权的目录",
            parent=group,
        )
        self.output_picker.setObjectName("diagnoseOutput")
        self.output_picker.set_path("runs/android-diagnose")
        output_label = QLabel("输出目录 *", group)
        output_label.setBuddy(self.output_picker.line_edit)
        form.addRow(output_label, self.output_picker)

        self.case_edit = QLineEdit("android-diagnose", group)
        self.case_edit.setObjectName("diagnoseCase")
        self.case_edit.setAccessibleName("诊断 case 名称")
        case_label = QLabel("Case", group)
        case_label.setBuddy(self.case_edit)
        form.addRow(case_label, self.case_edit)

        self.mode_combo = QComboBox(group)
        self.mode_combo.setObjectName("diagnoseMode")
        for mode, label in (
            ("frame", "Frame / Memory / Logcat"),
            ("cpu", "CPU / simpleperf"),
            ("trace", "Perfetto trace"),
            ("full", "Full（frame + CPU + trace）"),
        ):
            self.mode_combo.addItem(label, mode)
        mode_label = QLabel("模式", group)
        mode_label.setBuddy(self.mode_combo)
        form.addRow(mode_label, self.mode_combo)

        self.duration_spin = QSpinBox(group)
        self.duration_spin.setObjectName("diagnoseDuration")
        self.duration_spin.setRange(1, 3600)
        self.duration_spin.setValue(30)
        self.duration_spin.setSuffix(" 秒")
        self.duration_label = QLabel("持续时间", group)
        self.duration_label.setBuddy(self.duration_spin)
        form.addRow(self.duration_label, self.duration_spin)

        self.interactive_check = QCheckBox("需要人工操作步骤", group)
        self.interactive_check.setObjectName("diagnoseInteractive")
        form.addRow("交互", self.interactive_check)

        self.memory_profile_combo = QComboBox(group)
        self.memory_profile_combo.setObjectName("diagnoseMemoryProfile")
        for profile in MEMORY_PROFILES:
            self.memory_profile_combo.addItem(profile, profile)
        self.memory_profile_combo.setCurrentIndex(
            max(0, self.memory_profile_combo.findData("mainstream"))
        )
        memory_label = QLabel("内存档位", group)
        memory_label.setBuddy(self.memory_profile_combo)
        form.addRow(memory_label, self.memory_profile_combo)

        self.first_party_regex_edit = QLineEdit(group)
        self.first_party_regex_edit.setObjectName("diagnoseFirstPartyRegex")
        self.first_party_regex_edit.setPlaceholderText("可选，例如 ^com\\.example")
        regex_label = QLabel("First-party 正则", group)
        regex_label.setBuddy(self.first_party_regex_edit)
        form.addRow(regex_label, self.first_party_regex_edit)

        self.content_layout.addWidget(group)

        self.manual_notice = ValidationBanner(self)
        self.manual_notice.setObjectName("diagnoseManualNotice")
        self.content_layout.addWidget(self.manual_notice)

        self.validation_banner = ValidationBanner(self)
        self.validation_banner.setObjectName("diagnoseValidation")
        self.content_layout.addWidget(self.validation_banner)

    def _build_command_and_actions(self) -> None:
        command_group = QGroupBox("等价命令预览", self)
        command_layout = QVBoxLayout(command_group)
        self.command_preview = QPlainTextEdit(command_group)
        self.command_preview.setObjectName("diagnoseCommandPreview")
        self.command_preview.setReadOnly(True)
        self.command_preview.setMaximumHeight(96)
        self.command_preview.setAccessibleName("等价 CLI 命令预览")
        command_layout.addWidget(self.command_preview)
        self.content_layout.addWidget(command_group)

        actions = QHBoxLayout()
        self.start_button = QPushButton("开始诊断", self)
        self.start_button.setObjectName("diagnoseStart")
        self.cancel_button = QPushButton("取消任务", self)
        self.cancel_button.setObjectName("diagnoseCancel")
        self.cancel_button.setEnabled(False)
        self.reset_button = QPushButton("清空/恢复默认", self)
        self.reset_button.setObjectName("diagnoseReset")
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        self.content_layout.addLayout(actions)

    def _build_task_widgets(self) -> None:
        task_group = QGroupBox("任务", self)
        task_layout = QVBoxLayout(task_group)
        self.task_console = TaskConsole(task_group)
        self.task_console.setMinimumHeight(220)
        self.task_console.bind_controller(self.controller)
        task_layout.addWidget(self.task_console)
        self.content_layout.addWidget(task_group)

        result_group = QGroupBox("结果", self)
        result_layout = QVBoxLayout(result_group)
        self.task_summary = TaskSummary(result_group)
        self.task_summary.setMinimumHeight(180)
        self.task_summary.bind_controller(self.controller)
        result_layout.addWidget(self.task_summary)
        self.content_layout.addWidget(result_group)
        self.content_layout.addStretch(1)

    def _connect_signals(self) -> None:
        for widget in (
            self.serial_edit,
            self.package_edit,
            self.case_edit,
            self.first_party_regex_edit,
        ):
            widget.textChanged.connect(self._update_command_preview)
        self.output_picker.path_changed.connect(self._update_command_preview)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.duration_spin.valueChanged.connect(self._update_command_preview)
        self.interactive_check.toggled.connect(self._interactive_changed)
        self.memory_profile_combo.currentIndexChanged.connect(self._update_command_preview)
        self.start_button.clicked.connect(self._start_task)
        self.cancel_button.clicked.connect(self._cancel_task)
        self.reset_button.clicked.connect(self._reset_form)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.validation_failed.connect(self._on_controller_validation_failed)
        self.controller.launch_failed.connect(self._on_launch_failed)
        self.controller.finished.connect(self._on_finished)
        self.task_summary.open_result_requested.connect(self._open_result)
        self.task_console.open_log_requested.connect(self._open_log_path)
        if hasattr(self.platform_open, "open_failed"):
            self.platform_open.open_failed.connect(self._show_open_error)

    def _mode_changed(self) -> None:
        self._update_mode_fields()
        self._update_command_preview()

    def _interactive_changed(self) -> None:
        self._update_manual_notice()
        self._update_command_preview()

    def _update_mode_fields(self) -> None:
        visible = self.mode == "frame"
        self.duration_label.setVisible(not visible)
        self.duration_spin.setVisible(not visible)

    def _update_manual_notice(self) -> None:
        if self.interactive_check.isChecked():
            self.manual_notice.set_messages(
                "运行期间请保持设备可操作。出现人工步骤时，任务区会显示非阻塞提示；点击继续会向 CLI 写入 Enter。",
                severity=ValidationSeverity.WARNING,
            )
        else:
            self.manual_notice.clear()

    @property
    def mode(self) -> str:
        return str(self.mode_combo.currentData())

    def _command_builder(self) -> CommandBuilder:
        if self._builder is not None:
            return self._builder
        if self._settings is not None:
            self._builder = command_builder_from_settings(self._settings, self._workspace)
        else:
            self._builder = CommandBuilder(self._workspace)
        return self._builder

    def _build_task(self) -> TaskSpec:
        package_name = self.package_edit.text().strip()
        if not package_name:
            raise CommandValidationError.from_message("package_name", "不能为空")
        if not _PACKAGE_PATTERN.fullmatch(package_name):
            raise CommandValidationError.from_message(
                "package_name", "格式无效，例如 com.example.app"
            )
        first_party_regex = self.first_party_regex_edit.text().strip() or None
        if first_party_regex:
            try:
                re.compile(first_party_regex)
            except re.error as exc:
                raise CommandValidationError.from_message(
                    "first_party_regex", f"正则无效：{exc}"
                ) from exc
        return self._command_builder().build_diagnose(
            package_name=package_name,
            output=self.output_picker.path,
            case_id=self.case_edit.text(),
            mode=self.mode,
            duration=self.duration_spin.value(),
            serial=self.serial_edit.text() or None,
            interactive=self.interactive_check.isChecked(),
            memory_profile=str(self.memory_profile_combo.currentData()),
            first_party_regex=first_party_regex,
        )

    def _update_command_preview(self) -> None:
        try:
            task = self._build_task()
        except (CommandValidationError, ValueError):
            self.command_preview.setPlainText("填写必填参数后显示等价命令。")
            self.task_console.set_command_preview("")
            self.task_summary.set_command_preview("")
            return
        self.command_preview.setPlainText(task.display_command)
        self.task_console.set_command_preview(task.display_command)
        self.task_summary.set_command_preview(task.display_command)

    def _start_task(self) -> None:
        self.validation_banner.clear()
        try:
            task = self._build_task()
        except CommandValidationError as exc:
            self.validation_banner.set_messages(
                [f"{issue.field}: {issue.message}" for issue in exc.issues],
                severity=ValidationSeverity.ERROR,
            )
            return
        except ValueError as exc:
            self.validation_banner.set_messages(str(exc), severity=ValidationSeverity.ERROR)
            return
        self.task_console.clear_visible()
        self.task_summary.clear()
        self.task_console.set_command_preview(task.display_command)
        self.task_summary.set_command_preview(task.display_command)
        if not self.controller.start(task):
            return
        self.validation_banner.set_messages("诊断任务已启动。", severity=ValidationSeverity.INFO)

    def _cancel_task(self) -> None:
        if self.controller.cancel():
            self.validation_banner.set_messages(
                "正在请求 CLI 停止；超时后会执行强制终止，已有日志和部分产物会保留。",
                severity=ValidationSeverity.WARNING,
            )

    def _reset_form(self) -> None:
        if self.controller.is_active:
            self.validation_banner.set_messages(
                "任务运行期间不能清空表单。",
                severity=ValidationSeverity.WARNING,
            )
            return
        self.serial_edit.clear()
        self.package_edit.clear()
        self.output_picker.set_path("runs/android-diagnose")
        self.case_edit.setText("android-diagnose")
        self.mode_combo.setCurrentIndex(self.mode_combo.findData("frame"))
        self.duration_spin.setValue(30)
        self.interactive_check.setChecked(False)
        self.memory_profile_combo.setCurrentIndex(
            self.memory_profile_combo.findData("mainstream")
        )
        self.first_party_regex_edit.clear()
        self.validation_banner.clear()
        self.task_console.clear_visible()
        self.task_summary.clear()
        if self.controller.state != TaskState.IDLE:
            self.controller.reset()
        self._update_command_preview()

    def _on_state_changed(self, state: TaskState) -> None:
        active = state in {
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.WAITING_FOR_USER,
            TaskState.CANCELLING,
        }
        self.start_button.setEnabled(not active)
        self.cancel_button.setEnabled(
            state in {TaskState.STARTING, TaskState.RUNNING, TaskState.WAITING_FOR_USER}
        )
        self.reset_button.setEnabled(not active)

    def _on_controller_validation_failed(self, issues: tuple[str, ...]) -> None:
        self.validation_banner.set_messages(issues, severity=ValidationSeverity.ERROR)

    def _on_launch_failed(self, message: str) -> None:
        self.validation_banner.set_messages(
            f"CLI 无法启动：{message}。请检查设置中的 CLI 路径和执行权限。",
            severity=ValidationSeverity.ERROR,
        )

    def _on_finished(self, result: TaskResult) -> None:
        if result.outcome.value == "cancelled":
            message = "诊断任务已取消，日志和部分产物已保留。"
            severity = ValidationSeverity.WARNING
        elif result.outcome.value in {"tool_error", "launch_error", "unexpected_error"}:
            message = "诊断任务异常结束；请查看 stderr 和完整日志。"
            severity = ValidationSeverity.ERROR
        else:
            message = "诊断任务已结束，可在结果区打开报告、artifact 或输出目录。"
            severity = ValidationSeverity.INFO
        self.validation_banner.set_messages(message, severity=severity)

    def _open_result(self, descriptor: ResultDescriptor) -> None:
        if descriptor.viewer == ResultViewer.REPORT:
            self._choose_report_open(descriptor)
            return
        if descriptor.viewer == ResultViewer.PERFETTO:
            self._confirm_perfetto_open(descriptor)
            return
        self._show_open_result(self.platform_open.open_descriptor(descriptor))

    def _choose_report_open(self, descriptor: ResultDescriptor) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("打开报告")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("选择报告打开方式")
        box.setInformativeText(
            "完整查看器会在 127.0.0.1 启动限时本地服务；静态打开不启动服务，但报告中的 Perfetto 直达按钮可能不可用。"
        )
        full_button = box.addButton("完整查看器", QMessageBox.ButtonRole.AcceptRole)
        static_button = box.addButton("静态打开", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is full_button:
            result = self.platform_open.open_descriptor(
                descriptor, confirm_local_server=True
            )
            self._show_open_result(result)
        elif box.clickedButton() is static_button:
            self._show_open_result(self.platform_open.open_static(descriptor))

    def _confirm_perfetto_open(self, descriptor: ResultDescriptor) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("打开 Perfetto UI")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("此操作会访问外部站点 ui.perfetto.dev")
        box.setInformativeText(
            "PerfEval 只在 127.0.0.1 提供限时 trace 服务。若需离线处理，可改为打开 artifact 所在目录或使用 Android Studio。"
        )
        external_button = box.addButton("继续打开 Perfetto UI", QMessageBox.ButtonRole.AcceptRole)
        directory_button = box.addButton("打开本地目录", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is external_button:
            self._show_open_result(
                self.platform_open.open_descriptor(
                    descriptor, confirm_external_site=True
                )
            )
        elif box.clickedButton() is directory_button:
            self._show_open_result(self.platform_open.open_parent_directory(descriptor))

    def _open_log_path(self, path: str) -> None:
        descriptor = ResultDescriptor(
            type=self._log_result_type(),
            label="完整日志",
            location=path,
            exists=Path(path).exists(),
            viewer=ResultViewer.SYSTEM,
        )
        self._show_open_result(self.platform_open.open_descriptor(descriptor))

    @staticmethod
    def _log_result_type():
        from perfeval.gui.models.result import ResultType

        return ResultType.LOG

    def _show_open_result(self, result: OpenResult) -> None:
        if result.confirmation_required:
            return
        severity = ValidationSeverity.INFO if result.success else ValidationSeverity.ERROR
        self.validation_banner.set_messages(result.message, severity=severity)

    def _show_open_error(self, message: str) -> None:
        self.validation_banner.set_messages(message, severity=ValidationSeverity.ERROR)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.controller.is_active:
            self.controller.shutdown()
        if hasattr(self.platform_open, "shutdown"):
            self.platform_open.shutdown()
        super().closeEvent(event)
