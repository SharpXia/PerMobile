from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any

from perfeval.core._stats import (
    bootstrap_median_delta,
    confidence_level_from_score,
    empty_series_summary,
    evidence_level_from_rank,
    evidence_rank,
    merge_unique,
    numeric_value_from_mapping,
    percentile,
    stable_seed,
    summarize_numeric_series,
)


DEFAULT_BUDGET = {
    "defaults": {
        "minRuns": 5,
        "recommendedRuns": 10,
        "confidence": 0.95,
        "outlierPolicy": "mad_3",
    },
    "metrics": {},
}

# N 路判定（2026-11-08 S3 compare-two-groups-only，契约 §9 note 声明）：
# groups 形参（>=2 组）以**首组为全局基线**，其余各组逐一与基线两两判定；
# 每指标 judgement 取各对中最差（fail > warn > unknown > pass），输出保持
# perfeval.compare.v1 顶层兼容（schemaVersion 不变，新增可选字段走 §11.1）。
N_WAY_MAX_GROUPS = 10
_PAIR_RANK = {"pass": 0, "unknown": 1, "warn": 2, "fail": 3}

PRIORITY_BY_METRIC = {
    "stability.crash.count": "P0",
    "stability.anr.count": "P0",
    "stability.hang.count": "P0",
    "startup.cold.ms": "P0",
    "startup.first_frame.ms": "P0",
    "frame.jank_rate": "P0",
    "frame.duration.p95.ms": "P1",
    "frame.p95": "P1",
    "memory.pss.peak_mb": "P1",
    "memory.rss.peak_mb": "P1",
    "cpu.process.avg_pct": "P1",
    "cpu.main_thread_busy.ms": "P1",
}


def _as_summary_list(value: object | None) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping) and "metrics" in value:
        return [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("summary group must be a summary mapping or a sequence of summary mappings")
    return [item for item in value if isinstance(item, Mapping)]


def _summary_metric_value(metric: object) -> tuple[float | None, dict[str, Any]]:
    if isinstance(metric, Mapping):
        value = numeric_value_from_mapping(metric)
        payload = dict(metric)
        if "artifactRefs" in payload and isinstance(payload["artifactRefs"], str):
            payload["artifactRefs"] = [payload["artifactRefs"]]
        if "sourceEvents" in payload and isinstance(payload["sourceEvents"], str):
            payload["sourceEvents"] = [payload["sourceEvents"]]
        return value, payload
    if isinstance(metric, (int, float)) and not isinstance(metric, bool):
        value = numeric_value_from_mapping({"value": metric})
        if value is not None:
            return value, {"value": value}
    return None, {}


def _summary_metrics(summary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for metric_id, metric in metrics.items():
        value, payload = _summary_metric_value(metric)
        if value is None:
            continue
        payload.setdefault("value", value)
        normalized[str(metric_id)] = payload
    return normalized


def _first_non_empty(*values: object) -> object | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _identity_from_summary(summary: Mapping[str, Any], kind: str) -> str | None:
    if kind == "device":
        device = summary.get("device")
        if isinstance(device, Mapping):
            for key in ("serialOrUdid", "udid", "serial", "deviceId"):
                value = device.get(key)
                if isinstance(value, str) and value:
                    return f"{key}:{value}"
            candidates = [
                device.get("model"),
                device.get("osVersion"),
                device.get("apiLevel"),
                device.get("refreshRateHz"),
                device.get("cpuAbi"),
            ]
            if any(candidate is not None for candidate in candidates):
                return "|".join("" if value is None else str(value) for value in candidates)
        return None

    if kind == "app":
        app = summary.get("app")
        if isinstance(app, Mapping):
            for key in ("artifactSha256", "buildId"):
                value = app.get(key)
                if isinstance(value, str) and value:
                    return f"{key}:{value}"
            candidates = [
                app.get("packageName"),
                app.get("bundleId"),
                app.get("versionName"),
                app.get("versionCode"),
                app.get("appId"),
            ]
            if any(candidate is not None for candidate in candidates):
                return "|".join("" if value is None else str(value) for value in candidates)
        return None

    if kind == "scenario":
        scenario = summary.get("scenario")
        if isinstance(scenario, Mapping):
            value = scenario.get("scenarioId") or scenario.get("id")
            if isinstance(value, str) and value:
                return value
        value = summary.get("scenarioId")
        if isinstance(value, str) and value:
            return value
    return None


def _focused_windows_from_summaries(summaries: Sequence[Mapping[str, Any]]) -> list[str]:
    return merge_unique(
        window
        for summary in summaries
        for window in _focused_windows_from_summary(summary)
    )  # type: ignore[return-value]


def _focused_windows_from_summary(summary: Mapping[str, Any]) -> list[str]:
    windows = summary.get("focusedWindows")
    if isinstance(windows, Sequence) and not isinstance(windows, (str, bytes, bytearray)):
        return [window for window in windows if isinstance(window, str) and window]
    window = summary.get("focusedWindow")
    return [window] if isinstance(window, str) and window else []


def _focused_window_family(component: str) -> str:
    activity = component.split("/", 1)[-1].rsplit(".", 1)[-1]
    family = "".join(character.lower() for character in activity if character.isalnum())
    suffixes = ("activity", "compose")
    changed = True
    while changed and family:
        changed = False
        for suffix in suffixes:
            if family.endswith(suffix) and len(family) > len(suffix):
                family = family[: -len(suffix)]
                changed = True
    return family


def _evidence_levels_from_summary(summary: Mapping[str, Any]) -> list[str]:
    levels = summary.get("evidenceLevel")
    if isinstance(levels, str):
        return [levels]
    if isinstance(levels, Sequence):
        return [level for level in levels if isinstance(level, str)]
    return []


def _thermal_nominal(summary: Mapping[str, Any]) -> bool:
    environment = summary.get("environment")
    if isinstance(environment, Mapping):
        thermal = environment.get("thermalState")
        if isinstance(thermal, str):
            return thermal == "nominal"
    thermal = summary.get("thermalState")
    if isinstance(thermal, str):
        return thermal == "nominal"
    return True


def _artifact_refs(summary: Mapping[str, Any], metric: Mapping[str, Any] | None = None) -> list[str]:
    refs: list[str] = []
    artifacts = summary.get("artifacts")
    if isinstance(artifacts, Sequence):
        for artifact in artifacts:
            if isinstance(artifact, Mapping):
                path = artifact.get("path")
                if isinstance(path, str):
                    refs.append(path)
    if metric is not None:
        for ref in metric.get("artifactRefs", []) or []:
            if isinstance(ref, str):
                refs.append(ref)
    return merge_unique(refs)  # type: ignore[return-value]


def _metric_entries(
    summaries: Sequence[Mapping[str, Any]],
    metric_id: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for summary in summaries:
        metrics = _summary_metrics(summary)
        metric = metrics.get(metric_id)
        if metric is None:
            continue
        value = numeric_value_from_mapping(metric)
        if value is None:
            continue
        entries.append(
            {
                "runId": summary.get("runId"),
                "value": value,
                "metric": metric,
                "summary": summary,
            }
        )
    return entries


# stream 指标三列对比的统计量映射（CM-TASK-024，REPORT_PRESENTATION §3）：
# 展示名 -> summary 指标 entry 上的字段名（均值 / 峰值（max）/ p95）
STREAM_STATISTIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("mean", "mean"),
    ("peak", "max"),
    ("p95", "p95"),
)


def _is_stream_entries(entries: list[dict[str, Any]]) -> bool:
    return any(entry["metric"].get("isStream") is True for entry in entries)


def _stream_per_run_statistic(entries: list[dict[str, Any]], field_key: str) -> list[float]:
    """收集各 run 内已聚合好的统计量（summary 对全量采样点计算，见 SAMPLING_FRAMEWORK §3）。"""
    values: list[float] = []
    for entry in entries:
        raw = entry["metric"].get(field_key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if math.isfinite(value):
            values.append(value)
    return values


def _stream_series_segments(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 runId 分段收集 series（series ts 为相对 run 起点毫秒，跨 repeat 回绕，禁止跨 run 合并连线）。"""
    segments: list[dict[str, Any]] = []
    for entry in entries:
        raw_series = entry["metric"].get("series")
        if not isinstance(raw_series, Sequence) or isinstance(raw_series, (str, bytes, bytearray)):
            continue
        points: list[dict[str, Any]] = []
        for raw_point in raw_series:
            if not isinstance(raw_point, Mapping):
                continue
            ts = raw_point.get("ts")
            value = raw_point.get("v")
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            points.append({"ts": int(ts), "v": float(value)})
        if not points:
            continue
        points.sort(key=lambda point: point["ts"])
        run_id = entry.get("runId")
        segments.append(
            {
                "runId": run_id if isinstance(run_id, str) and run_id else f"run-{len(segments) + 1}",
                "points": points,
            }
        )
    segments.sort(key=lambda segment: segment["runId"])
    return segments


def _stream_side_meta(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """单侧（baseline/candidate）的 sampleCount 与分离后置信度。

    confidence 直接读取 summary 侧已做 sampleCount 语义分离的 stream confidence
    （CM-001 §4，repeatCount 定基数），compare 侧不重复计算、不因采样点多而虚高；
    sampleCount 取跨 run 最小值（序列完整性按最短 run 的保守口径展示）。
    """
    sample_counts = [
        int(entry["metric"]["sampleCount"])
        for entry in entries
        if isinstance(entry["metric"].get("sampleCount"), int)
        and not isinstance(entry["metric"].get("sampleCount"), bool)
    ]
    confidences = [
        str(entry["metric"]["confidence"])
        for entry in entries
        if isinstance(entry["metric"].get("confidence"), str)
    ]
    meta: dict[str, Any] = {}
    if sample_counts:
        meta["sampleCount"] = min(sample_counts)
    if confidences:
        meta["confidence"] = confidences[0]
    return meta


def _stream_block(
    *,
    baseline_entries: list[dict[str, Any]],
    candidate_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """stream 指标对比输入适配（CM-TASK-024，REPORT_PRESENTATION §3）。

    对 isStream=true 的指标额外输出均值 / 峰值（max）/ p95 三统计量对比：
    各 run 内统计量取跨 run 中位数进对比列（与判定主输入的跨 run 中位数口径一致）。
    该块仅供展示与叠加图，判定（judgement）仍走既有 median + 阈值引擎，
    不得因三列适配漂移（CM-AC-007：fail==fail）。
    """
    if not _is_stream_entries(baseline_entries) and not _is_stream_entries(candidate_entries):
        return None

    statistics: dict[str, dict[str, float | None]] = {}
    for stat_name, field_key in STREAM_STATISTIC_FIELDS:
        baseline_values = _stream_per_run_statistic(baseline_entries, field_key)
        candidate_values = _stream_per_run_statistic(candidate_entries, field_key)
        baseline_stat = median(baseline_values) if baseline_values else None
        candidate_stat = median(candidate_values) if candidate_values else None
        delta_abs: float | None = None
        delta_pct: float | None = None
        if baseline_stat is not None and candidate_stat is not None:
            delta_abs = candidate_stat - baseline_stat
            if baseline_stat != 0:
                delta_pct = delta_abs / baseline_stat
        statistics[stat_name] = {
            "baseline": baseline_stat,
            "candidate": candidate_stat,
            "deltaAbs": delta_abs,
            "deltaPct": delta_pct,
        }

    baseline_meta = _stream_side_meta(baseline_entries)
    candidate_meta = _stream_side_meta(candidate_entries)
    return {
        "isStream": True,
        "statistics": statistics,
        "sampleCount": {
            "baseline": baseline_meta.get("sampleCount"),
            "candidate": candidate_meta.get("sampleCount"),
        },
        "confidence": {
            "baseline": baseline_meta.get("confidence"),
            "candidate": candidate_meta.get("confidence"),
        },
        "series": {
            "baseline": _stream_series_segments(baseline_entries),
            "candidate": _stream_series_segments(candidate_entries),
        },
    }


def _budget_for_metric(metric_id: str, budget: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = dict(DEFAULT_BUDGET["defaults"])
    if budget and isinstance(budget.get("defaults"), Mapping):
        merged.update(budget["defaults"])
    if budget and isinstance(budget.get("metrics"), Mapping):
        metric_budget = budget["metrics"].get(metric_id)
        if isinstance(metric_budget, Mapping):
            merged.update(metric_budget)
    return merged


def _metric_priority(metric_id: str) -> str:
    if metric_id in PRIORITY_BY_METRIC:
        return PRIORITY_BY_METRIC[metric_id]
    if metric_id.startswith("stability."):
        return "P0"
    if metric_id.startswith("startup.") or metric_id.startswith("frame."):
        return "P1"
    if metric_id.startswith("memory.") or metric_id.startswith("cpu."):
        return "P1"
    return "P2"


def _compare_direction(direction: str, baseline_median: float, candidate_median: float) -> tuple[float, float | None, bool]:
    delta_abs = float(candidate_median - baseline_median)
    if baseline_median == 0:
        delta_pct = None
    else:
        delta_pct = delta_abs / baseline_median
    if direction == "higher_better":
        regression = delta_abs < 0
    else:
        regression = delta_abs > 0
    return delta_abs, delta_pct, regression


def _absolute_fail(direction: str, candidate_value: float, fail_abs: Any) -> bool:
    if fail_abs is None:
        return False
    try:
        fail_abs_value = float(fail_abs)
    except (TypeError, ValueError):
        return False
    if direction == "higher_better":
        return candidate_value < fail_abs_value
    return candidate_value > fail_abs_value


def _absolute_warn(direction: str, candidate_value: float, warn_abs: Any) -> bool:
    if warn_abs is None:
        return False
    try:
        warn_abs_value = float(warn_abs)
    except (TypeError, ValueError):
        return False
    if direction == "higher_better":
        return candidate_value < warn_abs_value
    return candidate_value > warn_abs_value


def _regression_breaches(
    *,
    regression: bool,
    delta_abs: float,
    delta_pct: float | None,
    min_delta_abs: Any,
    warn_regression_abs: Any,
    fail_regression_abs: Any,
    warn_regression_pct: Any,
    fail_regression_pct: Any,
) -> tuple[str | None, str | None]:
    try:
        min_delta_abs_value = abs(float(min_delta_abs)) if min_delta_abs is not None else 0.0
    except (TypeError, ValueError):
        min_delta_abs_value = 0.0

    try:
        warn_abs_value = abs(float(warn_regression_abs)) if warn_regression_abs is not None else None
    except (TypeError, ValueError):
        warn_abs_value = None

    try:
        fail_abs_value = abs(float(fail_regression_abs)) if fail_regression_abs is not None else None
    except (TypeError, ValueError):
        fail_abs_value = None

    try:
        warn_pct_value = abs(float(warn_regression_pct)) if warn_regression_pct is not None else None
    except (TypeError, ValueError):
        warn_pct_value = None

    try:
        fail_pct_value = abs(float(fail_regression_pct)) if fail_regression_pct is not None else None
    except (TypeError, ValueError):
        fail_pct_value = None

    if not regression:
        return None, None

    if fail_abs_value is not None and abs(delta_abs) >= max(min_delta_abs_value, fail_abs_value):
        return "fail", "regression_abs_exceeds_fail"
    if fail_pct_value is not None and delta_pct is not None and abs(delta_pct) >= fail_pct_value and abs(delta_abs) >= min_delta_abs_value:
        return "fail", f"regression_pct_exceeds_{int(round(fail_pct_value * 100))}"
    if warn_abs_value is not None and abs(delta_abs) >= max(min_delta_abs_value, warn_abs_value):
        return "warn", "regression_abs_exceeds_warn"
    if warn_pct_value is not None and delta_pct is not None and abs(delta_pct) >= warn_pct_value and abs(delta_abs) >= min_delta_abs_value:
        return "warn", f"regression_pct_exceeds_{int(round(warn_pct_value * 100))}"
    return None, None


def _noise_label(ci_width_pct: float | None, outlier_ratio: float, has_bootstrap: bool) -> str:
    if not has_bootstrap:
        return "unknown"
    if ci_width_pct is None:
        return "unknown"
    if ci_width_pct <= 0.05 and outlier_ratio <= 0.1:
        return "low"
    if ci_width_pct <= 0.2:
        return "moderate"
    return "high"


def _reliability(
    *,
    baseline_stats: Mapping[str, Any],
    candidate_stats: Mapping[str, Any],
    recommended_runs: int,
    baseline_thermal_nominal: bool,
    candidate_thermal_nominal: bool,
    evidence_level: str | None,
    required_evidence_level: str | None,
    bootstrap_ci: Sequence[float] | None,
) -> dict[str, Any]:
    min_runs = min(int(baseline_stats["n"]), int(candidate_stats["n"]))
    score = 1.0
    reasons: list[str] = []

    if min_runs < recommended_runs:
        score -= 0.2
        reasons.append("run_count_below_recommended")

    combined_outlier_ratio = 0.0
    total_runs = int(baseline_stats["n"]) + int(candidate_stats["n"])
    if total_runs:
        combined_outlier_ratio = (int(baseline_stats["outlierCount"]) + int(candidate_stats["outlierCount"])) / total_runs
    if combined_outlier_ratio > 0.2:
        score -= 0.2
        reasons.append("outlier_ratio_high")

    if not baseline_thermal_nominal or not candidate_thermal_nominal:
        score -= 0.2
        reasons.append("thermal_state_not_nominal")

    evidence_rank_value = evidence_rank(evidence_level)
    required_rank_value = evidence_rank(required_evidence_level)
    if required_rank_value is not None and (evidence_rank_value is None or evidence_rank_value < required_rank_value):
        score -= 0.2
        reasons.append("evidence_below_required")

    ci_width_pct = None
    if bootstrap_ci and len(bootstrap_ci) == 2:
        lo, hi = float(bootstrap_ci[0]), float(bootstrap_ci[1])
        width = abs(hi - lo)
        baseline_median = float(baseline_stats["median"]) if baseline_stats["median"] is not None else 0.0
        scale = abs(baseline_median) if baseline_median else max(abs(float(candidate_stats["median"] or 0.0)), 1.0)
        ci_width_pct = width / scale
        if ci_width_pct > 0.2:
            score -= 0.2
            reasons.append("ci_width_high")

    score = max(0.0, min(1.0, score))
    level = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    return {
        "score": score,
        "level": level,
        "reasons": reasons,
        "ciWidthPct": ci_width_pct,
    }


def _empty_metric_result(metric_id: str, budget: Mapping[str, Any]) -> dict[str, Any]:
    merged_budget = _budget_for_metric(metric_id, budget)
    return {
        "priority": _metric_priority(metric_id),
        "unit": merged_budget.get("unit"),
        "direction": merged_budget.get("direction", "lower_better"),
        "baseline": empty_series_summary(),
        "candidate": empty_series_summary(),
        "delta": {
            "abs": None,
            "pct": None,
            "bootstrapCi95": None,
        },
        "budget": merged_budget,
        "judgement": "unknown",
        "reason": "missing_metric",
        "confidence": {
            "level": "low",
            "bootstrapCi95": None,
            "noise": "unknown",
        },
        "reliability": {
            "score": 0.0,
            "level": "low",
            "reasons": ["missing_metric"],
        },
        "evidence": {
            "level": None,
            "artifacts": [],
            "missingRuns": [],
        },
        "sourceRunIds": [],
    }


def _metric_output(
    metric_id: str,
    *,
    baseline_entries: list[dict[str, Any]],
    candidate_entries: list[dict[str, Any]],
    budget: Mapping[str, Any] | None,
    compare_id: str,
    mode: str,
) -> dict[str, Any]:
    merged_budget = _budget_for_metric(metric_id, budget)
    baseline_values = [entry["value"] for entry in baseline_entries]
    candidate_values = [entry["value"] for entry in candidate_entries]

    baseline_evidence: list[str] = []
    for entry in baseline_entries:
        metric_evidence = entry["metric"].get("evidenceLevel")
        if isinstance(metric_evidence, str):
            baseline_evidence.append(metric_evidence)
        elif isinstance(metric_evidence, Sequence):
            baseline_evidence.extend(level for level in metric_evidence if isinstance(level, str))
        else:
            baseline_evidence.extend(_evidence_levels_from_summary(entry["summary"]))
    candidate_evidence: list[str] = []
    for entry in candidate_entries:
        metric_evidence = entry["metric"].get("evidenceLevel")
        if isinstance(metric_evidence, str):
            candidate_evidence.append(metric_evidence)
        elif isinstance(metric_evidence, Sequence):
            candidate_evidence.extend(level for level in metric_evidence if isinstance(level, str))
        else:
            candidate_evidence.extend(_evidence_levels_from_summary(entry["summary"]))
    evidence_levels = merge_unique([*baseline_evidence, *candidate_evidence])  # type: ignore[arg-type]
    evidence_rank_value = None
    ranks = [rank for rank in (evidence_rank(level) for level in evidence_levels) if rank is not None]
    if ranks:
        evidence_rank_value = min(ranks)
    evidence_level_value = evidence_level_from_rank(evidence_rank_value)

    if baseline_values:
        baseline_stats = summarize_numeric_series(
            baseline_values,
            evidence_rank_value=evidence_rank_value,
            thermal_nominal=all(_thermal_nominal(entry["summary"]) for entry in baseline_entries),
        )
    else:
        baseline_stats = empty_series_summary()
    if candidate_values:
        candidate_stats = summarize_numeric_series(
            candidate_values,
            evidence_rank_value=evidence_rank_value,
            thermal_nominal=all(_thermal_nominal(entry["summary"]) for entry in candidate_entries),
        )
    else:
        candidate_stats = empty_series_summary()

    direction = str(
        _first_non_empty(
            merged_budget.get("direction"),
            baseline_entries[0]["metric"].get("direction") if baseline_entries else None,
            candidate_entries[0]["metric"].get("direction") if candidate_entries else None,
            "lower_better",
        )
    )
    merged_budget.setdefault("direction", direction)
    unit = _first_non_empty(
        merged_budget.get("unit"),
        baseline_entries[0]["metric"].get("unit") if baseline_entries else None,
        candidate_entries[0]["metric"].get("unit") if candidate_entries else None,
    )
    if unit is not None:
        unit = str(unit)
        merged_budget.setdefault("unit", unit)

    # stream 输入适配（CM-TASK-024）：isStream 指标附加均值/峰值/p95 三列与 series 分段，
    # 仅展示用，judgement 输入不变（CM-AC-007）
    stream_block = _stream_block(baseline_entries=baseline_entries, candidate_entries=candidate_entries)

    source_run_ids = merge_unique(
        entry["runId"]
        for entry in baseline_entries + candidate_entries
        if isinstance(entry.get("runId"), str)
    )
    artifacts = merge_unique(
        ref
        for entry in baseline_entries + candidate_entries
        for ref in _artifact_refs(entry["summary"], entry.get("metric"))
        if isinstance(ref, str)
    )
    min_runs = min(int(baseline_stats["n"]), int(candidate_stats["n"]))
    required_runs = _positive_int(merged_budget.get("minRuns"), default=5)
    hard_fail_abs = False
    hard_fail_reason = None
    if candidate_stats["n"]:
        if _absolute_fail(direction, float(candidate_stats["median"]), merged_budget.get("failAbs")):
            hard_fail_abs = True
            hard_fail_reason = "candidate_exceeds_fail_abs"
        if metric_id.startswith("stability.crash") and float(candidate_stats["max"] or 0.0) > 0:
            hard_fail_abs = True
            hard_fail_reason = "stability_crash_detected"

    if not baseline_values or not candidate_values:
        missing_output: dict[str, Any] = {
            "priority": _metric_priority(metric_id),
            "unit": unit,
            "direction": direction,
            "baseline": baseline_stats,
            "candidate": candidate_stats,
            "delta": {
                "abs": None,
                "pct": None,
                "bootstrapCi95": None,
            },
            "budget": merged_budget,
            "judgement": "unknown",
            "reason": "missing_metric",
            "confidence": {
                "level": "low",
                "bootstrapCi95": None,
                "noise": "unknown",
            },
            "reliability": {
                "score": 0.0,
                "level": "low",
                "reasons": ["missing_metric"],
            },
            "evidence": {
                "level": evidence_level_value,
                "artifacts": artifacts,
                "missingRuns": [
                    *(
                        entry["runId"]
                        for entry in baseline_entries
                        if not baseline_values
                    ),
                    *(
                        entry["runId"]
                        for entry in candidate_entries
                        if not candidate_values
                    ),
                ],
            },
            "sourceRunIds": source_run_ids,
        }
        if stream_block is not None:
            missing_output["stream"] = stream_block
        return missing_output

    delta_abs, delta_pct, regression = _compare_direction(direction, float(baseline_stats["median"]), float(candidate_stats["median"]))
    bootstrap_ci = None
    if min_runs >= 5:
        deltas = bootstrap_median_delta(
            baseline_values,
            candidate_values,
            seed=stable_seed(compare_id, metric_id, mode, direction),
        )
        if deltas:
            bootstrap_ci = [percentile(deltas, 2.5), percentile(deltas, 97.5)]

    reliability = _reliability(
        baseline_stats=baseline_stats,
        candidate_stats=candidate_stats,
        recommended_runs=_positive_int(merged_budget.get("recommendedRuns"), default=10),
        baseline_thermal_nominal=all(_thermal_nominal(entry["summary"]) for entry in baseline_entries),
        candidate_thermal_nominal=all(_thermal_nominal(entry["summary"]) for entry in candidate_entries),
        evidence_level=evidence_level_value,
        required_evidence_level=merged_budget.get("requiredEvidenceLevel"),
        bootstrap_ci=bootstrap_ci,
    )
    ci_width_pct = reliability.get("ciWidthPct")
    confidence_level = "high"
    if reliability["score"] < 0.5 or min_runs < required_runs:
        confidence_level = "low"
    elif bootstrap_ci is None:
        confidence_level = "medium" if reliability["score"] >= 0.8 else "low"
    elif reliability["score"] < 0.8 or (ci_width_pct is not None and ci_width_pct > 0.2):
        confidence_level = "medium"

    judgement = "pass"
    reason = "within_budget"
    if hard_fail_abs:
        judgement = "fail"
        reason = hard_fail_reason or "candidate_exceeds_fail_abs"
    elif metric_id.startswith("stability.crash") and float(candidate_stats["max"] or 0.0) > 0:
        judgement = "fail"
        reason = "stability_crash_detected"
    elif min_runs < required_runs:
        judgement = "unknown"
        reason = f"sample_size_below_{required_runs}"
    elif direction == "neutral":
        judgement = "pass"
        reason = "neutral_metric"
    else:
        regression_judgement, regression_reason = _regression_breaches(
            regression=regression,
            delta_abs=delta_abs,
            delta_pct=delta_pct,
            min_delta_abs=merged_budget.get("minDeltaAbs"),
            warn_regression_abs=merged_budget.get("warnRegressionAbs"),
            fail_regression_abs=merged_budget.get("failRegressionAbs"),
            warn_regression_pct=merged_budget.get("warnRegressionPct"),
            fail_regression_pct=merged_budget.get("failRegressionPct"),
        )
        noise_overlaps_zero = (
            bootstrap_ci is not None
            and bootstrap_ci[0] <= 0 <= bootstrap_ci[1]
        )
        if regression_judgement == "fail" and not noise_overlaps_zero:
            judgement = "fail"
            reason = regression_reason or "regression_threshold"
        elif _absolute_warn(direction, float(candidate_stats["median"]), merged_budget.get("warnAbs")):
            judgement = "warn"
            reason = "candidate_exceeds_warn_abs"
        elif regression_judgement == "warn" and not noise_overlaps_zero:
            judgement = "warn"
            reason = regression_reason or "regression_threshold"
        elif not reliability["score"] or reliability["score"] < 0.5:
            judgement = "warn" if hard_fail_abs else "unknown"
            reason = "low_reliability"
        elif noise_overlaps_zero:
            judgement = "pass"
            reason = "noise_overlap_zero"

    if reliability["score"] < 0.5 and judgement == "fail" and not hard_fail_abs and reason != "stability_crash_detected":
        judgement = "warn"
        reason = "low_reliability"

    if reliability["score"] < 0.5 and judgement == "pass":
        judgement = "warn"
        reason = "low_reliability"

    noise = _noise_label(ci_width_pct if isinstance(ci_width_pct, float) else None, float(baseline_stats["outlierRatio"] + candidate_stats["outlierRatio"]) / 2.0, bootstrap_ci is not None)

    output: dict[str, Any] = {
        "priority": _metric_priority(metric_id),
        "unit": unit,
        "direction": direction,
        "baseline": baseline_stats,
        "candidate": candidate_stats,
        "delta": {
            "abs": delta_abs,
            "pct": delta_pct,
            "bootstrapCi95": bootstrap_ci,
        },
        "budget": merged_budget,
        "judgement": judgement,
        "reason": reason,
        "confidence": {
            "level": confidence_level,
            "bootstrapCi95": bootstrap_ci,
            "noise": noise,
        },
        "reliability": {
            "score": reliability["score"],
            "level": reliability["level"],
            "reasons": reliability["reasons"],
        },
        "evidence": {
            "level": evidence_level_value,
            "artifacts": artifacts,
            "missingRuns": [],
        },
        "sourceRunIds": source_run_ids,
    }
    if stream_block is not None:
        output["stream"] = stream_block
    return output


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _validate_compare(
    *,
    baseline_summaries: Sequence[Mapping[str, Any]],
    candidate_summaries: Sequence[Mapping[str, Any]],
    mode: str,
) -> list[str]:
    errors: list[str] = []
    if mode not in {"same_device_versions", "same_app_devices"}:
        errors.append(f"unsupported_mode:{mode}")
        return errors

    baseline_device_ids = merge_unique(
        identity for identity in (_identity_from_summary(summary, "device") for summary in baseline_summaries) if identity is not None
    )
    candidate_device_ids = merge_unique(
        identity for identity in (_identity_from_summary(summary, "device") for summary in candidate_summaries) if identity is not None
    )
    baseline_app_ids = merge_unique(
        identity for identity in (_identity_from_summary(summary, "app") for summary in baseline_summaries) if identity is not None
    )
    candidate_app_ids = merge_unique(
        identity for identity in (_identity_from_summary(summary, "app") for summary in candidate_summaries) if identity is not None
    )
    baseline_scenarios = merge_unique(
        identity for identity in (_identity_from_summary(summary, "scenario") for summary in baseline_summaries) if identity is not None
    )
    candidate_scenarios = merge_unique(
        identity for identity in (_identity_from_summary(summary, "scenario") for summary in candidate_summaries) if identity is not None
    )
    baseline_windows = _focused_windows_from_summaries(baseline_summaries)
    candidate_windows = _focused_windows_from_summaries(candidate_summaries)
    baseline_window_families = merge_unique(_focused_window_family(window) for window in baseline_windows)
    candidate_window_families = merge_unique(_focused_window_family(window) for window in candidate_windows)

    if len(baseline_device_ids) > 1:
        errors.append("baseline_device_inconsistent")
    if len(candidate_device_ids) > 1:
        errors.append("candidate_device_inconsistent")
    if len(baseline_app_ids) > 1:
        errors.append("baseline_app_inconsistent")
    if len(candidate_app_ids) > 1:
        errors.append("candidate_app_inconsistent")
    if len(baseline_scenarios) > 1:
        errors.append("baseline_scenario_inconsistent")
    if len(candidate_scenarios) > 1:
        errors.append("candidate_scenario_inconsistent")
    if len(baseline_window_families) > 1:
        errors.append("baseline_focused_window_inconsistent")
    if len(candidate_window_families) > 1:
        errors.append("candidate_focused_window_inconsistent")

    if baseline_scenarios and candidate_scenarios and set(baseline_scenarios) != set(candidate_scenarios):
        errors.append("scenario_mismatch")
    if baseline_window_families and candidate_window_families and set(baseline_window_families) != set(candidate_window_families):
        errors.append("focused_window_mismatch")

    if mode == "same_device_versions":
        if baseline_device_ids and candidate_device_ids and set(baseline_device_ids) != set(candidate_device_ids):
            errors.append("device_mismatch")
        if baseline_app_ids and candidate_app_ids and set(baseline_app_ids) == set(candidate_app_ids):
            errors.append("app_version_not_changed")
    elif mode == "same_app_devices":
        if baseline_app_ids and candidate_app_ids and set(baseline_app_ids) != set(candidate_app_ids):
            errors.append("app_mismatch")
        if baseline_device_ids and candidate_device_ids and set(baseline_device_ids) == set(candidate_device_ids):
            errors.append("device_not_changed")

    return errors


def _invalid_result(
    *,
    compare_id: str,
    mode: str,
    focused_windows: Mapping[str, list[str]],
    errors: Sequence[str],
) -> dict[str, Any]:
    """invalid 形态（两路与 N 路共用骨架；字段语义与既有 2 路一致）。"""
    return {
        "schemaVersion": "perfeval.compare.v1",
        "compareId": compare_id,
        "mode": mode,
        "baselineGroup": "baseline",
        "candidateGroup": "candidate",
        "status": "invalid",
        "errors": list(errors),
        "focusedWindows": focused_windows,
        "metrics": {},
        "reliability": {
            "score": 0.0,
            "level": "low",
            "reasons": list(errors),
        },
    }


def _compare_n_groups(
    groups: Sequence[Sequence[Mapping[str, Any]] | Mapping[str, Any]] | list[Any],
    *,
    mode: str,
    budget: Mapping[str, Any] | None,
    compare_id: str | None,
) -> dict[str, Any]:
    """N 路判定内核（groups 2–10 组；首组=全局基线，其余逐组与基线两两判定）。

    组间语义（契约 §9 note 同源声明）：每指标 judgement/reason 取各对中最差
    （fail > warn > unknown > pass，同分取先出现的对）；``baseline``/``candidate``
    顶层字段沿用最差对的两侧统计（两路语义可直接读，其余组经 ``pairs``/``perGroup``
    读取）；``reliability`` 取各对最小分（保守），reasons 并集去重；
    ``sourceRunIds``/``evidence.artifacts`` 覆盖全部组。
    """
    group_lists: list[list[Mapping[str, Any]]] = []
    for group in groups:
        group_lists.append(
            [summary for summary in _as_summary_list(group) if summary.get("status") != "invalid"]
        )
    if len(group_lists) > N_WAY_MAX_GROUPS:
        return _invalid_result(
            compare_id=compare_id or f"compare-{mode}-{len(group_lists)}g",
            mode=mode,
            focused_windows={},
            errors=[f"too_many_groups:{len(group_lists)}"],
        )
    baseline_list = group_lists[0]
    if not baseline_list or len(group_lists) < 2:
        return _invalid_result(
            compare_id=compare_id or f"compare-{mode}-{len(group_lists)}g",
            mode=mode,
            focused_windows={},
            errors=["baseline_or_candidate_missing"],
        )
    compare_id = compare_id or f"compare-{mode}-{len(group_lists)}g"

    # 前提校验：基线自洽 + 每个非基线组与基线的同款校验（错误码并集去重）
    validation_errors: list[str] = []
    for index, group in enumerate(group_lists[1:], start=1):
        validation_errors.extend(
            _validate_compare(baseline_summaries=baseline_list, candidate_summaries=group, mode=mode)
        )
    validation_errors = merge_unique(validation_errors)  # type: ignore[arg-type]
    focused_windows = {
        "baseline": _focused_windows_from_summaries(baseline_list),
        "candidate": _focused_windows_from_summaries(
            [summary for group in group_lists[1:] for summary in group]
        ),
    }
    if validation_errors:
        return _invalid_result(
            compare_id=compare_id,
            mode=mode,
            focused_windows=focused_windows,
            errors=validation_errors,
        )

    metric_ids = sorted(
        set().union(*(set(_summary_metrics(summary).keys()) for group in group_lists for summary in group))
    )
    metrics: dict[str, dict[str, Any]] = {}
    total_reliability_scores: list[float] = []
    statuses: list[str] = []
    unknown_metrics: list[str] = []

    for metric_id in metric_ids:
        baseline_entries = _metric_entries(baseline_list, metric_id)
        pair_outputs: list[tuple[int, list[dict[str, Any]], dict[str, Any]]] = []
        for index, group in enumerate(group_lists[1:], start=1):
            entries = _metric_entries(group, metric_id)
            output = _metric_output(
                metric_id,
                baseline_entries=baseline_entries,
                candidate_entries=entries,
                budget=budget,
                compare_id=compare_id,
                mode=mode,
            )
            pair_outputs.append((index, entries, output))
        merged = _merge_metric_nway(metric_id, baseline_entries, pair_outputs)
        metrics[metric_id] = merged
        total_reliability_scores.append(float(merged["reliability"]["score"]))
        statuses.append(merged["judgement"])
        if merged["judgement"] == "unknown":
            unknown_metrics.append(metric_id)

    if not metrics:
        overall_status = "unknown"
    elif any(status == "fail" for status in statuses):
        overall_status = "fail"
    elif any(status == "warn" for status in statuses):
        overall_status = "warn"
    elif any(status == "unknown" for status in statuses):
        overall_status = "unknown"
    else:
        overall_status = "pass"

    overall_score = min(total_reliability_scores) if total_reliability_scores else 0.0
    overall_level = confidence_level_from_score(overall_score)

    return {
        "schemaVersion": "perfeval.compare.v1",
        "compareId": compare_id,
        "mode": mode,
        "baselineGroup": "baseline",
        "candidateGroup": "candidate",
        "groups": [
            {"index": index, "runIds": [str(summary.get("runId")) for summary in group if summary.get("runId") is not None], "runCount": len(group)}
            for index, group in enumerate(group_lists)
        ],
        "status": overall_status,
        "focusedWindows": focused_windows,
        "metrics": metrics,
        "summary": {
            "baselineRuns": len(baseline_list),
            "candidateRuns": sum(len(group) for group in group_lists[1:]),
            "groupCounts": [len(group) for group in group_lists],
            "metricCount": len(metrics),
            "unknownMetrics": unknown_metrics,
        },
        "reliability": {
            "score": overall_score,
            "level": overall_level,
            "reasons": merge_unique(
                reason
                for metric in metrics.values()
                for reason in metric["reliability"]["reasons"]
            ),
        },
    }


def _merge_metric_nway(
    metric_id: str,
    baseline_entries: list[dict[str, Any]],
    pair_outputs: list[tuple[int, list[dict[str, Any]], dict[str, Any]]],
) -> dict[str, Any]:
    """N 路单指标合并：最差对作 headline（baseline/candidate/delta/judgement/reason/
    confidence/budget 原样），reliability 取各对最小分、sourceRunIds/artifacts 全组并集；
    附 §11.1 可选字段 ``pairs``（逐对判定明细）与 ``perGroup``（逐组序列统计，UI 呈现面）。"""
    worst_pair = max(
        pair_outputs,
        key=lambda pair: (_PAIR_RANK.get(pair[2]["judgement"], 1), -pair[0]),
    )
    _worst_index, _worst_entries, headline = worst_pair
    merged: dict[str, Any] = dict(headline)

    scores = [float(output["reliability"]["score"]) for _index, _entries, output in pair_outputs]
    min_score = min(scores) if scores else 0.0
    merged["reliability"] = {
        "score": min_score,
        "level": confidence_level_from_score(min_score),
        "reasons": merge_unique(
            reason
            for _index, _entries, output in pair_outputs
            for reason in output["reliability"]["reasons"]
        ),
    }

    merged["sourceRunIds"] = merge_unique(
        [
            *(entry["runId"] for entry in baseline_entries if isinstance(entry.get("runId"), str)),
            *(
                entry["runId"]
                for _index, entries, _output in pair_outputs
                for entry in entries
                if isinstance(entry.get("runId"), str)
            ),
        ]
    )
    merged["evidence"] = {
        "level": headline["evidence"]["level"],
        "artifacts": merge_unique(
            artifact
            for _index, _entries, output in pair_outputs
            for artifact in output["evidence"]["artifacts"]
        ),
        "missingRuns": merge_unique(
            missing
            for _index, _entries, output in pair_outputs
            for missing in output["evidence"]["missingRuns"]
        ),
    }
    merged["pairs"] = [
        {
            "groupIndex": index,
            "runIds": [entry["runId"] for entry in entries],
            "judgement": output["judgement"],
            "reason": output["reason"],
            "delta": dict(output["delta"]),
            "confidence": dict(output["confidence"]),
            "reliability": dict(output["reliability"]),
        }
        for index, entries, output in pair_outputs
    ]
    # perGroup[0] = 全局基线组（任一对的 baseline 侧统计同源）；其余组取各自 candidate 侧
    per_group: list[dict[str, Any]] = [
        {"groupIndex": 0, **_series_stats_projection(baseline_entries)}
    ]
    for index, entries, _output in pair_outputs:
        per_group.append({"groupIndex": index, **_series_stats_projection(entries)})
    merged["perGroup"] = per_group
    return merged


def _series_stats_projection(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """单组 entries → N 路 perGroup 展示投影（无数据组各值 null，不填假零值）。"""
    values = [entry["value"] for entry in entries]
    if not values:
        return {"n": 0, "median": None, "mean": None, "min": None, "max": None, "p95": None}
    stats = summarize_numeric_series(values)
    return {
        "n": int(stats["n"]),
        "median": stats["median"],
        "mean": stats["mean"],
        "min": stats["min"],
        "max": stats["max"],
        "p95": stats["p95"],
    }


def compare_summaries(
    baseline_summaries: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    candidate_summaries: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    mode: str = "same_device_versions",
    budget: Mapping[str, Any] | None = None,
    compare_id: str | None = None,
    baseline_group: str = "baseline",
    candidate_group: str = "candidate",
    groups: Sequence[Sequence[Mapping[str, Any]] | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """两路（baseline/candidate，既有语义不变）或 N 路（groups 2–10 组）判定。

    N 路（groups 非空时优先，baseline/candidate 形参被忽略）：以**首组为全局基线**，
    其余各组逐一与基线两两判定（组间语义见 N_WAY_MAX_GROUPS 注，契约 §9 note 同源）；
    每指标顶层 judgement 取各对最差，``pairs``/``perGroup``/顶层 ``groups`` 为
    §11.1 新增可选字段；两路路径输出逐字节不变（回归保障）。
    """
    if groups is not None:
        return _compare_n_groups(list(groups), mode=mode, budget=budget, compare_id=compare_id)
    baseline_list = [summary for summary in _as_summary_list(baseline_summaries) if summary.get("status") != "invalid"]
    candidate_list = [summary for summary in _as_summary_list(candidate_summaries) if summary.get("status") != "invalid"]
    focused_windows = {
        "baseline": _focused_windows_from_summaries(baseline_list),
        "candidate": _focused_windows_from_summaries(candidate_list),
    }

    compare_id = compare_id or f"compare-{mode}-{baseline_group}-{candidate_group}"
    if not baseline_list or not candidate_list:
        return {
            "schemaVersion": "perfeval.compare.v1",
            "compareId": compare_id,
            "mode": mode,
            "baselineGroup": baseline_group,
            "candidateGroup": candidate_group,
            "status": "invalid",
            "errors": ["baseline_or_candidate_missing"],
            "focusedWindows": focused_windows,
            "metrics": {},
            "reliability": {
                "score": 0.0,
                "level": "low",
                "reasons": ["baseline_or_candidate_missing"],
            },
        }

    validation_errors = _validate_compare(
        baseline_summaries=baseline_list,
        candidate_summaries=candidate_list,
        mode=mode,
    )
    if validation_errors:
        return {
            "schemaVersion": "perfeval.compare.v1",
            "compareId": compare_id,
            "mode": mode,
            "baselineGroup": baseline_group,
            "candidateGroup": candidate_group,
            "status": "invalid",
            "errors": validation_errors,
            "focusedWindows": focused_windows,
            "metrics": {},
            "reliability": {
                "score": 0.0,
                "level": "low",
                "reasons": validation_errors,
            },
        }

    metric_ids = sorted(
        set().union(*(set(_summary_metrics(summary).keys()) for summary in baseline_list + candidate_list))
    )
    metrics: dict[str, dict[str, Any]] = {}
    total_reliability_scores: list[float] = []
    statuses: list[str] = []
    unknown_metrics: list[str] = []

    for metric_id in metric_ids:
        baseline_entries = _metric_entries(baseline_list, metric_id)
        candidate_entries = _metric_entries(candidate_list, metric_id)
        metric_output = _metric_output(
            metric_id,
            baseline_entries=baseline_entries,
            candidate_entries=candidate_entries,
            budget=budget,
            compare_id=compare_id,
            mode=mode,
        )
        metrics[metric_id] = metric_output
        total_reliability_scores.append(float(metric_output["reliability"]["score"]))
        statuses.append(metric_output["judgement"])
        if metric_output["judgement"] == "unknown":
            unknown_metrics.append(metric_id)

    if not metrics:
        overall_status = "unknown"
    elif any(status == "fail" for status in statuses):
        overall_status = "fail"
    elif any(status == "warn" for status in statuses):
        overall_status = "warn"
    elif any(status == "unknown" for status in statuses):
        overall_status = "unknown"
    else:
        overall_status = "pass"

    overall_score = min(total_reliability_scores) if total_reliability_scores else 0.0
    overall_level = confidence_level_from_score(overall_score)

    return {
        "schemaVersion": "perfeval.compare.v1",
        "compareId": compare_id,
        "mode": mode,
        "baselineGroup": baseline_group,
        "candidateGroup": candidate_group,
        "status": overall_status,
        "focusedWindows": focused_windows,
        "metrics": metrics,
        "summary": {
            "baselineRuns": len(baseline_list),
            "candidateRuns": len(candidate_list),
            "metricCount": len(metrics),
            "unknownMetrics": unknown_metrics,
        },
        "reliability": {
            "score": overall_score,
            "level": overall_level,
            "reasons": merge_unique(
                reason
                for metric in metrics.values()
                for reason in metric["reliability"]["reasons"]
            ),
        },
    }


build_compare = compare_summaries
compare_run_groups = compare_summaries


__all__ = [
    "build_compare",
    "compare_run_groups",
    "compare_summaries",
]
