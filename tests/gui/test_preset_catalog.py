from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from perfeval.gui.models.preset import PresetCategory, PresetSource, PresetTaskKind
from perfeval.gui.services.preset_catalog import PresetCatalogService
from perfeval.gui.services.settings import SettingsService


def _settings(tmp_path: Path) -> SettingsService:
    backend = QSettings(str(tmp_path / "presets.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def test_catalog_merges_five_builtins_and_classifies_workspace_scenarios(tmp_path: Path) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "startup.yaml").write_text(
        """schemaVersion: perfeval.scenario.v1
scenarioId: project_startup
description: Project startup
apps:
  - id: target
    platform: android
    packageName: com.example.app
run:
  repeats: 5
  mode: same_device_versions
budgets:
  startup.cold.ms:
    direction: lower_better
""",
        encoding="utf-8",
    )
    (scenarios / "broken.yaml").write_text("schemaVersion: [", encoding="utf-8")

    catalog = PresetCatalogService(_settings(tmp_path))
    values = catalog.discover(tmp_path)

    builtins = [value for value in values if value.source is PresetSource.BUILTIN]
    workspace = [value for value in values if value.source is PresetSource.WORKSPACE]
    assert [value.title for value in builtins] == ["启动速度", "页面流畅度", "内存稳定性", "多版本 A/B", "深度诊断"]
    smoothness = next(value for value in builtins if value.preset_id == "builtin.smoothness")
    assert smoothness.metadata["scenarioId"] == "quick_smoothness"
    startup = next(value for value in workspace if value.title == "project_startup")
    assert startup.category is PresetCategory.STARTUP
    assert startup.task_kind is PresetTaskKind.RUN
    assert startup.default_package == "com.example.app"
    assert startup.default_repeat == 5
    broken = next(value for value in workspace if value.scenario_path.name == "broken.yaml")
    assert not broken.valid
    assert broken.issues


def test_workspace_override_is_persisted_without_modifying_yaml(tmp_path: Path) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    path = scenarios / "custom.yaml"
    source = """schemaVersion: perfeval.scenario.v1
scenarioId: custom
run:
  repeats: 3
  mode: same_device_versions
"""
    path.write_text(source, encoding="utf-8")
    catalog = PresetCatalogService(_settings(tmp_path))

    catalog.set_workspace_override(
        tmp_path,
        "scenarios/custom.yaml",
        task_kind=PresetTaskKind.AB,
        category=PresetCategory.AB,
    )
    value = next(item for item in catalog.discover(tmp_path) if item.relative_path == "scenarios/custom.yaml")

    assert value.task_kind is PresetTaskKind.AB
    assert value.category is PresetCategory.AB
    assert path.read_text(encoding="utf-8") == source


def test_catalog_excludes_workspace_scenarios_disabled_for_quick_start(tmp_path: Path) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "visible.yaml").write_text(
        """schemaVersion: perfeval.scenario.v1
scenarioId: visible_project_scenario
""",
        encoding="utf-8",
    )
    (scenarios / "fixture.yaml").write_text(
        """schemaVersion: perfeval.scenario.v1
scenarioId: hidden_fixture_scenario
gui:
  quickStart: false
""",
        encoding="utf-8",
    )

    values = PresetCatalogService(_settings(tmp_path)).discover(tmp_path)
    workspace_titles = {
        value.title for value in values if value.source is PresetSource.WORKSPACE
    }

    assert workspace_titles == {"visible_project_scenario"}
