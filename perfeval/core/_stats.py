from __future__ import annotations

import hashlib
import math
import random
from statistics import mean, median
from typing import Iterable, Mapping, Sequence


EVIDENCE_RANKS = {
    "L0": 0,
    "L1": 1,
    "L2": 2,
    "L3": 3,
    "L4": 4,
}

CONFIDENCE_LEVELS = ("low", "medium", "high")

# stream 指标置信度因子（CM-001 §4）：sampleCount 只影响"序列完整性"，不顶满置信度
STREAM_MIN_SAMPLES = 10
STREAM_MIN_COVERAGE = 0.6


def merge_unique(values: Iterable[object]) -> list[object]:
    seen: set[object] = set()
    merged: list[object] = []
    for value in values:
        if value is None:
            continue
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def evidence_rank(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized in EVIDENCE_RANKS:
            return EVIDENCE_RANKS[normalized]
        upper = normalized.upper()
        if upper in EVIDENCE_RANKS:
            return EVIDENCE_RANKS[upper]
    return None


def evidence_level_from_rank(rank: int | None) -> str | None:
    if rank is None:
        return None
    for level, level_rank in EVIDENCE_RANKS.items():
        if level_rank == rank:
            return level
    return None


def confidence_level_from_score(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def confidence_score(
    sample_count: int,
    *,
    outlier_ratio: float = 0.0,
    evidence_rank_value: int | None = None,
    ci_width_pct: float | None = None,
    thermal_nominal: bool = True,
) -> tuple[str, float]:
    score = 1.0
    if sample_count <= 1:
        score -= 0.75
    elif sample_count < 5:
        score -= 0.25
    if outlier_ratio > 0.2:
        score -= 0.25
    if evidence_rank_value is not None and evidence_rank_value < EVIDENCE_RANKS["L2"]:
        score -= 0.25
    if ci_width_pct is not None and ci_width_pct > 0.2:
        score -= 0.25
    if not thermal_nominal:
        score -= 0.2
    score = max(0.0, min(1.0, score))
    return confidence_level_from_score(score), score


def stream_confidence(
    repeat_count: int,
    sample_count: int,
    *,
    coverage: float | None = None,
    outlier_ratio: float = 0.0,
    evidence_rank_value: int | None = None,
) -> str:
    """stream 指标置信度：repeatCount（跨 run 次数）定基数，sampleCount 只影响序列完整性。

    基数沿用现有分档（<=1 / <3 / <5 / >=5）；单 run 序列点数 < 10 降一档，
    coverage < 0.6 再降一档，禁止 sampleCount 直接顶满 confidence。
    """
    if repeat_count <= 1:
        score = 0.25
    elif repeat_count < 3:
        score = 0.5
    elif repeat_count < 5:
        score = 0.7
    else:
        score = 0.9
    if sample_count < STREAM_MIN_SAMPLES:
        score -= 0.25
    if coverage is not None and coverage < STREAM_MIN_COVERAGE:
        score -= 0.25
    if outlier_ratio > 0.2:
        score -= 0.25
    if evidence_rank_value is not None and evidence_rank_value < EVIDENCE_RANKS["L2"]:
        # 证据等级降档幅度沿用 summary 引擎置信度表（-0.1），避免 L1 采样一票否决
        score -= 0.1
    return confidence_level_from_score(max(0.0, min(1.0, score)))


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        raise ValueError("percentile() requires at least one value")
    if len(values) == 1:
        return float(values[0])
    if pct <= 0:
        return float(values[0])
    if pct >= 100:
        return float(values[-1])
    index = (len(values) - 1) * (pct / 100.0)
    lo = math.floor(index)
    hi = math.ceil(index)
    if lo == hi:
        return float(values[lo])
    fraction = index - lo
    return float(values[lo] + (values[hi] - values[lo]) * fraction)


def median_absolute_deviation(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median_absolute_deviation() requires at least one value")
    med = median(values)
    deviations = [abs(value - med) for value in values]
    return float(median(deviations))


def stable_seed(*parts: object) -> int:
    payload = "\u241f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def bootstrap_median_delta(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    seed: int,
    replicates: int = 5000,
) -> list[float]:
    if not baseline or not candidate:
        return []
    rng = random.Random(seed)
    n_baseline = len(baseline)
    n_candidate = len(candidate)
    deltas: list[float] = []
    for _ in range(replicates):
        resampled_baseline = [baseline[rng.randrange(n_baseline)] for _ in range(n_baseline)]
        resampled_candidate = [candidate[rng.randrange(n_candidate)] for _ in range(n_candidate)]
        deltas.append(float(median(resampled_candidate) - median(resampled_baseline)))
    deltas.sort()
    return deltas


def summarize_numeric_series(
    values: Sequence[float],
    *,
    evidence_rank_value: int | None = None,
    thermal_nominal: bool = True,
    minimum_outlier_samples: int = 5,
) -> dict[str, object]:
    if not values:
        raise ValueError("summarize_numeric_series() requires at least one value")

    ordered: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("numeric series cannot contain booleans")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("numeric series cannot contain NaN or infinity")
        ordered.append(numeric)
    ordered.sort()
    n = len(ordered)
    med = float(median(ordered))
    mad = median_absolute_deviation(ordered)

    if n < minimum_outlier_samples:
        outlier_mask = [False] * n
    elif mad == 0:
        outlier_mask = [value != med for value in ordered]
    else:
        threshold = 3.0 * mad
        outlier_mask = [abs(value - med) > threshold for value in ordered]

    outlier_count = sum(outlier_mask)
    outlier_ratio = outlier_count / n if n else 0.0
    confidence, confidence_score_value = confidence_score(
        n,
        outlier_ratio=outlier_ratio,
        evidence_rank_value=evidence_rank_value,
        thermal_nominal=thermal_nominal,
    )

    return {
        "n": n,
        "min": float(ordered[0]),
        "max": float(ordered[-1]),
        "sum": float(sum(ordered)),
        "mean": float(mean(ordered)),
        "median": med,
        "value": med,
        "p75": percentile(ordered, 75),
        "p90": percentile(ordered, 90),
        "p95": percentile(ordered, 95),
        "mad": float(mad),
        "outlierCount": outlier_count,
        "outlierRatio": outlier_ratio,
        "confidence": confidence,
        "_confidenceScore": confidence_score_value,
        "_ordered": ordered,
        "_outlierMask": outlier_mask,
    }


def empty_series_summary() -> dict[str, object]:
    return {
        "n": 0,
        "min": None,
        "max": None,
        "sum": None,
        "mean": None,
        "median": None,
        "value": None,
        "p75": None,
        "p90": None,
        "p95": None,
        "mad": None,
        "outlierCount": 0,
        "outlierRatio": 0.0,
        "confidence": "low",
    }


def numeric_value_from_mapping(mapping: Mapping[str, object]) -> float | None:
    for key in ("value", "median", "mean"):
        raw = mapping.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            if math.isfinite(value):
                return value
    return None
