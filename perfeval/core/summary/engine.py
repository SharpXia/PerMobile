from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from perfeval.core._stats import (
    evidence_level_from_rank,
    evidence_rank,
    merge_unique,
    numeric_value_from_mapping,
    stream_confidence,
    summarize_numeric_series,
)

# stream 判定与 series 输出阈值（CM-001 §3）：事件 type=counter 且观测数 >= 32 视为连续序列
STREAM_MIN_OBSERVATIONS = 32
SERIES_MAX_POINTS = 600
SAMPLES_KEEP_HEAD = 3
SAMPLES_KEEP_TAIL = 3
# peak 类指标（CM-001 §5）：聚合值取序列真实最大值，兑现 "peak" 语义。
# thermal.max.c：契约口径「最高温区温度」（METRIC_SOURCES §3 / REPORT_PRESENTATION
# §展示映射），采样层每温区各记一条（tags.zone）——gpu-1 修复（2026-11-04 体检）：
# 此前不在白名单，headline 退化为全温区混合中位数，系统性低估峰值（真机实测
# 42.1°C 峰值只显示 30.8°C）。
PEAK_AGGREGATION_METRICS = {"memory.pss.peak_mb", "memory.rss.peak_mb", "thermal.max.c"}
# 积分型指标（power-2 修复，2026-11-04 体检）：记录 = 单窗口能耗增量，会话总量
# = 序列求和——通用聚合取中位数会把「估算能耗 X mAh」展示成单窗/中点值
# （300 窗真值 4368 mAh 只显示 14.5 mAh，量级差数百倍）。
INTEGRAL_AGGREGATION_METRICS = {"power.energy.mah"}

# FPS 派生（CM-TASK-009，METRIC_SOURCES §1/§4）：逐帧 counter 事件在 summary 层派生
# frame.fps.mean/median（metric、L1、higher_better）与 jankSegments（连续 >= 3 个
# jank 帧的帧序段，供报告掉帧高亮）；fpsSource 标注来源（tp/framestats）。
FRAME_DETAIL_METRIC_ID = "frame.duration.detail.ms"
FRAME_FPS_MEAN_METRIC_ID = "frame.fps.mean"
FRAME_FPS_MEDIAN_METRIC_ID = "frame.fps.median"
JANK_SEGMENT_MIN_CONSECUTIVE = 3
_FRAME_DERIVED_EVIDENCE_LEVEL = "L1"
# 帧耗时分布直方图最大桶数（CM-TASK-010 报告分布图初版）
FRAME_DISTRIBUTION_MAX_BINS = 20
# FPS 来源链降级诊断（collector 产出，随事件流转入 summary diagnostics）
FRAME_CHAIN_DIAGNOSTIC_NAMES = frozenset({"trace_processor.unavailable", "fps.source.degraded"})


def _normalize_evidence_levels(
    evidence_level: Sequence[str] | str | None,
    inferred_levels: Iterable[object],
) -> list[str]:
    if evidence_level is None:
        inferred: list[str] = [level for level in inferred_levels if isinstance(level, str)]
        ordered = merge_unique(inferred)
        return sorted(
            ordered,
            key=lambda level: evidence_rank(level) if evidence_rank(level) is not None else 99,
        )  # type: ignore[return-value]
    if isinstance(evidence_level, str):
        return [evidence_level]
    return merge_unique(evidence_level)  # type: ignore[return-value]


def _extract_from_timeline_event(event: Mapping[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    metric_id = event.get("metricId") or event.get("name")
    if not isinstance(metric_id, str) or not metric_id:
        return None, None

    payload = event.get("data")
    if not isinstance(payload, Mapping):
        payload = {}

    value = numeric_value_from_mapping(payload)
    unit = payload.get("unit")
    direction = payload.get("direction")
    source_event_id = event.get("eventId")
    source_events = [source_event_id] if isinstance(source_event_id, str) else []
    artifact_refs = payload.get("artifactRefs") or payload.get("artifactRef") or []
    if isinstance(artifact_refs, str):
        artifact_refs = [artifact_refs]
    evidence_level = event.get("source", {}).get("evidenceLevel") if isinstance(event.get("source"), Mapping) else None
    event_run_id = event.get("runId") if isinstance(event.get("runId"), str) else None

    if value is None and event.get("type") == "interval" and isinstance(event.get("durationNs"), (int, float)):
        value = float(event["durationNs"]) / 1_000_000.0
        if unit is None:
            unit = "ms"
    elif value is None and isinstance(payload.get("frameDurationMs"), (int, float)):
        value = float(payload["frameDurationMs"])
        if unit is None:
            unit = "ms"
    elif value is None and isinstance(payload.get("durationMs"), (int, float)):
        value = float(payload["durationMs"])
        if unit is None:
            unit = "ms"

    if value is None:
        return None, None

    event_type = event.get("type") if isinstance(event.get("type"), str) else None
    ts_monotonic_ns = event.get("tsMonotonicNs")
    sampling_window_ms = payload.get("samplingWindowMs")
    # 非保留 tag（如 thermal 的 zone）随观测透传，供 stream series 点携带（CM-TASK-019）
    event_tags = event.get("tags")

    return metric_id, {
        "value": value,
        "unit": unit,
        "direction": direction,
        "sourceEvents": source_events,
        "artifactRefs": list(artifact_refs),
        "evidenceLevel": evidence_level,
        "runId": event_run_id,
        "eventType": event_type,
        "tsMonotonicNs": ts_monotonic_ns if isinstance(ts_monotonic_ns, (int, float)) and not isinstance(ts_monotonic_ns, bool) else None,
        "samplingWindowMs": sampling_window_ms
        if isinstance(sampling_window_ms, (int, float)) and not isinstance(sampling_window_ms, bool)
        else None,
        "tags": {str(key): event_tags[key] for key in event_tags}
        if isinstance(event_tags, Mapping) and event_tags
        else None,
    }


def _normalize_metric_inputs(
    metric_inputs: Mapping[str, Sequence[Any]] | None,
    timeline_events: Sequence[Mapping[str, Any]] | None,
) -> dict[str, list[dict[str, Any]]]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    if metric_inputs:
        for metric_id, observations in metric_inputs.items():
            if observations is None:
                continue
            bucket = normalized.setdefault(str(metric_id), [])
            for observation in observations:
                if isinstance(observation, Mapping):
                    payload = dict(observation)
                    if "sourceEvents" in payload and isinstance(payload["sourceEvents"], str):
                        payload["sourceEvents"] = [payload["sourceEvents"]]
                    if "artifactRefs" in payload and isinstance(payload["artifactRefs"], str):
                        payload["artifactRefs"] = [payload["artifactRefs"]]
                    bucket.append(payload)
                else:
                    bucket.append({"value": observation})

    if timeline_events:
        for event in timeline_events:
            metric_id, observation = _extract_from_timeline_event(event)
            if metric_id is None or observation is None:
                continue
            normalized.setdefault(metric_id, []).append(observation)

    return normalized


def _metric_confidence(
    count: int,
    *,
    evidence_levels: Sequence[str] | None = None,
    outlier_ratio: float = 0.0,
) -> str:
    evidence_rank_value = None
    if evidence_levels:
        ranks = [rank for rank in (evidence_rank(level) for level in evidence_levels) if rank is not None]
        if ranks:
            evidence_rank_value = max(ranks)

    if count <= 1:
        base = 0.25
    elif count < 3:
        base = 0.5
    elif count < 5:
        base = 0.7
    else:
        base = 0.9

    if outlier_ratio > 0.2:
        base -= 0.2
    if evidence_rank_value is not None and evidence_rank_value < 2:
        base -= 0.1

    if base >= 0.8:
        return "high"
    if base >= 0.5:
        return "medium"
    return "low"


def _stream_series_points(counter_observations: list[tuple[dict[str, Any], float]]) -> list[dict[str, Any]]:
    """把 counter 观测转为 series 点：ts 取 tsMonotonicNs 的毫秒值（相对 run 起点）。

    观测携带非空 tags（如 thermal 的 zone）时点上附加 tags 字段（CM-TASK-019
    最小扩展），供报告按 tag 拆分序列；无 tags 的点保持 {ts, v} 原形态。
    """
    points: list[dict[str, Any]] = []
    for observation, value in counter_observations:
        ts_ns = observation.get("tsMonotonicNs")
        ts_ms = int(ts_ns / 1_000_000) if isinstance(ts_ns, (int, float)) and not isinstance(ts_ns, bool) else 0
        point: dict[str, Any] = {"ts": ts_ms, "v": value}
        tags = observation.get("tags")
        if isinstance(tags, Mapping) and tags:
            point["tags"] = dict(tags)
        points.append(point)
    return points


def _downsample_series(
    points: list[dict[str, Any]],
    *,
    max_points: int = SERIES_MAX_POINTS,
) -> tuple[list[dict[str, Any]], bool]:
    """分桶降采样（bucket = ceil(n/max_points)），保峰：每桶保留距桶均值更远的 min/max 点（等距取 max）。"""
    total = len(points)
    if total <= max_points:
        return points, False
    bucket_size = math.ceil(total / max_points)
    downsampled: list[dict[str, Any]] = []
    for start in range(0, total, bucket_size):
        bucket = points[start : start + bucket_size]
        if len(bucket) == 1:
            downsampled.append(bucket[0])
            continue
        bucket_values = [point["v"] for point in bucket]
        bucket_min = min(bucket_values)
        bucket_max = max(bucket_values)
        bucket_mean = sum(bucket_values) / len(bucket_values)
        keep_value = bucket_max if (bucket_max - bucket_mean) >= (bucket_mean - bucket_min) else bucket_min
        downsampled.append(next(point for point in bucket if point["v"] == keep_value))
    return downsampled, True


def _truncate_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """stream 指标 samples 只保留首尾各 3 条，防止 summary 膨胀。

    跨 run 合并时按 run 分组各自截断：趋势报告按 runId 取样本中位数画线，
    全局截断会让中间 repeat 断点、首尾 repeat 失真。
    """
    samples_by_run: dict[Any, list[dict[str, Any]]] = {}
    for sample in samples:
        samples_by_run.setdefault(sample.get("runId"), []).append(sample)
    if len(samples_by_run) <= 1:
        if len(samples) <= SAMPLES_KEEP_HEAD + SAMPLES_KEEP_TAIL:
            return samples
        return samples[:SAMPLES_KEEP_HEAD] + samples[-SAMPLES_KEEP_TAIL:]
    truncated: list[dict[str, Any]] = []
    for run_samples in samples_by_run.values():
        if len(run_samples) <= SAMPLES_KEEP_HEAD + SAMPLES_KEEP_TAIL:
            truncated.extend(run_samples)
        else:
            truncated.extend(run_samples[:SAMPLES_KEEP_HEAD])
            truncated.extend(run_samples[-SAMPLES_KEEP_TAIL:])
    return truncated


def _stream_coverage(
    counter_observations: list[tuple[dict[str, Any], float]],
    *,
    default_run_id: str,
) -> float | None:
    """coverage = 采样时间跨度 / 声明采样窗口（事件 data.samplingWindowMs），无窗口声明时不计算。

    跨 run 合并时按 run 分组取各 run 覆盖率的最大值。
    """
    window_ms = next(
        (
            observation.get("samplingWindowMs")
            for observation, _value in counter_observations
            if isinstance(observation.get("samplingWindowMs"), (int, float))
            and not isinstance(observation.get("samplingWindowMs"), bool)
            and observation["samplingWindowMs"] > 0
        ),
        None,
    )
    if window_ms is None:
        return None

    first_ts_by_run: dict[str, float] = {}
    last_ts_by_run: dict[str, float] = {}
    for observation, _value in counter_observations:
        ts_ns = observation.get("tsMonotonicNs")
        if not isinstance(ts_ns, (int, float)) or isinstance(ts_ns, bool):
            continue
        run_key = str(observation.get("runId") or default_run_id)
        ts_value = float(ts_ns)
        if run_key not in first_ts_by_run:
            first_ts_by_run[run_key] = ts_value
        last_ts_by_run[run_key] = ts_value
    if not first_ts_by_run:
        return None

    coverages = [
        (last_ts_by_run[run_key] - first_ts_by_run[run_key]) / (float(window_ms) * 1_000_000.0)
        for run_key in first_ts_by_run
    ]
    return max(coverage for coverage in coverages if coverage >= 0) if any(c >= 0 for c in coverages) else None


def _derive_frame_metrics(
    timeline_events: Sequence[Mapping[str, Any]] | None,
    *,
    run_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """从逐帧 counter 事件派生 FPS 指标与掉帧区间（CM-TASK-009/013）。

    口径（METRIC_SOURCES §1「FPS 派生」/§4 总表）：
    - ``frame.fps.mean = 1000 / mean(frame_duration_ms)``、median 同理；
      metric 事件、evidenceLevel L1、direction higher_better；
    - ``jankSegments``：连续 >= 3 个 jank 帧的帧序段（startIndex/endIndex/frameCount），
      挂 summary 根字段供报告掉帧高亮；
    - ``fpsSource``：逐帧事件 fpsSource 标注（tp/framestats）唯一时提升为 summary
      字段；无逐帧事件但聚合事件标注 aggregate 时置 "aggregate"（降级 C）。

    第三返回值为 FPS 来源链降级诊断事件（trace_processor.unavailable /
    fps.source.degraded），由调用方并入 summary diagnostics。

    无逐帧事件时返回空指标（聚合兜底路径不产 FPS 指标）。
    """
    durations: list[float] = []
    jank_flags: list[bool] = []
    order_keys: list[int] = []
    epoch_times: list[float | None] = []
    source_event_ids: list[str] = []
    artifact_refs: list[str] = []
    fps_sources: list[str] = []
    chain_diagnostics: list[dict[str, Any]] = []
    aggregate_annotated = False
    position = 0
    for event in timeline_events or []:
        if not isinstance(event, Mapping):
            continue
        event_name = event.get("name")
        event_type = event.get("type")
        if event_type == "diagnostic":
            if event_name in FRAME_CHAIN_DIAGNOSTIC_NAMES:
                payload = event.get("data") if isinstance(event.get("data"), Mapping) else {}
                chain_diagnostics.append(
                    {
                        "schemaVersion": "perfeval.timeline.v1",
                        "seq": 0,
                        "runId": event.get("runId") or run_id,
                        "eventId": f"{run_id}-frame-chain-{len(chain_diagnostics):03d}",
                        "type": "diagnostic",
                        "name": str(event_name),
                        "tsMonotonicNs": 0,
                        "source": {"platform": "host", "collector": "android.artifacts", "evidenceLevel": "L0"},
                        "data": {
                            "severity": str(payload.get("severity") or "warning"),
                            "code": str(payload.get("code") or event_name),
                            "message": str(payload.get("message") or event_name),
                        },
                    }
                )
            continue
        if event_name != FRAME_DETAIL_METRIC_ID:
            payload = event.get("data") if isinstance(event.get("data"), Mapping) else {}
            if (
                isinstance(event_name, str)
                and event_name.startswith("frame.")
                and payload.get("fpsSource") == "aggregate"
            ):
                aggregate_annotated = True
            continue
        if event_type is not None and event_type != "counter":
            continue
        payload = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        value = payload.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        durations.append(float(value))
        jank_flags.append(payload.get("jank") is True)
        frame_index = payload.get("frameIndex")
        order_keys.append(frame_index if isinstance(frame_index, int) and not isinstance(frame_index, bool) else position)
        frame_epoch = payload.get("tsEpochMs")
        epoch_times.append(
            float(frame_epoch)
            if isinstance(frame_epoch, (int, float)) and not isinstance(frame_epoch, bool)
            else None
        )
        event_id = event.get("eventId")
        if isinstance(event_id, str):
            source_event_ids.append(event_id)
        refs = payload.get("artifactRefs") or []
        if isinstance(refs, str):
            refs = [refs]
        artifact_refs.extend(ref for ref in refs if isinstance(ref, str))
        source = payload.get("fpsSource")
        if isinstance(source, str) and source:
            fps_sources.append(source)
        position += 1

    if not durations:
        extras: dict[str, Any] = {}
        if aggregate_annotated:
            extras["fpsSource"] = "aggregate"
        return {}, extras, chain_diagnostics

    mean_ms = sum(durations) / len(durations)
    median_ms = statistics.median(durations)
    source_events = merge_unique(source_event_ids)
    artifact_refs_merged = merge_unique(artifact_refs)
    confidence = _metric_confidence(len(durations), evidence_levels=[_FRAME_DERIVED_EVIDENCE_LEVEL])

    def _fps_entry(fps_value: float) -> dict[str, Any]:
        return {
            "value": round(fps_value, 6),
            "unit": "fps",
            "direction": "higher_better",
            "n": len(durations),
            "confidence": confidence,
            "sourceEvents": source_events,
            "artifactRefs": artifact_refs_merged,
            "evidenceLevel": _FRAME_DERIVED_EVIDENCE_LEVEL,
            "derivedFrom": FRAME_DETAIL_METRIC_ID,
            "runId": run_id,
        }

    derived: dict[str, dict[str, Any]] = {}
    if mean_ms > 0:
        derived[FRAME_FPS_MEAN_METRIC_ID] = _fps_entry(1000.0 / mean_ms)
    if median_ms > 0:
        derived[FRAME_FPS_MEDIAN_METRIC_ID] = _fps_entry(1000.0 / median_ms)

    extras: dict[str, Any] = {
        "jankSegments": _jank_segments(order_keys, jank_flags, epoch_times=epoch_times),
        "frameDurationDistribution": _frame_duration_distribution(durations),
    }
    unique_sources = merge_unique(fps_sources)
    if len(unique_sources) == 1:
        extras["fpsSource"] = unique_sources[0]
    return derived, extras, chain_diagnostics


def _frame_duration_distribution(
    durations: Sequence[float],
    *,
    max_bins: int = FRAME_DISTRIBUTION_MAX_BINS,
) -> list[dict[str, Any]]:
    """帧耗时等宽直方图（报告帧耗时分布图初版的数据源）。

    返回 [{"upToMs": 桶上界(ms), "count": 帧数}]；所有帧等耗时（极差为 0）时
    退化为单桶。空输入返回空列表。
    """
    if not durations:
        return []
    low = min(durations)
    high = max(durations)
    if high <= low:
        return [{"upToMs": round(high, 3), "count": len(durations)}]
    width = (high - low) / max_bins
    bins = [0] * max_bins
    for duration in durations:
        index = min(max_bins - 1, int((duration - low) / width))
        bins[index] += 1
    return [
        {"upToMs": round(low + width * (index + 1), 3), "count": count}
        for index, count in enumerate(bins)
    ]


def _jank_segments(
    order_keys: Sequence[int],
    jank_flags: Sequence[bool],
    *,
    epoch_times: Sequence[float | None] = (),
) -> list[dict[str, Any]]:
    """连续 >= JANK_SEGMENT_MIN_CONSECUTIVE 个 jank 帧的帧序段（按 frameIndex 定位）。

    CM-TASK-021：帧事件携带 tsEpochMs（tp + boottime 锚点）时附段起止 epoch 毫秒
    （startTsEpochMs/endTsEpochMs），供报告掉帧区间→录屏时刻跳转；framestats
    路径无 wall-clock，段不含 epoch 字段（跳转按钮由报告侧隐藏）。
    """
    segments: list[dict[str, Any]] = []
    run_start = 0
    while run_start < len(jank_flags):
        if not jank_flags[run_start]:
            run_start += 1
            continue
        run_end = run_start
        while run_end + 1 < len(jank_flags) and jank_flags[run_end + 1]:
            run_end += 1
        run_length = run_end - run_start + 1
        if run_length >= JANK_SEGMENT_MIN_CONSECUTIVE:
            segment: dict[str, Any] = {
                "startIndex": order_keys[run_start],
                "endIndex": order_keys[run_end],
                "frameCount": run_length,
            }
            if epoch_times:
                window = [
                    epoch_times[index]
                    for index in range(run_start, run_end + 1)
                    if index < len(epoch_times) and epoch_times[index] is not None
                ]
                if window:
                    segment["startTsEpochMs"] = int(min(window))
                    segment["endTsEpochMs"] = int(max(window))
            segments.append(segment)
        run_start = run_end + 1
    return segments


def _materialize_artifacts(
    artifacts: Sequence[Mapping[str, Any]] | None,
    metric_map: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if artifacts is not None:
        return [dict(artifact) for artifact in artifacts]

    seen: set[str] = set()
    materialized: list[dict[str, Any]] = []
    for observations in metric_map.values():
        for observation in observations:
            for ref in observation.get("artifactRefs", []) or []:
                if not isinstance(ref, str) or ref in seen:
                    continue
                seen.add(ref)
                materialized.append(
                    {
                        "id": ref.rsplit("/", 1)[-1].split(".", 1)[0],
                        "path": ref,
                    }
                )
    return materialized


def aggregate_summary(
    run_id: str,
    metric_inputs: Mapping[str, Sequence[Any]] | None = None,
    *,
    timeline_events: Sequence[Mapping[str, Any]] | None = None,
    status: str = "completed",
    evidence_level: Sequence[str] | str | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    diagnostics: Sequence[Mapping[str, Any]] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metric_map = _normalize_metric_inputs(metric_inputs, timeline_events)

    summary_metrics: dict[str, dict[str, Any]] = {}
    inferred_evidence_levels: list[str] = []

    for metric_id in sorted(metric_map):
        observations = metric_map[metric_id]
        normalized_observations: list[tuple[dict[str, Any], float]] = []
        for observation in observations:
            value = numeric_value_from_mapping(observation)
            if value is not None:
                normalized_observations.append((observation, value))
        values = [value for _observation, value in normalized_observations]
        if not values:
            continue

        metric_evidence_levels = [
            level
            for level in (
                observation.get("evidenceLevel")
                for observation in observations
            )
            if isinstance(level, str)
        ]
        inferred_evidence_levels.extend(metric_evidence_levels)
        evidence_rank_value = None
        if metric_evidence_levels:
            metric_ranks = [rank for rank in (evidence_rank(level) for level in metric_evidence_levels) if rank is not None]
            if metric_ranks:
                evidence_rank_value = min(metric_ranks)

        stats = summarize_numeric_series(values, evidence_rank_value=evidence_rank_value)
        unit = next((observation.get("unit") for observation in observations if observation.get("unit")), None)
        direction = next((observation.get("direction") for observation in observations if observation.get("direction")), None)
        source_events = merge_unique(
            source_event
            for observation in observations
            for source_event in observation.get("sourceEvents", []) or []
            if isinstance(source_event, str)
        )
        artifact_refs = merge_unique(
            artifact_ref
            for observation in observations
            for artifact_ref in observation.get("artifactRefs", []) or []
            if isinstance(artifact_ref, str)
        )
        confidence = str(stats["confidence"])

        samples = [
            {
                "value": value,
                "runId": observation.get("runId") or run_id,
            }
            for observation, value in normalized_observations
        ]

        counter_observations = [
            (observation, value)
            for observation, value in normalized_observations
            if observation.get("eventType") == "counter"
        ]
        is_stream = len(counter_observations) >= STREAM_MIN_OBSERVATIONS

        # peak 类指标（CM-001 §5）：聚合值取序列真实最大值，兑现 "peak" 语义；
        # 积分型指标（power.energy.mah）：聚合值取序列求和（会话总能耗）
        if metric_id in PEAK_AGGREGATION_METRICS:
            metric_value = stats["max"]
        elif metric_id in INTEGRAL_AGGREGATION_METRICS:
            metric_value = stats["sum"]
        else:
            metric_value = stats["value"]

        metric_entry: dict[str, Any] = {
            "value": metric_value,
            "unit": unit,
            "direction": direction,
            "n": stats["n"],
            "min": stats["min"],
            "max": stats["max"],
            "mean": stats["mean"],
            "median": stats["median"],
            "p75": stats["p75"],
            "p90": stats["p90"],
            "p95": stats["p95"],
            "mad": stats["mad"],
            "outlierCount": stats["outlierCount"],
            "outlierRatio": stats["outlierRatio"],
            "confidence": confidence,
            "sourceEvents": source_events,
            "artifactRefs": artifact_refs,
            "evidenceLevel": evidence_level_from_rank(evidence_rank_value),
            "samples": samples,
        }

        if is_stream:
            series_points = _stream_series_points(counter_observations)
            series, series_downsampled = _downsample_series(series_points)
            metric_entry["series"] = series
            metric_entry["seriesDownsampled"] = series_downsampled
            metric_entry["isStream"] = True
            metric_entry["sampleCount"] = stats["n"]
            metric_entry["samples"] = _truncate_samples(samples)
            # 置信度语义分离（CM-001 §4）：repeatCount 定基数，sampleCount 只影响序列完整性
            samples_per_run: dict[str, int] = {}
            for observation, _value in counter_observations:
                run_key = str(observation.get("runId") or run_id)
                samples_per_run[run_key] = samples_per_run.get(run_key, 0) + 1
            metric_entry["confidence"] = stream_confidence(
                repeat_count=len(samples_per_run),
                sample_count=min(samples_per_run.values()),
                coverage=_stream_coverage(counter_observations, default_run_id=run_id),
                outlier_ratio=float(stats["outlierRatio"]),
                evidence_rank_value=evidence_rank_value,
            )

        summary_metrics[metric_id] = metric_entry

    # FPS 派生（CM-TASK-009）：逐帧 counter 事件 -> frame.fps.mean/median + jankSegments
    derived_frame_metrics, frame_extras, frame_chain_diagnostics = _derive_frame_metrics(
        timeline_events, run_id=run_id
    )
    summary_metrics.update(derived_frame_metrics)
    summary_diagnostics = [dict(diagnostic) for diagnostic in diagnostics or []]
    summary_diagnostics.extend(frame_chain_diagnostics)

    merged_evidence_level = _normalize_evidence_levels(evidence_level, inferred_evidence_levels)
    summary: dict[str, Any] = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": run_id,
        "status": status,
        "evidenceLevel": merged_evidence_level,
        "metrics": summary_metrics,
        "diagnostics": summary_diagnostics,
        "artifacts": _materialize_artifacts(artifacts, metric_map),
    }

    focused_windows = merge_unique(
        payload.get("window")
        for event in timeline_events or []
        if isinstance(event, Mapping)
        for payload in [event.get("data")]
        if isinstance(payload, Mapping) and isinstance(payload.get("window"), str)
    )
    if focused_windows:
        summary["focusedWindows"] = focused_windows
        if len(focused_windows) == 1:
            summary["focusedWindow"] = focused_windows[0]

    if frame_extras:
        summary.update(frame_extras)
    if context:
        summary.update(dict(context))
    return summary


def summarize_timeline(
    run_id: str,
    timeline_events: Sequence[Mapping[str, Any]],
    *,
    status: str = "completed",
    evidence_level: Sequence[str] | str | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    diagnostics: Sequence[Mapping[str, Any]] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return aggregate_summary(
        run_id,
        timeline_events=timeline_events,
        status=status,
        evidence_level=evidence_level,
        artifacts=artifacts,
        diagnostics=diagnostics,
        context=context,
    )


build_summary = aggregate_summary
aggregate_summary_from_metrics = aggregate_summary
aggregate_summary_from_timeline = summarize_timeline
summarize_run = aggregate_summary


__all__ = [
    "aggregate_summary",
    "aggregate_summary_from_metrics",
    "aggregate_summary_from_timeline",
    "build_summary",
    "summarize_run",
    "summarize_timeline",
]
