from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_MEMORY_PROFILE = "mainstream"
MEMORY_PROFILES = ("lightweight", "mainstream", "heavy")

MEMORY_PROFILE_THRESHOLDS: dict[str, dict[str, tuple[float, float, float]]] = {
    "lightweight": {
        "memory.pss.peak_mb": (300, 600, 900),
        "memory.pss.mean_mb": (300, 600, 900),
        "memory.rss.peak_mb": (300, 600, 900),
    },
    "mainstream": {
        "memory.pss.peak_mb": (400, 800, 1200),
        "memory.pss.mean_mb": (400, 800, 1200),
        "memory.rss.peak_mb": (400, 800, 1200),
    },
    "heavy": {
        "memory.pss.peak_mb": (700, 1200, 1800),
        "memory.pss.mean_mb": (700, 1200, 1800),
        "memory.rss.peak_mb": (700, 1200, 1800),
    },
}


def normalize_memory_profile(value: object) -> tuple[str, str | None]:
    if isinstance(value, str) and value.strip():
        profile = value.strip().lower()
        if profile in MEMORY_PROFILES:
            return profile, None
        return DEFAULT_MEMORY_PROFILE, f"memoryProfile.unknown:{value}"
    return DEFAULT_MEMORY_PROFILE, None


def normalize_budget_metrics(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    raw_metrics = value.get("metrics") if isinstance(value.get("metrics"), Mapping) else value
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_metrics, Mapping):
        return result
    for metric_id, metric_budget in raw_metrics.items():
        if isinstance(metric_id, str) and isinstance(metric_budget, Mapping):
            result[metric_id] = dict(metric_budget)
    return result


def rating_thresholds_for_metric(
    metric_id: str,
    *,
    budgets: Mapping[str, Any] | None = None,
    memory_profile: str = DEFAULT_MEMORY_PROFILE,
) -> tuple[float, float, float] | None:
    metric_budget = normalize_budget_metrics(budgets or {}).get(metric_id, {})
    explicit = _coerce_rating_abs(metric_budget.get("ratingAbs"))
    if explicit is not None:
        return explicit
    profile, _diagnostic = normalize_memory_profile(memory_profile)
    return MEMORY_PROFILE_THRESHOLDS.get(profile, {}).get(metric_id)


def fail_abs_for_metric(metric_id: str, budgets: Mapping[str, Any] | None = None) -> float | None:
    metric_budget = normalize_budget_metrics(budgets or {}).get(metric_id, {})
    return _coerce_float(metric_budget.get("failAbs"))


def _coerce_rating_abs(value: object) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    numbers = [_coerce_float(item) for item in value]
    if any(item is None for item in numbers):
        return None
    first, second, third = (float(item) for item in numbers if item is not None)
    if not (first <= second <= third):
        return None
    return first, second, third


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
