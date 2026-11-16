from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from perfeval.core._stats import percentile
from perfeval.core.summary import aggregate_summary


MANIFEST_SCHEMA_VERSION = "perfeval.manifest.v1"
TIMELINE_SCHEMA_VERSION = "perfeval.timeline.v1"
SUMMARY_SCHEMA_VERSION = "perfeval.summary.v1"
COMPARE_SCHEMA_VERSION = "perfeval.compare.v1"

PRIMARY_METRICS = (
    "startup.cold.ms",
    "frame.jank_rate",
    "memory.pss.peak_mb",
    "stability.crash.count",
)
METRIC_SEQUENCE = {metric_name: offset for offset, metric_name in enumerate(PRIMARY_METRICS, start=2)}

_BASE_TIME = datetime(2026, 6, 24, 10, 30, tzinfo=timezone.utc)


def _json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _json_line(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True) + "\n"


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if percentile <= 0:
        return ordered[0]
    if percentile >= 100:
        return ordered[-1]
    rank = max(1, -(-len(ordered) * percentile // 100))
    return ordered[rank - 1]


def _stats(values: list[float]) -> dict[str, float | int]:
    center = median(values)
    deviations = [abs(value - center) for value in values]
    mad = median(deviations) if deviations else 0
    return {
        "n": len(values),
        "median": center,
        "p90": _percentile(values, 90),
        "mad": mad,
    }


def _delta_pct(delta_abs: float, baseline_median: float) -> float:
    if baseline_median == 0:
        return 0.0
    return round(delta_abs / baseline_median, 4)


def _metric_event(
    *,
    seq: int,
    run_id: str,
    name: str,
    ts_ns: int,
    value: float | int,
    unit: str,
    platform: str,
    collector: str,
    evidence_level: str,
    source_event: str | None = None,
) -> dict[str, Any]:
    event = {
        "schemaVersion": TIMELINE_SCHEMA_VERSION,
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-evt-{seq:03d}",
        "type": "metric",
        "name": name,
        "tsMonotonicNs": ts_ns,
        "source": {
            "platform": platform,
            "collector": collector,
            "evidenceLevel": evidence_level,
        },
        "data": {
            "value": value,
            "unit": unit,
            "direction": "lower_better",
            "aggregation": "single_run",
        },
    }
    if source_event is not None:
        event["data"]["sourceEventId"] = source_event
    return event


def _time_sync_event(*, seq: int, run_id: str, ts_ns: int, utc: str) -> dict[str, Any]:
    return {
        "schemaVersion": TIMELINE_SCHEMA_VERSION,
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-evt-{seq:03d}",
        "type": "instant",
        "name": "time.sync",
        "tsMonotonicNs": ts_ns,
        "source": {
            "platform": "host",
            "collector": "synthetic",
            "evidenceLevel": "L0",
        },
        "data": {
            "hostMonotonicNs": ts_ns,
            "deviceElapsedRealtimeNs": ts_ns - 6_000_000,
            "deviceUptimeNs": ts_ns - 8_000_000,
            "utc": utc,
            "confidence": "high",
        },
    }


def _diagnostic_event(
    *,
    seq: int,
    run_id: str,
    ts_ns: int,
    severity: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": TIMELINE_SCHEMA_VERSION,
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-evt-{seq:03d}",
        "type": "diagnostic",
        "name": f"diagnostic.{code}",
        "tsMonotonicNs": ts_ns,
        "source": {
            "platform": "host",
            "collector": "synthetic",
            "evidenceLevel": "L0",
        },
        "data": {
            "severity": severity,
            "code": code,
            "message": message,
        },
    }


def _run_manifest(*, case_id: str, group: str, index: int, status: str, started_at: str, ended_at: str) -> dict[str, Any]:
    group_label = "candidate" if group == "candidate" else "baseline"
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "runId": f"{case_id}-{group_label}-r{index:03d}",
        "createdAtUtc": started_at,
        "mode": "same_device_versions",
        "scenario": {
            "scenarioId": case_id,
            "description": CASE_DEFINITIONS[case_id]["description"],
            "flowHash": f"sha256:synthetic-{case_id}",
        },
        "app": {
            "platform": "android",
            "appId": group_label,
            "packageName": "com.perfeval.fixture",
            "bundleId": None,
            "versionName": "1.0.0" if group == "baseline" else "1.0.1",
            "versionCode": "100" if group == "baseline" else "101",
            "buildId": f"synthetic:{case_id}:{group_label}:r{index:03d}",
            "artifactPath": f"artifacts/app-{group_label}.apk",
            "artifactSha256": f"sha256:synthetic-{case_id}-{group_label}",
        },
        "device": {
            "platform": "android",
            "deviceId": "synthetic-pixel6",
            "serialOrUdid": "synthetic-serial",
            "model": "Pixel 6",
            "osVersion": "14",
            "apiLevel": 34,
            "refreshRateHz": 60,
            "cpuAbi": "arm64-v8a",
            "memoryTotalMb": 8192,
        },
        "environment": {
            "hostOs": "macOS",
            "hostArch": "arm64",
            "toolVersion": "0.1.0",
            "adbVersion": None,
            "xcodeVersion": None,
            "networkProfile": "wifi-lab",
            "batteryLevel": 82,
            "charging": True,
            "thermalState": "nominal",
        },
        "run": {
            "repeatIndex": index,
            "warmupIndex": 0,
            "startedAtUtc": started_at,
            "endedAtUtc": ended_at,
            "status": status,
            "evidenceLevel": ["L0", "L1", "L2"],
        },
    }


def _run_summary(
    *,
    case_id: str,
    group: str,
    index: int,
    run_id: str,
    status: str,
    metrics: Mapping[str, float | int | None],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    summary_metrics: dict[str, Any] = {}
    for metric_name, value in metrics.items():
        if value is None:
            continue
        unit = {
            "startup.cold.ms": "ms",
            "frame.jank_rate": "ratio",
            "memory.pss.peak_mb": "MB",
            "stability.crash.count": "count",
        }[metric_name]
        confidence = "high"
        if case_id == "noisy_unknown":
            confidence = "low"
        if metric_name == "stability.crash.count" and value == 0 and status == "failed":
            confidence = "medium"
        summary_metrics[metric_name] = {
            "value": value,
            "unit": unit,
            "direction": "lower_better",
            "sourceEvents": [f"{run_id}-evt-{METRIC_SEQUENCE[metric_name]:03d}"],
            "artifactRefs": [],
            "confidence": confidence,
        }
    return {
        "schemaVersion": SUMMARY_SCHEMA_VERSION,
        "runId": run_id,
        "status": status,
        "evidenceLevel": ["L0", "L1", "L2"],
        "metrics": summary_metrics,
        "diagnostics": diagnostics,
        "artifacts": [],
    }


def _run_timeline(
    *,
    case_id: str,
    group: str,
    index: int,
    run_id: str,
    started_at: str,
    metrics: Mapping[str, float | int | None],
) -> list[dict[str, Any]]:
    base_ts = 1_000_000_000 + (index - 1) * 5_000_000
    events: list[dict[str, Any]] = [
        _time_sync_event(seq=1, run_id=run_id, ts_ns=base_ts, utc=started_at),
    ]
    platform = "android"
    collector = "synthetic"
    source_level = "L1"
    for offset, metric_name in enumerate(PRIMARY_METRICS, start=2):
        value = metrics.get(metric_name)
        if value is None:
            continue
        unit = {
            "startup.cold.ms": "ms",
            "frame.jank_rate": "ratio",
            "memory.pss.peak_mb": "MB",
            "stability.crash.count": "count",
        }[metric_name]
        events.append(
            _metric_event(
                seq=offset,
                run_id=run_id,
                name=metric_name,
                ts_ns=base_ts + offset * 1_000_000,
                value=value,
                unit=unit,
                platform=platform,
                collector=collector,
                evidence_level=source_level,
                source_event=f"{run_id}-evt-{offset:03d}",
            )
        )
    return events


def _run_diagnostics(
    *,
    case_id: str,
    group: str,
    index: int,
    status: str,
    crash_count: int,
) -> list[dict[str, Any]]:
    run_label = f"{case_id}:{group}:r{index:03d}"
    diagnostics = [
        {
            "schemaVersion": TIMELINE_SCHEMA_VERSION,
            "seq": 1,
            "runId": f"{case_id}-{group}-r{index:03d}",
            "eventId": f"{case_id}-{group}-r{index:03d}-diag-001",
            "type": "diagnostic",
            "name": "synthetic.case",
            "tsMonotonicNs": 5_000_000_000,
            "source": {
                "platform": "host",
                "collector": "synthetic",
                "evidenceLevel": "L0",
            },
            "data": {
                "severity": "info",
                "code": "synthetic.generated",
                "message": f"Synthetic fixture for {run_label}",
            },
        }
    ]
    if crash_count > 0:
        diagnostics.append(
            {
                "schemaVersion": TIMELINE_SCHEMA_VERSION,
                "seq": 2,
                "runId": f"{case_id}-{group}-r{index:03d}",
                "eventId": f"{case_id}-{group}-r{index:03d}-diag-002",
                "type": "diagnostic",
                "name": "stability.crash.detected",
                "tsMonotonicNs": 5_100_000_000,
                "source": {
                    "platform": "host",
                    "collector": "synthetic",
                    "evidenceLevel": "L0",
                },
                "data": {
                    "severity": "error",
                    "code": "stability.crash",
                    "message": "Candidate run reported a crash event",
                },
            }
        )
    if status == "failed" and crash_count == 0:
        diagnostics.append(
            {
                "schemaVersion": TIMELINE_SCHEMA_VERSION,
                "seq": 2,
                "runId": f"{case_id}-{group}-r{index:03d}",
                "eventId": f"{case_id}-{group}-r{index:03d}-diag-002",
                "type": "diagnostic",
                "name": "run.failed",
                "tsMonotonicNs": 5_100_000_000,
                "source": {
                    "platform": "host",
                    "collector": "synthetic",
                    "evidenceLevel": "L0",
                },
                "data": {
                    "severity": "warning",
                    "code": "synthetic.run.failed",
                    "message": "Run was marked failed by the synthetic case definition",
                },
            }
        )
    return diagnostics


def _build_run(case_id: str, group: str, index: int, series: Mapping[str, list[float | int | None]]) -> "SyntheticRunFixture":
    run_id = f"{case_id}-{group}-r{index:03d}"
    started = _BASE_TIME + timedelta(minutes=(index - 1) + (0 if group == "baseline" else 10))
    ended = started + timedelta(seconds=30 if series["stability.crash.count"][index - 1] == 0 else 8)
    started_at = started.isoformat().replace("+00:00", "Z")
    ended_at = ended.isoformat().replace("+00:00", "Z")

    metrics = {name: values[index - 1] for name, values in series.items()}
    crash_count = int(metrics["stability.crash.count"] or 0)
    status = "failed" if crash_count > 0 else "completed"
    diagnostics = _run_diagnostics(
        case_id=case_id,
        group=group,
        index=index,
        status=status,
        crash_count=crash_count,
    )
    manifest = _run_manifest(
        case_id=case_id,
        group=group,
        index=index,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
    )
    timeline = _run_timeline(
        case_id=case_id,
        group=group,
        index=index,
        run_id=run_id,
        started_at=started_at,
        metrics=metrics,
    )
    summary = _run_summary(
        case_id=case_id,
        group=group,
        index=index,
        run_id=run_id,
        status=status,
        metrics=metrics,
        diagnostics=diagnostics,
    )
    return SyntheticRunFixture(
        case_id=case_id,
        group=group,
        index=index,
        run_id=run_id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        metrics=metrics,
        manifest=manifest,
        timeline=timeline,
        summary=summary,
        diagnostics=diagnostics,
    )


def _compare_expected(case_id: str, primary_metric: str, baseline: list[float | int], candidate: list[float | int], *, status: str, reason: str) -> dict[str, Any]:
    baseline_stats = _stats([float(value) for value in baseline])
    candidate_stats = _stats([float(value) for value in candidate])
    delta_abs = round(candidate_stats["median"] - baseline_stats["median"], 4)
    return {
        "schemaVersion": COMPARE_SCHEMA_VERSION,
        "compareId": f"compare-{case_id}",
        "caseId": case_id,
        "mode": "same_device_versions",
        "baselineGroup": "baseline",
        "candidateGroup": "candidate",
        "status": status,
        "expectedOutcome": status,
        "metrics": {
            primary_metric: {
                "baseline": baseline_stats,
                "candidate": candidate_stats,
                "delta": {
                    "abs": delta_abs,
                    "pct": _delta_pct(delta_abs, float(baseline_stats["median"])),
                },
                "judgement": status,
                "reason": reason,
                "confidence": {
                    "level": "low" if len(baseline) < 5 or len(candidate) < 5 else "high",
                    "sampleSize": min(len(baseline), len(candidate)),
                },
            }
        },
    }


# --- continuous_stream：run 内连续采样序列（CM-001 §3，测试策略 §3） ---

STREAM_CASE_ID = "continuous_stream"
STREAM_LENGTH = 60
STREAM_SPIKE_COUNT = 8
STREAM_SPIKE_BASELINE_MS = 55.0
STREAM_SPIKE_CANDIDATE_MS = 110.0
_STREAM_UNIT_MAP = {
    "startup.cold.ms": "ms",
    "frame.jank_rate": "ratio",
    "memory.pss.peak_mb": "MB",
    "stability.crash.count": "count",
    "frame.duration.p95.ms": "ms",
}


def _cpu_stream(group: str, index: int) -> list[float]:
    """60 点平稳 CPU 序列；candidate 均值抬升 30%。"""
    base = 38.0 if group == "baseline" else round(38.0 * 1.3, 3)
    return [round(base + ((offset * 7 + index) % 5) * 0.4, 3) for offset in range(STREAM_LENGTH)]


def _frame_stream(group: str, index: int) -> list[float]:
    """60 点帧耗时序列，含 8 帧尖刺；candidate 均值抬升 30% 且尖刺加倍。"""
    base = 16.5 if group == "baseline" else round(16.5 * 1.3, 3)
    spike = STREAM_SPIKE_BASELINE_MS if group == "baseline" else STREAM_SPIKE_CANDIDATE_MS
    values = [round(base + ((offset * 3 + index) % 4) * 0.5, 3) for offset in range(STREAM_LENGTH)]
    for spike_index in range(STREAM_SPIKE_COUNT):
        values[spike_index * 7] = spike
    return values


def _synthetic_counter_event(
    *,
    seq: int,
    run_id: str,
    name: str,
    ts_ns: int,
    value: float,
    unit: str,
    direction: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": TIMELINE_SCHEMA_VERSION,
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-sample-{seq:03d}",
        "type": "counter",
        "name": name,
        "tsMonotonicNs": ts_ns,
        "source": {
            "platform": "android",
            "collector": "synthetic",
            "evidenceLevel": "L1",
        },
        "data": {
            "value": value,
            "unit": unit,
            "direction": direction,
        },
    }


def _stream_run_timeline(
    *,
    group: str,
    index: int,
    run_id: str,
    started_at: str,
    metrics: Mapping[str, float | int | None],
) -> list[dict[str, Any]]:
    base_ts = 1_000_000_000 + (index - 1) * 5_000_000
    events: list[dict[str, Any]] = [
        _time_sync_event(seq=1, run_id=run_id, ts_ns=base_ts, utc=started_at),
    ]
    for offset, metric_name in enumerate(PRIMARY_METRICS, start=2):
        value = metrics.get(metric_name)
        if value is None:
            continue
        events.append(
            _metric_event(
                seq=offset,
                run_id=run_id,
                name=metric_name,
                ts_ns=base_ts + offset * 1_000_000,
                value=value,
                unit=_STREAM_UNIT_MAP[metric_name],
                platform="android",
                collector="synthetic",
                evidence_level="L1",
                source_event=f"{run_id}-evt-{offset:03d}",
            )
        )

    sampled: list[tuple[int, str, float, str, str]] = []
    cpu_values = _cpu_stream(group, index)
    frame_values = _frame_stream(group, index)
    for sample_index, value in enumerate(cpu_values):
        sampled.append((base_ts + (sample_index + 1) * 1_000_000_000, "cpu.proc.pct", value, "%", "none"))
    for sample_index, value in enumerate(frame_values):
        sampled.append((base_ts + (sample_index + 1) * 500_000_000, "frame.duration.detail.ms", value, "ms", "lower_better"))
    sampled.sort(key=lambda entry: (entry[0], entry[1]))
    for entry_index, (ts_ns, name, value, unit, direction) in enumerate(sampled):
        events.append(
            _synthetic_counter_event(
                seq=len(events) + 1,
                run_id=run_id,
                name=name,
                ts_ns=ts_ns,
                value=value,
                unit=unit,
                direction=direction,
            )
        )

    frame_p95 = percentile(sorted(frame_values), 95)
    events.append(
        _metric_event(
            seq=len(events) + 1,
            run_id=run_id,
            name="frame.duration.p95.ms",
            ts_ns=base_ts + (STREAM_LENGTH + 1) * 1_000_000_000,
            value=frame_p95,
            unit=_STREAM_UNIT_MAP["frame.duration.p95.ms"],
            platform="android",
            collector="synthetic",
            evidence_level="L1",
        )
    )
    return events


def _build_stream_run(case_id: str, group: str, index: int, series: Mapping[str, list[float | int | None]]) -> "SyntheticRunFixture":
    """continuous_stream run：summary 由 timeline counter 事件聚合出 series。"""
    run_id = f"{case_id}-{group}-r{index:03d}"
    started = _BASE_TIME + timedelta(minutes=(index - 1) + (0 if group == "baseline" else 10))
    ended = started + timedelta(seconds=STREAM_LENGTH + 5)
    started_at = started.isoformat().replace("+00:00", "Z")
    ended_at = ended.isoformat().replace("+00:00", "Z")

    metrics = {name: values[index - 1] for name, values in series.items()}
    status = "completed"
    diagnostics = _run_diagnostics(
        case_id=case_id,
        group=group,
        index=index,
        status=status,
        crash_count=0,
    )
    manifest = _run_manifest(
        case_id=case_id,
        group=group,
        index=index,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
    )
    timeline = _stream_run_timeline(
        group=group,
        index=index,
        run_id=run_id,
        started_at=started_at,
        metrics=metrics,
    )
    summary = aggregate_summary(
        run_id,
        timeline_events=timeline,
        status=status,
        diagnostics=diagnostics,
    )
    return SyntheticRunFixture(
        case_id=case_id,
        group=group,
        index=index,
        run_id=run_id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        metrics=metrics,
        manifest=manifest,
        timeline=timeline,
        summary=summary,
        diagnostics=diagnostics,
    )


CASE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "no_regression": {
        "description": "Synthetic cold start with no regression.",
        "expected_outcome": "pass",
        "primary_metric": "startup.cold.ms",
        "reason": "within_budget",
        "series": {
            "baseline": {
                "startup.cold.ms": [1000, 1005, 995, 1002, 998],
                "frame.jank_rate": [0.03, 0.031, 0.029, 0.03, 0.032],
                "memory.pss.peak_mb": [300, 301, 299, 300, 302],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
            "candidate": {
                "startup.cold.ms": [1010, 1008, 1012, 1006, 1009],
                "frame.jank_rate": [0.031, 0.03, 0.032, 0.03, 0.031],
                "memory.pss.peak_mb": [302, 303, 301, 302, 304],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
        },
    },
    "startup_regression": {
        "description": "Synthetic cold start regression.",
        "expected_outcome": "fail",
        "primary_metric": "startup.cold.ms",
        "reason": "candidate_median_exceeds_regression_threshold",
        "series": {
            "baseline": {
                "startup.cold.ms": [995, 1000, 1003, 998, 1004],
                "frame.jank_rate": [0.03, 0.031, 0.03, 0.029, 0.032],
                "memory.pss.peak_mb": [300, 301, 299, 300, 302],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
            "candidate": {
                "startup.cold.ms": [1240, 1250, 1265, 1235, 1255],
                "frame.jank_rate": [0.031, 0.03, 0.031, 0.03, 0.032],
                "memory.pss.peak_mb": [302, 303, 301, 302, 304],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
        },
    },
    "jank_regression": {
        "description": "Synthetic frame jank regression.",
        "expected_outcome": "fail",
        "primary_metric": "frame.jank_rate",
        "reason": "candidate_jank_rate_exceeds_regression_threshold",
        "series": {
            "baseline": {
                "startup.cold.ms": [1000, 1001, 999, 1002, 998],
                "frame.jank_rate": [0.03, 0.031, 0.029, 0.03, 0.032],
                "memory.pss.peak_mb": [300, 301, 299, 300, 302],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
            "candidate": {
                "startup.cold.ms": [1002, 1000, 1001, 1003, 999],
                "frame.jank_rate": [0.12, 0.115, 0.118, 0.123, 0.119],
                "memory.pss.peak_mb": [302, 303, 301, 302, 304],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
        },
    },
    "memory_regression": {
        "description": "Synthetic memory peak regression.",
        "expected_outcome": "fail",
        "primary_metric": "memory.pss.peak_mb",
        "reason": "candidate_memory_peak_exceeds_regression_threshold",
        "series": {
            "baseline": {
                "startup.cold.ms": [1000, 1001, 999, 1002, 998],
                "frame.jank_rate": [0.03, 0.031, 0.029, 0.03, 0.032],
                "memory.pss.peak_mb": [295, 300, 302, 298, 301],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
            "candidate": {
                "startup.cold.ms": [1002, 1000, 1001, 1003, 999],
                "frame.jank_rate": [0.031, 0.03, 0.031, 0.03, 0.032],
                "memory.pss.peak_mb": [388, 390, 392, 386, 391],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
        },
    },
    "noisy_unknown": {
        "description": "Synthetic noisy short-run case.",
        "expected_outcome": "unknown",
        "primary_metric": "startup.cold.ms",
        "reason": "sample_size_below_threshold",
        "series": {
            "baseline": {
                "startup.cold.ms": [820, 1100, 1320],
                "frame.jank_rate": [0.02, 0.09, 0.04],
                "memory.pss.peak_mb": [250, 405, 315],
                "stability.crash.count": [0, 0, 0],
            },
            "candidate": {
                "startup.cold.ms": [850, 1160, 1285],
                "frame.jank_rate": [0.021, 0.085, 0.041],
                "memory.pss.peak_mb": [260, 415, 320],
                "stability.crash.count": [0, 0, 0],
            },
        },
    },
    "crash_fail": {
        "description": "Synthetic crash during one candidate run.",
        "expected_outcome": "fail",
        "primary_metric": "stability.crash.count",
        "reason": "crash_count_non_zero",
        "series": {
            "baseline": {
                "startup.cold.ms": [1000, 1003, 998, 1001, 999],
                "frame.jank_rate": [0.03, 0.031, 0.029, 0.03, 0.032],
                "memory.pss.peak_mb": [300, 301, 299, 300, 302],
                "stability.crash.count": [0, 0, 0, 0, 0],
            },
            "candidate": {
                "startup.cold.ms": [1001, 1004, 999, 1002, 1000],
                "frame.jank_rate": [0.03, 0.031, 0.03, 0.032, 0.031],
                "memory.pss.peak_mb": [301, 302, 300, 301, 303],
                "stability.crash.count": [0, 0, 1, 0, 0],
            },
        },
    },
    "continuous_stream": {
        "description": "Synthetic continuous CPU/frame streams with regression.",
        "expected_outcome": "fail",
        "primary_metric": "frame.duration.p95.ms",
        "reason": "regression_pct_exceeds_20",
        "stream": True,
        "series": {
            "baseline": {
                "startup.cold.ms": [1000, 1003, 998, 1001, 999],
                "frame.jank_rate": [0.03, 0.031, 0.029, 0.03, 0.032],
                "memory.pss.peak_mb": [295, 300, 302, 298, 301],
                "stability.crash.count": [0, 0, 0, 0, 0],
                "frame.duration.p95.ms": [55.0, 55.0, 55.0, 55.0, 55.0],
            },
            "candidate": {
                "startup.cold.ms": [1008, 1011, 1005, 1010, 1006],
                "frame.jank_rate": [0.031, 0.03, 0.032, 0.031, 0.03],
                "memory.pss.peak_mb": [298, 302, 305, 300, 303],
                "stability.crash.count": [0, 0, 0, 0, 0],
                "frame.duration.p95.ms": [110.0, 110.0, 110.0, 110.0, 110.0],
            },
        },
    },
}


@dataclass(frozen=True)
class SyntheticRunFixture:
    case_id: str
    group: str
    index: int
    run_id: str
    status: str
    started_at: str
    ended_at: str
    metrics: Mapping[str, float | int | None]
    manifest: Mapping[str, Any]
    timeline: list[dict[str, Any]] = field(default_factory=list)
    summary: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    @property
    def run_dir_name(self) -> str:
        return f"run-{self.index:03d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "group": self.group,
            "index": self.index,
            "runId": self.run_id,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "metrics": dict(self.metrics),
            "manifest": dict(self.manifest),
            "timeline": list(self.timeline),
            "summary": dict(self.summary),
            "diagnostics": list(self.diagnostics),
        }

    def write(self, root_dir: str | Path) -> Path:
        run_dir = Path(root_dir) / self.group / self.run_dir_name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text(_json_text(self.manifest), encoding="utf-8")
        (run_dir / "timeline.ndjson").write_text(
            "".join(_json_line(event) for event in self.timeline),
            encoding="utf-8",
        )
        (run_dir / "summary.json").write_text(_json_text(self.summary), encoding="utf-8")
        (run_dir / "diagnostics.ndjson").write_text(
            "".join(_json_line(entry) for entry in self.diagnostics),
            encoding="utf-8",
        )
        (run_dir / "artifacts").mkdir(exist_ok=True)
        return run_dir


@dataclass(frozen=True)
class SyntheticCaseFixture:
    case_id: str
    description: str
    expected_outcome: str
    primary_metric: str
    reason: str
    baseline_runs: tuple[SyntheticRunFixture, ...]
    candidate_runs: tuple[SyntheticRunFixture, ...]
    compare_expected: Mapping[str, Any]

    @property
    def repeat_count(self) -> int:
        return len(self.baseline_runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "description": self.description,
            "expectedOutcome": self.expected_outcome,
            "primaryMetric": self.primary_metric,
            "reason": self.reason,
            "baselineRuns": [run.to_dict() for run in self.baseline_runs],
            "candidateRuns": [run.to_dict() for run in self.candidate_runs],
            "compareExpected": dict(self.compare_expected),
        }

    def write(self, out_dir: str | Path) -> Path:
        output_dir = Path(out_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for run in self.baseline_runs:
            run.write(output_dir)
        for run in self.candidate_runs:
            run.write(output_dir)
        expected_dir = output_dir / "expected"
        expected_dir.mkdir(exist_ok=True)
        (expected_dir / "compare.json").write_text(_json_text(self.compare_expected), encoding="utf-8")
        return output_dir


def available_cases() -> tuple[str, ...]:
    return tuple(CASE_DEFINITIONS.keys())


def get_case(case_id: str) -> SyntheticCaseFixture:
    if case_id not in CASE_DEFINITIONS:
        raise KeyError(f"unknown synthetic case: {case_id}")

    definition = CASE_DEFINITIONS[case_id]
    baseline_series = definition["series"]["baseline"]
    candidate_series = definition["series"]["candidate"]
    run_builder = _build_stream_run if definition.get("stream") else _build_run
    baseline_runs = tuple(
        run_builder(case_id, "baseline", index, baseline_series)
        for index in range(1, len(next(iter(baseline_series.values()))) + 1)
    )
    candidate_runs = tuple(
        run_builder(case_id, "candidate", index, candidate_series)
        for index in range(1, len(next(iter(candidate_series.values()))) + 1)
    )
    compare_expected = _compare_expected(
        case_id,
        definition["primary_metric"],
        baseline_series[definition["primary_metric"]],
        candidate_series[definition["primary_metric"]],
        status=definition["expected_outcome"],
        reason=definition["reason"],
    )
    return SyntheticCaseFixture(
        case_id=case_id,
        description=definition["description"],
        expected_outcome=definition["expected_outcome"],
        primary_metric=definition["primary_metric"],
        reason=definition["reason"],
        baseline_runs=baseline_runs,
        candidate_runs=candidate_runs,
        compare_expected=compare_expected,
    )


def generate_case(case_id: str, out_dir: str | Path | None = None) -> SyntheticCaseFixture:
    fixture = get_case(case_id)
    if out_dir is not None:
        fixture.write(out_dir)
    return fixture


def write_case(case_id: str, out_dir: str | Path) -> SyntheticCaseFixture:
    return generate_case(case_id, out_dir=out_dir)


def load_golden_compare(case_id: str) -> dict[str, Any]:
    golden_path = Path(__file__).with_name("golden") / f"{case_id}.compare.json"
    return json.loads(golden_path.read_text(encoding="utf-8"))
