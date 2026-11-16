from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from perfeval.gui.models.device import DeviceInfo, EnvironmentSnapshot, ToolStatus
from perfeval.gui.pages.environment_page import EnvironmentPage
from perfeval.gui.services.device_service import classify_android_status, parse_adb_devices
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.tool_paths import (
    TRACE_PROCESSOR_ENV_VAR,
    command_builder_from_settings,
    resolve_tool_paths,
)
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.widgets.validation_banner import ValidationSeverity


class FakeDeviceService(QObject):
    refresh_started = Signal()
    snapshot_ready = Signal(object)
    refresh_failed = Signal(str)
    refresh_finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.refresh_count = 0
        self.refreshing = False

    def refresh(self) -> bool:
        if self.refreshing:
            return False
        self.refresh_count += 1
        self.refreshing = True
        self.refresh_started.emit()
        return True

    def publish(self, snapshot: EnvironmentSnapshot) -> None:
        self.snapshot_ready.emit(snapshot)
        self.refreshing = False
        self.refresh_finished.emit()


def _settings(tmp_path: Path) -> SettingsService:
    backend = QSettings(str(tmp_path / "environment.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def test_parse_adb_devices_distinguishes_online_offline_and_unauthorized() -> None:
    devices = parse_adb_devices(
        """List of devices attached
online-1\tdevice product:pixel model:Pixel_8 device:husky transport_id:1
offline-1\toffline model:Pixel_6
locked-1\tunauthorized usb:1-2
"""
    )

    assert [(device.serial, device.state) for device in devices] == [
        ("online-1", "device"),
        ("offline-1", "offline"),
        ("locked-1", "unauthorized"),
    ]
    assert devices[0].model == "Pixel_8"
    assert classify_android_status((devices[0],)) == ("ready", ())
    assert classify_android_status((devices[1],))[0] == "offline"
    assert classify_android_status((devices[2],))[0] == "unauthorized"


def test_invalid_configured_adb_warns_and_falls_back_to_system_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.set_external_tool_path("adb", str(tmp_path / "missing-adb"))
    monkeypatch.setattr(
        "perfeval.gui.services.tool_paths.shutil.which",
        lambda name: "/system/platform-tools/adb" if name == "adb" else None,
    )

    resolved = resolve_tool_paths(
        settings,
        environ={"PATH": "/system/bin"},
        home=tmp_path / "empty-home",
        platform_name="darwin",
    )

    assert resolved.adb_program == "/system/platform-tools/adb"
    assert resolved.environment == {}
    assert "回退自动发现" in resolved.warnings[0]


def test_valid_configured_adb_is_prepended_to_task_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    adb = tmp_path / "platform tools" / "adb"
    adb.parent.mkdir()
    adb.write_text("adb", encoding="utf-8")
    settings.set_external_tool_path("adb", str(adb))
    monkeypatch.setenv("PATH", "/system/bin")

    resolved = resolve_tool_paths(settings)
    builder = command_builder_from_settings(settings, tmp_path)

    assert resolved.adb_program == str(adb.resolve())
    assert resolved.warnings == ()
    assert resolved.environment["PATH"] == f"{adb.parent}{os.pathsep}/system/bin"
    assert builder.environment["PATH"] == resolved.environment["PATH"]


def test_default_macos_android_tools_are_discovered_without_login_shell(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    adb = tmp_path / "Library/Android/sdk/platform-tools/adb"
    studio = tmp_path / "Applications/Android Studio.app/Contents/MacOS/studio"
    adb.parent.mkdir(parents=True)
    studio.parent.mkdir(parents=True)
    adb.write_text("adb", encoding="utf-8")
    studio.write_text("studio", encoding="utf-8")
    monkeypatch.setattr(
        "perfeval.gui.services.tool_paths._default_android_studio_candidates",
        lambda _home, _system, _environ: (tmp_path / "Applications/Android Studio.app",),
    )

    resolved = resolve_tool_paths(
        settings,
        environ={"PATH": "/usr/bin:/bin"},
        home=tmp_path,
        platform_name="darwin",
        which=lambda _name: None,
    )

    assert resolved.adb_program == str(adb.resolve())
    assert resolved.adb_source == "default"
    assert resolved.environment["PATH"].startswith(f"{adb.parent}{os.pathsep}")
    assert resolved.android_studio_program == str(studio.resolve())
    assert resolved.android_studio_source == "default"


def test_configured_android_studio_app_bundle_is_normalized_to_executable(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = tmp_path / "Android Studio.app"
    studio = app / "Contents/MacOS/studio"
    studio.parent.mkdir(parents=True)
    studio.write_text("studio", encoding="utf-8")
    settings.set_external_tool_path("android_studio", str(app))

    resolved = resolve_tool_paths(
        settings,
        environ={"PATH": "/usr/bin:/bin"},
        home=tmp_path / "empty-home",
        platform_name="darwin",
        which=lambda _name: None,
    )

    assert resolved.android_studio_program == str(studio.resolve())
    assert resolved.android_studio_source == "configured"
    assert resolved.warnings == ()


def test_configured_trace_processor_is_exported_to_task_environment(
    tmp_path: Path,
) -> None:
    """TASK-025/ADR-0001：GUI 配置经 PERFEVAL_TRACE_PROCESSOR 进入 CLI 子进程发现链。"""
    settings = _settings(tmp_path)
    tp = tmp_path / "tools" / "trace_processor_shell"
    tp.parent.mkdir(parents=True)
    tp.write_text("binary", encoding="utf-8")
    settings.set_external_tool_path("trace_processor", str(tp))

    resolved = resolve_tool_paths(
        settings,
        environ={"PATH": "/usr/bin"},
        home=tmp_path / "empty-home",
        platform_name="darwin",
        which=lambda _name: None,
    )
    builder = command_builder_from_settings(settings, tmp_path)

    assert resolved.trace_processor_program == str(tp.resolve())
    assert resolved.configured_trace_processor == str(tp)
    assert resolved.warnings == ()
    assert resolved.environment[TRACE_PROCESSOR_ENV_VAR] == str(tp.resolve())
    assert builder.environment[TRACE_PROCESSOR_ENV_VAR] == str(tp.resolve())


def test_invalid_configured_trace_processor_warns_and_leaves_env_unset(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.set_external_tool_path("trace_processor", str(tmp_path / "missing-tp"))

    resolved = resolve_tool_paths(
        settings,
        environ={"PATH": "/usr/bin"},
        home=tmp_path / "empty-home",
        platform_name="darwin",
        which=lambda _name: None,
    )

    assert resolved.trace_processor_program is None
    assert TRACE_PROCESSOR_ENV_VAR not in resolved.environment
    assert resolved.warnings and "trace_processor" in resolved.warnings[0]


def test_unset_trace_processor_keeps_task_environment_clean(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    resolved = resolve_tool_paths(
        settings,
        environ={"PATH": "/usr/bin"},
        home=tmp_path / "empty-home",
        platform_name="darwin",
        which=lambda _name: None,
    )

    assert resolved.trace_processor_program is None
    assert resolved.configured_trace_processor is None
    assert TRACE_PROCESSOR_ENV_VAR not in resolved.environment
    assert resolved.warnings == ()


def test_trace_processor_env_var_matches_cli_discovery_constant() -> None:
    from perfeval.collectors.android.perfetto_trace import (
        TRACE_PROCESSOR_ENV_VAR as cli_env_var,
    )

    assert TRACE_PROCESSOR_ENV_VAR == cli_env_var


def test_environment_page_saves_paths_refreshes_and_renders_actionable_states(
    qtbot,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    service = FakeDeviceService()
    page = EnvironmentPage(
        settings=settings,
        workspace_service=workspace,
        device_service=service,
    )
    qtbot.addWidget(page)
    cli = tmp_path / "perfeval"
    adb = tmp_path / "adb"
    studio = tmp_path / "Android Studio"
    for path in (cli, adb, studio):
        path.write_text("tool", encoding="utf-8")
    page.cli_picker.set_path(str(cli))
    page.adb_picker.set_path(str(adb))
    page.android_studio_picker.set_path(str(studio))

    page.refresh_button.click()

    assert service.refresh_count == 1
    assert not page.refresh_button.isEnabled()
    assert settings.external_tool_path("cli") == str(cli)
    assert settings.external_tool_path("adb") == str(adb)
    snapshot = EnvironmentSnapshot(
        tools=(
            ToolStatus("cli", "PerfEval CLI", True, str(cli), "perfeval 0.1.0"),
            ToolStatus("adb", "adb", True, str(adb), "自定义路径"),
            ToolStatus("android_studio", "Android Studio", True, str(studio), "可选工具"),
            ToolStatus("browser", "系统浏览器", True, None, "系统默认应用"),
        ),
        devices=(
            DeviceInfo("online-1", "device", model="Pixel 8"),
            DeviceInfo("offline-1", "offline", model="Pixel 6"),
            DeviceInfo("locked-1", "unauthorized", model="Phone"),
        ),
        android_status="ready",
        diagnostics=("android.device.ambiguous",),
    )

    service.publish(snapshot)

    assert page.snapshot == snapshot
    assert page.refresh_button.isEnabled()
    assert page.tools_table.item(0, 1).text() == "可用"
    assert page.tools_table.item(1, 2).text() == str(adb)
    assert page.devices_table.item(0, 2).text() == "在线"
    assert page.devices_table.item(1, 2).text() == "离线"
    assert "重新连接" in page.devices_table.item(1, 3).text()
    assert page.devices_table.item(2, 2).text() == "未授权"
    assert "USB 调试授权" in page.devices_table.item(2, 3).text()
    assert page.validation_banner.severity == ValidationSeverity.WARNING
    assert "多个在线设备" in page.validation_banner.messages[0]
