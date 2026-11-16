from __future__ import annotations

from pathlib import Path

from perfeval.gui.services.preset_catalog import PresetCatalogService
from perfeval.gui.services.settings import SettingsService


ROOT = Path(__file__).resolve().parents[2]


def test_builtin_preset_resources_are_declared_for_setuptools_and_pyinstaller(tmp_path: Path) -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    spec = (ROOT / "packaging" / "pyinstaller" / "perfeval.spec").read_text(encoding="utf-8")
    smoke = (ROOT / "packaging" / "pyinstaller" / "smoke.py").read_text(encoding="utf-8")
    assert "gui/resources/presets/*.json" in pyproject
    assert "gui/resources/presets/*.yaml" in pyproject
    assert "gui/resources/presets/*.json" in spec
    assert "gui/resources/presets/*.yaml" in spec
    assert '"presets.manifest"' in smoke
    assert '"presets.scenarios"' in smoke

    values = PresetCatalogService(SettingsService()).discover(tmp_path)
    assert len([value for value in values if value.source.value == "builtin"]) == 5
    assert all(value.scenario_path is None or value.scenario_path.is_file() for value in values)
