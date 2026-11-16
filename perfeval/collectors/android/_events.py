from __future__ import annotations

from typing import Any


def metric_event(
    *,
    seq: int,
    run_id: str,
    name: str,
    value: float | int,
    unit: str,
    collector: str,
    evidence_level: str = "L1",
    ts_monotonic_ns: int = 0,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "value": value,
        "unit": unit,
        "direction": "lower_better",
    }
    if data:
        payload.update(data)
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-android-{collector}-{seq:03d}",
        "type": "metric",
        "name": name,
        "tsMonotonicNs": ts_monotonic_ns,
        "source": {
            "platform": "android",
            "collector": collector,
            "evidenceLevel": evidence_level,
        },
        "data": payload,
    }


def diagnostic_event(
    *,
    seq: int,
    run_id: str,
    name: str,
    severity: str,
    code: str,
    message: str,
    collector: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-android-{collector}-diag-{seq:03d}",
        "type": "diagnostic",
        "name": name,
        "tsMonotonicNs": 0,
        "source": {
            "platform": "android",
            "collector": collector,
            "evidenceLevel": "L1",
        },
        "data": {
            "severity": severity,
            "code": code,
            "message": message,
        },
    }

