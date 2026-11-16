from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from perfeval.core.config import load_scenario_config, validate_scenario_config
from perfeval.gui.models.preset import (
    PresetCategory,
    PresetScenario,
    PresetSource,
    PresetTaskKind,
)

from .settings import SettingsService


class PresetCatalogService:
    def __init__(self, settings: SettingsService) -> None:
        self._settings = settings

    def discover(self, workspace: str | Path) -> tuple[PresetScenario, ...]:
        root = Path(workspace).expanduser().resolve(strict=False)
        return (*self._load_builtins(), *self._load_workspace(root))

    def set_workspace_override(
        self,
        workspace: str | Path,
        relative_path: str,
        *,
        task_kind: PresetTaskKind | None = None,
        category: PresetCategory | None = None,
    ) -> None:
        self._settings.set_scenario_override(
            workspace,
            relative_path,
            task_kind=task_kind.value if task_kind else None,
            category=category.value if category else None,
        )

    def _load_builtins(self) -> tuple[PresetScenario, ...]:
        root = _builtin_root()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schemaVersion") != "perfeval.gui.presets.v1":
            raise ValueError("unsupported built-in preset manifest")
        values: list[PresetScenario] = []
        for raw in manifest.get("presets", []):
            if not isinstance(raw, Mapping):
                continue
            scenario_name = raw.get("scenario")
            scenario_path = root / str(scenario_name) if scenario_name else None
            scenario_metadata: Mapping[str, Any] = {}
            if scenario_path is not None and scenario_path.is_file():
                try:
                    scenario_metadata = load_scenario_config(scenario_path)
                except (OSError, ValueError):
                    scenario_metadata = {}
            values.append(
                PresetScenario(
                    preset_id=str(raw["id"]),
                    title=str(raw["title"]),
                    description=str(raw["description"]),
                    task_kind=PresetTaskKind(str(raw["taskKind"])),
                    category=PresetCategory(str(raw["category"])),
                    source=PresetSource.BUILTIN,
                    scenario_path=scenario_path,
                    default_repeat=int(raw.get("repeat", 3)),
                    default_package=str(raw["packageName"]) if raw.get("packageName") else None,
                    default_mode=str(raw["mode"]) if raw.get("mode") else None,
                    interactive=bool(raw.get("interactive", False)),
                    metadata={**dict(scenario_metadata), **dict(raw)},
                )
            )
        return tuple(values)

    def _load_workspace(self, workspace: Path) -> tuple[PresetScenario, ...]:
        scenario_root = workspace / "scenarios"
        if not scenario_root.is_dir():
            return ()
        paths = sorted({*scenario_root.rglob("*.yaml"), *scenario_root.rglob("*.yml")})
        values = [
            preset
            for path in paths
            if (preset := self._workspace_preset(workspace, path)) is not None
        ]
        return tuple(values)

    def _workspace_preset(self, workspace: Path, path: Path) -> PresetScenario | None:
        relative = path.relative_to(workspace).as_posix()
        preset_id = f"workspace:{relative}"
        try:
            config = load_scenario_config(path)
            issues = tuple(validate_scenario_config(config))
        except (OSError, ValueError) as exc:
            return PresetScenario(
                preset_id=preset_id,
                title=path.stem,
                description="场景文件无法解析。",
                task_kind=PresetTaskKind.RUN,
                category=PresetCategory.PROJECT,
                source=PresetSource.WORKSPACE,
                scenario_path=path,
                valid=False,
                issues=(str(exc),),
                relative_path=relative,
            )

        if not _show_in_quick_start(config):
            return None

        inferred_kind = _infer_task_kind(config)
        inferred_category = _infer_category(config, inferred_kind)
        override = self._settings.scenario_override(workspace, relative)
        kind = _enum_or_default(PresetTaskKind, override.get("task_kind"), inferred_kind)
        category = _enum_or_default(PresetCategory, override.get("category"), inferred_category)
        defaults = config.get("run") if isinstance(config.get("run"), Mapping) else {}
        package_name, activity = _app_defaults(config)
        return PresetScenario(
            preset_id=preset_id,
            title=str(config.get("scenarioId") or path.stem),
            description=str(config.get("description") or "项目场景"),
            task_kind=kind,
            category=category,
            source=PresetSource.WORKSPACE,
            scenario_path=path,
            default_repeat=int(defaults.get("repeats") or 3),
            default_package=package_name,
            default_activity=activity,
            interactive=_has_manual_step(config),
            valid=not issues,
            issues=issues,
            relative_path=relative,
            metadata=config,
        )


def _builtin_root() -> Path:
    local = Path(__file__).resolve().parents[1] / "resources" / "presets"
    if (local / "manifest.json").is_file():
        return local
    from .packaging_paths import runtime_paths

    resource_root = runtime_paths().resource_root
    for candidate in (
        resource_root / "gui" / "resources" / "presets",
        resource_root / "perfeval" / "gui" / "resources" / "presets",
    ):
        if (candidate / "manifest.json").is_file():
            return candidate
    raise FileNotFoundError("built-in preset manifest is missing")


def _infer_task_kind(config: Mapping[str, Any]) -> PresetTaskKind:
    apps = config.get("apps")
    android_apps = [value for value in apps if isinstance(value, Mapping) and value.get("platform") == "android"] if isinstance(apps, list) else []
    return PresetTaskKind.AB if len(android_apps) >= 2 else PresetTaskKind.RUN


def _infer_category(config: Mapping[str, Any], kind: PresetTaskKind) -> PresetCategory:
    if kind is PresetTaskKind.AB:
        return PresetCategory.AB
    budgets = config.get("budgets") if isinstance(config.get("budgets"), Mapping) else {}
    metric_ids = {str(value) for value in budgets}
    if any(value.startswith("startup.") for value in metric_ids):
        return PresetCategory.STARTUP
    if any(value.startswith("frame.") for value in metric_ids):
        return PresetCategory.SMOOTHNESS
    if any(value.startswith(("memory.", "stability.")) for value in metric_ids):
        return PresetCategory.MEMORY
    return PresetCategory.PROJECT


def _app_defaults(config: Mapping[str, Any]) -> tuple[str | None, str | None]:
    apps = config.get("apps")
    package_name = None
    if isinstance(apps, list):
        app = next((value for value in apps if isinstance(value, Mapping) and value.get("platform") == "android"), None)
        if isinstance(app, Mapping) and isinstance(app.get("packageName"), str):
            package_name = str(app["packageName"])
    activity = None
    flow = config.get("flow")
    steps = flow.get("steps") if isinstance(flow, Mapping) else None
    if isinstance(steps, list):
        launch = next((value for value in steps if isinstance(value, Mapping) and value.get("type") == "launch"), None)
        if isinstance(launch, Mapping) and isinstance(launch.get("activity"), str):
            activity = str(launch["activity"])
    return package_name, activity


def _has_manual_step(config: Mapping[str, Any]) -> bool:
    flow = config.get("flow")
    steps = flow.get("steps") if isinstance(flow, Mapping) else None
    return any(isinstance(value, Mapping) and value.get("type") == "manual" for value in steps) if isinstance(steps, list) else False


def _show_in_quick_start(config: Mapping[str, Any]) -> bool:
    gui = config.get("gui")
    return not isinstance(gui, Mapping) or gui.get("quickStart") is not False


def _enum_or_default(enum_type, value: str | None, default):
    try:
        return enum_type(value) if value else default
    except ValueError:
        return default
