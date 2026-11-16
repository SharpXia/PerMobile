from __future__ import annotations

from dataclasses import dataclass, field

from .adb import AdbClient, AndroidDevice


@dataclass(frozen=True)
class AndroidPreflightResult:
    ok: bool
    adb_available: bool
    selected_serial: str | None = None
    devices: tuple[AndroidDevice, ...] = ()
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


def select_device(devices: list[AndroidDevice], serial: str | None = None) -> tuple[AndroidDevice | None, list[str]]:
    online = [device for device in devices if device.state == "device"]
    diagnostics: list[str] = []
    if serial:
        matches = [device for device in devices if device.serial == serial]
        if not matches:
            return None, [f"android.device.not_found:{serial}"]
        selected = matches[0]
        if selected.state != "device":
            return None, [f"android.device.not_online:{serial}:{selected.state}"]
        return selected, diagnostics
    if not online:
        return None, ["android.device.none_online"]
    if len(online) > 1:
        return None, ["android.device.ambiguous"]
    return online[0], diagnostics


def preflight_from_devices(
    devices: list[AndroidDevice],
    *,
    adb_available: bool = True,
    serial: str | None = None,
) -> AndroidPreflightResult:
    selected, diagnostics = select_device(devices, serial=serial)
    if not adb_available:
        diagnostics.insert(0, "android.adb.not_found")
    return AndroidPreflightResult(
        ok=adb_available and selected is not None and not diagnostics,
        adb_available=adb_available,
        selected_serial=selected.serial if selected else None,
        devices=tuple(devices),
        diagnostics=tuple(diagnostics),
    )


def run_preflight(adb: AdbClient | None = None, *, serial: str | None = None) -> AndroidPreflightResult:
    client = adb or AdbClient()
    if not client.available():
        return AndroidPreflightResult(ok=False, adb_available=False, diagnostics=("android.adb.not_found",))
    try:
        devices = client.list_devices()
    except Exception as exc:
        return AndroidPreflightResult(
            ok=False,
            adb_available=True,
            diagnostics=(f"android.adb.devices_failed:{exc}",),
        )
    return preflight_from_devices(devices, adb_available=True, serial=serial)

