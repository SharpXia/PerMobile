from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.design_tokens import COMPONENTS
from perfeval.gui.models.device import EnvironmentSnapshot
from perfeval.gui.models.history import HistoryQuery, HistoryTaskKind
from perfeval.gui.models.preset import (
    PresetScenario,
    PresetCategory,
    PresetSource,
    PresetTaskKind,
    TaskLaunchConfig,
)
from perfeval.gui.services.command_builder import (
    CandidateInput,
    CommandBuilder,
    CommandValidationError,
)
from perfeval.gui.services.device_service import DeviceService
from perfeval.gui.services.experiment_cache import ExperimentCacheService
from perfeval.gui.services.preset_catalog import PresetCatalogService
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.tool_paths import command_builder_from_settings
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.widgets.candidate_editor import CandidateEditor
from perfeval.gui.widgets.path_picker import PathPicker, PathPickerMode
from perfeval.gui.widgets.recent_value_combo import RecentValueComboBox
from perfeval.gui.widgets.validation_banner import ValidationBanner, ValidationSeverity

from .ab_page import AbPage
from .base_page import BasePage
from .diagnose_page import DiagnosePage
from .run_page import RunPage


BuilderFactory = Callable[[Path], CommandBuilder]
DEFAULT_PACKAGE_NAME = "com.perfeval.gamebox"


_PHASES = {
    PresetTaskKind.RUN: (
        ("device_check", "检查设备"),
        ("app_prepare", "启动应用"),
        ("interaction", "执行测试操作"),
        ("collection", "采集性能数据"),
        ("analysis", "分析采集数据"),
        ("report", "生成报告"),
    ),
    PresetTaskKind.AB: (
        ("device_check", "检查设备"),
        ("variant_prepare", "准备测试版本"),
        ("interaction", "执行测试操作"),
        ("collection", "采集性能数据"),
        ("comparison", "比较版本结果"),
        ("report", "生成报告"),
    ),
    PresetTaskKind.DIAGNOSE: (
        ("device_check", "检查设备"),
        ("diagnosis", "采集诊断证据"),
        ("report", "生成报告"),
    ),
}


class QuickStartPanel(QWidget):
    start_requested = Signal(object)
    recent_results_requested = Signal(object)

    def __init__(
        self,
        *,
        settings: SettingsService,
        workspace_service: WorkspaceService,
        device_service: DeviceService,
        session: TaskSessionController,
        builder_factory: BuilderFactory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._workspace_service = workspace_service
        self._device_service = device_service
        self._session = session
        self._builder_factory = builder_factory
        self._catalog = PresetCatalogService(settings)
        self._presets: tuple[PresetScenario, ...] = ()
        self._snapshot: EnvironmentSnapshot | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.environment_label = QLabel("环境检查 · 正在检查设备…", self)
        self.environment_label.setObjectName("quickEnvironmentLine")
        layout.addWidget(self.environment_label)

        content = QFrame(self)
        content.setObjectName("quickStartSurface")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)

        scenario_panel = QFrame(content)
        scenario_panel.setObjectName("scenarioPanel")
        scenario_layout = QVBoxLayout(scenario_panel)
        scenario_layout.setContentsMargins(14, 14, 14, 14)
        scenario_layout.setSpacing(8)
        scenario_kicker = QLabel("测试场景", scenario_panel)
        scenario_kicker.setObjectName("scenarioKicker")
        scenario_layout.addWidget(scenario_kicker)
        scenario_help = QLabel("选择目标指标，系统会带你完成一次可复现测试", scenario_panel)
        scenario_help.setObjectName("scenarioHelp")
        scenario_help.setWordWrap(True)
        scenario_layout.addWidget(scenario_help)

        self.scenario_list = QListWidget(scenario_panel)
        self.scenario_list.setObjectName("presetScenarioList")
        self.scenario_list.setAccessibleName("预置场景")
        self.scenario_list.setMinimumWidth(190)
        self.scenario_list.setMaximumWidth(270)
        self.scenario_list.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        self.scenario_list.setSpacing(2)
        self.scenario_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scenario_list.currentRowChanged.connect(self._on_preset_changed)
        scenario_layout.addWidget(self.scenario_list, 1)
        content_layout.addWidget(scenario_panel)

        detail = QFrame(content)
        detail.setObjectName("presetDetailPane")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(20, 22, 20, 20)
        detail_layout.setSpacing(15)

        self.preset_title = QLabel("页面流畅度", detail)
        self.preset_title.setObjectName("presetTitle")
        self.preset_description = QLabel(detail)
        self.preset_description.setObjectName("pageDescription")
        self.preset_description.setWordWrap(True)
        self.source_label = QLabel(detail)
        self.source_label.setObjectName("presetSourceLabel")
        detail_layout.addWidget(self.preset_title)
        detail_layout.addWidget(self.preset_description)
        detail_layout.addWidget(self.source_label)

        form_intro = QLabel("先确认设备和应用，再开始采集。高级选项会保持折叠。", detail)
        form_intro.setObjectName("formIntro")
        form_intro.setWordWrap(True)
        detail_layout.addWidget(form_intro)

        line = QFrame(detail)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("subtleDivider")
        detail_layout.addWidget(line)

        self.form_host = QWidget(detail)
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(12)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.form.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        self.device_combo = QComboBox(self.form_host)
        self.device_combo.setObjectName("quickDeviceCombo")
        self.device_combo.setAccessibleName("快速启动设备")
        self.device_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.form.addRow("设备", self.device_combo)

        self.package_combo = RecentValueComboBox(self.form_host)
        self.package_combo.setObjectName("quickPackageCombo")
        self.package_combo.setPlaceholderText("例如 com.perfeval.gamebox")
        self.package_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.package_combo.remove_requested.connect(self._remove_recent_package)
        self.form.addRow("应用", self.package_combo)

        self.repeat_spin = QSpinBox(self.form_host)
        self.repeat_spin.setObjectName("quickRepeatSpin")
        self.repeat_spin.setRange(1, 100)
        self.repeat_spin.setValue(3)
        self.repeat_spin.setSuffix(" 次")
        self.repeat_spin.setMinimumWidth(120)
        self.repeat_note = QLabel("3 次适合快速检查；5 次可获得更高置信度。", self.form_host)
        self.repeat_note.setObjectName("mutedText")
        self.repeat_note.setWordWrap(True)
        repeat_host = QWidget(self.form_host)
        # Expand to the full field column; otherwise the wrapped note is squeezed
        # into its size hint width and overlaps the row below.
        repeat_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        repeat_layout = QVBoxLayout(repeat_host)
        repeat_layout.setContentsMargins(0, 0, 0, 0)
        repeat_layout.setSpacing(4)
        repeat_layout.addWidget(self.repeat_spin, 0, Qt.AlignmentFlag.AlignLeft)
        repeat_layout.addWidget(self.repeat_note)
        self.form.addRow("运行次数", repeat_host)

        self.baseline_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 Baseline APK",
            file_filter="Android APK (*.apk);;所有文件 (*)",
            parent=self.form_host,
        )
        self.form.addRow("Baseline APK", self.baseline_picker)
        self.candidate_editor = CandidateEditor(parent=self.form_host)
        self.form.addRow("Candidate", self.candidate_editor)

        self.app_toggle = QToolButton(self.form_host)
        self.app_toggle.setObjectName("quickAppToggle")
        self.app_toggle.setText("更换 APK 或应用")
        self.app_toggle.setCheckable(True)
        self.app_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.app_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.app_toggle.toggled.connect(self._toggle_app_details)
        self.form.addRow("", self.app_toggle)

        self.app_details = QGroupBox("应用设置", self.form_host)
        self.app_details.setObjectName("quickAppDetails")
        self.app_details.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.app_form = QFormLayout(self.app_details)
        self.app_form.setContentsMargins(16, 20, 16, 16)
        self.app_form.setHorizontalSpacing(16)
        self.app_form.setVerticalSpacing(12)
        self.app_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.app_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.app_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.apk_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 APK",
            file_filter="Android APK (*.apk);;所有文件 (*)",
            placeholder="可选；留空则使用设备中已安装的该包名应用",
            parent=self.app_details,
        )
        self.app_form.addRow("APK", self.apk_picker)
        self.activity_combo = QComboBox(self.app_details)
        self.activity_combo.setEditable(True)
        self.activity_combo.setPlaceholderText("可选；留空时自动识别 Launcher Activity")
        self.activity_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.activity_combo.setMinimumContentsLength(12)
        self.activity_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self.app_form.addRow("Activity", self.activity_combo)
        self.app_details.hide()
        self.form.addRow("", self.app_details)

        self.advanced_section = QWidget(self.form_host)
        self.advanced_section.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.advanced_section_layout = QVBoxLayout(self.advanced_section)
        self.advanced_section_layout.setContentsMargins(0, 0, 0, 0)
        self.advanced_section_layout.setSpacing(COMPONENTS.space_2)

        self.advanced_toggle = QToolButton(self.advanced_section)
        self.advanced_toggle.setObjectName("quickAdvancedToggle")
        self.advanced_toggle.setText("高级设置")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.advanced_section_layout.addWidget(
            self.advanced_toggle,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )

        self.advanced_group = QFrame(self.advanced_section)
        self.advanced_group.setObjectName("quickAdvancedGroup")
        self.advanced_group.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        advanced_form = QFormLayout(self.advanced_group)
        advanced_form.setContentsMargins(
            COMPONENTS.space_3,
            COMPONENTS.space_3 - 1,
            COMPONENTS.space_3,
            COMPONENTS.space_3,
        )
        self.clear_data_check = QCheckBox("运行前清除应用数据", self.advanced_group)
        self.reinstall_check = QCheckBox("运行前安装或重装 APK", self.advanced_group)
        self.perfetto_check = QCheckBox("同时采集 Perfetto", self.advanced_group)
        self.interactive_check = QCheckBox("允许场景中的人工步骤", self.advanced_group)
        advanced_form.addRow(self.clear_data_check)
        advanced_form.addRow(self.reinstall_check)
        advanced_form.addRow(self.perfetto_check)
        advanced_form.addRow(self.interactive_check)
        self.override_kind_combo = QComboBox(self.advanced_group)
        self.override_kind_combo.addItem("单版本采集", PresetTaskKind.RUN.value)
        self.override_kind_combo.addItem("多版本 A/B", PresetTaskKind.AB.value)
        self.override_category_combo = QComboBox(self.advanced_group)
        for label, value in (
            ("启动速度", PresetCategory.STARTUP),
            ("页面流畅度", PresetCategory.SMOOTHNESS),
            ("内存稳定性", PresetCategory.MEMORY),
            ("多版本 A/B", PresetCategory.AB),
            ("项目场景", PresetCategory.PROJECT),
        ):
            self.override_category_combo.addItem(label, value.value)
        self.save_override_button = QPushButton("保存场景归类", self.advanced_group)
        self.save_override_button.clicked.connect(self._save_override)
        advanced_form.addRow("任务类型", self.override_kind_combo)
        advanced_form.addRow("场景分类", self.override_category_combo)
        advanced_form.addRow("", self.save_override_button)
        self.advanced_group.hide()
        self.advanced_section_layout.addWidget(self.advanced_group)
        self.form.addRow("", self.advanced_section)

        self.form_scroll = QScrollArea(detail)
        self.form_scroll.setObjectName("quickFormScroll")
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.form_scroll.setWidget(self.form_host)
        detail_layout.addWidget(self.form_scroll, 1)

        self.validation_banner = ValidationBanner(detail)
        detail_layout.addWidget(self.validation_banner)
        content_layout.addWidget(detail, 1)
        layout.addWidget(content, 1)

        action_bar = QFrame(self)
        action_bar.setObjectName("quickActionBar")
        footer = QVBoxLayout(action_bar)
        footer.setContentsMargins(16, 12, 16, 12)
        footer.setSpacing(6)
        self.save_note = QLabel("将使用预置流程，报告保存到当前工作区。", self)
        self.save_note.setObjectName("mutedText")
        self.save_note.setWordWrap(True)
        footer.addWidget(self.save_note)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.recent_button = QPushButton("查看最近结果", self)
        self.recent_button.clicked.connect(self._request_recent_results)
        actions.addWidget(self.recent_button)
        self.start_button = QPushButton("开始测试", self)
        self.start_button.setObjectName("primaryActionButton")
        self.start_button.setMinimumWidth(160)
        self.start_button.clicked.connect(self._start)
        actions.addWidget(self.start_button)
        footer.addLayout(actions)
        layout.addWidget(action_bar)

        self._device_service.snapshot_ready.connect(self.set_environment_snapshot)
        self._workspace_service.workspace_changed.connect(lambda _value: self.reload())
        self._session.controller.state_changed.connect(self._update_enabled)
        self.apk_picker.path_changed.connect(self._apk_changed)
        self.reload()
        self._schedule_form_geometry_update()

    @property
    def selected_preset(self) -> PresetScenario | None:
        row = self.scenario_list.currentRow()
        return self._presets[row] if 0 <= row < len(self._presets) else None

    def reload(self) -> None:
        workspace = self._workspace_service.current
        self._presets = self._catalog.discover(workspace)
        selected_id = self._settings.recent_preset(workspace) or "builtin.smoothness"
        self.scenario_list.blockSignals(True)
        self.scenario_list.clear()
        selected_row = next(
            (
                index
                for index, preset in enumerate(self._presets)
                if preset.preset_id == "builtin.smoothness" and preset.valid
            ),
            0,
        )
        for index, preset in enumerate(self._presets):
            category = {
                PresetCategory.STARTUP: "启动耗时",
                PresetCategory.SMOOTHNESS: "帧稳定性",
                PresetCategory.MEMORY: "内存峰值",
                PresetCategory.AB: "版本差异",
                PresetCategory.PROJECT: "项目场景",
            }.get(preset.category, "性能指标")
            item = QListWidgetItem(f"{preset.title}\n{category}  ·  {preset.source_label}")
            item.setData(Qt.ItemDataRole.UserRole, preset.preset_id)
            item.setToolTip("；".join(preset.issues) if preset.issues else preset.description)
            if not preset.valid:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.scenario_list.addItem(item)
            if preset.preset_id == selected_id and preset.valid:
                selected_row = index
        self.scenario_list.blockSignals(False)
        if self._presets:
            self.scenario_list.setCurrentRow(selected_row)
            self._on_preset_changed(selected_row)

        current_package = self.package_combo.currentText()
        self._refresh_recent_packages(current_package)

    def set_environment_snapshot(self, snapshot: EnvironmentSnapshot) -> None:
        self._snapshot = snapshot
        online = [device for device in snapshot.devices if device.online]
        adb = snapshot.tool("adb")
        if len(online) == 1:
            name = online[0].model or online[0].serial
            self.environment_label.setText(f"● {name} 已连接 · adb {'可用' if adb and adb.available else '不可用'}")
            state = "ready" if adb and adb.available else "ambiguous"
        elif online:
            self.environment_label.setText(f"● {len(online)} 台设备已连接 · 请选择测试设备")
            state = "ambiguous"
        else:
            self.environment_label.setText("○ 未检测到在线 Android 设备 · 请刷新环境")
            state = "offline"
        self.environment_label.setProperty("environmentState", state)
        self.environment_label.style().unpolish(self.environment_label)
        self.environment_label.style().polish(self.environment_label)

        previous = self._settings.recent_device(self._workspace_service.current)
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if len(online) != 1:
            self.device_combo.addItem("请选择设备" if online else "未检测到在线设备", None)
        for device in online:
            label = f"{device.model} · {device.serial}" if device.model else device.serial
            self.device_combo.addItem(label, device.serial)
        index = self.device_combo.findData(previous)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        self.device_combo.blockSignals(False)

    def _on_preset_changed(self, row: int) -> None:
        if not 0 <= row < len(self._presets):
            return
        preset = self._presets[row]
        self._settings.set_recent_preset(self._workspace_service.current, preset.preset_id)
        self.preset_title.setText(preset.title)
        self.preset_description.setText(preset.description)
        self.source_label.setText(f"{preset.source_label}场景" + (f" · {preset.relative_path}" if preset.relative_path else ""))
        self.repeat_spin.setValue(preset.default_repeat or 3)
        self.package_combo.setEditText(preset.default_package or DEFAULT_PACKAGE_NAME)
        if preset.default_activity:
            self.activity_combo.setEditText(preset.default_activity)
        self.interactive_check.setChecked(preset.interactive)
        self.override_kind_combo.setCurrentIndex(
            max(0, self.override_kind_combo.findData(preset.task_kind.value))
        )
        self.override_category_combo.setCurrentIndex(
            max(0, self.override_category_combo.findData(preset.category.value))
        )
        workspace_preset = preset.source is PresetSource.WORKSPACE
        self.override_kind_combo.setVisible(workspace_preset)
        self.override_category_combo.setVisible(workspace_preset)
        self.save_override_button.setVisible(workspace_preset)
        for field in (self.override_kind_combo, self.override_category_combo, self.save_override_button):
            label = advanced_form_label(self.advanced_group, field)
            if label is not None:
                label.setVisible(workspace_preset)
        ab = preset.task_kind is PresetTaskKind.AB
        self.baseline_picker.setVisible(ab)
        self.candidate_editor.setVisible(ab)
        self.form.labelForField(self.baseline_picker).setVisible(ab)
        self.form.labelForField(self.candidate_editor).setVisible(ab)
        self.repeat_spin.setEnabled(preset.task_kind is not PresetTaskKind.DIAGNOSE)
        self.validation_banner.clear()
        self._schedule_form_geometry_update()

    def _toggle_app_details(self, checked: bool) -> None:
        self.app_details.setVisible(checked)
        self.app_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self._schedule_form_geometry_update()

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_group.setVisible(checked)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self._schedule_form_geometry_update()

    def _schedule_form_geometry_update(self) -> None:
        QTimer.singleShot(0, self._update_form_geometry)

    def _update_form_geometry(self) -> None:
        compact = self.form_scroll.viewport().width() < 360
        self.form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows
            if compact
            else QFormLayout.RowWrapPolicy.DontWrapRows
        )
        self.app_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows
            if compact
            else QFormLayout.RowWrapPolicy.DontWrapRows
        )
        self.app_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            if compact
            else Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        if self.width() < 760:
            self.scenario_list.setMinimumWidth(165)
            self.scenario_list.setMaximumWidth(180)
        else:
            self.scenario_list.setMinimumWidth(220)
            self.scenario_list.setMaximumWidth(270)
        self.form.invalidate()
        self.form.activate()
        # Word-wrapping rows (e.g. the repeat note) need height-for-width; the
        # plain sizeHint underestimates them and lets text rows overlap.
        width = self.form_scroll.viewport().width()
        height = self.form.sizeHint().height()
        if self.form.hasHeightForWidth() and width > 0:
            height = max(height, self.form.heightForWidth(width))
        self.form_host.setMinimumHeight(height)
        self.form_host.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._schedule_form_geometry_update()

    def _apk_changed(self, path: str) -> None:
        if path:
            self.reinstall_check.setChecked(True)

    def _start(self) -> None:
        preset = self.selected_preset
        if preset is None or not preset.valid:
            self.validation_banner.set_messages("请选择一个可用场景。", severity=ValidationSeverity.ERROR)
            return
        try:
            launch = self._build_launch(preset)
            if self._session.start(launch):
                self._remember_inputs(launch)
                self.start_requested.emit(launch)
        except CommandValidationError as exc:
            self.validation_banner.set_messages(
                [f"{issue.field}：{issue.message}" for issue in exc.issues],
                severity=ValidationSeverity.ERROR,
            )
        except Exception as exc:
            self.validation_banner.set_messages(str(exc), severity=ValidationSeverity.ERROR)

    def _request_recent_results(self) -> None:
        preset = self.selected_preset
        if preset is None:
            return
        raw_scenario_id = preset.metadata.get("scenarioId")
        scenario_id = (
            str(raw_scenario_id).strip()
            if isinstance(raw_scenario_id, str) and raw_scenario_id.strip()
            else None
        )
        package_name = self.package_combo.currentText().strip() or preset.default_package
        query = HistoryQuery(
            label=preset.title,
            preset_id=preset.preset_id,
            scenario_id=scenario_id,
            package_name=package_name,
            task_kind=(
                HistoryTaskKind.COMPARE
                if preset.task_kind is PresetTaskKind.AB
                else HistoryTaskKind.RUN
            ),
        )
        self.recent_results_requested.emit(query)

    def _save_override(self) -> None:
        preset = self.selected_preset
        if preset is None or preset.source is not PresetSource.WORKSPACE or not preset.relative_path:
            return
        self._catalog.set_workspace_override(
            self._workspace_service.current,
            preset.relative_path,
            task_kind=PresetTaskKind(str(self.override_kind_combo.currentData())),
            category=PresetCategory(str(self.override_category_combo.currentData())),
        )
        selected_id = preset.preset_id
        self.reload()
        for row in range(self.scenario_list.count()):
            if self.scenario_list.item(row).data(Qt.ItemDataRole.UserRole) == selected_id:
                self.scenario_list.setCurrentRow(row)
                break
        self.validation_banner.set_messages("场景归类已保存；YAML 文件未被修改。", severity=ValidationSeverity.INFO)

    def _build_launch(self, preset: PresetScenario) -> TaskLaunchConfig:
        workspace = self._workspace_service.current
        builder = self._builder_factory(workspace)
        serial = self.device_combo.currentData()
        package_name = self.package_combo.currentText().strip()
        device_label = self.device_combo.currentText().strip() or None
        repeat = self.repeat_spin.value()
        if self._snapshot is not None:
            online = [device for device in self._snapshot.devices if device.online]
            if not online:
                raise ValueError("未检测到在线 Android 设备；请连接设备并刷新环境。")
            if len(online) > 1 and not serial:
                raise ValueError("检测到多台在线设备，请先选择测试设备。")
        if not package_name and not preset.default_package:
            raise ValueError("请输入要测试的 Android 应用包名。")
        reservation = ExperimentCacheService.reserve(workspace, preset.title)
        metadata = {
            "preset_id": preset.preset_id,
            "preset_task_kind": preset.task_kind.value,
            "scenario_title": preset.title,
            "device_serial": serial,
            "device_label": device_label,
            "package_name": package_name,
            "repeat_count": repeat,
            "quick_mode": True,
            "phase_ids": _PHASES[preset.task_kind],
            "experiment_id": reservation.experiment_id,
            "experiment_root": str(reservation.root),
        }
        if preset.task_kind is PresetTaskKind.RUN:
            if preset.scenario_path is None:
                raise ValueError("场景资源缺失")
            task = builder.build_run_device(
                scenario=preset.scenario_path,
                output=reservation.data_directory,
                repeat=repeat,
                package_name=package_name or None,
                serial=str(serial) if serial else None,
                apk=self.apk_picker.path or None,
                activity=self.activity_combo.currentText().strip() or None,
                clear_data=self.clear_data_check.isChecked(),
                reinstall=self.reinstall_check.isChecked(),
                perfetto=self.perfetto_check.isChecked(),
                post_launch_wait=2.0,
                interactive=self.interactive_check.isChecked(),
                metadata=metadata,
            )
        elif preset.task_kind is PresetTaskKind.AB:
            if preset.scenario_path is None:
                raise ValueError("场景资源缺失")
            task = builder.build_ab(
                scenario=preset.scenario_path,
                baseline_apk=self.baseline_picker.path,
                candidates=self.candidate_editor.candidate_inputs(),
                output=reservation.data_directory,
                repeat=repeat,
                serial=str(serial) if serial else None,
                package_name=package_name or None,
                activity=self.activity_combo.currentText().strip() or None,
                clear_data=self.clear_data_check.isChecked(),
                allow_uninstall=False,
                perfetto=self.perfetto_check.isChecked(),
                post_launch_wait=2.0,
                interactive=self.interactive_check.isChecked(),
                metadata=metadata,
            )
        else:
            task = builder.build_diagnose(
                package_name=package_name,
                output=reservation.data_directory,
                case_id="quick-diagnose",
                mode=preset.default_mode or "full",
                duration=30,
                serial=str(serial) if serial else None,
                interactive=self.interactive_check.isChecked(),
                memory_profile="mainstream",
                metadata=metadata,
            )
        return TaskLaunchConfig(
            preset_id=preset.preset_id,
            title=preset.title,
            task_kind=preset.task_kind,
            task=task,
            device_serial=str(serial) if serial else None,
            device_label=device_label,
            package_name=package_name or None,
            repeat_count=repeat,
            source=preset.source,
            quick_mode=True,
            phase_ids=_PHASES[preset.task_kind],
        )

    def _remember_inputs(self, launch: TaskLaunchConfig) -> None:
        workspace = self._workspace_service.current
        self._settings.set_recent_device(workspace, launch.device_serial)
        self._settings.remember_package(workspace, launch.package_name)
        self._refresh_recent_packages(launch.package_name or self.package_combo.currentText())
        self._settings.remember_apk(workspace, self.apk_picker.path)
        self._settings.remember_apk(workspace, self.baseline_picker.path)
        for value in self.candidate_editor.candidate_inputs():
            self._settings.remember_apk(workspace, value.apk)

    def _remove_recent_package(self, package_name: str) -> None:
        current = self.package_combo.currentText()
        self._settings.forget_package(self._workspace_service.current, package_name)
        self._refresh_recent_packages(current)

    def _refresh_recent_packages(self, current_text: str) -> None:
        values = self._settings.recent_packages(self._workspace_service.current)
        self.package_combo.set_recent_values(values, current_text=current_text)

    def _update_enabled(self, _state: object) -> None:
        self.start_button.setEnabled(not self._session.is_active)
        self.scenario_list.setEnabled(not self._session.is_active)
        self.form_host.setEnabled(not self._session.is_active)


class StartPage(BasePage):
    task_started = Signal(object)
    navigation_requested = Signal(str)
    history_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: SettingsService,
        workspace_service: WorkspaceService,
        device_service: DeviceService,
        session: TaskSessionController,
        builder_factory: BuilderFactory | None = None,
    ) -> None:
        super().__init__("性能工作台", "从一个场景开始，把性能变化变成可复现的证据。", parent)
        self._settings = settings
        self._workspace_service = workspace_service
        self.device_service = device_service
        self.session = session
        self._builder_factory = builder_factory or (
            lambda root: command_builder_from_settings(settings, root)
        )

        self.mode_tabs = QTabWidget(self)
        self.mode_tabs.setObjectName("startModeTabs")
        self.quick_panel = QuickStartPanel(
            settings=settings,
            workspace_service=workspace_service,
            device_service=device_service,
            session=session,
            builder_factory=self._builder_factory,
            parent=self.mode_tabs,
        )
        self.custom_tabs = QTabWidget(self.mode_tabs)
        self.custom_tabs.setObjectName("customTaskTabs")
        self.custom_run = RunPage(
            self.custom_tabs,
            settings=settings,
            workspace_service=workspace_service,
            device_service=device_service,
            controller=session.controller,
            builder_factory=self._builder_factory,
        )
        builder = self._builder_factory(workspace_service.current)
        self.custom_ab = AbPage(
            self.custom_tabs,
            workspace=workspace_service.current,
            command_builder=builder,
            controller=session.controller,
        )
        self.custom_diagnose = DiagnosePage(
            self.custom_tabs,
            settings=settings,
            workspace=workspace_service.current,
            command_builder=builder,
            controller=session.controller,
        )
        self.custom_tabs.addTab(self.custom_run, "单版本 / 离线")
        self.custom_tabs.addTab(self.custom_ab, "A/B 对比")
        self.custom_tabs.addTab(self.custom_diagnose, "聚焦诊断")

        self.mode_tabs.addTab(self.quick_panel, "一键测试")
        self.mode_tabs.addTab(self.custom_tabs, "高级配置")
        self.layout.addWidget(self.mode_tabs, 1)

        self.quick_panel.start_requested.connect(self.task_started)
        self.quick_panel.recent_results_requested.connect(self.history_requested)
        session.controller.started.connect(lambda _value: self.task_started.emit(session.launch))
        workspace_service.workspace_changed.connect(self._on_workspace_changed)

    @property
    def is_active(self) -> bool:
        return self.session.is_active

    def shutdown(self) -> bool:
        return self.session.shutdown()

    def _on_workspace_changed(self, workspace: str) -> None:
        builder = self._builder_factory(Path(workspace))
        self.custom_ab.set_workspace(workspace, command_builder=builder)
        self.custom_diagnose.set_workspace(workspace)


__all__ = ["QuickStartPanel", "StartPage"]


def advanced_form_label(group: QWidget, field: QWidget) -> QWidget | None:
    layout = group.layout()
    return layout.labelForField(field) if isinstance(layout, QFormLayout) else None
