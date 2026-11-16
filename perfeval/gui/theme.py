from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .design_tokens import DARK_THEME, LIGHT_THEME, COMPONENTS, SemanticTokens
from .services.settings import SettingsService


class ThemeMode(StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


def _build_stylesheet(theme: SemanticTokens) -> str:
    """Map semantic and component tokens to Qt Widgets component styles."""

    values = {
        "background": theme.background,
        "surface": theme.surface,
        "surface_alt": theme.surface_alt,
        "surface_strong": theme.surface_strong,
        "text": theme.text,
        "text_muted": theme.text_muted,
        "border": theme.border,
        "border_strong": theme.border_strong,
        "primary": theme.primary,
        "primary_hover": theme.primary_hover,
        "primary_active": theme.primary_active,
        "on_primary": theme.on_primary,
        "selection": theme.selection,
        "selection_text": theme.selection_text,
        "success": theme.success,
        "success_bg": theme.success_bg,
        "warning": theme.warning,
        "warning_bg": theme.warning_bg,
        "error": theme.error,
        "error_bg": theme.error_bg,
        "nav_bg": theme.nav_bg,
        "nav_surface": theme.nav_surface,
        "nav_text": theme.nav_text,
        "nav_muted": theme.nav_muted,
        "nav_selected": theme.nav_selected,
        "focus": theme.focus,
        "code_bg": theme.code_bg,
        "code_text": theme.code_text,
        "control_height": f"{COMPONENTS.control_height}px",
        "button_height": f"{COMPONENTS.button_height}px",
        "primary_height": f"{COMPONENTS.primary_height}px",
        "radius_sm": f"{COMPONENTS.radius_sm}px",
        "radius_md": f"{COMPONENTS.radius_md}px",
        "radius_lg": f"{COMPONENTS.radius_lg}px",
    }
    template = """
/* === Foundation: focused performance lab === */
QWidget {
    color: @text;
    font-size: 14px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
}
QWidget#appRoot, QFrame#contentShell, QStackedWidget#pageStack {
    background: @background;
}
QToolTip {
    color: @text;
    background: @surface_strong;
    border: 1px solid @border_strong;
    padding: 6px 9px;
    border-radius: @radius_sm;
}

/* === Application shell === */
QFrame#navigationRail {
    background: @nav_bg;
    border: none;
}
QLabel#brandMark {
    color: @on_primary;
    background: @primary;
    border-radius: 12px;
    font-size: 19px;
    font-weight: 800;
}
QLabel#brandLabel {
    color: @nav_text;
    font-size: 17px;
    font-weight: 760;
}
QLabel#brandKicker, QLabel#navigationSectionLabel, QLabel#workspaceCaption {
    color: @nav_muted;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
}
QFrame#workspaceCard {
    background: @nav_surface;
    border: none;
    border-radius: @radius_md;
}
QLabel#navigationWorkspaceLabel {
    color: @nav_text;
    font-size: 12px;
    font-weight: 600;
}
QLabel#privacyNote {
    color: @nav_muted;
    font-size: 10px;
    line-height: 1.35;
}
QFrame#topBar {
    background: @surface;
    border-bottom: 1px solid @border;
}
QLabel#topBarPageLabel {
    color: @text;
    font-size: 13px;
    font-weight: 700;
}
QLabel#topBarContext, QLabel#topBarWorkspace {
    color: @text_muted;
    font-size: 12px;
}
QLabel#topBarWorkspace {
    padding: 6px 10px;
    background: @surface_alt;
    border: 1px solid @border;
    border-radius: @radius_sm;
}
QListWidget#navigationList {
    color: @nav_muted;
    background: transparent;
    border: none;
    outline: none;
    padding: 0;
}
QListWidget#navigationList::item {
    min-height: 46px;
    padding: 0 14px;
    margin: 3px 0;
    border: none;
    border-radius: @radius_md;
}
QListWidget#navigationList::item:hover:!selected {
    color: @nav_text;
    background: @nav_surface;
}
QListWidget#navigationList::item:selected {
    color: @nav_text;
    background: @nav_selected;
    font-weight: 650;
    border-left: 3px solid @primary;
}
QListWidget#navigationList:focus {
    border: none;
    border-radius: @radius_md;
}
QStatusBar {
    color: @text_muted;
    background: @surface;
    border: none;
    font-size: 12px;
}

/* === Page hierarchy === */
QFrame#pageHeader {
    background: transparent;
    border: none;
}
QLabel#pageTitle {
    color: @text;
    font-size: 30px;
    font-weight: 760;
}
QLabel#pageDescription, QLabel#mutedText, QLabel#workspaceCaptionLight {
    color: @text_muted;
}
QLabel#pageDescription {
    font-size: 14px;
    line-height: 1.4;
}
QLabel#pageEyebrow {
    color: @primary;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.4px;
}
QLabel#sectionTitle {
    color: @text;
    font-size: 16px;
    font-weight: 650;
}

/* === Inputs and buttons === */
QPushButton {
    min-height: @button_height;
    padding: 0 15px;
    color: @text;
    background: @surface;
    border: 1px solid @border;
    border-radius: @radius_md;
    font-weight: 560;
}
QPushButton:hover {
    background: @selection;
    border-color: @primary;
}
QPushButton:pressed {
    background: @selection;
    border-color: @primary;
}
QPushButton:focus {
    border: 2px solid @focus;
}
QPushButton:disabled {
    color: @text_muted;
    background: @background;
    border-color: transparent;
}
QPushButton#primaryActionButton {
    min-height: @primary_height;
    color: @on_primary;
    background: @primary;
    border: 1px solid @primary;
    border-radius: @radius_md;
    font-size: 14px;
    font-weight: 700;
}
QPushButton#primaryActionButton:hover {
    background: @primary_hover;
    border-color: @primary_hover;
}
QPushButton#primaryActionButton:pressed {
    background: @primary_active;
    border-color: @primary_active;
}
QPushButton#primaryActionButton:focus {
    border: 2px solid @focus;
}
QPushButton#dangerButton {
    color: @error;
    background: @error_bg;
    border-color: transparent;
}
QPushButton#destructiveTextButton {
    color: @error;
    background: transparent;
    border-color: transparent;
}
QPushButton#destructiveTextButton:hover {
    background: @error_bg;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    min-height: @control_height;
    padding: 0 10px;
    color: @text;
    background: @surface_alt;
    border: 1px solid @border;
    border-radius: @radius_md;
    selection-background-color: @selection;
    selection-color: @selection_text;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 2px solid @focus;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: @text_muted;
    background: @background;
    border-color: transparent;
}
QComboBox::drop-down {
    width: 26px;
    border: none;
}
QComboBox QAbstractItemView {
    color: @text;
    background: @surface;
    border: 1px solid @border;
    border-radius: @radius_md;
    padding: 4px;
    outline: none;
    selection-background-color: @selection;
    selection-color: @selection_text;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid @border_strong;
    border-radius: 4px;
    background: @surface;
}
QCheckBox::indicator:checked {
    background: @primary;
    border-color: @primary;
}
QPlainTextEdit, QTextEdit {
    color: @text;
    background: @surface_alt;
    border: 1px solid transparent;
    border-radius: @radius_md;
    selection-background-color: @selection;
    selection-color: @selection_text;
}
QPlainTextEdit#historySummaryPreview,
QPlainTextEdit#historySummaryFullscreen {
    color: @code_text;
    background: @code_bg;
    border-color: @nav_surface;
    font-family: Menlo, Consolas, monospace;
    font-size: 13px;
}
QToolButton {
    min-height: 30px;
    color: @primary;
    background: transparent;
    border: none;
    border-radius: @radius_sm;
    padding: 2px 5px;
    font-weight: 650;
}
QToolButton:hover {
    background: @selection;
}
QToolButton:focus {
    border: 1px solid @focus;
}

/* === Surfaces and grouping === */
QGroupBox {
    color: @text;
    background: @surface;
    border: none;
    border-radius: @radius_lg;
    margin-top: 22px;
    padding-top: 8px;
    font-weight: 620;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 4px;
    top: 3px;
    padding: 0 5px;
    color: @text_muted;
    background: transparent;
    font-size: 12px;
    font-weight: 650;
}
QFrame#quickAdvancedGroup {
    color: @text;
    background: @surface_alt;
    border: 1px solid @border;
    border-radius: @radius_lg;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QSplitter::handle {
    background: transparent;
}
QFrame#subtleDivider {
    border: none;
}

/* === Tabs === */
QTabWidget::pane {
    border: none;
    padding-top: 14px;
}
QTabBar {
    background: @surface_alt;
    border: 1px solid @border;
    border: none;
    border-radius: @radius_md;
}
QTabBar::tab {
    min-height: 28px;
    padding: 7px 20px;
    margin: 2px;
    color: @text_muted;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
}
QTabBar::tab:selected {
    color: @text;
    background: @surface;
    border-color: transparent;
    font-weight: 650;
}
QTabBar::tab:hover:!selected {
    color: @text;
    background: @surface_alt;
}
QTabBar:focus {
    border: 1px solid @focus;
}

/* === Tables and scrollbars === */
QTableWidget, QTableView {
    color: @text;
    background: @surface;
    alternate-background-color: @surface_alt;
    border: 1px solid @border;
    border-radius: @radius_lg;
    gridline-color: transparent;
    selection-background-color: @selection;
    selection-color: @selection_text;
}
QTableWidget::item, QTableView::item {
    min-height: 32px;
    padding: 6px 8px;
    border: none;
}
QHeaderView::section {
    color: @text_muted;
    background: @surface_strong;
    border: none;
    border: none;
    padding: 8px 10px;
    font-family: Menlo, Consolas, monospace;
    font-size: 11px;
    font-weight: 700;
}
QLineEdit#historySearchEdit {
    min-height: 40px;
    padding-left: 13px;
    font-size: 14px;
}
QComboBox#historyStatusFilter, QComboBox#historyTaskKindFilter,
QComboBox#historyArtifactFilter, QComboBox#historyHealthFilter {
    min-width: 126px;
}
QLabel#historyWorkspaceLabel {
    color: @text_muted;
    font-family: Menlo, Consolas, monospace;
    font-size: 11px;
}
QTableCornerButton::section {
    background: @surface_strong;
    border: none;
}
QScrollBar:vertical {
    width: 10px;
    margin: 2px;
    background: transparent;
}
QScrollBar::handle:vertical {
    min-height: 30px;
    background: @border_strong;
    border-radius: 4px;
}
QScrollBar:horizontal {
    height: 10px;
    margin: 2px;
    background: transparent;
}
QScrollBar::handle:horizontal {
    min-width: 30px;
    background: @border_strong;
    border-radius: 4px;
}
QScrollBar::handle:hover {
    background: @text_muted;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}

/* === Quick start: experimental bench === */
QLabel#quickEnvironmentLine {
    min-height: 36px;
    padding: 0 13px;
    color: @text_muted;
    background: @surface;
    border: none;
    border-radius: @radius_md;
    font-weight: 620;
}
QLabel#quickEnvironmentLine[environmentState="ready"] {
    color: @success;
    background: @success_bg;
}
QLabel#quickEnvironmentLine[environmentState="ambiguous"] {
    color: @warning;
    background: @warning_bg;
}
QLabel#quickEnvironmentLine[environmentState="offline"] {
    color: @error;
    background: @error_bg;
}
QFrame#quickStartSurface, QFrame#taskBody {
    background: transparent;
}
QFrame#scenarioPanel, QFrame#presetDetailPane, QFrame#taskActionPanel,
QFrame#phasePanel {
    background: @surface;
    border: 1px solid @border;
    border-radius: @radius_lg;
}
QFrame#scenarioPanel {
    background: @surface_alt;
}
QFrame#quickActionBar {
    background: transparent;
    border: none;
}
QLabel#scenarioKicker, QLabel#phaseKicker {
    color: @text_muted;
    font-size: 12px;
    font-weight: 650;
}
QLabel#scenarioHelp {
    color: @text_muted;
    font-size: 12px;
}
QListWidget#presetScenarioList {
    background: transparent;
    border: none;
    outline: none;
    padding: 0;
}
QListWidget#presetScenarioList::item {
    min-height: 58px;
    padding: 8px 12px;
    margin: 4px 0;
    color: @text_muted;
    border: none;
    border-radius: @radius_md;
}
QListWidget#presetScenarioList::item:selected {
    color: @selection_text;
    background: @selection;
    font-weight: 650;
    border: 1px solid @primary;
}
QListWidget#presetScenarioList::item:hover:!selected {
    color: @text;
    background: @surface_strong;
}
QLabel#presetTitle, QLabel#taskStateTitle {
    color: @text;
    font-size: 23px;
    font-weight: 760;
}
QLabel#presetSourceLabel {
    color: @primary;
    font-size: 11px;
    font-weight: 600;
}
QLabel#formIntro {
    color: @text_muted;
    padding: 8px 10px;
    background: @surface_alt;
    border-left: 3px solid @primary;
    border-radius: @radius_sm;
    font-size: 12px;
}

/* === Native task progress === */
QFrame#phasePanel {
    background: @surface_alt;
}
QFrame#phasePanel QLabel#sectionTitle {
    color: @text;
}
QFrame#phasePanel QLabel#mutedText {
    color: @text_muted;
}
QFrame#phaseRow {
    min-height: 42px;
    background: transparent;
    border: none;
    border-radius: @radius_sm;
}
QFrame#phaseRow QLabel#phaseLabel {
    color: @text_muted;
}
QFrame#phaseRow QLabel#phaseIcon {
    color: @text_muted;
    font-weight: 700;
}
QFrame#phaseRow[phaseState="started"] {
    background: @selection;
}
QFrame#phaseRow[phaseState="started"] QLabel#phaseIcon,
QFrame#phaseRow[phaseState="started"] QLabel#phaseLabel {
    color: @selection_text;
    font-weight: 700;
}
QFrame#phaseRow[phaseState="completed"] {
    background: transparent;
}
QFrame#phaseRow[phaseState="completed"] QLabel#phaseIcon {
    color: @success;
}
QFrame#phaseRow[phaseState="failed"] {
    background: @error_bg;
}
QFrame#phaseRow[phaseState="failed"] QLabel#phaseIcon,
QFrame#phaseRow[phaseState="failed"] QLabel#phaseLabel {
    color: @error;
}
QLabel#taskContextLine {
    min-height: 30px;
    padding: 0;
    color: @text_muted;
    background: transparent;
    border: none;
    font-size: 12px;
}
QLabel#taskInstruction {
    color: @text;
    font-size: 16px;
    font-weight: 650;
}
QLabel#taskMetaCaption {
    color: @text_muted;
    font-size: 11px;
    font-weight: 700;
}
QGroupBox#taskLogGroup {
    background: @surface_alt;
}
QPlainTextEdit#taskLogPreview, QPlainTextEdit#taskConsoleOutput {
    color: @code_text;
    background: @code_bg;
    border-color: @nav_surface;
    font-family: Menlo, Consolas, monospace;
    font-size: 12px;
}
QFrame#taskFailureDetails {
    background: @error_bg;
    border: none;
    border-radius: @radius_md;
}
QPlainTextEdit#taskFailureOutput {
    color: @code_text;
    background: @code_bg;
    border-color: @error;
    font-family: Menlo, Consolas, monospace;
    font-size: 12px;
}

/* === State-bearing components === */
QFrame#validationBanner {
    background: @surface_alt;
    border: none;
    border-radius: @radius_md;
}
QFrame#validationBanner[severity="warning"] {
    background: @warning_bg;
}
QFrame#validationBanner[severity="error"] {
    background: @error_bg;
}
QFrame#validationBanner[severity="info"] {
    background: @selection;
}
QFrame#pageToast {
    background: @surface_strong;
    border: 1px solid @border_strong;
    border-radius: @radius_md;
}
QFrame#pageToast[severity="info"] {
    background: @selection;
    border-color: @primary;
}
QFrame#pageToast[severity="warning"] {
    background: @warning_bg;
    border-color: @warning;
}
QFrame#pageToast[severity="error"] {
    background: @error_bg;
    border-color: @error;
}
QLabel#pageToastText {
    color: @text;
}
QFrame#manualRequestFrame {
    background: @warning_bg;
    border: none;
    border-radius: @radius_md;
}
QLabel#taskConsoleStatus, QLabel#taskSummaryOutcome {
    padding: 3px 8px;
    background: @surface_strong;
    border-radius: @radius_sm;
    font-weight: 700;
}
QLabel#taskConsoleStatus[taskState="running"],
QLabel#taskConsoleStatus[taskState="starting"] {
    color: @primary;
    background: @selection;
}
QLabel#taskConsoleStatus[taskState="waiting_for_user"] {
    color: @warning;
    background: @warning_bg;
}
QLabel#taskSummaryOutcome[taskOutcome="pass"] {
    color: @success;
    background: @success_bg;
}
QLabel#taskSummaryOutcome[taskOutcome="warn"] {
    color: @warning;
    background: @warning_bg;
}
QLabel#taskSummaryOutcome[taskOutcome="fail"],
QLabel#taskSummaryOutcome[taskOutcome="tool_error"],
QLabel#taskSummaryOutcome[taskOutcome="launch_error"],
QLabel#taskSummaryOutcome[taskOutcome="unexpected_error"] {
    color: @error;
    background: @error_bg;
}
QLabel#resultGroupTitle {
    color: @primary;
    font-family: Menlo, Consolas, monospace;
    font-size: 11px;
    font-weight: 700;
}
QFrame#llmInterpretationPanel {
    background: @surface_alt;
    border: none;
    border-radius: @radius_md;
}
QComboBox#llmProfileCombo {
    padding-left: 0;
}
QFrame#historyReportSection {
    background: @surface;
    border: 1px solid @border;
    border-radius: @radius_lg;
}
QLabel#historyReportKicker {
    color: @primary;
    font-size: 12px;
    font-weight: 700;
}
QLabel#historyReportTitle {
    color: @text;
    font-size: 18px;
    font-weight: 700;
}
QLabel#historyReportAvailability {
    padding: 4px 9px;
    color: @success;
    background: @success_bg;
    border-radius: @radius_sm;
    font-size: 12px;
    font-weight: 700;
}
QLabel#historyReportAvailability[reportAvailable="false"] {
    color: @text_muted;
    background: @surface_alt;
}
QLabel#llmInterpretationState {
    padding: 3px 8px;
    color: @text_muted;
    background: @surface_strong;
    border-radius: @radius_sm;
    font-weight: 700;
}
QLabel#llmInterpretationState[interpretationState="queued"],
QLabel#llmInterpretationState[interpretationState="running"] {
    color: @primary;
    background: @selection;
}
QLabel#llmInterpretationState[interpretationState="completed"] {
    color: @success;
    background: @success_bg;
}
QLabel#llmInterpretationState[interpretationState="failed"],
QLabel#llmInterpretationState[interpretationState="interrupted"] {
    color: @error;
    background: @error_bg;
}
QLabel#llmInterpretationState[interpretationState="cancelled"] {
    color: @warning;
    background: @warning_bg;
}
QWidget#loadingOverlay {
    background: transparent;
}
QFrame#loadingCard {
    background: @surface;
    border: none;
    border-radius: @radius_lg;
}
QLabel#loadingText {
    color: @text_muted;
    font-weight: 650;
}

/* === Settings: macOS inset-grouped preferences === */
QWidget#settingsSection {
    background: transparent;
}
QLabel#settingsSectionTitle {
    color: @text_muted;
    font-size: 13px;
    font-weight: 650;
}
QFrame#settingsSectionCard {
    background: @surface;
    border: none;
    border-radius: @radius_lg;
}
QLabel#settingRowTitle {
    color: @text;
    font-weight: 620;
}
QPushButton#pathBrowseButton {
    min-width: 68px;
    padding-left: 8px;
    padding-right: 8px;
}
QPushButton#resetSettingsButton {
    color: @text_muted;
    background: transparent;
    border-color: transparent;
    padding-left: 4px;
    padding-right: 4px;
}
QPushButton#resetSettingsButton:hover {
    color: @text;
    background: transparent;
}
"""
    # Replace longer names first because component states share prefixes
    # (for example primary_hover contains primary).
    for name in sorted(values, key=len, reverse=True):
        value = values[name]
        template = template.replace(f"@{name}", value)
    return template


# Kept as a stable import for callers that only need the default stylesheet.
APP_STYLESHEET = _build_stylesheet(LIGHT_THEME)


class ThemeManager(QObject):
    """Apply system, light, or dark semantic themes to Qt Widgets."""

    theme_changed = Signal(str)

    def __init__(self, application: QApplication, settings: SettingsService) -> None:
        super().__init__()
        self._application = application
        self._settings = settings
        self._system_palette = QPalette(application.style().standardPalette())
        self._mode = ThemeMode.SYSTEM

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    def apply_saved_theme(self) -> ThemeMode:
        return self.apply(self._settings.theme)

    def apply(self, mode: ThemeMode | str, *, persist: bool = False) -> ThemeMode:
        try:
            selected = ThemeMode(mode)
        except ValueError:
            selected = ThemeMode.SYSTEM

        if selected is ThemeMode.SYSTEM:
            standard_window = self._system_palette.color(QPalette.ColorRole.Window)
            semantic = DARK_THEME if standard_window.lightness() < 128 else LIGHT_THEME
        elif selected is ThemeMode.DARK:
            semantic = DARK_THEME
        else:
            semantic = LIGHT_THEME

        self._application.setPalette(_palette(semantic))
        self._application.setStyleSheet(_build_stylesheet(semantic))
        self._mode = selected
        if persist:
            self._settings.theme = selected.value
        self.theme_changed.emit(selected.value)
        return selected


def _palette(theme: SemanticTokens) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.background))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.surface))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.surface_alt))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.surface_strong))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.text_muted))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.surface_alt))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(theme.error))
    palette.setColor(QPalette.ColorRole.Link, QColor(theme.primary))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.selection))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.selection_text))
    palette.setColor(QPalette.ColorRole.Mid, QColor(theme.text_muted))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(theme.border))
    palette.setColor(QPalette.ColorRole.Dark, QColor(theme.border_strong))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(theme.text_muted))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(theme.text_muted),
    )
    return palette
