from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from perfeval import __version__
from perfeval.cli.protocol import CliEventError, parse_event_line
from perfeval.gui.models.device import DeviceInfo, EnvironmentSnapshot, ToolStatus

from .settings import SettingsService
from .packaging_paths import cli_version_matches
from .tool_paths import command_builder_from_settings, resolve_tool_paths
from .workspace import WorkspaceService


class DeviceService(QObject):
    """Asynchronous, read-only CLI/tool/device discovery service."""

    refresh_started = Signal()
    snapshot_ready = Signal(object)
    refresh_failed = Signal(str)
    refresh_finished = Signal()

    def __init__(
        self,
        settings: SettingsService,
        workspace_service: WorkspaceService,
        parent: QObject | None = None,
        *,
        process_factory: Callable[[QObject], QProcess] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._workspace_service = workspace_service
        self._process_factory = process_factory or (lambda owner: QProcess(owner))
        self._process: QProcess | None = None
        self._stage = ""
        self._stage_token = 0
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._builder = None
        self._cli_available = False
        self._cli_version: str | None = None
        self._event_data: Mapping[str, Any] = {}
        self._devices: tuple[DeviceInfo, ...] = ()
        self._android_status = "unavailable"
        self._diagnostics: list[str] = []
        self._warnings: list[str] = []
        self._adb_program: str | None = None
        self._adb_source: str | None = None
        self._android_studio_program: str | None = None
        self._android_studio_source: str | None = None
        self._configured_adb_valid = False

    @property
    def is_refreshing(self) -> bool:
        return self._process is not None

    def refresh(self) -> bool:
        if self.is_refreshing:
            return False
        self._reset_refresh()
        self.refresh_started.emit()
        try:
            self._builder = command_builder_from_settings(
                self._settings,
                self._workspace_service.current,
            )
        except Exception as exc:
            self._warnings.append(f"无法定位 PerfEval CLI：{exc}")
            self._start_adb_probe()
            return True

        self._start_stage(
            "version",
            self._builder.program,
            [*self._builder.prefix_arguments, "--version"],
            self._builder.environment,
        )
        return True

    def _reset_refresh(self) -> None:
        self._cli_available = False
        self._cli_version = None
        self._event_data = {}
        self._devices = ()
        self._android_status = "unavailable"
        self._diagnostics = []
        resolution = resolve_tool_paths(self._settings)
        self._warnings = list(resolution.warnings)
        self._adb_program = resolution.adb_program
        self._adb_source = resolution.adb_source
        self._android_studio_program = resolution.android_studio_program
        self._android_studio_source = resolution.android_studio_source
        configured = resolution.configured_adb
        self._configured_adb_valid = bool(
            configured
            and resolution.adb_program
            and Path(configured).expanduser().resolve(strict=False)
            == Path(resolution.adb_program).expanduser().resolve(strict=False)
        )

    def _start_stage(
        self,
        stage: str,
        program: str,
        arguments: list[str],
        environment_values: Mapping[str, str] | None = None,
    ) -> None:
        self._stage_token += 1
        token = self._stage_token
        self._stage = stage
        self._stdout = bytearray()
        self._stderr = bytearray()
        process = self._process_factory(self)
        self._process = process
        process.setProgram(program)
        process.setArguments(arguments)
        process.setWorkingDirectory(str(self._workspace_service.current))
        if environment_values:
            environment = QProcessEnvironment.systemEnvironment()
            for key, value in environment_values.items():
                environment.insert(str(key), str(value))
            process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(lambda: self._read_stdout(token))
        process.readyReadStandardError.connect(lambda: self._read_stderr(token))
        process.finished.connect(lambda code, _status: self._complete_stage(token, int(code)))
        process.errorOccurred.connect(lambda error: self._process_error(token, error))
        process.start()

    def _read_stdout(self, token: int) -> None:
        if token == self._stage_token and self._process is not None:
            self._stdout.extend(bytes(self._process.readAllStandardOutput()))

    def _read_stderr(self, token: int) -> None:
        if token == self._stage_token and self._process is not None:
            self._stderr.extend(bytes(self._process.readAllStandardError()))

    def _process_error(self, token: int, error: QProcess.ProcessError) -> None:
        if token != self._stage_token or error != QProcess.ProcessError.FailedToStart:
            return
        QTimer.singleShot(0, lambda: self._complete_stage(token, None))

    def _complete_stage(self, token: int, exit_code: int | None) -> None:
        if token != self._stage_token or self._process is None:
            return
        self._read_stdout(token)
        self._read_stderr(token)
        process = self._process
        self._process = None
        stdout = self._stdout.decode("utf-8", errors="replace")
        stderr = self._stderr.decode("utf-8", errors="replace").strip()
        stage = self._stage
        process.deleteLater()

        if stage == "version":
            self._cli_available = exit_code == 0
            if self._cli_available:
                self._cli_version = stdout.strip() or "版本未知"
                if not cli_version_matches(self._cli_version, expected=__version__):
                    self._cli_available = False
                    self._warnings.append(
                        f"GUI 版本 {__version__} 与 CLI 版本 {self._cli_version} 不一致；请重新安装完整应用包。"
                    )
                    self._start_adb_probe()
                    return
                assert self._builder is not None
                self._start_stage(
                    "devices_event",
                    self._builder.program,
                    [*self._builder.prefix_arguments, "devices", "--gui-events"],
                    self._builder.environment,
                )
            else:
                self._warnings.append(f"PerfEval CLI 无法启动：{stderr or '未返回错误信息'}")
                self._start_adb_probe()
            return

        if stage == "devices_event":
            if exit_code == 0:
                self._consume_devices_event(stdout)
            else:
                self._warnings.append(f"CLI 环境检测失败：{stderr or f'退出码 {exit_code}'}")
            self._start_adb_probe()
            return

        if stage == "adb_devices":
            if exit_code == 0:
                self._devices = parse_adb_devices(stdout)
                self._android_status, direct_diagnostics = classify_android_status(self._devices)
                self._diagnostics.extend(direct_diagnostics)
            else:
                self._warnings.append(f"adb 设备刷新失败：{stderr or f'退出码 {exit_code}'}")
            self._publish_snapshot()

    def _consume_devices_event(self, stdout: str) -> None:
        event_data: Mapping[str, Any] | None = None
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = parse_event_line(line)
            except CliEventError:
                continue
            if event.type == "devices":
                event_data = event.data
                break
        if event_data is None:
            self._warnings.append("CLI 未返回可识别的 devices 机器结果。")
            return

        self._event_data = event_data
        android = event_data.get("android")
        if isinstance(android, Mapping):
            self._android_status = str(android.get("status") or "unavailable")
            diagnostics = android.get("diagnostics")
            if isinstance(diagnostics, list):
                self._diagnostics.extend(str(value) for value in diagnostics)
            event_devices = android.get("devices")
            if isinstance(event_devices, list):
                self._devices = tuple(
                    device_info_from_mapping(value)
                    for value in event_devices
                    if isinstance(value, Mapping) and value.get("serial")
                )

        if not self._settings.external_tool_path("adb"):
            tools = event_data.get("tools")
            if isinstance(tools, Mapping):
                adb = tools.get("adb")
                if isinstance(adb, Mapping) and adb.get("path"):
                    self._adb_program = str(adb["path"])

    def _start_adb_probe(self) -> None:
        if self._adb_program:
            environment = self._builder.environment if self._builder is not None else {}
            self._start_stage("adb_devices", self._adb_program, ["devices", "-l"], environment)
            return
        self._android_status = "adb_missing"
        self._diagnostics.append("android.adb.not_found")
        self._publish_snapshot()

    def _publish_snapshot(self) -> None:
        tools = self._tool_statuses()
        snapshot = EnvironmentSnapshot(
            tools=tools,
            devices=self._devices,
            android_status=self._android_status,
            diagnostics=tuple(dict.fromkeys(self._diagnostics)),
            warnings=tuple(dict.fromkeys(self._warnings)),
            cli_version=self._cli_version,
        )
        self.snapshot_ready.emit(snapshot)
        if snapshot.warnings:
            self.refresh_failed.emit("；".join(snapshot.warnings))
        self.refresh_finished.emit()

    def _tool_statuses(self) -> tuple[ToolStatus, ...]:
        configured_cli = self._settings.external_tool_path("cli")
        cli_path = configured_cli or (self._builder.program if self._builder is not None else None)
        adb_available = bool(self._adb_program)
        studio_available = bool(
            self._android_studio_program
            and Path(self._android_studio_program).expanduser().is_file()
        )
        return (
            ToolStatus(
                "cli",
                "PerfEval CLI",
                self._cli_available,
                cli_path,
                self._cli_version or "无法读取版本",
            ),
            ToolStatus(
                "adb",
                "adb",
                adb_available,
                self._adb_program,
                _tool_source_label(self._adb_source),
            ),
            ToolStatus(
                "android_studio",
                "Android Studio",
                studio_available,
                self._android_studio_program,
                f"{_tool_source_label(self._android_studio_source)}；可选 artifact 查看工具",
            ),
            ToolStatus(
                "browser",
                "系统浏览器",
                True,
                None,
                "由操作系统默认应用打开 HTML 报告",
            ),
        )


def _tool_source_label(source: str | None) -> str:
    return {
        "configured": "自定义路径",
        "environment": "环境变量",
        "path": "系统 PATH",
        "default": "默认安装路径",
    }.get(source, "未发现")


def device_info_from_mapping(value: Mapping[str, Any]) -> DeviceInfo:
    return DeviceInfo(
        serial=str(value.get("serial") or ""),
        state=str(value.get("state") or "unknown"),
        model=str(value["model"]) if value.get("model") is not None else None,
        product=str(value["product"]) if value.get("product") is not None else None,
        device=str(value["device"]) if value.get("device") is not None else None,
    )


def parse_adb_devices(output: str) -> tuple[DeviceInfo, ...]:
    devices: list[DeviceInfo] = []
    for raw_line in output.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        metadata: dict[str, str] = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                metadata[key] = value
        devices.append(
            DeviceInfo(
                serial=serial,
                state=state,
                model=metadata.get("model"),
                product=metadata.get("product"),
                device=metadata.get("device"),
            )
        )
    return tuple(devices)


def classify_android_status(devices: tuple[DeviceInfo, ...]) -> tuple[str, tuple[str, ...]]:
    online = [device for device in devices if device.online]
    if len(online) == 1:
        return "ready", ()
    if len(online) > 1:
        return "ambiguous", ("android.device.ambiguous",)
    if not devices:
        return "no_devices", ("android.device.none_online",)
    states = {device.state for device in devices}
    if "unauthorized" in states:
        return "unauthorized", ("android.device.none_online",)
    if "offline" in states:
        return "offline", ("android.device.none_online",)
    return "unavailable", ("android.device.none_online",)
