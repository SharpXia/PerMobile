from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_ACTIVITY_COMPONENT_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.]*/(?:\.?[A-Za-z0-9_$][A-Za-z0-9_.$]*)$"
)


@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    state: str
    product: str | None = None
    model: str | None = None
    device: str | None = None


def parse_adb_devices(output: str) -> list[AndroidDevice]:
    devices: list[AndroidDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        extras = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
        devices.append(
            AndroidDevice(
                serial=serial,
                state=state,
                product=extras.get("product"),
                model=extras.get("model"),
                device=extras.get("device"),
            )
        )
    return devices


# Homebrew 前缀为绝对路径，提为模块常量便于测试替换（不读环境变量）
_HOMEBREW_ADB_PATHS: tuple[Path, ...] = (
    Path("/opt/homebrew/bin/adb"),  # Homebrew ARM
    Path("/usr/local/bin/adb"),  # Homebrew Intel
)


def _well_known_adb() -> str | None:
    """PATH 找不到 adb 时的已知位置兜底（GUI 直启环境 PATH 受限）。

    背景：macOS Dock/Finder/launchd 启动的 app 只有 /usr/bin:/bin:/usr/sbin:/sbin，
    shutil.which("adb") 必然落空（adb 实际在 ~/Library/Android/sdk/platform-tools
    或 homebrew 前缀），导致后端 auto 探测误落 synthetic。显式 adb_path 参数恒胜出。
    """
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates: list[Path] = []
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(env_var)
        if sdk:
            candidates.append(Path(sdk) / "platform-tools" / exe)
    home = Path.home()
    candidates.extend(
        [
            home / "Library" / "Android" / "sdk" / "platform-tools" / exe,  # macOS Studio
            home / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / exe,  # Windows
            home / "Android" / "Sdk" / "platform-tools" / exe,  # Linux
            *_HOMEBREW_ADB_PATHS,
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


class AdbClient:
    def __init__(self, adb_path: str | None = None) -> None:
        self.adb_path = adb_path or shutil.which("adb") or _well_known_adb() or "adb"

    def available(self) -> bool:
        return bool(shutil.which(self.adb_path) or Path(self.adb_path).exists())

    def command(self, *args: str, serial: str | None = None) -> list[str]:
        command = [self.adb_path]
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        return command

    def run(
        self,
        *args: str,
        serial: str | None = None,
        timeout: float | None = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*args, serial=serial),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

    def list_devices(self) -> list[AndroidDevice]:
        result = self.run("devices", "-l")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "adb devices failed")
        return parse_adb_devices(result.stdout)


def install_command(adb: AdbClient, apk: str, *, serial: str | None = None) -> list[str]:
    return adb.command("install", "-r", "-d", apk, serial=serial)


def force_stop_command(adb: AdbClient, package_name: str, *, serial: str | None = None) -> list[str]:
    return adb.command("shell", "am", "force-stop", package_name, serial=serial)


def clear_data_command(adb: AdbClient, package_name: str, *, serial: str | None = None) -> list[str]:
    return adb.command("shell", "pm", "clear", package_name, serial=serial)


def query_launcher_activities_command(
    adb: AdbClient,
    package_name: str,
    *,
    serial: str | None = None,
) -> list[str]:
    return adb.command(
        "shell",
        "cmd",
        "package",
        "query-activities",
        "--brief",
        "-a",
        "android.intent.action.MAIN",
        "-c",
        "android.intent.category.LAUNCHER",
        package_name,
        serial=serial,
    )


def parse_launcher_activity_component(
    output: str,
    *,
    package_name: str | None = None,
) -> str | None:
    """Return the resolved ``package/activity`` component from adb output."""
    for raw_line in output.splitlines():
        component = raw_line.strip()
        if not _ACTIVITY_COMPONENT_RE.fullmatch(component):
            continue
        if package_name is None or component.partition("/")[0] == package_name:
            return component
    return None


def launch_command(
    adb: AdbClient,
    component: str,
    *,
    serial: str | None = None,
    extras: Sequence[str] = (),
    wait: bool = True,
) -> list[str]:
    flags = ["-W"] if wait else []
    return adb.command("shell", "am", "start", *flags, "-n", component, *extras, serial=serial)
