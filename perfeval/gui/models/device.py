from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolStatus:
    key: str
    label: str
    available: bool
    path: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    device: str | None = None

    @property
    def online(self) -> bool:
        return self.state == "device"


@dataclass(frozen=True)
class EnvironmentSnapshot:
    tools: tuple[ToolStatus, ...]
    devices: tuple[DeviceInfo, ...] = ()
    android_status: str = "unavailable"
    diagnostics: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    cli_version: str | None = None

    def tool(self, key: str) -> ToolStatus | None:
        return next((tool for tool in self.tools if tool.key == key), None)
