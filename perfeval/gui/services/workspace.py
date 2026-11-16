from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .settings import SettingsService


@dataclass(frozen=True)
class WorkspaceValidation:
    valid: bool
    path: Path | None
    message: str
    created: bool = False


class WorkspaceService(QObject):
    """Validate, create, persist, and publish the active workspace."""

    workspace_changed = Signal(str)

    def __init__(
        self,
        settings: SettingsService,
        *,
        default_workspace: Path | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._default_workspace = self._resolve_default_workspace(default_workspace)
        self._workspace = self._load_workspace()

    @property
    def current(self) -> Path:
        return self._workspace

    @property
    def default(self) -> Path:
        return self._default_workspace

    def validate(self, raw_path: str | os.PathLike[str], *, create: bool = False) -> WorkspaceValidation:
        text = os.fspath(raw_path).strip()
        if not text:
            return WorkspaceValidation(False, None, "请选择 workspace 目录。")

        try:
            path = Path(text).expanduser().resolve(strict=False)
            created = False
            if path.exists() and not path.is_dir():
                return WorkspaceValidation(False, path, "所选路径是文件，请选择目录。")
            if not path.exists():
                if not create:
                    return WorkspaceValidation(False, path, "目录不存在；可选择创建后再使用。")
                path.mkdir(parents=True, exist_ok=False)
                created = True
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                return WorkspaceValidation(False, path, "目录不可读写，请检查权限后重试。")
        except (OSError, ValueError) as exc:
            return WorkspaceValidation(False, None, f"无法使用该目录：{exc}")

        message = "workspace 已创建并可用。" if created else "workspace 可用。"
        return WorkspaceValidation(True, path, message, created)

    def set_workspace(
        self,
        raw_path: str | os.PathLike[str],
        *,
        create: bool = True,
    ) -> WorkspaceValidation:
        result = self.validate(raw_path, create=create)
        if result.valid and result.path is not None:
            self._workspace = result.path
            self._settings.workspace = str(result.path)
            self.workspace_changed.emit(str(result.path))
        return result

    def reset_to_default(self, *, persist: bool = False) -> Path:
        self._workspace = self._default_workspace
        if persist:
            self._settings.workspace = str(self._workspace)
        else:
            self._settings.clear_workspace()
        self.workspace_changed.emit(str(self._workspace))
        return self._workspace

    def _load_workspace(self) -> Path:
        saved = self._settings.workspace
        if saved:
            result = self.validate(saved)
            if result.valid and result.path is not None:
                return result.path
            self._settings.clear_workspace()
        return self._default_workspace

    @staticmethod
    def _resolve_default_workspace(default_workspace: Path | None) -> Path:
        if default_workspace is not None:
            return default_workspace.expanduser().resolve()

        try:
            current = Path.cwd().expanduser().resolve()
        except OSError:
            current = None
        if (
            current is not None
            and current.is_dir()
            and os.access(current, os.R_OK | os.W_OK | os.X_OK)
        ):
            return current

        # Finder/LaunchServices normally starts GUI applications with cwd=/.
        # The sealed macOS root is not writable, so use the existing user home
        # as the safe first-run workspace instead of failing during logging.
        return Path.home().expanduser().resolve()
