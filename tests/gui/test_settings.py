from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services import workspace as workspace_module
from perfeval.gui.services.workspace import WorkspaceService


def _settings(tmp_path: Path) -> SettingsService:
    backend = QSettings(str(tmp_path / "gui-settings.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def test_settings_persist_workspace_theme_and_external_tool(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = tmp_path / "工作区 with spaces"
    workspace.mkdir()

    settings.workspace = str(workspace)
    settings.theme = "dark"
    settings.set_external_tool_path("adb", "/tools/adb")
    settings.log_directory = str(tmp_path / "gui logs")

    restored = _settings(tmp_path)
    assert restored.workspace == str(workspace)
    assert restored.theme == "dark"
    assert restored.external_tool_path("adb") == "/tools/adb"
    assert restored.log_directory == str(tmp_path / "gui logs")


def test_trace_processor_tool_path_round_trips_and_clears(tmp_path: Path) -> None:
    """TASK-025：trace_processor 复用 external_tool_path 持久化。"""
    settings = _settings(tmp_path)
    trace_processor = tmp_path / "trace_processor_shell"

    settings.set_external_tool_path("trace_processor", str(trace_processor))

    restored = _settings(tmp_path)
    assert restored.external_tool_path("trace_processor") == str(trace_processor)

    restored.set_external_tool_path("trace_processor", None)
    assert _settings(tmp_path).external_tool_path("trace_processor") is None


def test_workspace_service_creates_and_restores_workspace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    default = tmp_path / "default"
    default.mkdir()
    service = WorkspaceService(settings, default_workspace=default)
    target = tmp_path / "新 workspace"

    result = service.set_workspace(target)

    assert result.valid
    assert result.created
    assert target.is_dir()
    assert settings.workspace == str(target.resolve())
    assert WorkspaceService(settings, default_workspace=default).current == target.resolve()


def test_workspace_service_rejects_file_and_reset_clears_saved_value(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    default = tmp_path / "default"
    default.mkdir()
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("x", encoding="utf-8")
    service = WorkspaceService(settings, default_workspace=default)

    result = service.set_workspace(file_path)

    assert not result.valid
    assert "文件" in result.message
    assert service.current == default.resolve()
    service.reset_to_default()
    assert settings.workspace is None


def test_clear_settings_restores_safe_theme_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.theme = "not-a-theme"
    assert settings.theme == "system"

    settings.workspace = str(tmp_path)
    settings.log_directory = str(tmp_path / "logs")
    settings.clear()

    assert settings.workspace is None
    assert settings.theme == "system"
    assert settings.log_directory is None


def test_recent_packages_are_bounded_and_can_be_removed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    for index in range(10):
        settings.remember_package(tmp_path, f"com.example.app{index}")

    assert settings.recent_packages(tmp_path) == tuple(
        f"com.example.app{index}" for index in range(9, 1, -1)
    )

    settings.forget_package(tmp_path, "com.example.app6")

    assert "com.example.app6" not in settings.recent_packages(tmp_path)


def test_workspace_service_falls_back_to_home_when_cwd_is_not_writable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    launch_directory = tmp_path / "launch-root"
    launch_directory.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(launch_directory)
    monkeypatch.setenv("HOME", str(home))
    real_access = workspace_module.os.access

    def fake_access(path, mode: int) -> bool:
        if Path(path).resolve() == launch_directory.resolve():
            return False
        return real_access(path, mode)

    monkeypatch.setattr(workspace_module.os, "access", fake_access)

    service = WorkspaceService(settings)

    assert service.current == home.resolve()
    assert service.default == home.resolve()
