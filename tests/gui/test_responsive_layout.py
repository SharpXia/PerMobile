from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QScrollArea,
)

from perfeval.gui.main_window import MainWindow
from perfeval.gui.design_tokens import COMPONENTS
from perfeval.gui.pages.start_page import StartPage
from perfeval.gui.pages.history_page import HistoryPage
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.theme import ThemeManager, ThemeMode


def _window(qtbot, tmp_path: Path) -> MainWindow:
    backend = QSettings(str(tmp_path / "responsive.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    settings = SettingsService(backend)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    theme = ThemeManager(application, settings)
    theme.apply(ThemeMode.LIGHT)
    window = MainWindow(
        settings=settings,
        workspace_service=workspace,
        theme_manager=theme,
    )
    qtbot.addWidget(window)
    window.resize(880, 600)
    window.show()
    qtbot.waitUntil(lambda: window.size().width() == 880)
    return window


def test_quick_start_minimum_window_keeps_fields_usable_when_expanded(
    qtbot,
    tmp_path: Path,
) -> None:
    window = _window(qtbot, tmp_path)
    page = window.page("start")
    assert isinstance(page, StartPage)
    panel = page.quick_panel

    panel.app_toggle.setChecked(True)
    panel.advanced_toggle.setChecked(True)
    qtbot.waitUntil(
        lambda: panel.form_host.minimumHeight() >= panel.form.sizeHint().height()
        and panel.form_scroll.horizontalScrollBar().maximum() == 0
    )

    assert panel.form_scroll.horizontalScrollBar().maximum() == 0
    assert panel.form_host.minimumHeight() >= panel.form.sizeHint().height()
    assert panel.app_form.fieldGrowthPolicy() == (
        panel.app_form.FieldGrowthPolicy.AllNonFixedFieldsGrow
    )
    assert abs(panel.app_details.x() - panel.app_toggle.x()) <= 2
    assert panel.app_details.geometry().right() >= (
        panel.form_host.width() - panel.form.contentsMargins().right() - 2
    )
    assert abs(panel.apk_picker.width() - panel.activity_combo.width()) <= 2
    assert panel.apk_picker.line_edit.width() > panel.apk_picker.browse_button.width()
    advanced_gap = panel.advanced_group.y() - panel.advanced_toggle.geometry().bottom() - 1
    assert advanced_gap == COMPONENTS.space_2
    assert panel.clear_data_check.y() == COMPONENTS.space_3
    widgets = [
        widget
        for widget_type in (QComboBox, QLineEdit, QAbstractSpinBox)
        for widget in panel.form_host.findChildren(widget_type)
        if not isinstance(widget.parentWidget(), QAbstractSpinBox)
    ]
    for widget in widgets:
        if widget.isVisibleTo(panel.form_host):
            assert widget.height() >= 30 or isinstance(widget, QAbstractSpinBox), (
                widget.objectName()
            )


def test_custom_pages_do_not_require_horizontal_scrolling_at_minimum_window(
    qtbot,
    tmp_path: Path,
) -> None:
    window = _window(qtbot, tmp_path)
    page = window.page("start")
    assert isinstance(page, StartPage)
    page.mode_tabs.setCurrentIndex(1)

    scroll_names = ("runConfigScroll", "abPageScroll", "diagnosePageScroll")
    for index, object_name in enumerate(scroll_names):
        page.custom_tabs.setCurrentIndex(index)
        qtbot.wait(0)
        scroll = page.custom_tabs.currentWidget().findChild(QScrollArea, object_name)
        assert scroll is not None
        assert scroll.horizontalScrollBar().maximum() == 0, object_name


def test_every_main_page_renders_at_declared_minimum_size(qtbot, tmp_path: Path) -> None:
    window = _window(qtbot, tmp_path)

    for page_id in window.page_ids:
        window.select_page(page_id)
        qtbot.wait(0)
        page = window.page(page_id)
        assert page.isVisibleTo(window)
        assert page.width() > 0
        assert page.height() > 0

    history = window.page("history")
    assert isinstance(history, HistoryPage)
    assert history.scroll.horizontalScrollBar().maximum() == 0
    assert history.table.minimumHeight() == history.table.maximumHeight()
