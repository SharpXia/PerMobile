from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Protocol

from PySide6.QtCore import QObject, Signal

from ..models.llm import InterpretationDepth, LlmProfile
from .settings import SettingsService


_PROFILE_SCHEMA = "perfeval.llm.profiles.v1"
_KEYRING_SERVICE = "PerfEval Lite LLM"


class KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class CredentialStore:
    """Store LLM keys in the OS credential store, with a session-only fallback."""

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._memory: dict[str, str] = {}
        self._backend = backend if backend is not None else _load_keyring_backend()
        self._persistent_available = self._backend is not None
        self._last_error = "" if self._persistent_available else "系统凭据库不可用，密钥仅在本次会话中保留。"

    @property
    def persistent_available(self) -> bool:
        return self._persistent_available

    @property
    def status_message(self) -> str:
        return self._last_error

    def get(self, profile_id: str) -> str | None:
        if profile_id in self._memory:
            return self._memory[profile_id]
        backend = self._backend
        if backend is None:
            return None
        try:
            value = backend.get_password(_KEYRING_SERVICE, profile_id)
        except Exception:
            self._degrade()
            return self._memory.get(profile_id)
        return str(value) if value else None

    def set(self, profile_id: str, api_key: str) -> bool:
        value = str(api_key).strip()
        if not value:
            self.delete(profile_id)
            return self._persistent_available
        self._memory[profile_id] = value
        backend = self._backend
        if backend is None:
            return False
        try:
            backend.set_password(_KEYRING_SERVICE, profile_id, value)
        except Exception:
            self._degrade()
            return False
        return True

    def delete(self, profile_id: str) -> None:
        self._memory.pop(profile_id, None)
        backend = self._backend
        if backend is None:
            return
        try:
            backend.delete_password(_KEYRING_SERVICE, profile_id)
        except Exception as exc:
            if exc.__class__.__name__ not in {"PasswordDeleteError", "InitError"}:
                self._degrade()

    def _degrade(self) -> None:
        self._backend = None
        self._persistent_available = False
        self._last_error = "系统凭据库不可用，密钥仅在本次会话中保留。"


class LlmProfileService(QObject):
    changed = Signal()

    def __init__(
        self,
        settings: SettingsService,
        parent: QObject | None = None,
        *,
        credentials: CredentialStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.credentials = credentials or CredentialStore()

    @property
    def profiles(self) -> tuple[LlmProfile, ...]:
        raw = self.settings.llm_profiles_payload
        if not raw:
            return ()
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(payload, dict) or payload.get("schemaVersion") != _PROFILE_SCHEMA:
            return ()
        values: list[LlmProfile] = []
        for item in payload.get("profiles", []):
            if not isinstance(item, dict):
                continue
            try:
                values.append(LlmProfile.from_mapping(item))
            except (TypeError, ValueError):
                continue
        return tuple(values)

    @property
    def default_profile_id(self) -> str | None:
        configured = self.settings.llm_default_profile_id
        ids = {profile.profile_id for profile in self.profiles}
        if configured in ids:
            return configured
        return self.profiles[0].profile_id if self.profiles else None

    @property
    def default_profile(self) -> LlmProfile | None:
        profile_id = self.default_profile_id
        return self.profile(profile_id) if profile_id else None

    @property
    def usable_profiles(self) -> tuple[LlmProfile, ...]:
        return tuple(profile for profile in self.profiles if self.api_key(profile.profile_id))

    @property
    def interpretation_depth(self) -> InterpretationDepth:
        try:
            return InterpretationDepth(self.settings.llm_interpretation_depth or "")
        except ValueError:
            return InterpretationDepth.STANDARD

    @interpretation_depth.setter
    def interpretation_depth(self, value: InterpretationDepth | str) -> None:
        self.settings.llm_interpretation_depth = InterpretationDepth(value).value

    def profile(self, profile_id: str | None) -> LlmProfile | None:
        if not profile_id:
            return None
        return next((profile for profile in self.profiles if profile.profile_id == profile_id), None)

    def api_key(self, profile_id: str) -> str | None:
        return self.credentials.get(profile_id)

    def has_api_key(self, profile_id: str) -> bool:
        return bool(self.api_key(profile_id))

    def save_profile(
        self,
        profile: LlmProfile,
        *,
        api_key: str | None = None,
        make_default: bool = False,
    ) -> bool:
        values = [value for value in self.profiles if value.profile_id != profile.profile_id]
        values.append(profile)
        values.sort(key=lambda value: value.name.casefold())
        self._write_profiles(values)
        persisted = self.credentials.persistent_available
        if api_key is not None and api_key.strip():
            persisted = self.credentials.set(profile.profile_id, api_key)
        if make_default or self.default_profile_id is None:
            self.settings.llm_default_profile_id = profile.profile_id
        self.changed.emit()
        return persisted

    def delete_profile(self, profile_id: str) -> None:
        values = [profile for profile in self.profiles if profile.profile_id != profile_id]
        self.credentials.delete(profile_id)
        self._write_profiles(values)
        if self.settings.llm_default_profile_id == profile_id:
            self.settings.llm_default_profile_id = values[0].profile_id if values else None
        self.changed.emit()

    def set_default(self, profile_id: str) -> None:
        if self.profile(profile_id) is None:
            raise KeyError(f"unknown LLM profile: {profile_id}")
        self.settings.llm_default_profile_id = profile_id
        self.changed.emit()

    def clear(self) -> None:
        for profile in self.profiles:
            self.credentials.delete(profile.profile_id)
        self.settings.llm_profiles_payload = None
        self.settings.llm_default_profile_id = None
        self.changed.emit()

    def _write_profiles(self, profiles: Iterable[LlmProfile]) -> None:
        payload = {
            "schemaVersion": _PROFILE_SCHEMA,
            "profiles": [profile.to_mapping() for profile in profiles],
        }
        self.settings.llm_profiles_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _load_keyring_backend() -> KeyringBackend | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
        priority = getattr(backend, "priority", 0)
        if not isinstance(priority, (int, float)) or priority <= 0:
            return None
    except Exception:
        return None
    return keyring


__all__ = ["CredentialStore", "LlmProfileService"]
