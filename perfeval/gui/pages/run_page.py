from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..controllers.task_controller import TaskController
from ..models.device import EnvironmentSnapshot
from ..models.task import TaskSpec, TaskState
from ..services.command_builder import (
    CommandBuilder,
    CommandValidationError,
    ValidationIssue,
)
from ..services.device_service import DeviceService
from ..services.settings import SettingsService
from ..services.tool_paths import command_builder_from_settings, resolve_tool_paths
from ..services.workspace import WorkspaceService
from ..widgets.path_picker import PathPicker, PathPickerMode
from ..widgets.task_console import TaskConsole
from ..widgets.task_summary import TaskSummary
from ..widgets.validation_banner import ValidationBanner, ValidationSeverity
from .base_page import BasePage


BuilderFactory = Callable[[Path], CommandBuilder]


class DeviceRunForm(QWidget):
    changed = Signal()
    refresh_devices_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.scenario_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 scenario YAML",
            file_filter="YAML (*.yaml *.yml);;所有文件 (*)",
            parent=self,
        )
        self.scenario_picker.setObjectName("runDeviceScenarioPicker")
        form.addRow("Scenario *", self.scenario_picker)

        self.output_edit = QLineEdit("runs", self)
        self.output_edit.setObjectName("runDeviceOutputEdit")
        self.output_edit.setAccessibleName("设备采集输出目录")
        form.addRow("输出目录 *", self.output_edit)

        self.repeat_spin = QSpinBox(self)
        self.repeat_spin.setObjectName("runDeviceRepeatSpin")
        self.repeat_spin.setRange(1, 100)
        self.repeat_spin.setValue(1)
        form.addRow("重复次数", self.repeat_spin)

        serial_host = QWidget(self)
        serial_layout = QHBoxLayout(serial_host)
        serial_layout.setContentsMargins(0, 0, 0, 0)
        self.serial_combo = QComboBox(serial_host)
        self.serial_combo.setObjectName("runDeviceSerialCombo")
        self.serial_combo.setEditable(True)
        self.serial_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.serial_combo.setPlaceholderText("自动选择唯一在线设备，或输入 serial")
        serial_layout.addWidget(self.serial_combo, 1)
        self.refresh_devices_button = QPushButton("刷新", serial_host)
        self.refresh_devices_button.setAccessibleName("刷新 Android 设备")
        self.refresh_devices_button.clicked.connect(self.refresh_devices_requested)
        serial_layout.addWidget(self.refresh_devices_button)
        form.addRow("设备 serial", serial_host)

        self.apk_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 APK",
            file_filter="Android APK (*.apk);;所有文件 (*)",
            placeholder="可选；留空时使用 scenario 中的 artifact",
            parent=self,
        )
        self.apk_picker.setObjectName("runDeviceApkPicker")
        form.addRow("APK", self.apk_picker)

        self.package_edit = QLineEdit(self)
        self.package_edit.setObjectName("runDevicePackageEdit")
        self.package_edit.setPlaceholderText("可选；留空时使用 scenario 中的 packageName")
        form.addRow("包名", self.package_edit)

        self.activity_edit = QLineEdit(self)
        self.activity_edit.setObjectName("runDeviceActivityEdit")
        self.activity_edit.setPlaceholderText("例如 .MainActivity")
        form.addRow("Activity", self.activity_edit)

        self.advanced_group = QGroupBox("高级选项", self)
        self.advanced_group.setCheckable(True)
        self.advanced_group.setChecked(False)
        advanced_form = QFormLayout(self.advanced_group)
        self.clear_data_check = QCheckBox("采集前清除应用数据", self.advanced_group)
        self.clear_data_check.setChecked(True)
        advanced_form.addRow(self.clear_data_check)
        self.reinstall_check = QCheckBox("采集前安装/重装 APK", self.advanced_group)
        self.reinstall_check.setChecked(True)
        advanced_form.addRow(self.reinstall_check)
        self.perfetto_check = QCheckBox("同时采集 Perfetto", self.advanced_group)
        advanced_form.addRow(self.perfetto_check)
        self.sampling_off_check = QCheckBox("关闭周期采样", self.advanced_group)
        self.sampling_off_check.setObjectName("runDeviceSamplingOffCheck")
        self.sampling_off_check.setAccessibleName("关闭周期采样")
        self.sampling_off_check.setToolTip(
            "勾选后附加 --sampling-off，忽略 scenario 中的采样器配置；"
            "不勾选时跟随 scenario 配置。"
        )
        advanced_form.addRow(self.sampling_off_check)
        self.sampling_interval_spin = QSpinBox(self.advanced_group)
        self.sampling_interval_spin.setObjectName("runDeviceSamplingIntervalSpin")
        self.sampling_interval_spin.setAccessibleName("采样间隔毫秒")
        self.sampling_interval_spin.setRange(0, 3_600_000)
        self.sampling_interval_spin.setValue(0)
        self.sampling_interval_spin.setSpecialValueText("跟随 scenario 配置")
        self.sampling_interval_spin.setSuffix(" ms")
        self.sampling_interval_spin.setToolTip(
            "覆盖所有 Android 采样器的采样间隔（毫秒，须大于 0）；0 表示跟随 scenario 配置。"
        )
        advanced_form.addRow("采样间隔", self.sampling_interval_spin)
        self.screen_record_check = QCheckBox("开启录屏", self.advanced_group)
        self.screen_record_check.setObjectName("runDeviceScreenRecordCheck")
        self.screen_record_check.setAccessibleName("开启录屏")
        self.screen_record_check.setToolTip(
            "勾选后附加 --screen-record，即使 scenario 未配置录屏；默认关闭。"
        )
        advanced_form.addRow(self.screen_record_check)
        self.post_launch_wait_spin = QDoubleSpinBox(self.advanced_group)
        self.post_launch_wait_spin.setRange(0, 3600)
        self.post_launch_wait_spin.setDecimals(1)
        self.post_launch_wait_spin.setValue(2.0)
        self.post_launch_wait_spin.setSuffix(" 秒")
        advanced_form.addRow("启动后等待", self.post_launch_wait_spin)
        self.interactive_check = QCheckBox("允许 scenario 人工步骤", self.advanced_group)
        advanced_form.addRow(self.interactive_check)
        form.addRow(self.advanced_group)

        self._connect_changes()

    def serial(self) -> str | None:
        index = self.serial_combo.currentIndex()
        if index >= 0 and self.serial_combo.currentText() == self.serial_combo.itemText(index):
            data = self.serial_combo.itemData(index)
            return str(data) if data else None
        text = self.serial_combo.currentText().strip()
        return text or None

    def set_devices(self, snapshot: EnvironmentSnapshot) -> None:
        previous = self.serial()
        online = [device for device in snapshot.devices if device.online]
        self.serial_combo.blockSignals(True)
        self.serial_combo.clear()
        if len(online) != 1:
            self.serial_combo.addItem(
                "请选择设备" if online else "未检测到在线设备，可手动输入 serial",
                None,
            )
        for device in online:
            label = device.serial
            if device.model:
                label += f" — {device.model}"
            self.serial_combo.addItem(label, device.serial)
        if previous:
            index = self.serial_combo.findData(previous)
            if index >= 0:
                self.serial_combo.setCurrentIndex(index)
            else:
                self.serial_combo.setEditText(previous)
        elif len(online) == 1:
            self.serial_combo.setCurrentIndex(0)
        self.serial_combo.blockSignals(False)
        self.changed.emit()

    def _connect_changes(self) -> None:
        self.scenario_picker.path_changed.connect(lambda _value: self.changed.emit())
        self.output_edit.textChanged.connect(lambda _value: self.changed.emit())
        self.repeat_spin.valueChanged.connect(lambda _value: self.changed.emit())
        self.serial_combo.currentTextChanged.connect(lambda _value: self.changed.emit())
        self.apk_picker.path_changed.connect(lambda _value: self.changed.emit())
        self.package_edit.textChanged.connect(lambda _value: self.changed.emit())
        self.activity_edit.textChanged.connect(lambda _value: self.changed.emit())
        self.clear_data_check.toggled.connect(lambda _value: self.changed.emit())
        self.reinstall_check.toggled.connect(lambda _value: self.changed.emit())
        self.perfetto_check.toggled.connect(lambda _value: self.changed.emit())
        self.sampling_off_check.toggled.connect(lambda _value: self.changed.emit())
        self.sampling_interval_spin.valueChanged.connect(lambda _value: self.changed.emit())
        self.screen_record_check.toggled.connect(lambda _value: self.changed.emit())
        self.post_launch_wait_spin.valueChanged.connect(lambda _value: self.changed.emit())
        self.interactive_check.toggled.connect(lambda _value: self.changed.emit())


class OfflineRunForm(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.scenario_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 scenario YAML",
            file_filter="YAML (*.yaml *.yml);;所有文件 (*)",
            parent=self,
        )
        self.scenario_picker.setObjectName("runOfflineScenarioPicker")
        form.addRow("Scenario *", self.scenario_picker)

        self.output_edit = QLineEdit("runs", self)
        self.output_edit.setObjectName("runOfflineOutputEdit")
        self.output_edit.setAccessibleName("离线分析输出目录")
        form.addRow("输出目录 *", self.output_edit)

        self.repeat_spin = QSpinBox(self)
        self.repeat_spin.setObjectName("runOfflineRepeatSpin")
        self.repeat_spin.setRange(1, 100)
        self.repeat_spin.setValue(1)
        form.addRow("重复次数", self.repeat_spin)

        self.artifacts_picker = PathPicker(
            mode=PathPickerMode.DIRECTORY,
            caption="选择 Android artifact 目录",
            placeholder="包含 gfxinfo、meminfo、logcat 或 Perfetto 的目录",
            parent=self,
        )
        self.artifacts_picker.setObjectName("runOfflineArtifactsPicker")
        form.addRow("Android artifacts *", self.artifacts_picker)

        self.package_edit = QLineEdit(self)
        self.package_edit.setObjectName("runOfflinePackageEdit")
        self.package_edit.setPlaceholderText("可选；用于补充 artifact 解析上下文")
        form.addRow("包名", self.package_edit)
        form.addRow(
            QLabel(
                "离线模式不会显示设备、APK 安装、Activity、Perfetto 或人工步骤选项。",
                self,
            )
        )
        self._connect_changes()

    def _connect_changes(self) -> None:
        self.scenario_picker.path_changed.connect(lambda _value: self.changed.emit())
        self.output_edit.textChanged.connect(lambda _value: self.changed.emit())
        self.repeat_spin.valueChanged.connect(lambda _value: self.changed.emit())
        self.artifacts_picker.path_changed.connect(lambda _value: self.changed.emit())
        self.package_edit.textChanged.connect(lambda _value: self.changed.emit())


class RunPage(BasePage):
    open_result_requested = Signal(object)
    open_log_requested = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: SettingsService | None = None,
        workspace_service: WorkspaceService | None = None,
        device_service: DeviceService | None = None,
        controller: TaskController | None = None,
        builder_factory: BuilderFactory | None = None,
    ) -> None:
        super().__init__(
            "单版本采集",
            "从 Android 设备采集性能数据，或分析已有 Android artifact。",
            parent,
        )
        self._settings = settings or SettingsService()
        self._workspace_service = workspace_service or WorkspaceService(self._settings)
        self.device_service = device_service or DeviceService(
            self._settings,
            self._workspace_service,
            self,
        )
        self.controller = controller or TaskController(self)
        self._builder_factory = builder_factory or (
            lambda workspace: command_builder_from_settings(self._settings, workspace)
        )

        config_content = QWidget(self)
        config_layout = QVBoxLayout(config_content)
        config_layout.setContentsMargins(4, 8, 4, 8)
        config_layout.setSpacing(10)

        self.workspace_label = QLabel(
            f"当前 workspace：{self._workspace_service.current}",
            config_content,
        )
        self.workspace_label.setObjectName("runWorkspaceLabel")
        self.workspace_label.setWordWrap(True)
        config_layout.addWidget(self.workspace_label)

        self.mode_tabs = QTabWidget(config_content)
        self.mode_tabs.setObjectName("runModeTabs")
        self.device_form = DeviceRunForm(self.mode_tabs)
        self.offline_form = OfflineRunForm(self.mode_tabs)
        self.mode_tabs.addTab(self.device_form, "设备采集")
        self.mode_tabs.addTab(self.offline_form, "离线 artifact")
        self.mode_tabs.setMinimumHeight(430)
        config_layout.addWidget(self.mode_tabs)

        self.validation_banner = ValidationBanner(config_content)
        config_layout.addWidget(self.validation_banner)

        self.manual_host = QWidget(self)
        manual_layout = QHBoxLayout(self.manual_host)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        self.manual_banner = ValidationBanner(self.manual_host)
        manual_layout.addWidget(self.manual_banner, 1)
        self.continue_button = QPushButton("已完成，继续", self.manual_host)
        self.continue_button.clicked.connect(self._continue_manual)
        manual_layout.addWidget(self.continue_button)
        self.manual_host.hide()

        self.preview_group = QGroupBox("命令预览", config_content)
        self.preview_group.setCheckable(True)
        self.preview_group.setChecked(False)
        preview_layout = QVBoxLayout(self.preview_group)
        self.preview_edit = QPlainTextEdit(self.preview_group)
        self.preview_edit.setObjectName("runCommandPreview")
        self.preview_edit.setAccessibleName("等价 CLI 命令预览")
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setMaximumHeight(76)
        preview_layout.addWidget(self.preview_edit)
        self.copy_preview_button = QPushButton("复制命令", self.preview_group)
        self.copy_preview_button.setEnabled(False)
        self.copy_preview_button.clicked.connect(self.copy_preview)
        preview_layout.addWidget(self.copy_preview_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.preview_group.toggled.connect(self.preview_edit.setVisible)
        self.preview_group.toggled.connect(self.copy_preview_button.setVisible)
        self.preview_edit.hide()
        self.copy_preview_button.hide()
        config_layout.addWidget(self.preview_group)
        config_layout.addStretch(1)

        actions = QHBoxLayout()
        self.preview_button = QPushButton("校验并预览", self)
        self.preview_button.setObjectName("previewRunCommandButton")
        self.preview_button.clicked.connect(self.preview_command)
        actions.addWidget(self.preview_button)
        self.start_button = QPushButton("开始", self)
        self.start_button.setObjectName("startRunButton")
        self.start_button.clicked.connect(self.start_task)
        actions.addWidget(self.start_button)
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.setObjectName("cancelRunButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_task)
        actions.addWidget(self.cancel_button)
        self.clear_button = QPushButton("清空任务区", self)
        self.clear_button.clicked.connect(self.clear_task_area)
        actions.addWidget(self.clear_button)
        actions.addStretch(1)

        config_scroll = QScrollArea(self)
        config_scroll.setObjectName("runConfigScroll")
        config_scroll.setWidgetResizable(True)
        config_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        config_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        config_scroll.setWidget(config_content)

        self.section_tabs = QTabWidget(self)
        self.section_tabs.setObjectName("runTaskTabs")
        self.task_console = TaskConsole(self.section_tabs)
        self.task_summary = TaskSummary(self.section_tabs)
        self.section_tabs.addTab(config_scroll, "配置")
        self.section_tabs.addTab(self.task_console, "任务日志")
        self.section_tabs.addTab(self.task_summary, "结果")
        self.layout.addWidget(self.manual_host)
        self.layout.addLayout(actions)
        self.layout.addWidget(self.section_tabs, 1)

        self.task_console.bind_controller(self.controller)
        self.task_summary.bind_controller(self.controller)
        self.task_console.open_log_requested.connect(self.open_log_requested)
        self.task_summary.open_result_requested.connect(self.open_result_requested)
        self.device_form.changed.connect(self._form_changed)
        self.offline_form.changed.connect(self._form_changed)
        self.device_form.refresh_devices_requested.connect(self.refresh_devices)
        self.mode_tabs.currentChanged.connect(lambda _index: self._form_changed())
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.validation_failed.connect(self._on_controller_validation)
        self.controller.manual_requested.connect(self._on_manual_requested)
        self.controller.launch_failed.connect(self._on_launch_failed)
        self.controller.finished.connect(self._on_finished)
        self.device_service.snapshot_ready.connect(self._on_device_snapshot)
        self.device_service.refresh_started.connect(
            lambda: self.device_form.refresh_devices_button.setEnabled(False)
        )
        self.device_service.refresh_finished.connect(
            lambda: self.device_form.refresh_devices_button.setEnabled(True)
        )
        self._workspace_service.workspace_changed.connect(self._on_workspace_changed)

    @property
    def is_active(self) -> bool:
        return self.controller.is_active

    def shutdown(self) -> bool:
        return self.controller.shutdown()

    def preview_command(self) -> TaskSpec | None:
        return self._prepare_task(start=False)

    def start_task(self) -> TaskSpec | None:
        return self._prepare_task(start=True)

    def cancel_task(self) -> None:
        if not self.controller.cancel():
            self.validation_banner.set_messages(
                "当前没有可取消的运行中任务。",
                severity=ValidationSeverity.WARNING,
            )

    def refresh_devices(self) -> None:
        if not self.device_service.refresh():
            self.validation_banner.set_messages(
                "设备刷新正在进行，请稍候。",
                severity=ValidationSeverity.WARNING,
            )

    def copy_preview(self) -> None:
        command = self.preview_edit.toPlainText()
        if command:
            QGuiApplication.clipboard().setText(command)

    def clear_task_area(self) -> None:
        if self.controller.is_active:
            self.validation_banner.set_messages(
                "请先取消并等待当前任务结束。",
                severity=ValidationSeverity.WARNING,
            )
            return
        self.controller.reset()
        self.validation_banner.clear()
        self.manual_host.hide()
        self.preview_edit.clear()
        self.copy_preview_button.setEnabled(False)
        self.task_console.clear_visible()
        self.task_console.set_command_preview("")
        self.task_summary.clear()
        self.task_summary.set_command_preview("")

    def _prepare_task(self, *, start: bool) -> TaskSpec | None:
        try:
            task = self._build_task()
        except CommandValidationError as exc:
            self._show_issues(exc.issues)
            return None
        except Exception as exc:
            self.validation_banner.set_messages(
                f"无法构造任务：{exc}",
                severity=ValidationSeverity.ERROR,
            )
            return None

        self._set_command_preview(task.display_command)
        warnings = resolve_tool_paths(self._settings).warnings
        if warnings:
            self.validation_banner.set_messages(warnings, severity=ValidationSeverity.WARNING)
        else:
            self.validation_banner.clear()

        if start:
            self.task_console.clear_visible()
            self.task_summary.clear()
            try:
                if not self.controller.start(task):
                    return None
                self.section_tabs.setCurrentIndex(1)
            except Exception as exc:
                self.validation_banner.set_messages(
                    f"任务无法启动：{exc}",
                    severity=ValidationSeverity.ERROR,
                )
                return None
        return task

    def _build_task(self) -> TaskSpec:
        builder = self._builder_factory(self._workspace_service.current)
        if self.mode_tabs.currentIndex() == 0:
            form = self.device_form
            return builder.build_run_device(
                scenario=form.scenario_picker.path,
                output=form.output_edit.text().strip(),
                repeat=form.repeat_spin.value(),
                package_name=form.package_edit.text().strip() or None,
                serial=form.serial(),
                apk=form.apk_picker.path or None,
                activity=form.activity_edit.text().strip() or None,
                clear_data=form.clear_data_check.isChecked(),
                reinstall=form.reinstall_check.isChecked(),
                perfetto=form.perfetto_check.isChecked(),
                post_launch_wait=form.post_launch_wait_spin.value(),
                interactive=form.interactive_check.isChecked(),
                sampling_off=form.sampling_off_check.isChecked(),
                sampling_interval_ms=form.sampling_interval_spin.value() or None,
                screen_record=form.screen_record_check.isChecked(),
            )

        form = self.offline_form
        if not form.artifacts_picker.path:
            raise CommandValidationError(
                [ValidationIssue("android_artifacts", "不能为空")]
            )
        return builder.build_run_offline(
            scenario=form.scenario_picker.path,
            output=form.output_edit.text().strip(),
            repeat=form.repeat_spin.value(),
            android_artifacts=form.artifacts_picker.path,
            package_name=form.package_edit.text().strip() or None,
        )

    def _set_command_preview(self, command: str) -> None:
        self.preview_edit.setPlainText(command)
        self.copy_preview_button.setEnabled(bool(command))
        self.task_console.set_command_preview(command)
        self.task_summary.set_command_preview(command)
        self.preview_group.setChecked(True)

    def _show_issues(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.validation_banner.set_messages(
            [f"{issue.field}：{issue.message}" for issue in issues],
            severity=ValidationSeverity.ERROR,
        )

    def _form_changed(self) -> None:
        if self.controller.is_active:
            return
        self.validation_banner.clear()
        self.preview_edit.clear()
        self.copy_preview_button.setEnabled(False)
        self.task_console.set_command_preview("")
        self.task_summary.set_command_preview("")

    def _on_state_changed(self, state: TaskState) -> None:
        active = state in {
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.WAITING_FOR_USER,
            TaskState.CANCELLING,
        }
        self.mode_tabs.setEnabled(not active)
        self.preview_button.setEnabled(not active)
        self.start_button.setEnabled(not active)
        self.clear_button.setEnabled(not active)
        self.cancel_button.setEnabled(active and state != TaskState.CANCELLING)
        if state != TaskState.WAITING_FOR_USER:
            self.manual_host.hide()

    def _on_controller_validation(self, issues: tuple[str, ...]) -> None:
        self.validation_banner.set_messages(list(issues), severity=ValidationSeverity.ERROR)

    def _on_manual_requested(self, request: object) -> None:
        prompt = str(getattr(request, "prompt", "请完成设备上的人工步骤。"))
        timeout = getattr(request, "timeout_seconds", None)
        if timeout:
            prompt += f"（CLI 超时：{timeout:g} 秒）"
        self.manual_banner.set_messages(prompt, severity=ValidationSeverity.WARNING)
        self.manual_host.show()

    def _continue_manual(self) -> None:
        if self.controller.continue_manual():
            self.manual_host.hide()
        else:
            self.validation_banner.set_messages(
                "当前任务未处于等待人工步骤状态。",
                severity=ValidationSeverity.WARNING,
            )

    def _on_launch_failed(self, message: object) -> None:
        self.validation_banner.set_messages(
            f"进程无法启动：{message}。请检查 CLI 路径、权限和安装版本。",
            severity=ValidationSeverity.ERROR,
        )

    def _on_finished(self, _result: object) -> None:
        self.manual_host.hide()
        self.section_tabs.setCurrentIndex(2)

    def _on_device_snapshot(self, snapshot: EnvironmentSnapshot) -> None:
        self.device_form.set_devices(snapshot)
        if snapshot.android_status in {"adb_missing", "offline", "unauthorized", "no_devices"}:
            self.validation_banner.set_messages(
                _device_snapshot_message(snapshot.android_status),
                severity=ValidationSeverity.WARNING,
            )

    def _on_workspace_changed(self, workspace: str) -> None:
        self.workspace_label.setText(f"当前 workspace：{workspace}")
        self._form_changed()


def _device_snapshot_message(status: str) -> str:
    return {
        "adb_missing": "未找到 adb；请在环境页配置 adb 可执行文件。",
        "offline": "设备处于 offline；重新连接 USB 并刷新。",
        "unauthorized": "设备未授权；解锁设备并确认 USB 调试授权。",
        "no_devices": "未发现设备；连接设备、开启 USB 调试并刷新。",
    }.get(status, "当前没有可用的 Android 设备。")
