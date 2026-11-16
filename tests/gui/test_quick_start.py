from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from perfeval.gui.controllers.task_session_controller import TaskSessionController
from perfeval.gui.models.device import DeviceInfo, EnvironmentSnapshot, ToolStatus
from perfeval.gui.models.history import HistoryQuery, HistoryTaskKind
from perfeval.gui.models.preset import PresetTaskKind
from perfeval.gui.pages.start_page import QuickStartPanel
from perfeval.gui.services.command_builder import CommandBuilder
from perfeval.gui.services.device_service import DeviceService
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService


def _panel(qtbot, tmp_path: Path):
    backend = QSettings(str(tmp_path / "quick.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    settings = SettingsService(backend)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    devices = DeviceService(settings, workspace)
    session = TaskSessionController()
    panel = QuickStartPanel(
        settings=settings,
        workspace_service=workspace,
        device_service=devices,
        session=session,
        builder_factory=lambda root: CommandBuilder(root, cli_program="perfeval", display_platform="posix"),
    )
    qtbot.addWidget(panel)
    return panel, settings, session


def test_quick_start_defaults_to_smoothness_and_preserves_app_state(qtbot, tmp_path: Path) -> None:
    panel, _settings, _session = _panel(qtbot, tmp_path)
    assert panel.package_combo.currentText() == "com.perfeval.gamebox"
    assert (
        panel.apk_picker.line_edit.placeholderText()
        == "可选；留空则使用设备中已安装的该包名应用"
    )
    assert panel.activity_combo.placeholderText() == "可选；留空时自动识别 Launcher Activity"
    panel.set_environment_snapshot(
        EnvironmentSnapshot(
            tools=(ToolStatus("adb", "adb", True, "/tools/adb"),),
            devices=(DeviceInfo("emulator-5554", "device", model="Pixel 8"),),
            android_status="ready",
        )
    )
    panel.package_combo.setEditText("com.example.app")

    preset = panel.selected_preset
    assert preset is not None
    assert preset.title == "页面流畅度"
    assert preset.task_kind is PresetTaskKind.RUN
    assert panel.repeat_spin.value() == 3

    launch = panel._build_launch(preset)
    assert "--repeat" in launch.task.arguments
    assert launch.task.arguments[launch.task.arguments.index("--repeat") + 1] == "3"
    assert "--no-clear-data" in launch.task.arguments
    assert "--no-reinstall" in launch.task.arguments
    assert "--interactive" in launch.task.arguments
    assert launch.package_name == "com.example.app"
    assert launch.device_serial == "emulator-5554"


def test_quick_start_falls_back_to_smoothness_when_recent_preset_is_hidden(
    qtbot, tmp_path: Path
) -> None:
    panel, settings, _session = _panel(qtbot, tmp_path)
    settings.set_recent_preset(tmp_path, "workspace:scenarios/hidden-fixture.yaml")

    panel.reload()

    preset = panel.selected_preset
    assert preset is not None
    assert preset.preset_id == "builtin.smoothness"


def test_quick_start_recent_results_emits_selected_scenario_query(
    qtbot, tmp_path: Path
) -> None:
    panel, _settings, _session = _panel(qtbot, tmp_path)
    panel.package_combo.setEditText("com.example.app")

    with qtbot.waitSignal(panel.recent_results_requested) as blocker:
        panel.recent_button.click()

    query = blocker.args[0]
    assert isinstance(query, HistoryQuery)
    assert query.label == "页面流畅度"
    assert query.preset_id == "builtin.smoothness"
    assert query.scenario_id == "quick_smoothness"
    assert query.package_name == "com.example.app"
    assert query.task_kind is HistoryTaskKind.RUN


def test_quick_start_saves_selects_and_removes_recent_packages(qtbot, tmp_path: Path) -> None:
    panel, settings, _session = _panel(qtbot, tmp_path)
    settings.remember_package(tmp_path, "com.example.first")
    settings.remember_package(tmp_path, "com.example.second")
    panel.reload()

    assert tuple(panel.package_combo.itemText(index) for index in range(panel.package_combo.count())) == (
        "com.example.second",
        "com.example.first",
    )
    panel.package_combo.setCurrentIndex(1)
    assert panel.package_combo.currentText() == "com.example.first"

    panel.show()
    panel.package_combo.showPopup()
    qtbot.waitUntil(lambda: panel.package_combo.view().isVisible())
    first_rect = panel.package_combo.view().visualRect(panel.package_combo.model().index(0, 0))
    QTest.mouseClick(
        panel.package_combo.view().viewport(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(first_rect.right() - 8, first_rect.center().y()),
    )

    assert settings.recent_packages(tmp_path) == ("com.example.first",)
    assert panel.package_combo.currentText() == "com.example.first"


def test_quick_ab_requires_apks_and_explicit_apk_enables_install(qtbot, tmp_path: Path) -> None:
    panel, _settings, _session = _panel(qtbot, tmp_path)
    panel.scenario_list.setCurrentRow(3)
    preset = panel.selected_preset
    assert preset is not None and preset.task_kind is PresetTaskKind.AB

    baseline = tmp_path / "baseline.apk"
    candidate = tmp_path / "candidate.apk"
    baseline.write_bytes(b"apk")
    candidate.write_bytes(b"apk")
    panel.baseline_picker.set_path(str(baseline))
    panel.candidate_editor.rows[0].apk_picker.set_path(str(candidate))
    panel.package_combo.setEditText("com.example.app")
    launch = panel._build_launch(preset)
    assert launch.task.task_type.value == "ab"

    panel.scenario_list.setCurrentRow(1)
    panel.apk_picker.set_path(str(candidate))
    assert panel.reinstall_check.isChecked()


def test_quick_start_requires_device_choice_when_multiple_are_online(qtbot, tmp_path: Path) -> None:
    panel, _settings, _session = _panel(qtbot, tmp_path)
    panel.set_environment_snapshot(
        EnvironmentSnapshot(
            tools=(ToolStatus("adb", "adb", True, "/tools/adb"),),
            devices=(
                DeviceInfo("device-1", "device", model="Pixel 8"),
                DeviceInfo("device-2", "device", model="Pixel 9"),
            ),
            android_status="ambiguous",
        )
    )
    panel.package_combo.setEditText("com.example.app")
    preset = panel.selected_preset
    assert preset is not None

    try:
        panel._build_launch(preset)
    except ValueError as exc:
        assert "多台" in str(exc)
    else:
        raise AssertionError("multiple devices must require an explicit choice")
