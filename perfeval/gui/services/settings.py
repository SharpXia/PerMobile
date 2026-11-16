from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings


@dataclass(frozen=True)
class SettingsKeys:
    WORKSPACE: str = "workspace/current"
    THEME: str = "appearance/theme"
    WINDOW_GEOMETRY: str = "window/geometry"
    WINDOW_STATE: str = "window/state"
    LOG_DIRECTORY: str = "logging/directory"
    EXTERNAL_TOOL_PREFIX: str = "external_tools"
    QUICK_PREFIX: str = "quick_start"
    LLM_PROFILES: str = "llm/profiles_v1"
    LLM_DEFAULT_PROFILE: str = "llm/default_profile"
    LLM_INTERPRETATION_DEPTH: str = "llm/interpretation_depth"


class SettingsService:
    """Typed access to the non-sensitive preferences stored by the GUI."""

    ORGANIZATION = "PerfEval"
    APPLICATION = "PerfEval Lite"
    KEYS = SettingsKeys()
    THEMES = frozenset({"system", "light", "dark"})

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or QSettings(self.ORGANIZATION, self.APPLICATION)

    @property
    def workspace(self) -> str | None:
        value = self._settings.value(self.KEYS.WORKSPACE)
        return str(value) if value else None

    @workspace.setter
    def workspace(self, path: str) -> None:
        self._settings.setValue(self.KEYS.WORKSPACE, path)
        self._settings.sync()

    def clear_workspace(self) -> None:
        self._settings.remove(self.KEYS.WORKSPACE)
        self._settings.sync()

    @property
    def theme(self) -> str:
        value = str(self._settings.value(self.KEYS.THEME, "system"))
        return value if value in self.THEMES else "system"

    @theme.setter
    def theme(self, value: str) -> None:
        normalized = value if value in self.THEMES else "system"
        self._settings.setValue(self.KEYS.THEME, normalized)
        self._settings.sync()

    @property
    def window_geometry(self) -> QByteArray | None:
        value = self._settings.value(self.KEYS.WINDOW_GEOMETRY)
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._settings.setValue(self.KEYS.WINDOW_GEOMETRY, value)

    @property
    def window_state(self) -> QByteArray | None:
        value = self._settings.value(self.KEYS.WINDOW_STATE)
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    @window_state.setter
    def window_state(self, value: QByteArray) -> None:
        self._settings.setValue(self.KEYS.WINDOW_STATE, value)
        self._settings.sync()

    def external_tool_path(self, name: str) -> str | None:
        value = self._settings.value(self._external_tool_key(name))
        return str(value) if value else None

    def set_external_tool_path(self, name: str, path: str | None) -> None:
        key = self._external_tool_key(name)
        if path:
            self._settings.setValue(key, path)
        else:
            self._settings.remove(key)
        self._settings.sync()

    @property
    def log_directory(self) -> str | None:
        value = self._settings.value(self.KEYS.LOG_DIRECTORY)
        return str(value) if value else None

    @log_directory.setter
    def log_directory(self, path: str | None) -> None:
        if path:
            self._settings.setValue(self.KEYS.LOG_DIRECTORY, path)
        else:
            self._settings.remove(self.KEYS.LOG_DIRECTORY)
        self._settings.sync()

    def clear(self) -> None:
        self._settings.clear()
        self._settings.sync()

    @property
    def llm_profiles_payload(self) -> str | None:
        return self._text_value(self.KEYS.LLM_PROFILES)

    @llm_profiles_payload.setter
    def llm_profiles_payload(self, value: str | None) -> None:
        self._set_optional(self.KEYS.LLM_PROFILES, value)

    @property
    def llm_default_profile_id(self) -> str | None:
        return self._text_value(self.KEYS.LLM_DEFAULT_PROFILE)

    @llm_default_profile_id.setter
    def llm_default_profile_id(self, value: str | None) -> None:
        self._set_optional(self.KEYS.LLM_DEFAULT_PROFILE, value)

    @property
    def llm_interpretation_depth(self) -> str | None:
        return self._text_value(self.KEYS.LLM_INTERPRETATION_DEPTH)

    @llm_interpretation_depth.setter
    def llm_interpretation_depth(self, value: str | None) -> None:
        self._set_optional(self.KEYS.LLM_INTERPRETATION_DEPTH, value)

    def recent_preset(self, workspace: str | Path) -> str | None:
        return self._text_value(self._quick_key(workspace, "preset"))

    def set_recent_preset(self, workspace: str | Path, preset_id: str | None) -> None:
        self._set_optional(self._quick_key(workspace, "preset"), preset_id)

    def recent_device(self, workspace: str | Path) -> str | None:
        return self._text_value(self._quick_key(workspace, "device"))

    def set_recent_device(self, workspace: str | Path, serial: str | None) -> None:
        self._set_optional(self._quick_key(workspace, "device"), serial)

    def recent_packages(self, workspace: str | Path) -> tuple[str, ...]:
        return self._recent_values(self._quick_key(workspace, "packages"))

    def remember_package(self, workspace: str | Path, package_name: str | None) -> None:
        self._remember_value(self._quick_key(workspace, "packages"), package_name)

    def forget_package(self, workspace: str | Path, package_name: str | None) -> None:
        self._forget_value(self._quick_key(workspace, "packages"), package_name)

    def recent_apks(self, workspace: str | Path) -> tuple[str, ...]:
        return self._recent_values(self._quick_key(workspace, "apks"))

    def remember_apk(self, workspace: str | Path, apk: str | None) -> None:
        self._remember_value(self._quick_key(workspace, "apks"), apk)

    def scenario_override(self, workspace: str | Path, relative_path: str) -> dict[str, str]:
        raw = self._text_value(self._quick_key(workspace, f"scenario_overrides/{_key_hash(relative_path)}"))
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        } if isinstance(payload, dict) else {}

    def set_scenario_override(
        self,
        workspace: str | Path,
        relative_path: str,
        *,
        task_kind: str | None = None,
        category: str | None = None,
    ) -> None:
        key = self._quick_key(workspace, f"scenario_overrides/{_key_hash(relative_path)}")
        payload = {name: value for name, value in {"task_kind": task_kind, "category": category}.items() if value}
        self._set_optional(key, json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None)

    def _recent_values(self, key: str) -> tuple[str, ...]:
        raw = self._text_value(key)
        if not raw:
            return ()
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        return tuple(str(value) for value in values if isinstance(value, str) and value)[:8] if isinstance(values, list) else ()

    def _remember_value(self, key: str, value: str | None) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            return
        values = [normalized, *(item for item in self._recent_values(key) if item != normalized)]
        self._settings.setValue(key, json.dumps(values[:8], ensure_ascii=False))
        self._settings.sync()

    def _forget_value(self, key: str, value: str | None) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            return
        values = [item for item in self._recent_values(key) if item != normalized]
        if values:
            self._settings.setValue(key, json.dumps(values, ensure_ascii=False))
        else:
            self._settings.remove(key)
        self._settings.sync()

    def _text_value(self, key: str) -> str | None:
        value = self._settings.value(key)
        return str(value) if value else None

    def _set_optional(self, key: str, value: str | None) -> None:
        if value:
            self._settings.setValue(key, value)
        else:
            self._settings.remove(key)
        self._settings.sync()

    @classmethod
    def _quick_key(cls, workspace: str | Path, suffix: str) -> str:
        root = str(Path(workspace).expanduser().resolve(strict=False))
        return f"{cls.KEYS.QUICK_PREFIX}/{_key_hash(root)}/{suffix}"

    @classmethod
    def _external_tool_key(cls, name: str) -> str:
        normalized = name.strip().lower().replace(" ", "_")
        if not normalized or "/" in normalized or "\\" in normalized:
            raise ValueError("external tool name must be a non-empty key")
        return f"{cls.KEYS.EXTERNAL_TOOL_PREFIX}/{normalized}"


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
