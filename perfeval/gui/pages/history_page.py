from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..design_tokens import COMPONENTS
from ..models.history import (
    HistoryArtifactKind,
    HistoryEntry,
    HistoryHealth,
    HistoryQuery,
    HistoryScanResult,
    HistorySource,
    HistoryTaskKind,
)
from ..models.result import ResultDescriptor
from ..services.history_service import HistoryDeleteResult, HistoryService
from ..models.llm import InterpretationSource, InterpretationState
from ..services.llm_artifacts import source_from_history
from ..services.llm_jobs import InterpretationJobManager
from ..services.llm_profiles import LlmProfileService
from ..services.platform_open import OpenResult, PlatformOpenService
from ..services.settings import SettingsService
from ..services.workspace import WorkspaceService
from ..widgets.activity_indicator import LoadingOverlay
from ..widgets.llm_interpretation_panel import LlmInterpretationPanel
from ..widgets.page_toast import PageToast
from ..widgets.validation_banner import ValidationSeverity
from .base_page import BasePage


_HISTORY_TABLE_HEIGHT = COMPONENTS.space_8 * 10


class _ScanSignals(QObject):
    finished = Signal(int, object)


class _ScanTask(QRunnable):
    """Run a bounded history scan off the UI thread."""

    def __init__(self, service: HistoryService, generation: int, signals: _ScanSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._service = service
        self._generation = generation
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._service.scan()
        except Exception as exc:
            result = HistoryScanResult(entries=(), scanned_nodes=0, warnings=(str(exc),))
        try:
            self._signals.finished.emit(self._generation, result)
        except RuntimeError:
            pass  # page torn down while the scan was still running


class HistoryPage(BasePage):
    open_result_requested = Signal(object)
    entries_changed = Signal(object)
    scan_started = Signal()
    scan_finished = Signal(object)
    settings_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        workspace_service: WorkspaceService | None = None,
        history_service: HistoryService | None = None,
        opener: PlatformOpenService | None = None,
        llm_profiles: LlmProfileService | None = None,
        interpretation_manager: InterpretationJobManager | None = None,
    ) -> None:
        super().__init__(
            "历史结果",
            "默认展示从快速启动选择的场景；需要时可切换查看旧 runs/reports。",
            parent,
        )
        header_item = self.layout.takeAt(0)
        header = header_item.widget() if header_item is not None else None
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("historyPageScroll")
        self.scroll.setAccessibleName("历史结果页面内容")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_content = QWidget(self.scroll)
        self.scroll_content.setObjectName("historyPageScrollContent")
        self.content_layout = QVBoxLayout(self.scroll_content)
        self.content_layout.setContentsMargins(32, 26, 32, 26)
        self.content_layout.setSpacing(18)
        self.content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        if header is not None:
            header.setParent(self.scroll_content)
            self.content_layout.addWidget(header)
        self.scroll.setWidget(self.scroll_content)
        self.layout.addWidget(self.scroll, 1)
        if workspace_service is None:
            settings = SettingsService()
            workspace_service = WorkspaceService(settings)
        self._workspace_service = workspace_service
        self.history_service = history_service or HistoryService(workspace_service.current)
        self.opener = opener
        self._interpretation_manager = interpretation_manager
        self._entries: tuple[HistoryEntry, ...] = ()
        self._related_query: HistoryQuery | None = None
        self._scan_result: HistoryScanResult | None = None
        self._scan_generation = 0
        self._scan_active = False
        self._rescan_pending = False
        self._initial_scan_requested = False
        self._pending_delete_notice: tuple[str, ValidationSeverity] | None = None
        self._visible_entry_count = 0
        self._scan_signals = _ScanSignals(self)
        self._scan_signals.finished.connect(self._on_scan_finished)

        self.workspace_label = QLabel(
            f"当前 workspace：{workspace_service.current}",
            self,
        )
        self.workspace_label.setObjectName("historyWorkspaceLabel")
        self.workspace_label.setWordWrap(True)
        self.content_layout.addWidget(self.workspace_label)

        self.related_filter_bar = QWidget(self)
        related_filter_layout = QHBoxLayout(self.related_filter_bar)
        related_filter_layout.setContentsMargins(0, 0, 0, 0)
        self.related_filter_label = QLabel(self.related_filter_bar)
        self.related_filter_label.setObjectName("pageDescription")
        self.related_filter_label.setWordWrap(True)
        self.related_filter_label.setAccessibleName("当前历史场景筛选")
        related_filter_layout.addWidget(self.related_filter_label, 1)
        self.clear_related_button = QPushButton("清除场景筛选", self.related_filter_bar)
        self.clear_related_button.setAccessibleName("清除历史场景筛选")
        self.clear_related_button.clicked.connect(self.clear_related_results)
        related_filter_layout.addWidget(self.clear_related_button)
        self.related_filter_bar.hide()
        self.content_layout.addWidget(self.related_filter_bar)

        filters = QGridLayout()
        self.filters_layout = filters
        filters.setHorizontalSpacing(10)
        filters.setVerticalSpacing(8)
        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("historySearchEdit")
        self.search_edit.setPlaceholderText("按场景、平台或路径筛选")
        self.search_edit.setAccessibleName("历史结果搜索")
        self.search_edit.textChanged.connect(self._apply_filters)
        filters.addWidget(self.search_edit, 0, 0, 1, 4)
        self.status_combo = QComboBox(self)
        self.status_combo.setObjectName("historyStatusFilter")
        self.status_combo.setAccessibleName("历史任务状态筛选")
        for label, value in (
            ("全部任务状态", ""),
            ("通过", "pass"),
            ("警告", "warn"),
            ("失败", "fail"),
            ("已完成", "completed"),
            ("部分完成", "partial"),
            ("无效", "invalid"),
            ("未知", "unknown"),
            ("已取消", "cancelled"),
            ("工具错误", "tool_error"),
            ("启动错误", "launch_error"),
            ("意外错误", "unexpected_error"),
        ):
            self.status_combo.addItem(label, value)
        self.status_combo.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.status_combo, 1, 0)
        self.task_kind_combo = QComboBox(self)
        self.task_kind_combo.setObjectName("historyTaskKindFilter")
        self.task_kind_combo.setAccessibleName("历史任务类型筛选")
        self.task_kind_combo.addItem("全部任务类型", "")
        self.task_kind_combo.addItem("单版本 / 诊断", HistoryTaskKind.RUN.value)
        self.task_kind_combo.addItem("A/B 对比", HistoryTaskKind.COMPARE.value)
        self.task_kind_combo.addItem("未知类型", HistoryTaskKind.UNKNOWN.value)
        self.task_kind_combo.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.task_kind_combo, 1, 1)

        self.source_combo = QComboBox(self)
        self.source_combo.setObjectName("historySourceFilter")
        self.source_combo.setAccessibleName("历史任务来源筛选")
        self.source_combo.addItem("快速启动任务", HistorySource.QUICK_START.value)
        self.source_combo.addItem("全部来源", "")
        self.source_combo.addItem("旧 runs/reports", HistorySource.LEGACY.value)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        filters.addWidget(self.source_combo, 1, 2)

        self.artifact_combo = QComboBox(self)
        self.artifact_combo.setObjectName("historyArtifactFilter")
        self.artifact_combo.setAccessibleName("历史报告可用性筛选")
        self.artifact_combo.addItem("全部报告状态", "")
        self.artifact_combo.addItem("含报告", HistoryArtifactKind.REPORT.value)
        self.artifact_combo.addItem("无报告", "no_report")
        self.artifact_combo.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.artifact_combo, 2, 0)

        self.health_combo = QComboBox(self)
        self.health_combo.setObjectName("historyHealthFilter")
        self.health_combo.setAccessibleName("历史结果完整性筛选")
        self.health_combo.addItem("全部完整性", "")
        self.health_combo.addItem("完整", HistoryHealth.COMPLETE.value)
        self.health_combo.addItem("部分", HistoryHealth.PARTIAL.value)
        self.health_combo.addItem("未知", HistoryHealth.UNKNOWN.value)
        self.health_combo.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.health_combo, 2, 1)

        self.kind_combo = self.task_kind_combo
        self.refresh_button = QPushButton("刷新", self)
        self.refresh_button.setObjectName("refreshHistoryButton")
        self.refresh_button.clicked.connect(self.refresh)
        filters.addWidget(self.refresh_button, 2, 2)
        self.delete_entry_button = QPushButton("删除所选结果", self)
        self.delete_entry_button.setObjectName("dangerButton")
        self.delete_entry_button.setAccessibleName("删除选中的历史结果")
        self.delete_entry_button.setToolTip("永久删除选中的历史结果及其报告、日志和原始数据")
        self.delete_entry_button.clicked.connect(self._delete_selected_entry)
        filters.addWidget(self.delete_entry_button, 2, 3)
        self.content_layout.addLayout(filters)

        self.toast = PageToast(self)
        # Preserve the existing attribute for callers while moving the visual
        # feedback out of the page layout.
        self.validation_banner = self.toast

        list_actions = QHBoxLayout()
        self.list_actions_layout = list_actions
        self.list_status_label = QLabel("尚未扫描历史结果。", self.scroll_content)
        self.list_status_label.setObjectName("mutedText")
        self.list_status_label.setWordWrap(True)
        list_actions.addWidget(self.list_status_label, 1)
        self.clear_history_button = QPushButton("清空快速启动历史", self.scroll_content)
        self.clear_history_button.setObjectName("destructiveTextButton")
        self.clear_history_button.setAccessibleName("清空当前来源的历史结果")
        self.clear_history_button.setToolTip("永久清空当前来源的全部历史结果")
        self.clear_history_button.clicked.connect(self._clear_history)
        list_actions.addWidget(self.clear_history_button)
        self.content_layout.addLayout(list_actions)

        self.table = QTableWidget(0, 8, self.scroll_content)
        self.table.setObjectName("historyTable")
        self.table.setHorizontalHeaderLabels(
            ("时间", "选择的场景", "任务类型", "状态", "完整性", "报告", "平台", "LLM 解读")
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAccessibleName("历史结果列表")
        self.table.setAccessibleDescription(
            "支持 Command/Ctrl 或 Shift 多选；有报告的结果可双击打开报告。"
        )
        self.table.setToolTip("Command/Ctrl 或 Shift 可多选；双击有报告的行可打开报告")
        self.table.setAlternatingRowColors(True)
        self.table.setFixedHeight(_HISTORY_TABLE_HEIGHT)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(self._open_item_report)
        self.loading_overlay = LoadingOverlay(self.table, text="正在扫描历史结果…")
        self.content_layout.addWidget(self.table)

        report_section = QFrame(self.scroll_content)
        report_section.setObjectName("historyReportSection")
        self.report_section = report_section
        # Keep the previous attribute as a compatibility alias for callers.
        self.detail_section = report_section
        report_layout = QVBoxLayout(report_section)
        report_layout.setContentsMargins(18, 16, 18, 18)
        report_layout.setSpacing(14)

        report_header = QHBoxLayout()
        report_header.setSpacing(12)
        report_text = QVBoxLayout()
        report_text.setContentsMargins(0, 0, 0, 0)
        report_text.setSpacing(4)
        report_kicker = QLabel("报告", report_section)
        report_kicker.setObjectName("historyReportKicker")
        report_text.addWidget(report_kicker)
        self.report_title_label = QLabel(report_section)
        self.report_title_label.setObjectName("historyReportTitle")
        self.report_title_label.setWordWrap(True)
        self.report_title_label.setAccessibleName("当前选择的历史报告")
        report_text.addWidget(self.report_title_label)
        self.report_meta_label = QLabel(report_section)
        self.report_meta_label.setObjectName("mutedText")
        self.report_meta_label.setWordWrap(True)
        report_text.addWidget(self.report_meta_label)
        report_header.addLayout(report_text, 1)

        self.report_availability_label = QLabel(report_section)
        self.report_availability_label.setObjectName("historyReportAvailability")
        self.report_availability_label.setAccessibleName("原始报告可用状态")
        self.report_availability_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.report_availability_label.setFixedHeight(COMPONENTS.button_height)
        report_header.addWidget(
            self.report_availability_label,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        self.open_report_button = QPushButton("查看原始报告", report_section)
        self.open_report_button.setObjectName("primaryActionButton")
        self.open_report_button.setAccessibleName("查看所选结果的原始报告")
        self.open_report_button.clicked.connect(self._open_report)
        self.open_report_button.setFixedHeight(COMPONENTS.primary_height)
        report_header.addWidget(
            self.open_report_button,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        report_layout.addLayout(report_header)

        self.llm_panel: LlmInterpretationPanel | None = None
        if llm_profiles is not None and interpretation_manager is not None:
            self.llm_panel = LlmInterpretationPanel(
                llm_profiles,
                interpretation_manager,
                report_section,
            )
            self.llm_panel.settings_requested.connect(self.settings_requested)
            self.llm_panel.open_result_requested.connect(
                lambda descriptor: self._request_open(descriptor, static=True)
            )
            report_layout.addWidget(self.llm_panel)
        report_section.hide()
        self.content_layout.addWidget(report_section)
        self.content_layout.addStretch(1)
        self._set_actions_enabled(None)

        workspace_service.workspace_changed.connect(self.set_workspace)
        if interpretation_manager is not None:
            interpretation_manager.job_state_changed.connect(self._on_llm_job_state)

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        return self._entries

    @property
    def is_scanning(self) -> bool:
        return self._scan_active

    @property
    def related_query(self) -> HistoryQuery | None:
        return self._related_query

    def load_once(self) -> None:
        """Start the initial background scan once, independent of tab visibility."""
        if self._initial_scan_requested:
            return
        if self._entries or self._scan_active:
            self._initial_scan_requested = True
            return
        self._initial_scan_requested = True
        self.refresh()

    def show_related_results(self, query: HistoryQuery) -> None:
        self._related_query = query
        suffix = f" · {query.package_name}" if query.package_name else ""
        self.related_filter_label.setText(f"当前查询：{query.label}{suffix}")
        self.related_filter_bar.show()
        self._reset_standard_filters()
        self._apply_filters()
        self.refresh()

    def clear_related_results(self) -> None:
        self._related_query = None
        self.related_filter_bar.hide()
        self._apply_filters()

    def set_workspace(self, workspace: str) -> None:
        # Invalidate any in-flight scan so its stale result gets discarded.
        self._scan_generation += 1
        self.history_service.set_workspace(workspace)
        self.workspace_label.setText(f"当前 workspace：{workspace}")
        self._entries = ()
        self._initial_scan_requested = False
        self._related_query = None
        self.related_filter_bar.hide()
        self.entries_changed.emit(self._entries)
        self._apply_filters()
        self.validation_banner.set_messages(
            "workspace 已变化；正在后台重新扫描历史。默认只展示快速启动任务。",
            severity=ValidationSeverity.INFO,
        )
        if self._scan_active:
            self._rescan_pending = True
            self._initial_scan_requested = True
        else:
            QTimer.singleShot(0, self.load_once)

    def refresh(self) -> None:
        """Start a background history scan; results arrive via scan_finished."""
        if self._scan_active:
            self._rescan_pending = True
            return
        self._scan_active = True
        self._scan_generation += 1
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("正在扫描…")
        self.list_status_label.setText("正在后台扫描历史结果…")
        self.loading_overlay.start()
        self.scan_started.emit()
        QThreadPool.globalInstance().start(
            _ScanTask(self.history_service, self._scan_generation, self._scan_signals)
        )

    def _on_scan_finished(self, generation: int, result: HistoryScanResult) -> None:
        self._scan_active = False
        if self._rescan_pending:
            # 直接续扫，保持 loading 遮罩显示，避免闪烁。
            self._rescan_pending = False
            self.refresh()
            return
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("刷新")
        self.loading_overlay.stop()
        if generation != self._scan_generation:
            return
        self._scan_result = result
        self._entries = result.entries
        self.entries_changed.emit(self._entries)
        self._apply_filters()
        if self._pending_delete_notice is not None:
            message, severity = self._pending_delete_notice
            self._pending_delete_notice = None
            self.validation_banner.set_messages(message, severity=severity)
        elif result.warnings:
            self.validation_banner.set_messages(
                result.warnings,
                severity=ValidationSeverity.WARNING,
            )
        elif self._related_query is not None:
            visible_count = self.table.rowCount()
            message = (
                f"找到 {visible_count} 条与“{self._related_query.label}”相关的历史结果；"
                f"共扫描 {result.scanned_nodes} 个节点。"
                if visible_count
                else f"未找到与“{self._related_query.label}”相关的历史结果；"
                f"共扫描 {result.scanned_nodes} 个节点。"
            )
            self.validation_banner.set_messages(
                message,
                severity=ValidationSeverity.INFO,
            )
        elif result.entries:
            visible_count = self.table.rowCount()
            report_count = sum(entry.has_report for entry in result.entries)
            self.validation_banner.set_messages(
                f"当前显示 {visible_count} 条{self.source_combo.currentText()}；"
                f"扫描发现 {len(result.entries)} 条结果，其中 {report_count} 条含报告；"
                f"扫描 {result.scanned_nodes} 个节点。",
                severity=ValidationSeverity.INFO,
            )
        else:
            self.validation_banner.set_messages(
                "当前 workspace 尚未发现可识别的 run/report。",
                severity=ValidationSeverity.INFO,
            )
        self.scan_finished.emit(result)

    def _apply_filters(self) -> None:
        query = self.search_edit.text().strip().casefold()
        status = str(self.status_combo.currentData() or "")
        task_kind = str(self.task_kind_combo.currentData() or "")
        source = str(self.source_combo.currentData() or "")
        artifact = str(self.artifact_combo.currentData() or "")
        health = str(self.health_combo.currentData() or "")
        visible = [
            entry
            for entry in self._entries
            if _matches_related_query(entry, self._related_query)
            and (not status or entry.status == status)
            and (not task_kind or entry.task_kind.value == task_kind)
            and (not source or entry.source.value == source)
            and (
                not artifact
                or artifact == HistoryArtifactKind.REPORT.value
                and entry.has_report
                or artifact == "no_report"
                and not entry.has_report
            )
            and (not health or entry.health.value == health)
            and (
                not query
                or query
                in " ".join(
                    (
                        entry.name,
                        entry.task_kind.value,
                        entry.status,
                        entry.health.value,
                        entry.platform,
                        entry.source.value,
                        str(entry.preset_id or ""),
                        str(entry.scenario_id or ""),
                        str(entry.package_name or ""),
                        str(entry.directory),
                        str(entry.report_path or ""),
                        str(entry.interpretation_path or ""),
                        entry.interpretation_state.value,
                    )
                ).casefold()
            )
        ]
        self.table.setRowCount(len(visible))
        self._visible_entry_count = len(visible)
        for row, entry in enumerate(visible):
            values = (
                entry.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                entry.name,
                _task_kind_label(entry.task_kind),
                entry.status,
                _health_label(entry.health),
                "有" if entry.has_report else "无",
                entry.platform,
                _interpretation_state_label(entry.interpretation_state),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                item.setToolTip(
                    "双击打开报告" if entry.has_report else "此结果没有可用报告"
                )
                self.table.setItem(row, column, item)
        self._update_list_status()
        self._set_detail_visible(False)
        self._set_actions_enabled(None)
        if self.llm_panel is not None:
            self.llm_panel.set_source(None)
        self._update_clear_history_button()

    def _source_changed(self) -> None:
        self._update_clear_history_button()
        self._apply_filters()

    def _update_clear_history_button(self) -> None:
        source = str(self.source_combo.currentData() or "")
        label = {
            HistorySource.QUICK_START.value: "清空快速启动历史",
            HistorySource.LEGACY.value: "清空旧结果",
            "": "清空全部历史",
        }[source]
        self.clear_history_button.setText(label)
        self.clear_history_button.setEnabled(
            any(
                entry.status != "running" and not self._entry_has_active_llm_job(entry)
                for entry in self._entries_for_current_source()
            )
        )

    def _entries_for_current_source(self) -> tuple[HistoryEntry, ...]:
        source = str(self.source_combo.currentData() or "")
        return tuple(
            entry
            for entry in self._entries
            if not source or entry.source.value == source
        )

    def _reset_standard_filters(self) -> None:
        widgets = (
            self.search_edit,
            self.status_combo,
            self.task_kind_combo,
            self.source_combo,
            self.artifact_combo,
            self.health_combo,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.search_edit.clear()
            self.status_combo.setCurrentIndex(0)
            self.task_kind_combo.setCurrentIndex(0)
            self.artifact_combo.setCurrentIndex(0)
            self.health_combo.setCurrentIndex(0)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _selected_entry(self) -> HistoryEntry | None:
        row = self.table.currentRow()
        selection_model = self.table.selectionModel()
        selected_rows = selection_model.selectedRows(0) if selection_model is not None else ()
        selected_row_numbers = {index.row() for index in selected_rows}
        if row < 0 or row not in selected_row_numbers:
            row = selected_rows[0].row() if selected_rows else -1
        if row < 0:
            return None
        item = self.table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, HistoryEntry) else None

    def _selected_entries(self) -> tuple[HistoryEntry, ...]:
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return ()
        entries: list[HistoryEntry] = []
        for index in sorted(selection_model.selectedRows(0), key=lambda value: value.row()):
            item = self.table.item(index.row(), 0)
            value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(value, HistoryEntry):
                entries.append(value)
        return tuple(entries)

    def _update_list_status(self) -> None:
        selected_count = len(self._selected_entries())
        selected = f"；已选择 {selected_count} 条" if selected_count else ""
        self.list_status_label.setText(
            f"当前显示 {self._visible_entry_count} / {len(self._entries)} 条结果{selected}。"
        )

    def _selection_changed(self) -> None:
        entry = self._selected_entry()
        entries = self._selected_entries()
        self._update_list_status()
        self._set_actions_enabled(entry, entries)
        if entry is None:
            self._set_detail_visible(False)
            if self.llm_panel is not None:
                self.llm_panel.set_source(None)
            return
        self._set_detail_visible(True)
        selection_prefix = f"已选择 {len(entries)} 条 · 当前报告 · " if len(entries) > 1 else ""
        self.report_title_label.setText(f"{selection_prefix}{entry.name}")
        self.report_meta_label.setText(
            f"{entry.updated_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
            f" · 解读{_interpretation_state_label(entry.interpretation_state)}"
        )
        self.report_availability_label.setText("原始报告可用" if entry.has_report else "无原始报告")
        self.report_availability_label.setProperty("reportAvailable", entry.has_report)
        self.report_availability_label.style().unpolish(self.report_availability_label)
        self.report_availability_label.style().polish(self.report_availability_label)
        if self.llm_panel is not None:
            self.llm_panel.set_source(source_from_history(entry))

    def _set_detail_visible(self, visible: bool) -> None:
        self.detail_section.setVisible(visible)
        self.content_layout.invalidate()
        self.scroll_content.updateGeometry()

    def _delete_selected_entry(self) -> None:
        selected = self._selected_entries()
        if not selected:
            return
        entries = tuple(
            entry
            for entry in selected
            if entry.status != "running" and not self._entry_has_active_llm_job(entry)
        )
        protected_count = len(selected) - len(entries)
        if not entries:
            self.validation_banner.set_messages(
                "所选结果仍有任务或 LLM 解读正在运行，暂时不能删除。",
                severity=ValidationSeverity.WARNING,
            )
            return
        names = "\n".join(f"• {entry.name}" for entry in entries[:8])
        if len(entries) > 8:
            names += f"\n• …另有 {len(entries) - 8} 条"
        protected_note = (
            f"\n\n另有 {protected_count} 条正在运行的所选结果会被保留。"
            if protected_count
            else ""
        )
        answer = QMessageBox.warning(
            self,
            "永久删除所选历史结果",
            f"将永久删除所选 {len(entries)} 条历史结果的完整目录，包括报告、日志、"
            f"summary、LLM 解读和原始 artifact。\n\n{names}{protected_note}"
            "\n\n此操作无法撤销，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        success_message = (
            f"已删除“{entries[0].name}”。"
            if len(entries) == 1
            else f"已删除所选 {len(entries)} 条历史结果。"
        )
        self._delete_entries(entries, success_message=success_message)

    def _clear_history(self) -> None:
        entries = tuple(
            entry
            for entry in self._entries_for_current_source()
            if entry.status != "running" and not self._entry_has_active_llm_job(entry)
        )
        if not entries:
            self.validation_banner.set_messages(
                "当前来源没有可删除的历史结果。",
                severity=ValidationSeverity.INFO,
            )
            return
        running_count = sum(
            entry.status == "running" or self._entry_has_active_llm_job(entry)
            for entry in self._entries_for_current_source()
        )
        running_note = (
            f"\n\n另有 {running_count} 个正在运行的任务会被保留。"
            if running_count
            else ""
        )
        answer = QMessageBox.warning(
            self,
            "清空历史结果",
            f"将永久删除“{self.source_combo.currentText()}”中的 {len(entries)} 条结果，"
            f"包括报告、summary 和原始 artifact。{running_note}\n\n此操作无法撤销，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._delete_entries(
            entries,
            success_message=f"已清空 {len(entries)} 条{self.source_combo.currentText()}。",
        )

    def _delete_entries(
        self,
        entries: tuple[HistoryEntry, ...],
        *,
        success_message: str,
    ) -> None:
        result = self.history_service.delete_entries(entries)
        self._pending_delete_notice = _delete_notice(result, success_message)
        if result.deleted:
            self.delete_entry_button.setEnabled(False)
            self.clear_history_button.setEnabled(False)
            self.refresh()
            return
        message, severity = self._pending_delete_notice
        self._pending_delete_notice = None
        self.validation_banner.set_messages(message, severity=severity)

    def _open_report(self) -> None:
        entry = self._selected_entry()
        if entry is not None and entry.report_descriptor is not None:
            self._request_open(entry.report_descriptor, static=True)

    def _open_item_report(self, item: QTableWidgetItem) -> None:
        entry_item = self.table.item(item.row(), 0)
        entry = (
            entry_item.data(Qt.ItemDataRole.UserRole)
            if entry_item is not None
            else None
        )
        if not isinstance(entry, HistoryEntry):
            return
        descriptor = entry.report_descriptor
        if descriptor is not None and descriptor.exists:
            self._request_open(descriptor, static=True)

    def _request_open(self, descriptor: ResultDescriptor, *, static: bool) -> None:
        self.open_result_requested.emit(descriptor)
        if self.opener is None:
            return
        result = (
            self.opener.open_static(descriptor)
            if static
            else self.opener.open_descriptor(descriptor)
        )
        self._show_open_result(result)

    def _show_open_result(self, result: OpenResult) -> None:
        severity = ValidationSeverity.INFO if result.success else ValidationSeverity.ERROR
        self.validation_banner.set_messages(result.message, severity=severity)

    def _set_actions_enabled(
        self,
        entry: HistoryEntry | None,
        selected_entries: tuple[HistoryEntry, ...] | None = None,
    ) -> None:
        selected = self._selected_entries() if selected_entries is None else selected_entries
        self.open_report_button.setEnabled(
            bool(entry and entry.report_descriptor and entry.report_descriptor.exists)
        )
        deletable = tuple(
            value
            for value in selected
            if value.status != "running" and not self._entry_has_active_llm_job(value)
        )
        self.delete_entry_button.setEnabled(bool(deletable))
        self.delete_entry_button.setText(
            f"删除所选结果（{len(selected)}）" if len(selected) > 1 else "删除所选结果"
        )
        self.delete_entry_button.setAccessibleName(
            f"删除选中的 {len(selected)} 条历史结果"
            if selected
            else "删除选中的历史结果"
        )

    def _entry_has_active_llm_job(self, entry: HistoryEntry) -> bool:
        manager = self._interpretation_manager
        return bool(manager and manager.is_active_for(source_from_history(entry)))

    def _on_llm_job_state(
        self,
        _job_id: str,
        source: InterpretationSource,
        state: InterpretationState,
    ) -> None:
        updated_entry: HistoryEntry | None = None
        updated_entries: list[HistoryEntry] = []
        for entry in self._entries:
            if entry.directory.resolve(strict=False) == source.root:
                updated_entry = replace(
                    entry,
                    interpretation_state=state,
                    interpretation_path=(
                        source.interpretation_html_path
                        if source.interpretation_html_path.is_file()
                        else entry.interpretation_path
                    ),
                )
                updated_entries.append(updated_entry)
            else:
                updated_entries.append(entry)
        self._entries = tuple(updated_entries)
        if updated_entry is not None:
            self.entries_changed.emit(self._entries)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            entry = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(entry, HistoryEntry) and entry.directory.resolve(strict=False) == source.root:
                if updated_entry is not None and item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, updated_entry)
                self.table.setItem(row, 7, QTableWidgetItem(_interpretation_state_label(state)))
                self._selection_changed()
                break


def _health_label(health: HistoryHealth) -> str:
    return {
        HistoryHealth.COMPLETE: "完整",
        HistoryHealth.PARTIAL: "partial",
        HistoryHealth.UNKNOWN: "unknown",
    }[health]


def _task_kind_label(task_kind: HistoryTaskKind) -> str:
    return {
        HistoryTaskKind.RUN: "单版本",
        HistoryTaskKind.COMPARE: "A/B",
        HistoryTaskKind.UNKNOWN: "未知",
    }[task_kind]


def _interpretation_state_label(state: InterpretationState) -> str:
    return {
        InterpretationState.NOT_GENERATED: "未生成",
        InterpretationState.QUEUED: "排队中",
        InterpretationState.RUNNING: "生成中",
        InterpretationState.COMPLETED: "已完成",
        InterpretationState.FAILED: "失败",
        InterpretationState.CANCELLED: "已取消",
        InterpretationState.INTERRUPTED: "已中断",
    }[state]


def _matches_related_query(entry: HistoryEntry, query: HistoryQuery | None) -> bool:
    if query is None:
        return True
    if query.task_kind is not None and entry.task_kind is not query.task_kind:
        return False

    identifiers_match = any(
        (
            expected
            and actual
            and expected.casefold() == actual.casefold()
        )
        for expected, actual in (
            (query.preset_id, entry.preset_id),
            (query.scenario_id, entry.scenario_id),
            (query.label, entry.name),
        )
    )
    if not identifiers_match:
        return False
    if (
        query.package_name
        and entry.package_name
        and query.package_name.casefold() != entry.package_name.casefold()
    ):
        return False
    return True


def _delete_notice(
    result: HistoryDeleteResult,
    success_message: str,
) -> tuple[str, ValidationSeverity]:
    if result.errors:
        prefix = f"已删除 {len(result.deleted)} 个目录；" if result.deleted else ""
        return (
            prefix + "部分结果删除失败：" + "；".join(result.errors),
            ValidationSeverity.ERROR,
        )
    return success_message, ValidationSeverity.INFO
