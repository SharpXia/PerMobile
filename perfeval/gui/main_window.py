from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSystemTrayIcon,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .models.result import ResultDescriptor, ResultType, ResultViewer
from .models.history import HistoryQuery
from .controllers.task_session_controller import TaskSessionController
from .pages import EnvironmentPage, HistoryPage, SettingsPage, StartPage, TaskPage
from .services.command_builder import CommandBuilder
from .services.device_service import DeviceService
from .services.platform_open import OpenResult, PlatformOpenService
from .services.settings import SettingsService
from .services.llm_jobs import InterpretationJobManager
from .services.llm_profiles import LlmProfileService
from .services.tool_paths import command_builder_from_settings
from .services.workspace import WorkspaceService
from .design_tokens import COMPONENTS
from .theme import ThemeManager

if TYPE_CHECKING:
    from .services.gui_logging import GuiLogService


@dataclass(frozen=True)
class NavigationItem:
    page_id: str
    label: str


NAVIGATION = (
    NavigationItem("start", "开始测试"),
    NavigationItem("task", "任务"),
    NavigationItem("history", "历史结果"),
    NavigationItem("environment", "环境与设备"),
    NavigationItem("settings", "设置"),
)

NAVIGATION_SYMBOLS = ("⌁", "◉", "▤", "⌘", "⚙")


class MainWindow(QMainWindow):
    page_changed = Signal(str)

    def __init__(
        self,
        *,
        settings: SettingsService,
        workspace_service: WorkspaceService,
        theme_manager: ThemeManager,
        gui_log_service: GuiLogService | None = None,
        confirm_close: Callable[[int], bool] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._workspace_service = workspace_service
        self._theme_manager = theme_manager
        self._gui_log_service = gui_log_service
        self._confirm_close = confirm_close or self._confirm_running_tasks_close
        self._pages: dict[str, QWidget] = {}
        self._run_platform_open: PlatformOpenService | None = None
        self._close_after_shutdown = False
        self._allow_close = False
        self.device_service = DeviceService(settings, workspace_service, self)
        self.task_session = TaskSessionController(self)
        self.llm_profiles = LlmProfileService(settings, self)
        self.interpretation_manager = InterpretationJobManager(self.llm_profiles, self)
        self._notification_tray: QSystemTrayIcon | None = None

        self.setWindowTitle("PerfEval Lite")
        self.resize(1280, 800)
        # Keep the compact layout usable for QA laptops and the existing
        # minimum-size contract; the page-level scroll areas absorb overflow.
        self.setMinimumSize(880, 600)

        root = QWidget(self)
        root.setObjectName("appRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        navigation_rail = QFrame(root)
        navigation_rail.setObjectName("navigationRail")
        navigation_rail.setFixedWidth(COMPONENTS.nav_width)
        navigation_layout = QVBoxLayout(navigation_rail)
        navigation_layout.setContentsMargins(14, 18, 14, 14)
        navigation_layout.setSpacing(12)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        brand_mark = QLabel("P", navigation_rail)
        brand_mark.setObjectName("brandMark")
        brand_mark.setFixedSize(38, 38)
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(brand_mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand = QLabel("PerfEval Lite", navigation_rail)
        brand.setObjectName("brandLabel")
        brand_kicker = QLabel("移动性能评估", navigation_rail)
        brand_kicker.setObjectName("brandKicker")
        brand_text.addWidget(brand)
        brand_text.addWidget(brand_kicker)
        brand_row.addLayout(brand_text, 1)
        navigation_layout.addLayout(brand_row)

        workspace_card = QFrame(navigation_rail)
        workspace_card.setObjectName("workspaceCard")
        workspace_layout = QVBoxLayout(workspace_card)
        workspace_layout.setContentsMargins(12, 10, 12, 10)
        workspace_layout.setSpacing(4)
        workspace_caption = QLabel("当前工作区", workspace_card)
        workspace_caption.setObjectName("workspaceCaption")
        workspace_layout.addWidget(workspace_caption)
        self.workspace_label = QLabel(str(workspace_service.current), workspace_card)
        self.workspace_label.setObjectName("navigationWorkspaceLabel")
        self.workspace_label.setWordWrap(True)
        self.workspace_label.setToolTip(str(workspace_service.current))
        workspace_layout.addWidget(self.workspace_label)
        navigation_layout.addWidget(workspace_card)

        navigation_section = QLabel("导航", navigation_rail)
        navigation_section.setObjectName("navigationSectionLabel")
        navigation_layout.addWidget(navigation_section)

        self.navigation_list = QListWidget(navigation_rail)
        self.navigation_list.setObjectName("navigationList")
        self.navigation_list.setAccessibleName("主导航")
        self.navigation_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.navigation_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        for index, item in enumerate(NAVIGATION):
            widget_item = QListWidgetItem(f"{NAVIGATION_SYMBOLS[index]}  {item.label}")
            widget_item.setData(Qt.ItemDataRole.UserRole, item.page_id)
            widget_item.setToolTip(f"打开{item.label}")
            self.navigation_list.addItem(widget_item)
        self.navigation_list.currentRowChanged.connect(self._activate_row)
        navigation_layout.addWidget(self.navigation_list, 1)

        privacy_note = QLabel("本地实验室 · 数据只保存在工作区", navigation_rail)
        privacy_note.setObjectName("privacyNote")
        privacy_note.setWordWrap(True)
        navigation_layout.addWidget(privacy_note)

        root_layout.addWidget(navigation_rail)

        content_shell = QFrame(root)
        content_shell.setObjectName("contentShell")
        shell_layout = QVBoxLayout(content_shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        top_bar = QFrame(content_shell)
        top_bar.setObjectName("topBar")
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(30, 14, 30, 14)
        top_bar_layout.setSpacing(12)
        self.top_bar_page_label = QLabel("开始测试", top_bar)
        self.top_bar_page_label.setObjectName("topBarPageLabel")
        top_bar_layout.addWidget(self.top_bar_page_label)
        top_bar_context = QLabel("本地执行 · 可复现的性能证据", top_bar)
        top_bar_context.setObjectName("topBarContext")
        top_bar_layout.addWidget(top_bar_context)
        top_bar_layout.addStretch(1)
        self.top_bar_workspace = QLabel(str(workspace_service.current), top_bar)
        self.top_bar_workspace.setObjectName("topBarWorkspace")
        self.top_bar_workspace.setToolTip(str(workspace_service.current))
        top_bar_layout.addWidget(self.top_bar_workspace)
        shell_layout.addWidget(top_bar)

        self.page_stack = QStackedWidget(content_shell)
        self.page_stack.setObjectName("pageStack")
        shell_layout.addWidget(self.page_stack, 1)
        root_layout.addWidget(content_shell, 1)

        self._install_pages()
        self.interpretation_manager.notification_requested.connect(self._notify_llm_result)
        self._workspace_service.workspace_changed.connect(self._on_workspace_changed)
        self._restore_window_state()
        self.select_page("start")
        history_page = self._pages.get("history")
        if isinstance(history_page, HistoryPage):
            QTimer.singleShot(0, history_page.load_once)

    @property
    def current_page_id(self) -> str:
        row = self.navigation_list.currentRow()
        if row < 0:
            return ""
        item = self.navigation_list.item(row)
        return str(item.data(Qt.ItemDataRole.UserRole))

    @property
    def page_ids(self) -> tuple[str, ...]:
        return tuple(item.page_id for item in NAVIGATION)

    def page(self, page_id: str) -> QWidget:
        return self._pages[page_id]

    def select_page(self, page_id: str) -> None:
        for row in range(self.navigation_list.count()):
            item = self.navigation_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == page_id:
                self.navigation_list.setCurrentRow(row)
                return
        raise KeyError(f"unknown page: {page_id}")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        active_pages = self._active_task_pages()
        if active_pages and not self._allow_close:
            if not self._close_after_shutdown:
                if not self._confirm_close(len(active_pages)):
                    event.ignore()
                    return
                self._close_after_shutdown = True
                for page in active_pages:
                    page.shutdown()
            event.ignore()
            QTimer.singleShot(50, self._finish_close_after_shutdown)
            return

        self._settings.window_geometry = self.saveGeometry()
        self._settings.window_state = self.saveState()
        self._shutdown_viewers()
        super().closeEvent(event)

    def _install_pages(self) -> None:
        workspace = self._workspace_service.current
        builder = self._builder_for(workspace)
        self._run_platform_open = PlatformOpenService(
            self,
            cli_program=builder.program,
            cli_prefix_arguments=builder.prefix_arguments,
            working_directory=workspace,
            allowed_roots=(workspace,),
        )
        pages: tuple[tuple[str, QWidget], ...] = (
            (
                "start",
                StartPage(
                    self,
                    settings=self._settings,
                    workspace_service=self._workspace_service,
                    device_service=self.device_service,
                    session=self.task_session,
                    builder_factory=lambda root: self._builder_for(root),
                ),
            ),
            (
                "task",
                TaskPage(
                    self,
                    session=self.task_session,
                    llm_profiles=self.llm_profiles,
                    interpretation_manager=self.interpretation_manager,
                ),
            ),
            (
                "history",
                HistoryPage(
                    self,
                    workspace_service=self._workspace_service,
                    opener=self._run_platform_open,
                    llm_profiles=self.llm_profiles,
                    interpretation_manager=self.interpretation_manager,
                ),
            ),
            (
                "environment",
                EnvironmentPage(
                    self,
                    settings=self._settings,
                    workspace_service=self._workspace_service,
                    device_service=self.device_service,
                ),
            ),
            (
                "settings",
                SettingsPage(
                    self._settings,
                    self._workspace_service,
                    self,
                    diagnostics_service=self._gui_log_service,
                    llm_profiles=self.llm_profiles,
                    interpretation_manager=self.interpretation_manager,
                ),
            ),
        )
        for page_id, page in pages:
            page.setObjectName(f"{page_id}Page")
            self._pages[page_id] = page
            self.page_stack.addWidget(page)

        start_page = self._pages["start"]
        assert isinstance(start_page, StartPage)
        start_page.navigation_requested.connect(self.select_page)
        start_page.history_requested.connect(self._show_related_history)
        start_page.task_started.connect(lambda _launch: self.select_page("task"))
        task_page = self._pages["task"]
        assert isinstance(task_page, TaskPage)
        task_page.navigation_requested.connect(self._handle_navigation_request)
        task_page.open_result_requested.connect(self._open_run_result)
        task_page.open_log_requested.connect(self._open_run_log)
        settings_page = self._pages["settings"]
        assert isinstance(settings_page, SettingsPage)
        settings_page.theme_change_requested.connect(lambda mode: self._theme_manager.apply(mode))
        history_page = self._pages["history"]
        assert isinstance(history_page, HistoryPage)
        history_page.settings_requested.connect(self._show_llm_settings)

        start_page.custom_run.open_result_requested.connect(self._open_run_result)
        start_page.custom_run.open_log_requested.connect(self._open_run_log)
        start_page.custom_ab.result_open_requested.connect(self._open_run_result)
        environment_page = self._pages["environment"]
        assert isinstance(environment_page, EnvironmentPage)
        environment_page.device_service.snapshot_ready.connect(
            start_page.quick_panel.set_environment_snapshot
        )
        self.task_session.controller.finished.connect(lambda _result: self._finish_close_after_shutdown())

    def _show_related_history(self, query: HistoryQuery) -> None:
        history_page = self._pages["history"]
        assert isinstance(history_page, HistoryPage)
        self.select_page("history")
        history_page.show_related_results(query)

    def _handle_navigation_request(self, page_id: str) -> None:
        if page_id == "settings":
            self._show_llm_settings()
            return
        self.select_page(page_id)

    def _show_llm_settings(self) -> None:
        self.select_page("settings")
        page = self._pages.get("settings")
        if isinstance(page, SettingsPage):
            QTimer.singleShot(0, page.focus_llm_section)

    def _activate_row(self, row: int) -> None:
        if row < 0 or row >= self.page_stack.count():
            return
        self.page_stack.setCurrentIndex(row)
        navigation = NAVIGATION[row]
        self.setWindowTitle(f"{navigation.label} — PerfEval Lite")
        self.top_bar_page_label.setText(navigation.label)
        self.navigation_list.setAccessibleDescription(f"当前页面：{navigation.label}")
        self.page_changed.emit(navigation.page_id)

    def refresh_overview(self) -> None:
        environment_page = self._pages.get("environment")
        if isinstance(environment_page, EnvironmentPage):
            environment_page.refresh()

    def _on_workspace_changed(self, workspace: str) -> None:
        self.workspace_label.setText(workspace)
        self.workspace_label.setToolTip(workspace)
        self.top_bar_workspace.setText(workspace)
        self.top_bar_workspace.setToolTip(workspace)
        target = Path(workspace).expanduser().resolve()
        builder = self._builder_for(target)
        start_page = self._pages.get("start")
        if isinstance(start_page, StartPage):
            start_page.quick_panel.reload()
            start_page.custom_ab.set_workspace(target, command_builder=builder)
            start_page.custom_diagnose.set_workspace(target)
        if self._run_platform_open is not None:
            self._run_platform_open.configure_cli(
                program=builder.program,
                prefix_arguments=builder.prefix_arguments,
                working_directory=target,
            )
            self._run_platform_open.set_allowed_roots((target,))

    def _restore_window_state(self) -> None:
        geometry = self._settings.window_geometry
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self._settings.window_state
        if state is not None:
            self.restoreState(state)

    def _builder_for(self, workspace: str | Path) -> CommandBuilder:
        try:
            return command_builder_from_settings(self._settings, workspace)
        except Exception:
            return CommandBuilder(workspace)

    def _active_task_pages(self) -> tuple[object, ...]:
        values: list[object] = []
        if self.task_session.is_active:
            values.append(self.task_session)
        if self.interpretation_manager.active_count:
            values.append(self.interpretation_manager)
        return tuple(values)

    def _finish_close_after_shutdown(self) -> None:
        if not self._close_after_shutdown:
            return
        if self._active_task_pages():
            QTimer.singleShot(50, self._finish_close_after_shutdown)
            return
        self._close_after_shutdown = False
        self._allow_close = True
        self.close()

    def _confirm_running_tasks_close(self, active_count: int) -> bool:
        test_count = 1 if self.task_session.is_active else 0
        llm_count = self.interpretation_manager.active_count
        parts = []
        if test_count:
            parts.append(f"{test_count} 个性能测试")
        if llm_count:
            parts.append(f"{llm_count} 个 LLM 解读")
        detail = "、".join(parts) or f"{active_count} 个任务"
        answer = QMessageBox.question(
            self,
            "任务仍在运行",
            f"当前有{detail}仍在运行。关闭应用会取消这些任务；已完成的报告和解读会保留，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _notify_llm_result(self, title: str, message: str, success: bool) -> None:
        self.statusBar().showMessage(f"{title}：{message}", 10_000)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._notification_tray is None:
            icon = self.windowIcon()
            if icon.isNull():
                app = QApplication.instance()
                icon = app.windowIcon() if isinstance(app, QApplication) else QIcon()
            if icon.isNull():
                icon = QIcon.fromTheme("dialog-information")
            self._notification_tray = QSystemTrayIcon(icon, self)
            self._notification_tray.setToolTip("PerfEval Lite")
            self._notification_tray.show()
        icon_type = (
            QSystemTrayIcon.MessageIcon.Information
            if success
            else QSystemTrayIcon.MessageIcon.Warning
        )
        self._notification_tray.showMessage(title, message, icon_type, 8_000)

    def _open_run_result(self, descriptor: ResultDescriptor) -> None:
        service = self._run_platform_open
        if service is None:
            return
        result = (
            service.open_static(descriptor)
            if descriptor.viewer in {ResultViewer.REPORT, ResultViewer.PERFETTO}
            else service.open_descriptor(descriptor)
        )
        self._show_open_result(result)

    def _open_run_log(self, location: str) -> None:
        path = Path(location).expanduser().resolve(strict=False)
        descriptor = ResultDescriptor(
            type=ResultType.LOG,
            label="完整日志",
            location=str(path),
            exists=path.is_file(),
            viewer=ResultViewer.SYSTEM,
        )
        if self._run_platform_open is not None:
            self._show_open_result(self._run_platform_open.open_descriptor(descriptor))

    def _show_open_result(self, result: OpenResult) -> None:
        self.statusBar().showMessage(result.message, 8000)

    def _shutdown_viewers(self) -> None:
        if self._notification_tray is not None:
            self._notification_tray.hide()
        if self._run_platform_open is not None:
            self._run_platform_open.shutdown()
        for page_id in ("ab", "diagnose"):
            page = self._pages.get(page_id)
            platform_open = getattr(page, "platform_open", None)
            if platform_open is not None:
                platform_open.shutdown()
