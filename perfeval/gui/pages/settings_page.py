from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..services.gui_logging import GuiLogService, PRIVACY_NOTICE
from ..models.llm import LLM_API_FORMAT_LABELS, LlmApiFormat, LlmProfile
from ..services.llm_jobs import InterpretationJobManager
from ..services.llm_profiles import LlmProfileService
from ..services.llm_protocols import validate_base_url
from ..services.settings import SettingsService
from ..services.workspace import WorkspaceService
from ..theme import ThemeMode
from ..widgets.path_picker import PathPicker, PathPickerMode
from ..widgets.validation_banner import ValidationBanner, ValidationSeverity
from .base_page import BasePage


class SettingsPage(BasePage):
    theme_change_requested = Signal(str)

    def __init__(
        self,
        settings: SettingsService,
        workspace_service: WorkspaceService,
        parent: QWidget | None = None,
        *,
        diagnostics_service: GuiLogService | None = None,
        llm_profiles: LlmProfileService | None = None,
        interpretation_manager: InterpretationJobManager | None = None,
    ) -> None:
        super().__init__("设置", "管理 workspace、主题、外部工具和本机诊断偏好。", parent)
        self._settings = settings
        self._workspace_service = workspace_service
        self._diagnostics_service = diagnostics_service
        self._llm_profiles = llm_profiles or LlmProfileService(settings, self)
        self._interpretation_manager = interpretation_manager or InterpretationJobManager(
            self._llm_profiles,
            self,
        )
        self._editing_llm_profile_id: str | None = None
        self._connection_test_id: str | None = None

        scroll = QScrollArea(self)
        self.scroll = scroll
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 2, 14, 8)
        content_layout.setSpacing(18)

        workspace_section, workspace_group, workspace_layout = _settings_section(
            "工作区",
            content,
        )
        self.workspace_picker = PathPicker(
            mode=PathPickerMode.DIRECTORY,
            caption="选择 workspace",
            placeholder="选择现有目录，或输入要创建的新目录",
            parent=workspace_group,
        )
        self.workspace_picker.setObjectName("workspacePicker")
        self.workspace_picker.set_path(str(workspace_service.current))
        workspace_layout.addWidget(self.workspace_picker)
        self.apply_workspace_button = QPushButton("应用 workspace", workspace_group)
        self.apply_workspace_button.setObjectName("applyWorkspaceButton")
        self.apply_workspace_button.clicked.connect(self._apply_workspace)
        workspace_layout.addWidget(
            self.apply_workspace_button,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        content_layout.addWidget(workspace_section)

        appearance_section, appearance_group, appearance_layout = _settings_section(
            "外观",
            content,
        )
        appearance_row = QWidget(appearance_group)
        appearance_row_layout = QHBoxLayout(appearance_row)
        appearance_row_layout.setContentsMargins(0, 0, 0, 0)
        appearance_row_layout.setSpacing(20)
        appearance_copy = QVBoxLayout()
        appearance_copy.setSpacing(3)
        appearance_title = QLabel("主题", appearance_row)
        appearance_title.setObjectName("settingRowTitle")
        appearance_description = QLabel("使用系统外观，或固定使用浅色或深色。", appearance_row)
        appearance_description.setObjectName("mutedText")
        appearance_description.setWordWrap(True)
        appearance_copy.addWidget(appearance_title)
        appearance_copy.addWidget(appearance_description)
        appearance_row_layout.addLayout(appearance_copy, 1)
        self.theme_combo = QComboBox(appearance_group)
        self.theme_combo.setObjectName("themeCombo")
        self.theme_combo.setAccessibleName("主题")
        self.theme_combo.addItem("跟随系统", ThemeMode.SYSTEM.value)
        self.theme_combo.addItem("浅色", ThemeMode.LIGHT.value)
        self.theme_combo.addItem("深色", ThemeMode.DARK.value)
        theme_index = self.theme_combo.findData(settings.theme)
        self.theme_combo.setCurrentIndex(max(0, theme_index))
        self.theme_combo.setMinimumWidth(170)
        self.theme_combo.currentIndexChanged.connect(self._request_theme_change)
        appearance_row_layout.addWidget(self.theme_combo)
        appearance_layout.addWidget(appearance_row)
        content_layout.addWidget(appearance_section)

        tools_section, tools_group, tools_layout = _settings_section(
            "外部工具",
            content,
        )
        self.cli_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 PerfEval CLI",
            placeholder="留空时自动定位",
            parent=tools_group,
        )
        self.cli_picker.set_path(settings.external_tool_path("cli") or "")
        tools_layout.addWidget(_setting_field("PerfEval CLI", self.cli_picker, tools_group))
        self.adb_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 adb",
            placeholder="留空时使用系统 PATH",
            parent=tools_group,
        )
        self.adb_picker.set_path(settings.external_tool_path("adb") or "")
        tools_layout.addWidget(_setting_field("adb", self.adb_picker, tools_group))
        self.android_studio_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 Android Studio",
            placeholder="可选 artifact 查看工具",
            parent=tools_group,
        )
        self.android_studio_picker.set_path(
            settings.external_tool_path("android_studio") or ""
        )
        tools_layout.addWidget(
            _setting_field("Android Studio", self.android_studio_picker, tools_group)
        )
        self.trace_processor_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 trace_processor",
            placeholder="可选；Perfetto 帧时间轴解析，留空自动发现",
            parent=tools_group,
        )
        self.trace_processor_picker.setObjectName("traceProcessorPicker")
        self.trace_processor_picker.set_path(
            settings.external_tool_path("trace_processor") or ""
        )
        tools_layout.addWidget(
            _setting_field("trace_processor", self.trace_processor_picker, tools_group)
        )
        self.save_tools_button = QPushButton("保存工具路径", tools_group)
        self.save_tools_button.clicked.connect(self._save_tool_paths)
        tools_layout.addWidget(self.save_tools_button, 0, Qt.AlignmentFlag.AlignLeft)
        content_layout.addWidget(tools_section)

        llm_section, llm_group, llm_layout = _settings_section(
            "LLM 模型",
            content,
        )
        self.llm_section = llm_section
        profile_row = QHBoxLayout()
        self.llm_profile_combo = QComboBox(llm_group)
        self.llm_profile_combo.setObjectName("llmSettingsProfileCombo")
        self.llm_profile_combo.setAccessibleName("LLM 模型档案")
        self.llm_profile_combo.currentIndexChanged.connect(self._load_selected_llm_profile)
        profile_row.addWidget(self.llm_profile_combo, 1)
        self.new_llm_profile_button = QPushButton("新增", llm_group)
        self.new_llm_profile_button.clicked.connect(self._new_llm_profile)
        profile_row.addWidget(self.new_llm_profile_button)
        self.default_llm_profile_button = QPushButton("设为默认", llm_group)
        self.default_llm_profile_button.clicked.connect(self._set_default_llm_profile)
        profile_row.addWidget(self.default_llm_profile_button)
        self.delete_llm_profile_button = QPushButton("删除", llm_group)
        self.delete_llm_profile_button.setObjectName("dangerButton")
        self.delete_llm_profile_button.clicked.connect(self._delete_llm_profile)
        profile_row.addWidget(self.delete_llm_profile_button)
        llm_layout.addLayout(profile_row)

        self.llm_name_edit = QLineEdit(llm_group)
        self.llm_name_edit.setObjectName("llmProfileNameEdit")
        self.llm_name_edit.setAccessibleName("LLM 配置名称")
        self.llm_name_edit.setPlaceholderText("例如：公司网关 / Claude / Gemini")
        llm_layout.addWidget(_setting_field("配置名称", self.llm_name_edit, llm_group))

        self.llm_format_combo = QComboBox(llm_group)
        self.llm_format_combo.setObjectName("llmApiFormatCombo")
        self.llm_format_combo.setAccessibleName("LLM API 格式")
        for api_format, label in LLM_API_FORMAT_LABELS.items():
            self.llm_format_combo.addItem(label, api_format.value)
        self.llm_format_combo.currentIndexChanged.connect(self._apply_llm_format_default)
        llm_layout.addWidget(_setting_field("API 格式", self.llm_format_combo, llm_group))

        self.llm_url_edit = QLineEdit(llm_group)
        self.llm_url_edit.setObjectName("llmBaseUrlEdit")
        self.llm_url_edit.setAccessibleName("LLM Base URL")
        self.llm_url_edit.setPlaceholderText("https://api.example.com/v1")
        llm_layout.addWidget(_setting_field("Base URL", self.llm_url_edit, llm_group))

        self.llm_model_edit = QLineEdit(llm_group)
        self.llm_model_edit.setObjectName("llmModelEdit")
        self.llm_model_edit.setAccessibleName("LLM 模型名称")
        self.llm_model_edit.setPlaceholderText("服务端模型 ID")
        llm_layout.addWidget(_setting_field("模型", self.llm_model_edit, llm_group))

        api_key_row = QWidget(llm_group)
        api_key_layout = QHBoxLayout(api_key_row)
        api_key_layout.setContentsMargins(0, 0, 0, 0)
        self.llm_api_key_edit = QLineEdit(api_key_row)
        self.llm_api_key_edit.setObjectName("llmApiKeyEdit")
        self.llm_api_key_edit.setAccessibleName("LLM API Key")
        self.llm_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_api_key_edit.setPlaceholderText("输入后保存到系统凭据库")
        api_key_layout.addWidget(self.llm_api_key_edit, 1)
        self.show_llm_api_key = QCheckBox("显示", api_key_row)
        self.show_llm_api_key.toggled.connect(
            lambda checked: self.llm_api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        api_key_layout.addWidget(self.show_llm_api_key)
        llm_layout.addWidget(_setting_field("API Key", api_key_row, llm_group))

        self.llm_credential_notice = QLabel(llm_group)
        self.llm_credential_notice.setObjectName("mutedText")
        self.llm_credential_notice.setWordWrap(True)
        llm_layout.addWidget(self.llm_credential_notice)
        llm_actions = QHBoxLayout()
        self.save_llm_profile_button = QPushButton("保存模型配置", llm_group)
        self.save_llm_profile_button.setObjectName("primaryActionButton")
        self.save_llm_profile_button.clicked.connect(self._save_llm_profile)
        llm_actions.addWidget(self.save_llm_profile_button)
        self.test_llm_connection_button = QPushButton("测试连接", llm_group)
        self.test_llm_connection_button.clicked.connect(self._test_llm_connection)
        llm_actions.addWidget(self.test_llm_connection_button)
        llm_actions.addStretch(1)
        llm_layout.addLayout(llm_actions)
        content_layout.addWidget(llm_section)

        diagnostics_section, diagnostics_group, diagnostics_layout = _settings_section(
            "日志与诊断",
            content,
        )
        self.log_directory_picker = PathPicker(
            mode=PathPickerMode.DIRECTORY,
            caption="选择 GUI 日志目录",
            placeholder="默认保存到 workspace/.perfeval/gui-logs",
            parent=diagnostics_group,
        )
        self.log_directory_picker.setObjectName("guiLogDirectoryPicker")
        self.log_directory_picker.set_path(
            settings.log_directory
            or str(GuiLogService.default_log_directory(workspace_service.current))
        )
        diagnostics_layout.addWidget(
            _setting_field("GUI 日志目录", self.log_directory_picker, diagnostics_group)
        )
        diagnostics_actions = QHBoxLayout()
        self.apply_log_directory_button = QPushButton("应用日志目录", diagnostics_group)
        self.apply_log_directory_button.clicked.connect(self._apply_log_directory)
        diagnostics_actions.addWidget(self.apply_log_directory_button)
        self.export_diagnostics_button = QPushButton("导出诊断包…", diagnostics_group)
        self.export_diagnostics_button.setObjectName("exportDiagnosticsButton")
        self.export_diagnostics_button.clicked.connect(self._export_diagnostics)
        diagnostics_actions.addWidget(self.export_diagnostics_button)
        diagnostics_actions.addStretch(1)
        diagnostics_layout.addLayout(diagnostics_actions)
        privacy = QLabel(PRIVACY_NOTICE, diagnostics_group)
        privacy.setObjectName("mutedText")
        privacy.setWordWrap(True)
        diagnostics_layout.addWidget(privacy)
        content_layout.addWidget(diagnostics_section)

        self.reset_button = QPushButton("恢复默认偏好", content)
        self.reset_button.setObjectName("resetSettingsButton")
        self.reset_button.clicked.connect(self._reset_preferences)
        content_layout.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignLeft)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        self.layout.addWidget(scroll, 1)

        self.validation_banner = ValidationBanner(self)
        self.layout.addWidget(self.validation_banner)
        self.workspace_picker.path_changed.connect(lambda _path: self.validation_banner.clear())
        workspace_service.workspace_changed.connect(self._workspace_changed)
        self._llm_profiles.changed.connect(self._reload_llm_profiles)
        self._interpretation_manager.connection_test_finished.connect(
            self._on_llm_connection_test_finished
        )
        self._reload_llm_profiles()

    def _apply_workspace(self) -> None:
        result = self._workspace_service.set_workspace(self.workspace_picker.path, create=True)
        severity = ValidationSeverity.INFO if result.valid else ValidationSeverity.ERROR
        if result.valid and result.path is not None:
            self.workspace_picker.set_path(str(result.path))
        self.validation_banner.set_messages(result.message, severity=severity)

    def _request_theme_change(self) -> None:
        theme = str(self.theme_combo.currentData())
        self._settings.theme = theme
        self.theme_change_requested.emit(theme)

    def _save_tool_paths(self) -> None:
        self._settings.set_external_tool_path("cli", self.cli_picker.path or None)
        self._settings.set_external_tool_path("adb", self.adb_picker.path or None)
        self._settings.set_external_tool_path(
            "android_studio",
            self.android_studio_picker.path or None,
        )
        self._settings.set_external_tool_path(
            "trace_processor",
            self.trace_processor_picker.path or None,
        )
        self.validation_banner.set_messages(
            "外部工具路径已保存；环境页刷新后会显示实际生效状态。",
            severity=ValidationSeverity.INFO,
        )

    def _apply_log_directory(self) -> None:
        raw_path = self.log_directory_picker.path
        try:
            service = self._resolved_diagnostics_service()
            if service is not None:
                path = service.set_log_directory(raw_path or None)
            else:
                path = (
                    Path(raw_path).expanduser().resolve(strict=False)
                    if raw_path
                    else GuiLogService.default_log_directory(self._workspace_service.current)
                )
                path.mkdir(parents=True, exist_ok=True)
                default = GuiLogService.default_log_directory(self._workspace_service.current)
                self._settings.log_directory = None if path == default else str(path)
        except OSError as exc:
            self.validation_banner.set_messages(
                f"无法使用日志目录：{exc}",
                severity=ValidationSeverity.ERROR,
            )
            return
        self.log_directory_picker.set_path(str(path))
        self.validation_banner.set_messages(
            f"GUI 日志将写入：{path}",
            severity=ValidationSeverity.INFO,
        )

    def _export_diagnostics(self) -> None:
        service = self._resolved_diagnostics_service()
        if service is None:
            self.validation_banner.set_messages(
                "GUI 日志服务尚未装配，无法导出诊断包。",
                severity=ValidationSeverity.ERROR,
            )
            return
        answer = QMessageBox.warning(
            self,
            "诊断包隐私提示",
            PRIVACY_NOTICE,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        default_name = self._workspace_service.current / (
            f"perfeval-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip"
        )
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "导出 PerfEval GUI 诊断包",
            str(default_name),
            "ZIP archive (*.zip)",
        )
        if not selected:
            return
        try:
            result = service.export_diagnostics(selected)
        except OSError as exc:
            self.validation_banner.set_messages(
                f"诊断包导出失败：{exc}",
                severity=ValidationSeverity.ERROR,
            )
            return
        self.validation_banner.set_messages(
            f"诊断包已导出：{result.path}。分享前请检查本地路径和设备信息。",
            severity=ValidationSeverity.INFO,
        )

    def _reset_preferences(self) -> None:
        self._llm_profiles.clear()
        self._settings.clear()
        workspace = self._workspace_service.reset_to_default(persist=False)
        self.workspace_picker.set_path(str(workspace))
        for picker in (
            self.cli_picker,
            self.adb_picker,
            self.android_studio_picker,
            self.trace_processor_picker,
        ):
            picker.clear()
        default_log = GuiLogService.default_log_directory(workspace)
        self.log_directory_picker.set_path(str(default_log))
        service = self._resolved_diagnostics_service()
        if service is not None:
            service.set_log_directory(default_log, persist=False)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(ThemeMode.SYSTEM.value))
        self.theme_combo.blockSignals(False)
        self.theme_change_requested.emit(ThemeMode.SYSTEM.value)
        self.validation_banner.set_messages(
            "偏好已恢复为默认值。",
            severity=ValidationSeverity.INFO,
        )

    def focus_llm_section(self) -> None:
        self.scroll.ensureWidgetVisible(self.llm_section, 0, 20)
        self.llm_profile_combo.setFocus()

    def _reload_llm_profiles(self) -> None:
        selected = self._editing_llm_profile_id or self.llm_profile_combo.currentData()
        self.llm_profile_combo.blockSignals(True)
        self.llm_profile_combo.clear()
        default_id = self._llm_profiles.default_profile_id
        for profile in self._llm_profiles.profiles:
            suffix = "（默认）" if profile.profile_id == default_id else ""
            self.llm_profile_combo.addItem(f"{profile.name} · {profile.model}{suffix}", profile.profile_id)
        index = self.llm_profile_combo.findData(selected or default_id)
        self.llm_profile_combo.setCurrentIndex(index if index >= 0 else (0 if self.llm_profile_combo.count() else -1))
        self.llm_profile_combo.blockSignals(False)
        self.llm_credential_notice.setText(
            "API Key 将保存到系统凭据库，不会写入报告、日志或普通设置。"
            if self._llm_profiles.credentials.persistent_available
            else self._llm_profiles.credentials.status_message
        )
        if self.llm_profile_combo.count():
            self._load_selected_llm_profile()
        else:
            self._new_llm_profile()

    def _new_llm_profile(self) -> None:
        self._editing_llm_profile_id = None
        self.llm_profile_combo.setCurrentIndex(-1)
        self.llm_name_edit.clear()
        self.llm_format_combo.setCurrentIndex(0)
        self.llm_url_edit.clear()
        self.llm_model_edit.clear()
        self.llm_api_key_edit.clear()
        self.llm_api_key_edit.setPlaceholderText("输入后保存到系统凭据库")
        self.delete_llm_profile_button.setEnabled(False)
        self.default_llm_profile_button.setEnabled(False)
        self._apply_llm_format_default()
        self.llm_name_edit.setFocus()

    def _load_selected_llm_profile(self) -> None:
        profile = self._llm_profiles.profile(str(self.llm_profile_combo.currentData() or ""))
        if profile is None:
            return
        self._editing_llm_profile_id = profile.profile_id
        self.llm_name_edit.setText(profile.name)
        self.llm_format_combo.setCurrentIndex(self.llm_format_combo.findData(profile.api_format.value))
        self.llm_url_edit.setText(profile.base_url)
        self.llm_model_edit.setText(profile.model)
        self.llm_api_key_edit.clear()
        self.llm_api_key_edit.setPlaceholderText(
            "已安全保存；留空保持不变"
            if self._llm_profiles.has_api_key(profile.profile_id)
            else "尚未配置 API Key"
        )
        self.delete_llm_profile_button.setEnabled(True)
        self.default_llm_profile_button.setEnabled(
            profile.profile_id != self._llm_profiles.default_profile_id
        )

    def _apply_llm_format_default(self) -> None:
        defaults = {
            LlmApiFormat.OPENAI_CHAT.value: "https://api.openai.com/v1",
            LlmApiFormat.ANTHROPIC_MESSAGES.value: "https://api.anthropic.com/v1",
            LlmApiFormat.GEMINI_GENERATE_CONTENT.value: "https://generativelanguage.googleapis.com/v1beta",
        }
        current = self.llm_url_edit.text().strip()
        if not current or current in defaults.values():
            self.llm_url_edit.setText(defaults.get(str(self.llm_format_combo.currentData()), ""))

    def _save_llm_profile(self, *, show_message: bool = True) -> str | None:
        profile_id = self._editing_llm_profile_id or uuid4().hex
        try:
            profile = LlmProfile(
                profile_id=profile_id,
                name=self.llm_name_edit.text(),
                api_format=LlmApiFormat(str(self.llm_format_combo.currentData())),
                base_url=validate_base_url(self.llm_url_edit.text()),
                model=self.llm_model_edit.text(),
            )
        except (TypeError, ValueError) as exc:
            self.validation_banner.set_messages(str(exc), severity=ValidationSeverity.ERROR)
            return None
        api_key = self.llm_api_key_edit.text().strip()
        if not api_key and not self._llm_profiles.has_api_key(profile_id):
            self.validation_banner.set_messages("请输入 API Key。", severity=ValidationSeverity.ERROR)
            return None
        persisted = self._llm_profiles.save_profile(
            profile,
            api_key=api_key or None,
            make_default=self._llm_profiles.default_profile_id is None,
        )
        self._editing_llm_profile_id = profile_id
        self.llm_api_key_edit.clear()
        self._reload_llm_profiles()
        if show_message:
            message = "LLM 模型配置已保存。"
            if not persisted:
                message += " 系统凭据库不可用，API Key 仅在本次会话中保留。"
            self.validation_banner.set_messages(message, severity=ValidationSeverity.INFO)
        return profile_id

    def _delete_llm_profile(self) -> None:
        profile = self._llm_profiles.profile(self._editing_llm_profile_id)
        if profile is None:
            return
        answer = QMessageBox.warning(
            self,
            "删除 LLM 模型配置",
            f"将删除“{profile.name}”及其系统凭据库中的 API Key，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._llm_profiles.delete_profile(profile.profile_id)
        self._editing_llm_profile_id = None
        self._reload_llm_profiles()
        self.validation_banner.set_messages("LLM 模型配置已删除。", severity=ValidationSeverity.INFO)

    def _set_default_llm_profile(self) -> None:
        if self._editing_llm_profile_id:
            self._llm_profiles.set_default(self._editing_llm_profile_id)
            self._reload_llm_profiles()
            self.validation_banner.set_messages("默认 LLM 模型已更新。", severity=ValidationSeverity.INFO)

    def _test_llm_connection(self) -> None:
        profile_id = self._save_llm_profile(show_message=False)
        if profile_id is None:
            return
        try:
            self._connection_test_id = self._interpretation_manager.test_connection(profile_id)
        except Exception as exc:
            self.validation_banner.set_messages(str(exc), severity=ValidationSeverity.ERROR)
            return
        self.test_llm_connection_button.setEnabled(False)
        self.test_llm_connection_button.setText("正在测试…")
        self.validation_banner.set_messages(
            "正在发送最小连接测试；不会发送任何性能报告数据。",
            severity=ValidationSeverity.INFO,
        )

    def _on_llm_connection_test_finished(
        self,
        request_id: str,
        success: bool,
        message: str,
    ) -> None:
        if request_id != self._connection_test_id:
            return
        self._connection_test_id = None
        self.test_llm_connection_button.setEnabled(True)
        self.test_llm_connection_button.setText("测试连接")
        self.validation_banner.set_messages(
            message,
            severity=ValidationSeverity.INFO if success else ValidationSeverity.ERROR,
        )

    def _workspace_changed(self, workspace: str) -> None:
        self.workspace_picker.set_path(workspace)
        if not self._settings.log_directory:
            self.log_directory_picker.set_path(
                str(GuiLogService.default_log_directory(workspace))
            )

    def _resolved_diagnostics_service(self) -> GuiLogService | None:
        if self._diagnostics_service is not None:
            return self._diagnostics_service
        window = self.window()
        service = getattr(window, "gui_log_service", None)
        return service if isinstance(service, GuiLogService) else None


def _settings_section(
    title: str,
    parent: QWidget,
) -> tuple[QWidget, QFrame, QVBoxLayout]:
    section = QWidget(parent)
    section.setObjectName("settingsSection")
    section_layout = QVBoxLayout(section)
    section_layout.setContentsMargins(0, 0, 0, 0)
    section_layout.setSpacing(7)

    title_label = QLabel(title, section)
    title_label.setObjectName("settingsSectionTitle")
    section_layout.addWidget(title_label)

    card = QFrame(section)
    card.setObjectName("settingsSectionCard")
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(16, 14, 16, 14)
    card_layout.setSpacing(13)
    section_layout.addWidget(card)
    return section, card, card_layout


def _setting_field(title: str, field: QWidget, parent: QWidget) -> QWidget:
    row = QWidget(parent)
    row_layout = QFormLayout(row)
    row_layout.setContentsMargins(0, 0, 0, 0)
    row_layout.setHorizontalSpacing(0)
    row_layout.setVerticalSpacing(6)
    row_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    row_layout.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
    )
    title_label = QLabel(title, row)
    title_label.setObjectName("settingRowTitle")
    row_layout.addRow(title_label)
    row_layout.addRow(field)
    return row
