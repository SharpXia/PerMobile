"""compare_service：POST /api/reports/compare 数据面（契约 §9 + M3 扩展，R43/R44）。

职责：请求校验 → runId 解析到 run 目录（事实源）→ summary.json 装载 →
perfeval `compare_summaries` 引擎透传（不加壳、不改名、不新增必填字段，
[R§3.3]）→ 附顶层可选说明字段 `meta`（契约 §9 M3 扩展，走 §11.1：
承载 M3 对比页展示信息——各 runId 的 caseName/device/时长与可比前提提示）。

- mode 缺省 `same_app_devices`：契约 §9 明示「前提校验失败由引擎返回的
  `app_mismatch`/`device_not_changed` 结果字段承载」，两码均为
  same_app_devices 模式的产物（engine.py:946-962），据此取该缺省；
  `mode` 为可选请求字段（§11.1 不升版本）。
- run 目录不存在或 summary.json 缺失/不可解析 → 404 report_not_found
  （契约 §3：run/对比记录不存在）。
- meta 的 durationMs 取 build_run_statistics（与 §12 统计端点同源，ARCH §4.7
  单一事实源约束）；manifest 缺失（旧 run 兼容导入 [R§2.12]）时 caseName 回退
  目录名、device 为 null，不编造。

S3 扩展（2026-11-08，compare-budget-not-configurable-web / compare-no-interval-window /
compare-two-groups-only，§9 契约 delta 待 S1 落笔，§11.1 不升版本）：

- `budget`（可选对象，键=metricId，值口径随引擎 _budget_for_metric）：结构非法
  （非对象 / defaults、metrics 非对象 / 指标项非对象 / 叶值非标量）→ 400
  validation_error；透传 compare_summaries(budget=…)（引擎形参既有）。
- `fromMs`/`toMs`（可选，epoch ms 闭区间，时戳域按 §18：后端自产 run 的
  samples/timeline 时戳均为 epoch ms）：给出时按 run 读 samples/timeline 经窗口
  过滤后以 §12 同一统计内核（statistics_service 暴露的解析函数 + perfeval
  summarize_numeric_series）现算窗口化 per-metric 统计，组装 summary.v1 兼容
  窗口载荷喂引擎——**窗口过滤单一实现不复制第二份**（帧序列过滤复用
  statistics_service.read_frame_detail 的同批事件与 §18 同语义闭区间）；
  无窗口参数走整段 summary.json 路径不变。窗口内无样本的指标不进载荷
  （引擎按 missing_metric 判 unknown，如实不伪造）。
- `candidateRunIds`（可选数组）：N 路对比（引擎 groups 2–10 组，首条
  baselineRunIds 组=全局基线、每条 candidateRunId 各成一组）；旧单数
  `candidateRunId` 保留兼容（两者同给时以 candidateRunIds 为准）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from perfeval.collectors.android.samples import read_samples
from perfeval.core._stats import summarize_numeric_series
from perfeval.core.compare import compare_summaries
from perfeval.core.summary.engine import INTEGRAL_AGGREGATION_METRICS, PEAK_AGGREGATION_METRICS

from ..errors import ApiError, validation_error
from ..registry import METRICS
from .statistics_service import (
    DEFAULT_FPS_GE,
    build_run_statistics,
    read_frame_detail,
    resolve_run_dir,
)

#: 引擎支持的对比模式（compare_summaries mode 参数）；缺省取值理由见模块 docstring
COMPARE_MODES: tuple[str, ...] = ("same_device_versions", "same_app_devices")
DEFAULT_COMPARE_MODE = "same_app_devices"

#: N 路上限（引擎 N_WAY_MAX_GROUPS=10：基线 1 组 + candidateRunIds ≤9）
MAX_CANDIDATE_GROUPS = 9

#: 候选指标 unit/direction 兜底注册表（窗口载荷条目缺 unit/direction 时查）
_REGISTRY_BY_ID: dict[str, Any] = {spec.metric_id: spec for spec in METRICS}

_COMPARABLE_NOTE = (
    "可比前提：相同设备/场景/时长/初始状态（[F§8]）；设备/应用/版本错配由透传体 "
    "errors 与 reliability.reasons 承载（契约 §9，HTTP 仍 200）"
)

_N_WAY_NOTE = (
    "N 路判定：以首条 baseline 为全局基线，各 candidate 逐一与基线两两判定；"
    "每指标 judgement 取各对最差（pairs/perGroup/groups 为 §11.1 可选字段）"
)


def compare_runs(
    runs_dir: Path,
    baseline_run_ids: list[str],
    candidate_run_ids: list[str],
    mode: str = DEFAULT_COMPARE_MODE,
    budget: dict[str, Any] | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> dict[str, Any]:
    """契约 §9 POST /api/reports/compare：透传 perfeval.compare.v1 + 可选 meta。

    candidate_run_ids 单条走既有两路路径（响应形态与历史逐字节一致）；多条走
    引擎 N 路（groups）。budget/fromMs/toMs 全部可选（§11.1），缺省行为不变。
    """
    if not isinstance(mode, str) or mode not in COMPARE_MODES:
        raise validation_error(
            f"mode 必须是 {'|'.join(COMPARE_MODES)} 之一",
            field="mode",
        )
    validate_budget(budget)
    baseline = [
        _load_run_summary(runs_dir, run_id, from_ms=from_ms, to_ms=to_ms)
        for run_id in baseline_run_ids
    ]
    candidates = [
        _load_run_summary(runs_dir, run_id, from_ms=from_ms, to_ms=to_ms)
        for run_id in candidate_run_ids
    ]

    if len(candidates) == 1:
        result = compare_summaries(baseline, candidates[0], mode=mode, budget=budget)
    else:
        # N 路：基线 1 组 + 每条 candidate 各 1 组（compare-two-groups-only）
        result = compare_summaries(
            None,
            None,
            mode=mode,
            budget=budget,
            groups=[baseline, *([summary] for summary in candidates)],
        )
    # 契约 §9 M3 扩展（§11.1 可选说明字段）：透传体外新增顶层 meta，消费方必须可忽略
    meta: dict[str, Any] = {
        "baseline": [_run_meta(runs_dir, run_id) for run_id in baseline_run_ids],
        "candidate": [_run_meta(runs_dir, run_id) for run_id in candidate_run_ids],
        "preconditionNote": _COMPARABLE_NOTE,
    }
    if len(candidates) > 1:
        meta["nWayNote"] = _N_WAY_NOTE
    if from_ms is not None:
        meta["fromMs"] = from_ms
    if to_ms is not None:
        meta["toMs"] = to_ms
    result["meta"] = meta
    return result


def parse_run_ids(value: Any, field: str) -> list[str]:
    """请求字段 → runId 列表；非数组/含非字符串/空数组 → 400 validation_error。"""
    if not isinstance(value, list) or not value:
        raise validation_error(
            f"{field} 必须是非空数组（>=1 条 runId）", field=field
        )
    run_ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise validation_error(f"{field} 元素必须是非空字符串", field=field)
        run_ids.append(item)
    return run_ids


def parse_candidate_run_ids(body: dict[str, Any]) -> list[str]:
    """请求体 → candidate runId 列表（§9 S3 扩展）。

    - `candidateRunIds` 给出（非 null）：非空数组、元素非空字符串，长度 ≤
      MAX_CANDIDATE_GROUPS（引擎 2–10 组约束：基线 1 组 + 候选 ≤9）；
    - 否则回退旧单数 `candidateRunId`（必填非空字符串，兼容不变）；
    - 两者同给时以 candidateRunIds 为准（新字段优先，旧字段忽略）。
    """
    multi = body.get("candidateRunIds")
    if multi is not None:
        run_ids = parse_run_ids(multi, "candidateRunIds")
        if len(run_ids) > MAX_CANDIDATE_GROUPS:
            raise validation_error(
                f"candidateRunIds 最多 {MAX_CANDIDATE_GROUPS} 条"
                f"（引擎 N 路上限 {MAX_CANDIDATE_GROUPS + 1} 组：基线 1 组 + 候选）",
                field="candidateRunIds",
            )
        return run_ids
    single = body.get("candidateRunId")
    if not isinstance(single, str) or not single.strip():
        raise validation_error(
            "candidateRunId 必填且为非空字符串（或改用 candidateRunIds 数组）",
            field="candidateRunId",
        )
    return [single]


def validate_budget(budget: Any) -> None:
    """budget 结构校验（compare-budget-not-configurable-web）。

    None = 未提供（放行）；必须为对象；`defaults`/`metrics`（若给）须为对象；
    `metrics` 每个指标项须为对象；defaults 与指标项的叶值须为标量
    （str/int/float/bool/None），嵌套容器 → 400 validation_error
    （引擎 _budget_for_metric 只消费标量叶值）。
    """
    if budget is None:
        return
    if not isinstance(budget, dict):
        raise validation_error("budget 必须是对象（{defaults, metrics}）", field="budget")
    defaults = budget.get("defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            raise validation_error("budget.defaults 必须是对象", field="budget.defaults")
        _validate_scalar_leaves(defaults, "budget.defaults")
    metrics = budget.get("metrics")
    if metrics is None:
        return
    if not isinstance(metrics, dict):
        raise validation_error("budget.metrics 必须是对象", field="budget.metrics")
    for metric_id, item in metrics.items():
        if not isinstance(item, dict):
            raise validation_error(
                f"budget.metrics.{metric_id} 必须是对象", field="budget.metrics"
            )
        _validate_scalar_leaves(item, f"budget.metrics.{metric_id}")


def _validate_scalar_leaves(section: dict[str, Any], path: str) -> None:
    """节内叶值必须为标量；嵌套容器（dict/list）→ 400。"""
    for key, value in section.items():
        if isinstance(value, (dict, list)):
            raise validation_error(
                f"{path}.{key} 的值必须是标量（数字/字符串/布尔），收到嵌套容器",
                field=f"{path}.{key}",
            )


def parse_window(body: dict[str, Any]) -> tuple[int | None, int | None]:
    """请求体 → (fromMs, toMs)（§9 S3 扩展，校验同 §18：非整数/负值/from>to → 400）。

    复用 statistics_service.parse_interval_ms（§18 单一校验实现，不复制第二份）。
    """
    from .statistics_service import parse_interval_ms

    return parse_interval_ms(body)


# ---- 窗口化 summary 装载（compare-no-interval-window）----


def _load_run_summary(
    runs_dir: Path,
    run_id: str,
    *,
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> dict[str, Any]:
    """runId → summary.json（perfeval.summary.v1 原样）；缺失/不可解析 → 404。

    给出 fromMs/toMs 时在原样基础上以窗口化统计覆盖 `metrics`（身份字段
    device/app/scenario/focusedWindows 等保持原样——引擎前提校验口径不变）。
    """
    run_dir = resolve_run_dir(runs_dir, run_id)
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        raise ApiError(
            "report_not_found",
            f"run {run_id} 无 summary.json（不可对比）",
        )
    try:
        document = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ApiError(
            "report_not_found",
            f"run {run_id} summary.json 不可解析：{exc}",
        ) from None
    if not isinstance(document, dict):
        raise ApiError(
            "report_not_found",
            f"run {run_id} summary.json 不是 JSON 对象（不可对比）",
        )
    if from_ms is None and to_ms is None:
        return document
    windowed = dict(document)
    windowed["metrics"] = _windowed_metrics(
        run_dir, document.get("metrics"), from_ms=from_ms, to_ms=to_ms
    )
    return windowed


def _windowed_metrics(
    run_dir: Path,
    base_metrics: Any,
    *,
    from_ms: int | None,
    to_ms: int | None,
) -> dict[str, dict[str, Any]]:
    """窗口化 per-metric 统计（§12 同一统计内核，窗口过滤单一实现）。

    数据源（与 build_run_statistics 同一批事实源，§18 同语义闭区间过滤）：
    - samples.ndjson 点列：各 metricId 窗口内值序列 → summarize_numeric_series；
      聚合口径与 perfeval summary engine 一致（peak 类取 max、积分类取 sum、
      其余取中位数）；
    - timeline.ndjson 逐帧事件（statistics_service.read_frame_detail 同批事件）：
      窗口内帧耗时 → 派生 frame.fps.mean/frame.fps.median（1000/mean、1000/median，
      summary engine _derive_frame_metrics 同式；samples 已含该指标时不重复派生）。
    窗口内无样本的指标不进载荷（引擎按 missing_metric 判 unknown）。
    """
    metrics: dict[str, dict[str, Any]] = {}

    samples_path = run_dir / "samples.ndjson"
    if samples_path.is_file():
        try:
            records = read_samples(samples_path)
        except OSError:
            records = []
        values_by_metric: dict[str, list[float]] = {}
        series_by_metric: dict[str, list[tuple[int, float]]] = {}
        for record in records:
            value = record.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            ts = int(record.ts_ms)
            if (from_ms is not None and ts < from_ms) or (to_ms is not None and ts > to_ms):
                continue
            values_by_metric.setdefault(record.metric_id, []).append(float(value))
            series_by_metric.setdefault(record.metric_id, []).append((ts, float(value)))
        for metric_id, values in values_by_metric.items():
            metrics[metric_id] = _windowed_entry(
                metric_id, values, series_by_metric.get(metric_id, []),
                base_metrics=base_metrics,
            )

    # 逐帧派生（§12 frameEvents 基线同源）：samples 未含 frame.fps.mean 时补派生
    if "frame.fps.mean" not in metrics:
        frames = read_frame_detail(run_dir / "timeline.ndjson")
        durations = [
            float(row["duration_ms"])
            for row in frames
            if _ts_in_window(float(row["ts_ms"]), from_ms, to_ms)
        ]
        if durations:
            mean_ms = sum(durations) / len(durations)
            median_ms = summarize_numeric_series(durations)["median"]
            entry = {
                "value": round(1000.0 / mean_ms, 6) if mean_ms > 0 else None,
                "unit": "fps",
                "direction": "higher_better",
                "n": len(durations),
            }
            metrics["frame.fps.mean"] = entry
            if median_ms > 0:
                metrics["frame.fps.median"] = {
                    **entry,
                    "value": round(1000.0 / float(median_ms), 6),
                }
    return metrics


def _ts_in_window(ts: float, from_ms: int | None, to_ms: int | None) -> bool:
    """§18 同语义闭区间含端点（帧时戳可能为浮点相对域，端点比较同 build_run_statistics）。"""
    if from_ms is not None and ts < from_ms:
        return False
    if to_ms is not None and ts > to_ms:
        return False
    return True


def _windowed_entry(
    metric_id: str,
    values: list[float],
    series: list[tuple[int, float]],
    *,
    base_metrics: Any,
) -> dict[str, Any]:
    """单指标窗口化条目：perfeval 统计内核 + summary engine 聚合口径 + 原条目口径继承。"""
    stats = summarize_numeric_series(values)
    if metric_id in PEAK_AGGREGATION_METRICS:
        metric_value = stats["max"]
    elif metric_id in INTEGRAL_AGGREGATION_METRICS:
        metric_value = stats["sum"]
    else:
        metric_value = stats["value"]

    base = base_metrics.get(metric_id) if isinstance(base_metrics, dict) else None
    if not isinstance(base, dict):
        base = {}
    spec = _REGISTRY_BY_ID.get(metric_id)
    entry: dict[str, Any] = {
        "value": metric_value,
        "unit": base.get("unit") or (spec.unit if spec is not None else None),
        "direction": base.get("direction") or (spec.direction if spec is not None else None),
        "n": int(stats["n"]),
        "min": stats["min"],
        "max": stats["max"],
        "mean": stats["mean"],
        "median": stats["median"],
        "p95": stats["p95"],
        "mad": stats["mad"],
        "outlierCount": int(stats["outlierCount"]),
        "outlierRatio": float(stats["outlierRatio"]),
        # stream 展示块（engine _stream_block）的口径字段继承原条目；窗口化重算
        "isStream": base.get("isStream"),
        "sampleCount": int(stats["n"]),
        "series": _stride_series(series),
    }
    if base.get("evidenceLevel") is not None:
        entry["evidenceLevel"] = base["evidenceLevel"]
    return entry


def _stride_series(series: list[tuple[int, float]], max_points: int = 600) -> list[dict[str, Any]]:
    """窗口化序列点列（≤600 点等距步进取样；传输降采样，不参与统计计算）。"""
    if len(series) <= max_points:
        return [{"ts": ts, "v": value} for ts, value in series]
    stride = math.ceil(len(series) / max_points)
    return [{"ts": ts, "v": value} for ts, value in series[::stride]]


def _run_meta(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """单 run 展示信息（meta 元素）：caseName/device/时长/summary 可用性。"""
    run_dir = resolve_run_dir(runs_dir, run_id)
    manifest = _read_json_dict(run_dir / "manifest.json")
    case_name = manifest.get("caseName")
    if not isinstance(case_name, str) or not case_name:
        case_name = run_dir.name
    device = manifest.get("device")
    if not isinstance(device, str) or not device:
        device = None  # 旧 run manifest.device 为 Mapping，此处不编造标量
    return {
        "runId": run_id,
        "caseName": case_name,
        "device": device,
        # 与 §12 统计端点同源（build_run_statistics）；无帧数据 → 0
        "durationMs": build_run_statistics(runs_dir, run_id, list(DEFAULT_FPS_GE))["durationMs"],
        "summaryAvailable": (run_dir / "summary.json").is_file(),
    }


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return document if isinstance(document, dict) else {}
