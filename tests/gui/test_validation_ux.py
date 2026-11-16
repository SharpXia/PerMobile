from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from perfeval.gui.main_window import MainWindow
from perfeval.gui.models.task import LogChunk, LogStream
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.theme import ThemeManager, ThemeMode
from perfeval.gui.widgets.task_console import TaskConsole


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _window(qtbot, tmp_path: Path) -> tuple[MainWindow, ThemeManager, WorkspaceService]:
    backend = QSettings(str(tmp_path / "ux.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    settings = SettingsService(backend)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    theme = ThemeManager(application, settings)
    window = MainWindow(
        settings=settings,
        workspace_service=workspace,
        theme_manager=theme,
        confirm_close=lambda _count: False,
    )
    qtbot.addWidget(window)
    window.show()
    return window, theme, workspace


def test_validation_keyboard_navigation_reaches_pages_and_form_controls(qtbot, tmp_path: Path) -> None:
    window, _theme, _workspace = _window(qtbot, tmp_path)
    window.select_page("start")
    window.navigation_list.setFocus()

    QTest.keyClick(window.navigation_list, Qt.Key.Key_Down)
    assert window.current_page_id == "task"
    QTest.keyClick(window.navigation_list, Qt.Key.Key_Down)
    assert window.current_page_id == "history"

    start_page = window.page("start")
    window.select_page("start")
    window.raise_()
    window.activateWindow()
    QApplication.processEvents()
    first = start_page.quick_panel.scenario_list
    first.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(first.hasFocus, timeout=1000)
    QTest.keyClick(first, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is not first


def test_validation_key_inputs_have_accessible_names_or_buddy_labels(qtbot, tmp_path: Path) -> None:
    window, _theme, _workspace = _window(qtbot, tmp_path)
    targets: list[QWidget] = []
    for page_id in ("start", "task", "environment", "history", "settings"):
        page = window.page(page_id)
        for widget_type in (
            QLineEdit,
            QComboBox,
            QSpinBox,
            QDoubleSpinBox,
            QCheckBox,
            QPushButton,
        ):
            targets.extend(page.findChildren(widget_type))

    unlabeled = [
        _widget_name(widget)
        for widget in targets
        if widget.isEnabled()
        and not _has_accessible_label(widget, window)
        and not _is_internal_editor(widget)
    ]
    assert unlabeled == []


def test_validation_light_and_dark_palettes_remain_readable_and_textual(
    qtbot,
    tmp_path: Path,
) -> None:
    window, theme, _workspace = _window(qtbot, tmp_path)
    application = QApplication.instance()
    assert isinstance(application, QApplication)

    theme.apply(ThemeMode.LIGHT)
    light = QPalette(application.palette())
    assert _contrast(
        light.color(QPalette.ColorRole.Window),
        light.color(QPalette.ColorRole.WindowText),
    ) >= 4.5

    theme.apply(ThemeMode.DARK)
    dark = QPalette(application.palette())
    assert _contrast(
        dark.color(QPalette.ColorRole.Window),
        dark.color(QPalette.ColorRole.WindowText),
    ) >= 4.5
    assert theme.mode == ThemeMode.DARK

    ab_page = window.page("start").custom_ab
    assert "variant" in ab_page.variant_status_label.text().casefold()
    assert ab_page.console.status_label.text() == "空闲"
    theme.apply(ThemeMode.SYSTEM)


def test_validation_wide_form_fields_do_not_use_macos_size_hint_policy(
    qtbot,
    tmp_path: Path,
) -> None:
    window, _theme, _workspace = _window(qtbot, tmp_path)

    start_page = window.page("start")
    run_page = start_page.custom_run
    ab_page = start_page.custom_ab
    diagnose_page = start_page.custom_diagnose
    environment_page = window.page("environment")
    settings_page = window.page("settings")
    layouts = (
        run_page.device_form.layout(),
        run_page.offline_form.layout(),
        ab_page.common_group.layout(),
        ab_page.baseline_group.layout(),
        diagnose_page.package_edit.parentWidget().layout(),
        environment_page.cli_picker.parentWidget().layout(),
        settings_page.cli_picker.parentWidget().layout(),
    )

    for layout in layouts:
        assert isinstance(layout, QFormLayout)
        assert (
            layout.fieldGrowthPolicy()
            != QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )


def test_validation_workspace_is_consistent_initially_and_after_switch(qtbot, tmp_path: Path) -> None:
    window, _theme, workspace = _window(qtbot, tmp_path)
    initial = tmp_path.resolve()

    start_page = window.page("start")
    assert start_page.custom_run._workspace_service.current == initial
    assert start_page.custom_ab.workspace == initial
    assert start_page.custom_diagnose.workspace == initial
    assert window.page("history").history_service.workspace == initial

    target = tmp_path / "切换 后 workspace"
    result = workspace.set_workspace(target, create=True)
    assert result.valid
    QApplication.processEvents()

    assert start_page.custom_run._workspace_service.current == target.resolve()
    assert start_page.custom_ab.workspace == target.resolve()
    assert start_page.custom_diagnose.workspace == target.resolve()
    assert window.page("history").history_service.workspace == target.resolve()
    assert window._run_platform_open.allowed_roots == (target.resolve(),)


def test_validation_console_copies_visible_log_and_equivalent_command(qtbot) -> None:
    console = TaskConsole()
    qtbot.addWidget(console)
    console.append_chunk(LogChunk(1, LogStream.STDOUT, "中文 stdout\n", 1))
    console.append_chunk(LogChunk(2, LogStream.STDERR, "错误 stderr\n", 2))
    console.set_command_preview("perfeval run '空格 场景.yaml'")

    console.copy_button.click()
    visible = QApplication.clipboard().text()
    assert "中文 stdout" in visible
    assert "错误 stderr" in visible
    console.copy_command_button.click()
    assert QApplication.clipboard().text() == "perfeval run '空格 场景.yaml'"


def test_validation_offscreen_150_percent_scale_smoke(tmp_path: Path) -> None:
    settings_path = tmp_path / "scale-settings.ini"
    code = f"""
from pathlib import Path
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton
from perfeval.gui.main_window import MainWindow
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.theme import ThemeManager, ThemeMode

app = QApplication([])
backend = QSettings({str(settings_path)!r}, QSettings.Format.IniFormat)
backend.setFallbacksEnabled(False)
settings = SettingsService(backend)
workspace = WorkspaceService(settings, default_workspace=Path({str(tmp_path)!r}))
theme = ThemeManager(app, settings)
theme.apply(ThemeMode.LIGHT)
window = MainWindow(
    settings=settings,
    workspace_service=workspace,
    theme_manager=theme,
    confirm_close=lambda _count: False,
)
window.resize(1120, 720)
window.show()
app.processEvents()
assert app.primaryScreen().devicePixelRatio() >= 1.49
for page_id in window.page_ids:
    window.select_page(page_id)
    app.processEvents()
    page = window.page(page_id)
    for button in page.findChildren(QPushButton):
        if button.isVisibleTo(page):
            assert button.height() >= 24, (page_id, button.objectName(), button.height())
assert window.navigation_list.width() > 0
assert window.page_stack.width() > 0
window.close()
app.processEvents()
"""
    environment = os.environ.copy()
    environment.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "QT_SCALE_FACTOR": "1.5",
            "PYTHONPATH": str(PROJECT_ROOT),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def _has_accessible_label(widget: QWidget, root: QWidget) -> bool:
    if widget.accessibleName().strip():
        return True
    if isinstance(widget, QAbstractButton) and widget.text().strip():
        return True
    for label in root.findChildren(QLabel):
        buddy = label.buddy()
        if buddy is widget or (buddy is not None and buddy.isAncestorOf(widget)):
            return bool(label.text().strip())
    return False


def _is_internal_editor(widget: QWidget) -> bool:
    parent = widget.parentWidget()
    return isinstance(widget, QLineEdit) and isinstance(parent, (QComboBox, QSpinBox, QDoubleSpinBox))


def _widget_name(widget: QWidget) -> str:
    return widget.objectName() or f"{type(widget).__name__}:{getattr(widget, 'text', lambda: '')()}"


def _contrast(first: QColor, second: QColor) -> float:
    lighter = max(_luminance(first), _luminance(second))
    darker = min(_luminance(first), _luminance(second))
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(color: QColor) -> float:
    channels = []
    for value in (color.redF(), color.greenF(), color.blueF()):
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
