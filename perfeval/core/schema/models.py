from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .errors import ValidationError

MANIFEST_SCHEMA_VERSION = "perfeval.manifest.v1"
TIMELINE_SCHEMA_VERSION = "perfeval.timeline.v1"
ALLOWED_ENVIRONMENT_STATES = {"nominal", "fair", "serious", "critical", "unknown"}
ALLOWED_RUN_STATUSES = {"completed", "partial", "invalid"}


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string", field=field)
    return value


def _optional_str(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_str(value, field)


def _require_int(value: Any, field: str, *, min_value: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer", field=field)
    if min_value is not None and value < min_value:
        raise ValidationError(f"{field} must be >= {min_value}", field=field)
    return value


def _optional_int(value: Any, field: str, *, min_value: Optional[int] = None) -> Optional[int]:
    if value is None:
        return None
    return _require_int(value, field, min_value=min_value)


def _optional_float(value: Any, field: str, *, min_value: Optional[float] = None) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number", field=field)
    result = float(value)
    if min_value is not None and result < min_value:
        raise ValidationError(f"{field} must be >= {min_value}", field=field)
    return result


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be a mapping", field=field)
    return value


def _validate_iso8601_utc(value: str, field: str) -> str:
    if not value.endswith("Z"):
        raise ValidationError(f"{field} must be UTC ISO-8601 with trailing Z", field=field)
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"{field} is not valid ISO-8601 UTC", field=field) from exc
    return value


def _normalize_datetime_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000).isoformat().replace("+00:00", "Z")


def _normalize_version_code(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError("versionCode must be a string or integer", field="versionCode")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise ValidationError("versionCode must be a string or integer", field="versionCode")


@dataclass(frozen=True)
class ManifestScenario:
    scenario_id: str
    description: str
    flow_hash: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManifestScenario":
        payload = _require_mapping(data, "scenario")
        return cls(
            scenario_id=_require_str(payload.get("scenarioId"), "scenarioId"),
            description=_require_str(payload.get("description"), "description"),
            flow_hash=_optional_str(payload.get("flowHash"), "flowHash"),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {"scenarioId": self.scenario_id, "description": self.description}
        if self.flow_hash is not None:
            result["flowHash"] = self.flow_hash
        return result


@dataclass(frozen=True)
class ManifestApp:
    platform: str
    app_id: str
    package_name: Optional[str] = None
    bundle_id: Optional[str] = None
    version_name: Optional[str] = None
    version_code: Optional[str] = None
    build_id: Optional[str] = None
    artifact_path: Optional[str] = None
    artifact_sha256: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManifestApp":
        payload = _require_mapping(data, "app")
        return cls(
            platform=_require_str(payload.get("platform"), "platform"),
            app_id=_require_str(payload.get("appId"), "appId"),
            package_name=_optional_str(payload.get("packageName"), "packageName"),
            bundle_id=_optional_str(payload.get("bundleId"), "bundleId"),
            version_name=_optional_str(payload.get("versionName"), "versionName"),
            version_code=_normalize_version_code(payload.get("versionCode")),
            build_id=_optional_str(payload.get("buildId"), "buildId"),
            artifact_path=_optional_str(payload.get("artifactPath"), "artifactPath"),
            artifact_sha256=_optional_str(payload.get("artifactSha256"), "artifactSha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"platform": self.platform, "appId": self.app_id}
        for key, value in (
            ("packageName", self.package_name),
            ("bundleId", self.bundle_id),
            ("versionName", self.version_name),
            ("versionCode", self.version_code),
            ("buildId", self.build_id),
            ("artifactPath", self.artifact_path),
            ("artifactSha256", self.artifact_sha256),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class ManifestDevice:
    platform: str
    device_id: str
    serial_or_udid: str
    model: Optional[str] = None
    os_version: Optional[str] = None
    api_level: Optional[int] = None
    refresh_rate_hz: Optional[float] = None
    cpu_abi: Optional[str] = None
    memory_total_mb: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManifestDevice":
        payload = _require_mapping(data, "device")
        return cls(
            platform=_require_str(payload.get("platform"), "platform"),
            device_id=_require_str(payload.get("deviceId"), "deviceId"),
            serial_or_udid=_require_str(payload.get("serialOrUdid"), "serialOrUdid"),
            model=_optional_str(payload.get("model"), "model"),
            os_version=_optional_str(payload.get("osVersion"), "osVersion"),
            api_level=_optional_int(payload.get("apiLevel"), "apiLevel", min_value=0),
            refresh_rate_hz=_optional_float(payload.get("refreshRateHz"), "refreshRateHz", min_value=0.0),
            cpu_abi=_optional_str(payload.get("cpuAbi"), "cpuAbi"),
            memory_total_mb=_optional_int(payload.get("memoryTotalMb"), "memoryTotalMb", min_value=0),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "platform": self.platform,
            "deviceId": self.device_id,
            "serialOrUdid": self.serial_or_udid,
        }
        for key, value in (
            ("model", self.model),
            ("osVersion", self.os_version),
            ("apiLevel", self.api_level),
            ("refreshRateHz", self.refresh_rate_hz),
            ("cpuAbi", self.cpu_abi),
            ("memoryTotalMb", self.memory_total_mb),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class ManifestEnvironment:
    host_os: str
    host_arch: str
    tool_version: str
    adb_version: Optional[str] = None
    xcode_version: Optional[str] = None
    network_profile: Optional[str] = None
    battery_level: Optional[int] = None
    charging: Optional[bool] = None
    thermal_state: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManifestEnvironment":
        payload = _require_mapping(data, "environment")
        thermal_state = payload.get("thermalState")
        if thermal_state is not None:
            thermal_state = _require_str(thermal_state, "thermalState")
            if thermal_state not in ALLOWED_ENVIRONMENT_STATES:
                raise ValidationError(
                    f"thermalState must be one of {sorted(ALLOWED_ENVIRONMENT_STATES)}",
                    field="thermalState",
                )
        charging = payload.get("charging")
        if charging is not None and not isinstance(charging, bool):
            raise ValidationError("charging must be a boolean", field="charging")
        battery_level = _optional_int(payload.get("batteryLevel"), "batteryLevel", min_value=0)
        if battery_level is not None and battery_level > 100:
            raise ValidationError("batteryLevel must be <= 100", field="batteryLevel")
        return cls(
            host_os=_require_str(payload.get("hostOs"), "hostOs"),
            host_arch=_require_str(payload.get("hostArch"), "hostArch"),
            tool_version=_require_str(payload.get("toolVersion"), "toolVersion"),
            adb_version=_optional_str(payload.get("adbVersion"), "adbVersion"),
            xcode_version=_optional_str(payload.get("xcodeVersion"), "xcodeVersion"),
            network_profile=_optional_str(payload.get("networkProfile"), "networkProfile"),
            battery_level=battery_level,
            charging=charging,
            thermal_state=thermal_state,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "hostOs": self.host_os,
            "hostArch": self.host_arch,
            "toolVersion": self.tool_version,
        }
        for key, value in (
            ("adbVersion", self.adb_version),
            ("xcodeVersion", self.xcode_version),
            ("networkProfile", self.network_profile),
            ("batteryLevel", self.battery_level),
            ("charging", self.charging),
            ("thermalState", self.thermal_state),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class ManifestRun:
    repeat_index: int
    warmup_index: int
    started_at_utc: str
    ended_at_utc: Optional[str]
    status: str
    evidence_level: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManifestRun":
        payload = _require_mapping(data, "run")
        evidence = payload.get("evidenceLevel", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) and item for item in evidence):
            raise ValidationError("evidenceLevel must be a list of non-empty strings", field="evidenceLevel")
        status = _require_str(payload.get("status"), "status")
        if status not in ALLOWED_RUN_STATUSES:
            raise ValidationError(
                f"status must be one of {sorted(ALLOWED_RUN_STATUSES)}",
                field="status",
            )
        return cls(
            repeat_index=_require_int(payload.get("repeatIndex"), "repeatIndex", min_value=0),
            warmup_index=_require_int(payload.get("warmupIndex"), "warmupIndex", min_value=0),
            started_at_utc=_validate_iso8601_utc(_require_str(payload.get("startedAtUtc"), "startedAtUtc"), "startedAtUtc"),
            ended_at_utc=(
                _validate_iso8601_utc(_optional_str(payload.get("endedAtUtc"), "endedAtUtc"), "endedAtUtc")
                if payload.get("endedAtUtc") is not None
                else None
            ),
            status=status,
            evidence_level=tuple(evidence),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "repeatIndex": self.repeat_index,
            "warmupIndex": self.warmup_index,
            "startedAtUtc": self.started_at_utc,
            "status": self.status,
            "evidenceLevel": list(self.evidence_level),
        }
        if self.ended_at_utc is not None:
            result["endedAtUtc"] = self.ended_at_utc
        return result


@dataclass(frozen=True)
class ManifestContext:
    """run 上下文扩展（CM-TASK-021）：clockAnchors 等 run 级对齐信息。

    ``clockAnchors``（SCREEN_RECORD_SYNC §2）：t0（logcat epoch 首条）、video
    （screenrecord 起点/首帧估算/偏移）、markers（flow 事件 epoch）——离线可
    复算性能时间轴↔录屏时刻映射。
    """

    clock_anchors: Optional[Mapping[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManifestContext":
        payload = _require_mapping(data, "context")
        clock_anchors = payload.get("clockAnchors")
        return cls(
            clock_anchors=_require_mapping(clock_anchors, "clockAnchors")
            if clock_anchors is not None
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.clock_anchors is not None:
            result["clockAnchors"] = dict(self.clock_anchors)
        return result


@dataclass(frozen=True)
class Manifest:
    schema_version: str
    run_id: str
    created_at_utc: str
    mode: str
    scenario: ManifestScenario
    app: ManifestApp
    device: ManifestDevice
    environment: ManifestEnvironment
    run: ManifestRun
    context: Optional[ManifestContext] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Manifest":
        payload = _require_mapping(data, "manifest")
        schema_version = _require_str(payload.get("schemaVersion"), "schemaVersion")
        if schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValidationError(
                f"schemaVersion must be {MANIFEST_SCHEMA_VERSION}",
                field="schemaVersion",
            )
        context_payload = payload.get("context")
        return cls(
            schema_version=schema_version,
            run_id=_require_str(payload.get("runId"), "runId"),
            created_at_utc=_validate_iso8601_utc(_require_str(payload.get("createdAtUtc"), "createdAtUtc"), "createdAtUtc"),
            mode=_require_str(payload.get("mode"), "mode"),
            scenario=ManifestScenario.from_dict(payload.get("scenario", {})),
            app=ManifestApp.from_dict(payload.get("app", {})),
            device=ManifestDevice.from_dict(payload.get("device", {})),
            environment=ManifestEnvironment.from_dict(payload.get("environment", {})),
            run=ManifestRun.from_dict(payload.get("run", {})),
            context=ManifestContext.from_dict(context_payload) if isinstance(context_payload, Mapping) else None,
        )

    @classmethod
    def from_run_context(
        cls,
        *,
        run_id: str,
        created_at_utc: datetime,
        mode: str,
        scenario: ManifestScenario,
        app: ManifestApp,
        device: ManifestDevice,
        environment: ManifestEnvironment,
        run: ManifestRun,
        context: Optional[ManifestContext] = None,
    ) -> "Manifest":
        return cls(
            schema_version=MANIFEST_SCHEMA_VERSION,
            run_id=run_id,
            created_at_utc=_normalize_datetime_utc(created_at_utc),
            mode=mode,
            scenario=scenario,
            app=app,
            device=device,
            environment=environment,
            run=run,
            context=context,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "createdAtUtc": self.created_at_utc,
            "mode": self.mode,
            "scenario": self.scenario.to_dict(),
            "app": self.app.to_dict(),
            "device": self.device.to_dict(),
            "environment": self.environment.to_dict(),
            "run": self.run.to_dict(),
        }
        if self.context is not None and self.context.to_dict():
            result["context"] = self.context.to_dict()
        return result
