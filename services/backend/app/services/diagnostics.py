"""根因诊断数据面（M7 R51，契约 §22.2，对标 [F§9] analyze_root_cause）。

下降区间检测 + 皮尔逊相关系数 + 候选排序，全部纯函数、零设备依赖：

- 数据源：run 目录 ``samples.ndjson``（perfeval ``read_samples`` 同源，与 §19
  回放同一事实源）；采样点缺失/null 值行跳过（``read_samples`` 已滤）。
- **下降区间**（契约 §22.2「相邻点下降 ≥ dropThresholdPct% 且持续 ≥
  minDurationMs」，默认 -15%/1000ms）：相邻点触发语义逐字按契约——只在**相邻两
  点**跌幅 ≥ 阈值处开区间（渐进缓降不在相邻点触发的，不检出，如实声明）；区间
  持续期 = 值保持低于 ``baseline × (1 - 阈值)`` 恢复线，回到恢复线及以上即收口
  （toMs = 最后一个低于恢复线点的 ts）；``durationMs ≥ minDurationMs`` 才产出。
  baseline 非正（≤0）的相邻对不判跌幅百分比（除法无意义，跳过不猜）。
- **皮尔逊相关系数**：perfeval ``core/_stats.py`` 无相关系数基元（percentile/
  summarize_numeric_series 等，_stats.py:126-238），本模块按同一统计口径补
  ``pearson`` 纯 Python 等价实现（不引 numpy 依赖，backend requirements 无
  numpy）。区间内采样点对齐：其他指标每个落在区间内的点，取**最近邻锚点**
  （bisect 最近 ts）配对；样本对 < 3 的指标不出行（不填假相关）；任一侧方差
  为 0 时皮尔逊未定义 → 不出行（不填 0）。
- **候选评分**（契约 §22.2「score 主导项为 |pearson|，具体公式 M7 实现期定」）::

      score = pearson × weight          # 保留符号（方向信息，同 golden：
                                        # 负相关候选 score 为负）
      weight = 1.0  样本对 ≥ 10
             = 0.8  5 ≤ 样本对 < 10
             = 0.6  3 ≤ 样本对 < 5    # < 3 已被相关性出行门槛滤除
      排序键 = |score| = |pearson| × weight
             = 「相关系数绝对值 × 可用性加权」（R51 口径）

  weight 即可用性加权：样本对越少（区间越短/采样越稀）证据越弱，候选降权；
  样本充足（≥10 对）时 weight=1.0，score 与 pearson 相等（与契约 golden
  `score == pearson` 形态一致）。
- ``evidence`` 为服务端规则生成的确定性文本（无 LLM 调用，[P§2.2] LLM 只叙述
  不改变判定，本期叙述化不在契约）。
- 锚指标无数据点 → 200 空结构 + note（契约 golden 逐字）；有数据但无下降区间
  → 数组为空、note=null（这是计算结果而非缺数据）。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from perfeval.collectors.android.samples import read_samples

from ..errors import validation_error
from ..registry import is_registered
from .statistics_service import resolve_run_dir

DEFAULT_ANCHOR = "frame.fps.mean"
DEFAULT_DROP_THRESHOLD_PCT = 15.0
DEFAULT_MIN_DURATION_MS = 1000

# 评分公式权重表（公式与注释见模块 docstring；单一事实源）
SCORE_WEIGHT_SUFFICIENT = 1.0  # 样本对 ≥ 10
SCORE_WEIGHT_MEDIUM = 0.8  # 5 ≤ 样本对 < 10
SCORE_WEIGHT_WEAK = 0.6  # 3 ≤ 样本对 < 5
SCORE_WEIGHT_THRESHOLDS = (10, 5)  # (sufficient_min, medium_min)
MIN_CORRELATION_PAIRS = 3  # 样本对 < 3 的指标不出相关性行（契约 §22.2）

# 契约 §22.2 golden 空结构 note（逐字同源）
EMPTY_ANCHOR_NOTE = (
    "run 无锚指标 {anchor} 数据点，根因诊断不可计算；不填假零值（[R§8.6]）"
)

Interval = dict[str, Any]
Point = tuple[int, float]


# ---- 参数解析（契约 §22.2 查询参数）----


def parse_diagnostics_params(
    params: Mapping[str, str | None],
) -> dict[str, Any]:
    """查询参数 → (anchor, dropThresholdPct, minDurationMs, fromMs, toMs)。

    - ``anchor`` 缺省 ``frame.fps.mean``；提供但未注册 → 400 validation_error；
    - ``dropThresholdPct`` 缺省 15，正数；
    - ``minDurationMs`` 缺省 1000，正整数；
    - ``fromMs/toMs`` 可选闭区间（§18 同款防护，非整数/负值/from>to → 400），
      仅截取分析窗口（先截窗后检测）。
    """
    anchor = params.get("anchor") or DEFAULT_ANCHOR
    if not is_registered(anchor):
        raise validation_error(f"未注册的 anchor metricId: {anchor}", field="anchor")

    threshold_raw = params.get("dropThresholdPct")
    if threshold_raw is None or threshold_raw == "":
        threshold: float = DEFAULT_DROP_THRESHOLD_PCT
    else:
        try:
            threshold = float(threshold_raw)
        except ValueError:
            raise validation_error(
                f"dropThresholdPct 必须是正数，收到 {threshold_raw!r}",
                field="dropThresholdPct",
            ) from None
        if not math.isfinite(threshold) or threshold <= 0:
            raise validation_error(
                f"dropThresholdPct 必须是正数，收到 {threshold_raw!r}",
                field="dropThresholdPct",
            )

    duration_raw = params.get("minDurationMs")
    if duration_raw is None or duration_raw == "":
        min_duration = DEFAULT_MIN_DURATION_MS
    else:
        try:
            min_duration = int(duration_raw)
        except ValueError:
            raise validation_error(
                f"minDurationMs 必须是正整数，收到 {duration_raw!r}",
                field="minDurationMs",
            ) from None
        if min_duration <= 0:
            raise validation_error(
                f"minDurationMs 必须是正整数，收到 {duration_raw!r}",
                field="minDurationMs",
            )

    from_ms = _parse_epoch(params.get("fromMs"), "fromMs")
    to_ms = _parse_epoch(params.get("toMs"), "toMs")
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise validation_error(
            f"fromMs ({from_ms}) 不得大于 toMs ({to_ms})", field="fromMs"
        )
    return {
        "anchor": anchor,
        "dropThresholdPct": _integral(threshold),
        "minDurationMs": min_duration,
        "fromMs": from_ms,
        "toMs": to_ms,
    }


def _parse_epoch(raw: str | None, field: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise validation_error(
            f"{field} 必须是整数（epoch ms），收到 {raw!r}", field=field
        ) from None
    if value < 0:
        raise validation_error(f"{field} 不得为负值", field=field)
    return value


def _integral(value: float) -> float | int:
    """整值 float 归一为 int（契约 golden params 回显形态：15 而非 15.0）。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ---- 装配入口 ----


def build_diagnostics(
    runs_dir: Path,
    run_id: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """契约 §22.2 装配。run 不存在 → 404 report_not_found（resolve_run_dir）。"""
    anchor = str(params["anchor"])
    threshold = float(params["dropThresholdPct"])
    min_duration = int(params["minDurationMs"])
    from_ms = params.get("fromMs")
    to_ms = params.get("toMs")

    run_path = resolve_run_dir(runs_dir, run_id)
    samples_path = run_path / "samples.ndjson"
    records = read_samples(samples_path) if samples_path.is_file() else []
    series: dict[str, list[Point]] = {}
    for record in records:
        series.setdefault(record.metric_id, []).append(
            (int(record.ts_ms), float(record.value))
        )
    for points in series.values():
        points.sort()

    anchor_points = series.get(anchor, [])
    body: dict[str, Any] = {
        "runId": run_id,
        "anchor": anchor,
        "params": {
            "dropThresholdPct": _integral(threshold),
            "minDurationMs": min_duration,
        },
        "dropIntervals": [],
        "correlations": [],
        "candidates": [],
        "note": None,
    }
    if not anchor_points:
        body["note"] = EMPTY_ANCHOR_NOTE.format(anchor=anchor)
        return body

    window = _window_points(anchor_points, from_ms, to_ms)
    intervals = detect_drop_intervals(
        window, metric_id=anchor, threshold_pct=threshold, min_duration_ms=min_duration
    )
    body["dropIntervals"] = intervals
    if not intervals:
        return body  # 有数据、无下降：计算结果为空（note=null，非缺数据）

    correlations = correlate_metrics(
        series,
        anchor,
        intervals,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    body["correlations"] = correlations
    body["candidates"] = rank_candidates(correlations, anchor)
    return body


def _window_points(
    points: Sequence[Point],
    from_ms: int | None,
    to_ms: int | None,
) -> list[Point]:
    if from_ms is None and to_ms is None:
        return list(points)
    return [
        (ts, value)
        for ts, value in points
        if (from_ms is None or ts >= from_ms) and (to_ms is None or ts <= to_ms)
    ]


# ---- 下降区间检测 ----


def detect_drop_intervals(
    points: Sequence[Point],
    *,
    metric_id: str,
    threshold_pct: float,
    min_duration_ms: int,
) -> list[Interval]:
    """相邻点下降 ≥ ``threshold_pct``% 且持续 ≥ ``min_duration_ms`` 的区间列表。

    状态机（口径见模块 docstring）：触发点开区间（baseline=相邻前点、恢复线=
    baseline×(1-阈值)），持续期取 trough 并顺延 toMs，回到恢复线及以上收口；
    持续时长达标的区间才产出。``dropPct`` 由**回显后的 baseline/trough** 复算
    （golden 不变量：dropPct ≈ (baseline-trough)/baseline×100 < 0.1 恒成立）。
    """
    intervals: list[Interval] = []
    open_state: dict[str, Any] | None = None
    level = 0.0

    def close(final_ts: int) -> None:
        nonlocal open_state
        assert open_state is not None
        baseline = round(open_state["baseline"], 3)
        trough = round(open_state["trough"], 3)
        from_ts = open_state["from_ts"]
        duration = final_ts - from_ts
        state, open_state = open_state, None
        if baseline <= 0:
            return  # 基线非正不判百分比（触发即不可能，防御兜底）
        if duration < min_duration_ms:
            return  # 持续不足 → 不产出（契约「持续 ≥ minDurationMs」）
        drop_pct = round((baseline - trough) / baseline * 100, 1)
        intervals.append(
            {
                "metricId": metric_id,
                "fromMs": from_ts,
                "toMs": final_ts,
                "durationMs": duration,
                "dropPct": drop_pct,
                "baseline": baseline,
                "trough": trough,
            }
        )

    for index in range(1, len(points)):
        prev_ts, prev_value = points[index - 1]
        ts, value = points[index]
        if open_state is None:
            if prev_value <= 0:
                continue  # 基线非正：跌幅百分比无定义（跳过，不猜）
            pair_drop = (prev_value - value) / prev_value * 100
            if pair_drop >= threshold_pct:
                open_state = {
                    "baseline": prev_value,
                    "trough": value,
                    "from_ts": ts,
                    "last_ts": ts,
                }
                level = prev_value * (1 - threshold_pct / 100)
        else:
            if value < level:
                open_state["trough"] = min(open_state["trough"], value)
                open_state["last_ts"] = ts
            else:
                close(prev_ts)  # 收口于最后一个低于恢复线的点
    if open_state is not None:
        close(open_state["last_ts"])  # 序列末尾仍处下降 → 收口于最后点
    return intervals


# ---- 皮尔逊相关系数（纯 Python，perfeval _stats 无相关基元的等价实现）----


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """皮尔逊积矩相关系数；样本 <2 或任一侧零方差（未定义）→ None。"""
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    mean_x = math.fsum(xs[:n]) / n
    mean_y = math.fsum(ys[:n]) / n
    sxx = math.fsum((x - mean_x) ** 2 for x in xs[:n])
    syy = math.fsum((y - mean_y) ** 2 for y in ys[:n])
    if sxx == 0.0 or syy == 0.0:
        return None
    sxy = math.fsum(
        (x - mean_x) * (y - mean_y) for x, y in zip(xs[:n], ys[:n])
    )
    coefficient = sxy / math.sqrt(sxx * syy)
    # 浮点累计可能溢出 [-1, 1] 边界（如 1.0000000000000002），夹取保形
    return max(-1.0, min(1.0, coefficient))


def _nearest_anchor_value(
    anchor_ts: Sequence[int],
    anchor_points: Sequence[Point],
    ts: int,
) -> float | None:
    """最近邻锚点配对（区间内采样点对齐，契约 §22.2；锚点空 → None）。"""
    if not anchor_ts:
        return None
    index = bisect_left(anchor_ts, ts)
    candidates: list[int] = []
    if index < len(anchor_ts):
        candidates.append(index)
    if index > 0:
        candidates.append(index - 1)
    best = min(candidates, key=lambda i: (abs(anchor_ts[i] - ts), i))
    return anchor_points[best][1]


def correlate_metrics(
    series: Mapping[str, Sequence[Point]],
    anchor: str,
    intervals: Iterable[Interval],
    *,
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> list[dict[str, Any]]:
    """各其他指标 vs 锚指标在下降区间并集内的皮尔逊相关，|pearson| 降序。

    区间并集内逐指标配对：其他指标每个落点取最近邻锚点值配对（跨区间拼接为
    一行，同 golden 单行形态）；样本对 < ``MIN_CORRELATION_PAIRS`` 或皮尔逊
    未定义（零方差）的指标不出行。
    """
    anchor_points = list(series.get(anchor, ()))
    anchor_ts = [ts for ts, _ in anchor_points]
    spans = [
        (int(interval["fromMs"]), int(interval["toMs"])) for interval in intervals
    ]

    rows: list[dict[str, Any]] = []
    for metric_id in sorted(series):
        if metric_id == anchor:
            continue
        xs: list[float] = []
        ys: list[float] = []
        for ts, value in series[metric_id]:
            if from_ms is not None and ts < from_ms:
                continue
            if to_ms is not None and ts > to_ms:
                continue
            if not any(lo <= ts <= hi for lo, hi in spans):
                continue
            anchor_value = _nearest_anchor_value(anchor_ts, anchor_points, ts)
            if anchor_value is None:
                continue
            xs.append(anchor_value)
            ys.append(value)
        if len(xs) < MIN_CORRELATION_PAIRS:
            continue
        coefficient = pearson(xs, ys)
        if coefficient is None:
            continue
        rows.append(
            {
                "metricId": metric_id,
                "pearson": round(coefficient, 3),
                "sampleCount": len(xs),
            }
        )
    rows.sort(key=lambda row: abs(float(row["pearson"])), reverse=True)
    return rows


# ---- 候选排序（评分公式见模块 docstring，单一事实源在此）----


def _availability_weight(sample_count: int) -> float:
    sufficient_min, medium_min = SCORE_WEIGHT_THRESHOLDS
    if sample_count >= sufficient_min:
        return SCORE_WEIGHT_SUFFICIENT
    if sample_count >= medium_min:
        return SCORE_WEIGHT_MEDIUM
    return SCORE_WEIGHT_WEAK


def rank_candidates(
    correlations: Sequence[Mapping[str, Any]],
    anchor: str,
) -> list[dict[str, Any]]:
    """score = pearson × weight，按 |score| 降序出 rank 1..n 与确定性 evidence。"""
    candidates: list[dict[str, Any]] = []
    for row in correlations:
        pearson_value = float(row["pearson"])
        weight = _availability_weight(int(row["sampleCount"]))
        score = round(pearson_value * weight, 3)
        candidates.append(
            {
                "metricId": str(row["metricId"]),
                "score": score,
                "evidence": _evidence_text(
                    str(row["metricId"]), anchor, pearson_value, int(row["sampleCount"])
                ),
            }
        )
    candidates.sort(key=lambda item: abs(float(item["score"])), reverse=True)
    ranked = []
    for index, item in enumerate(candidates, start=1):
        ranked.append({"rank": index, **item})
    return ranked


def _evidence_text(
    metric_id: str, anchor: str, pearson_value: float, sample_count: int
) -> str:
    magnitude = abs(pearson_value)
    strength = "强" if magnitude >= 0.7 else ("中" if magnitude >= 0.4 else "弱")
    direction = "正相关" if pearson_value > 0 else "负相关"
    return (
        f"锚指标 {anchor} 下降区间内 {metric_id} 与其{direction}"
        f"（pearson={pearson_value:+.2f}，样本对 {sample_count}），{strength}候选"
    )
