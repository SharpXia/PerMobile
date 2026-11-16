from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.device import DeviceInfo, EnvironmentSnapshot, ToolStatus
from ..services.device_service import DeviceService
from ..services.settings import SettingsService
from ..services.workspace import WorkspaceService
from ..widgets.path_picker import PathPicker, PathPickerMode
from ..widgets.validation_banner import ValidationBanner, ValidationSeverity
from .base_page import BasePage


class EnvironmentPage(BasePage):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: SettingsService | None = None,
        workspace_service: WorkspaceService | None = None,
        device_service: DeviceService | None = None,
    ) -> None:
        super().__init__(
            "环境与设备",
            "检查 PerfEval CLI、adb、外部工具和已连接 Android 设备。刷新只执行只读探测。",
            parent,
        )
        self._settings = settings or SettingsService()
        self._workspace_service = workspace_service or WorkspaceService(self._settings)
        self.device_service = device_service or DeviceService(
            self._settings,
            self._workspace_service,
            self,
        )
        self._snapshot: EnvironmentSnapshot | None = None

        paths_group = QGroupBox("工具路径", self)
        paths_form = QFormLayout(paths_group)
        paths_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.cli_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 PerfEval CLI",
            placeholder="留空时自动定位当前环境中的 perfeval",
            parent=paths_group,
        )
        self.cli_picker.setObjectName("environmentCliPicker")
        self.cli_picker.set_path(self._settings.external_tool_path("cli") or "")
        paths_form.addRow("PerfEval CLI", self.cli_picker)

        self.adb_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 adb 可执行文件",
            placeholder="留空时使用系统 PATH 中的 adb",
            parent=paths_group,
        )
        self.adb_picker.setObjectName("environmentAdbPicker")
        self.adb_picker.set_path(self._settings.external_tool_path("adb") or "")
        paths_form.addRow("adb", self.adb_picker)

        self.android_studio_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 Android Studio",
            placeholder="可选，用于后续打开专业 artifact",
            parent=paths_group,
        )
        self.android_studio_picker.setObjectName("environmentAndroidStudioPicker")
        self.android_studio_picker.set_path(
            self._settings.external_tool_path("android_studio") or ""
        )
        paths_form.addRow("Android Studio", self.android_studio_picker)
        self.layout.addWidget(paths_group)

        actions = QHBoxLayout()
        self.save_button = QPushButton("保存路径", self)
        self.save_button.setObjectName("saveEnvironmentPathsButton")
        self.save_button.clicked.connect(self.save_paths)
        actions.addWidget(self.save_button)
        self.refresh_button = QPushButton("刷新环境与设备", self)
        self.refresh_button.setObjectName("refreshEnvironmentButton")
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        self.layout.addLayout(actions)

        self.validation_banner = ValidationBanner(self)
        self.layout.addWidget(self.validation_banner)

        results = QTabWidget(self)
        results.setObjectName("environmentResultsTabs")
        self.tools_table = self._create_table(
            ("工具", "状态", "生效路径", "说明"),
            compact_columns=(0, 1),
        )
        self.tools_table.setObjectName("environmentToolsTable")
        results.addTab(self.tools_table, "工具状态")
        self.devices_table = self._create_table(
            ("Serial", "型号", "状态", "处理建议"),
            compact_columns=(0, 1, 2),
        )
        self.devices_table.setObjectName("environmentDevicesTable")
        results.addTab(self.devices_table, "Android 设备")
        self.layout.addWidget(results, 1)
        self._set_initial_rows()

        self.device_service.refresh_started.connect(self._on_refresh_started)
        self.device_service.snapshot_ready.connect(self._on_snapshot)
        self.device_service.refresh_failed.connect(self._on_refresh_failed)
        self.device_service.refresh_finished.connect(self._on_refresh_finished)

    @property
    def snapshot(self) -> EnvironmentSnapshot | None:
        return self._snapshot

    def save_paths(self) -> None:
        self._settings.set_external_tool_path("cli", self.cli_picker.path or None)
        self._settings.set_external_tool_path("adb", self.adb_picker.path or None)
        self._settings.set_external_tool_path(
            "android_studio",
            self.android_studio_picker.path or None,
        )
        self.validation_banner.set_messages(
            "工具路径已保存；点击刷新后验证并显示实际生效路径。",
            severity=ValidationSeverity.INFO,
        )

    def refresh(self) -> None:
        self.save_paths()
        if not self.device_service.refresh():
            self.validation_banner.set_messages(
                "环境刷新正在进行，请等待当前只读探测完成。",
                severity=ValidationSeverity.WARNING,
            )

    def _on_refresh_started(self) -> None:
        self.refresh_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.validation_banner.set_messages(
            "正在检查 CLI、adb、外部工具和设备…",
            severity=ValidationSeverity.INFO,
        )

    def _on_snapshot(self, snapshot: EnvironmentSnapshot) -> None:
        self._snapshot = snapshot
        self._populate_tools(snapshot.tools)
        self._populate_devices(snapshot.devices)
        messages = list(snapshot.warnings)
        messages.extend(_diagnostic_suggestion(value) for value in snapshot.diagnostics)
        messages = list(dict.fromkeys(message for message in messages if message))
        if messages:
            self.validation_banner.set_messages(messages, severity=ValidationSeverity.WARNING)
        else:
            self.validation_banner.set_messages(
                "环境刷新完成；设备状态和生效工具路径已更新。",
                severity=ValidationSeverity.INFO,
            )

    def _on_refresh_failed(self, message: str) -> None:
        if self._snapshot is None:
            self.validation_banner.set_messages(message, severity=ValidationSeverity.WARNING)

    def _on_refresh_finished(self) -> None:
        self.refresh_button.setEnabled(True)
        self.save_button.setEnabled(True)

    def _populate_tools(self, tools: tuple[ToolStatus, ...]) -> None:
        self.tools_table.setRowCount(len(tools))
        for row, tool in enumerate(tools):
            values = (
                tool.label,
                "可用" if tool.available else "缺失/不可用",
                tool.path or "由系统提供",
                tool.detail or _tool_suggestion(tool),
            )
            for column, value in enumerate(values):
                self.tools_table.setItem(row, column, QTableWidgetItem(value))

    def _set_initial_rows(self) -> None:
        self.tools_table.setRowCount(1)
        for column, value in enumerate(("尚未刷新", "待检测", "—", "点击“刷新环境与设备”。")):
            self.tools_table.setItem(0, column, QTableWidgetItem(value))
        self.devices_table.setRowCount(1)
        for column, value in enumerate(("—", "—", "待检测", "点击刷新读取只读设备状态。")):
            self.devices_table.setItem(0, column, QTableWidgetItem(value))

    def _populate_devices(self, devices: tuple[DeviceInfo, ...]) -> None:
        if not devices:
            self.devices_table.setRowCount(1)
            for column, value in enumerate(
                ("—", "—", "未发现设备", "连接设备、开启 USB 调试，然后刷新。")
            ):
                self.devices_table.setItem(0, column, QTableWidgetItem(value))
            return

        self.devices_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            values = (
                device.serial,
                device.model or device.product or "未知",
                _device_state_label(device.state),
                _device_suggestion(device.state),
            )
            for column, value in enumerate(values):
                self.devices_table.setItem(row, column, QTableWidgetItem(value))

    @staticmethod
    def _create_table(
        headers: tuple[str, ...],
        *,
        compact_columns: tuple[int, ...],
    ) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setWordWrap(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column in range(len(headers)):
            mode = (
                QHeaderView.ResizeMode.ResizeToContents
                if column in compact_columns
                else QHeaderView.ResizeMode.Stretch
            )
            table.horizontalHeader().setSectionResizeMode(column, mode)
        table.setAccessibleName("、".join(headers))
        return table


def _device_state_label(state: str) -> str:
    return {
        "device": "在线",
        "offline": "离线",
        "unauthorized": "未授权",
    }.get(state, f"未知（{state}）")


def _device_suggestion(state: str) -> str:
    if state == "device":
        return "可用于采集。"
    if state == "offline":
        return "重新连接 USB，必要时重启 adb 后刷新。"
    if state == "unauthorized":
        return "解锁设备并确认 USB 调试授权，然后刷新。"
    return "检查设备连接与 adb 输出后刷新。"


def _diagnostic_suggestion(diagnostic: str) -> str:
    if diagnostic.startswith("android.adb.not_found"):
        return "未找到 adb；请配置 platform-tools 中的 adb 可执行文件。"
    if diagnostic.startswith("android.device.none_online"):
        return "没有在线设备；请检查 USB 连接、调试授权和设备状态。"
    if diagnostic.startswith("android.device.ambiguous"):
        return "检测到多个在线设备；运行任务时请选择明确的 serial。"
    if "unauthorized" in diagnostic:
        return "设备未授权；解锁设备并确认 USB 调试授权。"
    if "offline" in diagnostic:
        return "设备离线；重新连接并刷新。"
    return diagnostic


def _tool_suggestion(tool: ToolStatus) -> str:
    if tool.key == "cli":
        return "检查 PerfEval 安装或配置正确的 CLI 可执行文件。"
    if tool.key == "adb":
        return "安装 Android platform-tools 或配置 adb 可执行文件。"
    if tool.key == "android_studio":
        return "可选；需要专业 artifact 查看时再配置。"
    return "检查本机默认应用配置。"
