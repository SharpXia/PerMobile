from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ..schema.errors import ValidationError

TIMELINE_SCHEMA_VERSION = "perfeval.timeline.v1"
ALLOWED_EVENT_TYPES = {
    "instant",
    "interval",
    "counter",
    "sample",
    "metric",
    "artifact",
    "diagnostic",
}


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string", field=field)
    return value


def _require_int(value: Any, field: str, *, min_value: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer", field=field)
    if min_value is not None and value < min_value:
        raise ValidationError(f"{field} must be >= {min_value}", field=field)
    return value


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be a mapping", field=field)
    return value


def _optional_mapping(value: Any, field: str) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    return _require_mapping(value, field)


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


@dataclass(frozen=True)
class EventSource:
    platform: str
    collector: str
    evidence_level: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EventSource":
        payload = _require_mapping(data, "source")
        return cls(
            platform=_require_str(payload.get("platform"), "platform"),
            collector=_require_str(payload.get("collector"), "collector"),
            evidence_level=_require_str(payload.get("evidenceLevel"), "evidenceLevel"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "collector": self.collector,
            "evidenceLevel": self.evidence_level,
        }


@dataclass(frozen=True)
class TimelineEvent:
    schema_version: str
    seq: int
    run_id: str
    event_id: str
    type: str
    name: str
    ts_monotonic_ns: int
    source: EventSource
    ts_utc: Optional[str] = None
    duration_ns: Optional[int] = None
    scope: Optional[Mapping[str, Any]] = None
    tags: Optional[Mapping[str, Any]] = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TimelineEvent":
        payload = _require_mapping(data, "timeline event")
        schema_version = _require_str(payload.get("schemaVersion"), "schemaVersion")
        if schema_version != TIMELINE_SCHEMA_VERSION:
            raise ValidationError(
                f"schemaVersion must be {TIMELINE_SCHEMA_VERSION}",
                field="schemaVersion",
            )
        event_type = _require_str(payload.get("type"), "type")
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValidationError(
                f"type must be one of {sorted(ALLOWED_EVENT_TYPES)}",
                field="type",
            )
        duration_ns = payload.get("durationNs")
        if event_type == "interval" and duration_ns is None:
            raise ValidationError("interval events require durationNs", field="durationNs")
        if duration_ns is not None:
            duration_ns = _require_int(duration_ns, "durationNs", min_value=0)
        ts_utc = payload.get("tsUtc")
        if ts_utc is not None:
            ts_utc = _validate_iso8601_utc(_require_str(ts_utc, "tsUtc"), "tsUtc")
        scope = _optional_mapping(payload.get("scope"), "scope")
        tags = _optional_mapping(payload.get("tags"), "tags")
        return cls(
            schema_version=schema_version,
            seq=_require_int(payload.get("seq"), "seq", min_value=1),
            run_id=_require_str(payload.get("runId"), "runId"),
            event_id=_require_str(payload.get("eventId"), "eventId"),
            type=event_type,
            name=_require_str(payload.get("name"), "name"),
            ts_monotonic_ns=_require_int(payload.get("tsMonotonicNs"), "tsMonotonicNs", min_value=0),
            source=EventSource.from_dict(payload.get("source", {})),
            ts_utc=ts_utc,
            duration_ns=duration_ns,
            scope=scope,
            tags=tags,
            data=_require_mapping(payload.get("data", {}), "data"),
        )

    @classmethod
    def new(
        cls,
        *,
        seq: int,
        run_id: str,
        event_id: str,
        type: str,
        name: str,
        ts_monotonic_ns: int,
        source: EventSource,
        data: Mapping[str, Any],
        ts_utc: Optional[datetime | str] = None,
        duration_ns: Optional[int] = None,
        scope: Optional[Mapping[str, Any]] = None,
        tags: Optional[Mapping[str, Any]] = None,
    ) -> "TimelineEvent":
        normalized_ts_utc: str | None = None
        if isinstance(ts_utc, datetime):
            normalized_ts_utc = _normalize_datetime_utc(ts_utc)
        elif ts_utc is not None:
            normalized_ts_utc = ts_utc
        payload: dict[str, Any] = {
            "schemaVersion": TIMELINE_SCHEMA_VERSION,
            "seq": seq,
            "runId": run_id,
            "eventId": event_id,
            "type": type,
            "name": name,
            "tsMonotonicNs": ts_monotonic_ns,
            "source": source.to_dict(),
            "data": dict(data),
        }
        if normalized_ts_utc is not None:
            payload["tsUtc"] = normalized_ts_utc
        if duration_ns is not None:
            payload["durationNs"] = duration_ns
        if scope is not None:
            payload["scope"] = dict(scope)
        if tags is not None:
            payload["tags"] = dict(tags)
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "seq": self.seq,
            "runId": self.run_id,
            "eventId": self.event_id,
            "type": self.type,
            "name": self.name,
            "tsMonotonicNs": self.ts_monotonic_ns,
            "source": self.source.to_dict(),
            "data": dict(self.data),
        }
        if self.ts_utc is not None:
            result["tsUtc"] = self.ts_utc
        if self.duration_ns is not None:
            result["durationNs"] = self.duration_ns
        if self.scope is not None:
            result["scope"] = dict(self.scope)
        if self.tags is not None:
            result["tags"] = dict(self.tags)
        return result
