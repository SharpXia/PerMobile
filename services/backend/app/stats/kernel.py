"""perfdog.statistics.v1 统计内核（纯函数，M2 / DEVELOPMENT_PLAN R08-R11）。

公式依据（逐条对照，research/perfdog-features.md [F§3.1-3.5] 与
API_CONTRACT.md §12「字段单位与口径」表）：

- Jank 族双条件（[F§3.3]，公式公开、可精确复刻）：
    条件 a：当前帧耗时 > 前三帧平均耗时 × 2
    条件 b：当前帧耗时 > 电影帧阈值
      Jank      = a 且 > 1000ms/24×2（≈83.3333ms；官方文案取整写作 83.33/84ms）
      BigJank   = a 且 > 1000ms/24×3（=125ms）
      SmallJank = a 且 > 1000ms/24   （≈41.6667ms；[F§3.3] 表写作 ≈41.66ms、
                  [F§10.1] 写作 41.67ms——两处显示取整互相矛盾，以公式 1000/24 为准）
    首帧不足三个前驱不判（前 3 帧跳过）。
- Stutter = Σ Jank 帧耗时 / 总时长 × 100（[F§3.4]）。
- Jank(/10min) = Jank 次数 × 600000 / durationMs（[F§3.3]）。
- Delta(FTime) = 相邻两帧时间差 >100ms 的次数 × 3600000 / durationMs（[F§3.2]）。
- FPS 统计矩阵（[F§3.1]）：Avg/Var/Std、Drop（相邻点下降 >8 帧，每小时）、
  FPS>=x%、Min/Median/MedRange（偏离中位 ±20% 占比）。
- 1%Low = 帧耗时最长的 1% 求均值，1000 除之（[F§3.5]，可精确复刻）。
- Smooth：官方未公开算法公式（[F§3.5][F§10.1]），本文件 `smooth_index` 为行为近似，
  数值方向与官方建议阈值一致（越低越平稳：游戏 <8、应用滑动 <20），
  **数值与 PerfDog 不可直接对比**——报告侧恒带 note（statistics_service）。

约定：

- 全部函数为纯函数：不做 I/O、不依赖 FastAPI、同一输入恒同输出（golden 可复现）。
- 输出即展示值：小数位统一 `_r1`（1 位）/`_r2`（2 位）。小数位策略与契约 §12
  示例的精度形态一致（var/stutter 两位，其余一位——示例 0.16/0.02 vs 112.3/9.7），
  契约未规定精度，此为本实现的显式选择，golden 测试按同口径断言。
- var/std 取**总体方差/标准差**（statistics.pvariance/pstdev）：统计对象是采集期
  全量帧/点列（总体而非抽样），契约与官方文档未指明，此为显式选择。
- durationMs 由装配层决定（fpsSeries=点列首尾差；frameEvents=1s 桶覆盖时数），
  内核只消费；duration_ms<=0 的除法字段防守性取 0.0（装配层实际不会传入）。

与 perfeval 旧 jank 口径（帧耗时 > 1.5×刷新周期，gfxinfo.py:29/:324-338）**故意
不同**：那是 perfeval 文档口径，本内核按 PerfDog 官方双条件实现；差异对照见
tests/test_statistics_parity.py（口径差异，非 bug）。
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Sequence

# ---- 阈值常量（[F§3.2-3.5]，公式形态，不取显示取整值）----

#: 电影帧时长 1000ms/24（≈41.6667ms）
MOVIE_FRAME_MS = 1000.0 / 24.0
#: SmallJank 条件 b：一帧电影帧 1000/24（≈41.6667ms）
SMALL_JANK_THRESHOLD_MS = MOVIE_FRAME_MS
#: Jank 条件 b：两帧电影帧 1000/24×2（≈83.3333ms）
JANK_THRESHOLD_MS = MOVIE_FRAME_MS * 2.0
#: BigJank 条件 b：三帧电影帧 1000/24×3（=125ms）
BIG_JANK_THRESHOLD_MS = MOVIE_FRAME_MS * 3.0
#: 判定所需的最少前驱帧数（前 3 帧不判，[F§3.3]「前三帧平均」）
JANK_MIN_PREDECESSORS = 3
#: Delta(FTime)：相邻两帧时间差阈值（[F§3.2]）
DELTA_FTIME_THRESHOLD_MS = 100.0
#: Drop(FPS)：相邻 FPS 点下降阈值（[F§3.1]）
FPS_DROP_THRESHOLD_FPS = 8.0
#: MedRange：偏离中位数 ±20%（[F§3.1]）
MED_RANGE_TOLERANCE = 0.2
#: 1%Low：帧耗时最长的 1%（[F§3.5]）
LOW_FPS_RATIO = 0.01
MS_PER_HOUR = 3_600_000.0
MS_PER_10MIN = 600_000.0
#: Smooth 近似算法的窗口长度（1s，与 fps 点列节拍一致）
SMOOTH_WINDOW_MS = 1000.0


def _r1(value: float | None) -> float | None:
    """1 位小数展示值（契约 §12 示例精度形态；None 透传）。"""
    return None if value is None else round(float(value), 1)


def _r2(value: float | None) -> float | None:
    """2 位小数展示值（var/stutter；契约示例 0.16/0.02）。"""
    return None if value is None else round(float(value), 2)


def _finite_numbers(values: Iterable[Any]) -> list[float]:
    """过滤出有限数值（bool 排除，NaN/Inf 排除——引擎同款口径 engine.py:358）。"""
    return [
        float(v)
        for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
    ]


# ---- Jank 族（R08）----


def jank_classification(durations_ms: Sequence[float]) -> dict[str, list[bool]]:
    """逐帧 Jank/BigJank/SmallJank 标记（[F§3.3] 双条件；前 3 帧不判）。

    返回三个与输入等长的布尔列表；未判帧恒为 False（数量口径以本函数为准，
    **不复用** perfeval 事件里的 jank 标记——那是 1.5×刷新周期口径）。
    """
    n = len(durations_ms)
    flags: dict[str, list[bool]] = {
        "jank": [False] * n,
        "bigJank": [False] * n,
        "smallJank": [False] * n,
    }
    for i in range(JANK_MIN_PREDECESSORS, n):
        current = durations_ms[i]
        prev3_mean = (durations_ms[i - 1] + durations_ms[i - 2] + durations_ms[i - 3]) / 3.0
        if not current > prev3_mean * 2.0:  # 条件 a（严格大于）
            continue
        flags["smallJank"][i] = current > SMALL_JANK_THRESHOLD_MS
        flags["jank"][i] = current > JANK_THRESHOLD_MS
        flags["bigJank"][i] = current > BIG_JANK_THRESHOLD_MS
    return flags


def jank_group(durations_ms: Sequence[float], duration_ms: float) -> dict[str, Any]:
    """Jank 统计组（契约 §12 `jank`：small/big/jank/stutter/per10min）。

    - stutter = Σ Jank 帧耗时 / duration_ms × 100（[F§3.4]；Jank 帧取条件
      a+83.33ms 的 Jank 级帧，不含 Small/Big 扩集）；
    - per10min = Jank 次数 × 600000 / duration_ms（[F§3.3]）。
    空序列全部字段 None（不填假零值，[R§8.6]）。
    """
    values = _finite_numbers(durations_ms)
    if not values:
        return {"small": None, "big": None, "jank": None, "stutter": None, "per10min": None}
    flags = jank_classification(values)
    jank_count = sum(flags["jank"])
    jank_time_ms = sum(d for d, hit in zip(values, flags["jank"]) if hit)
    stutter_pct = jank_time_ms / duration_ms * 100.0 if duration_ms > 0 else 0.0
    per10min = jank_count * MS_PER_10MIN / duration_ms if duration_ms > 0 else 0.0
    return {
        "small": sum(flags["smallJank"]),
        "big": sum(flags["bigJank"]),
        "jank": jank_count,
        "stutter": _r2(stutter_pct),
        "per10min": _r1(per10min),
    }


# ---- FrameTime（R10）----


def frame_time_group(durations_ms: Sequence[float], duration_ms: float) -> dict[str, Any]:
    """FrameTime 统计组（契约 §12 `frameTime`：avg/std/var/delta，[F§3.2]）。

    delta：相邻两帧时间差（|d[i+1]−d[i]|）>100ms 的次数 × 3600000 / duration_ms。
    官方原文「两帧时间差 >100ms」未写明取绝对值；FTime 是显示间隔本身，
    相邻帧耗时的双向跳变都属增量异常，故取绝对差（显式选择，golden 固定）。
    """
    if not durations_ms:
        return {"avg": None, "std": None, "var": None, "delta": None}
    values = _finite_numbers(durations_ms)
    if not values:
        return {"avg": None, "std": None, "var": None, "delta": None}
    delta_count = sum(
        1 for a, b in zip(values, values[1:]) if abs(b - a) > DELTA_FTIME_THRESHOLD_MS
    )
    delta_per_hour = delta_count * MS_PER_HOUR / duration_ms if duration_ms > 0 else 0.0
    return {
        "avg": _r1(sum(values) / len(values)),
        "std": _r1(statistics.pstdev(values)),
        "var": _r2(statistics.pvariance(values)),
        "delta": _r1(delta_per_hour),
    }


# ---- Smooth（R09，行为近似）----


def smooth_index(
    durations_ms: Sequence[float],
    duration_ms: float,
    ts_ms: Sequence[float] | None = None,
) -> float | None:
    """Smooth 稳帧指数——**行为近似，口径与 PerfDog 不同**（[F§3.5][F§10.1]）。

    官方未公开算法，仅给建议阈值（游戏 <8、应用滑动 <20，越低越平稳）。
    近似定义（M2 实现期定，契约 §12 规则 4，golden 可复现）：
      各 1s 窗口内帧耗时总体标准差（ms）的均值。
    方向性：序列越平稳窗口内波动越小 → 值越低；稳定 60fps（抖动 ±0.5ms）≈0.5、
    明显卡顿序列可达数十，与官方建议阈值同一数量级方向。
    单位 ms（无量纲指数的近似替代）；数值**不可**与 PerfDog Smooth 直接对比，
    报告侧恒带 note（statistics_service.SMOOTH_NOTE_*）。

    ts_ms：与 durations 等长的帧时戳（ms，任意单调基准）；缺省按帧序在
    duration_ms 上均匀铺开（中点法），仅供无时戳调用方（测试）使用；
    装配层（frameEvents 基线）恒传真实时戳。
    """
    values = _finite_numbers(durations_ms)
    if not values:
        return None
    n = len(values)
    if ts_ms is None:
        if duration_ms > 0:
            ts = [duration_ms * (i + 0.5) / n for i in range(n)]
        else:
            ts = [0.0] * n
    else:
        ts = [float(t) for t in ts_ms[:n]]
    origin = min(ts)
    windows: dict[int, list[float]] = {}
    for value, t in zip(values, ts):
        windows.setdefault(int((t - origin) // SMOOTH_WINDOW_MS), []).append(value)
    # 空窗（采样间隙）不计入——与 frameEvents 基线「非空 1s 桶才成点」同哲学
    window_stds = [statistics.pstdev(bucket) for bucket in windows.values()]
    return _r1(sum(window_stds) / len(window_stds))


# ---- 1%Low（R09，精确口径）----


def low_fps_p1(durations_ms: Sequence[float]) -> float | None:
    """1%Low（[F§3.5]，可精确复刻）：帧耗时最长 1% 的均值，1000 除之（fps）。

    「最长 1%」的帧数取 k = max(1, ceil(n×1%))（覆盖不小于 1% 的最差帧；
    n<100 时退化为最差 1 帧）。官方未规定取整方式，此为显式选择，golden 固定。
    """
    values = _finite_numbers(durations_ms)
    if not values:
        return None
    k = max(1, math.ceil(len(values) * LOW_FPS_RATIO))
    worst = sorted(values, reverse=True)[:k]
    mean_worst = sum(worst) / k
    if mean_worst <= 0:
        return None
    return _r1(1000.0 / mean_worst)


# ---- FPS 统计矩阵（R11）----


def fps_group(
    fps_values: Sequence[float],
    duration_ms: float,
    thresholds: Sequence[int],
) -> dict[str, Any]:
    """FPS 统计组（契约 §12 `fps`，[F§3.1]）。

    - avg/var/std：fps 点列均值/总体方差/标准差；
    - drop：相邻点下降 >8 帧次数 × 3600000 / duration_ms（/h）；
    - min / median：点列最小值/中位数；
    - medRange：偏离中位 ±20% 以内（含端点）点占比 ×100；
    - ge：FPS ≥ 阈值点占比 ×100，键为阈值十进制字符串、按数值升序（契约 §12）。
    空点列：数值字段 None，ge 保留阈值键、值为 None（不填假零值）。
    """
    ge: dict[str, float | None] = {str(int(t)): None for t in sorted(set(int(t) for t in thresholds))}
    values = _finite_numbers(fps_values)
    if not values:
        return {
            "avg": None,
            "var": None,
            "std": None,
            "drop": None,
            "min": None,
            "median": None,
            "medRange": None,
            "ge": ge,
        }
    n = len(values)
    drop_count = sum(1 for a, b in zip(values, values[1:]) if a - b > FPS_DROP_THRESHOLD_FPS)
    drop_per_hour = drop_count * MS_PER_HOUR / duration_ms if duration_ms > 0 else 0.0
    median = statistics.median(values)
    lo, hi = median * (1 - MED_RANGE_TOLERANCE), median * (1 + MED_RANGE_TOLERANCE)
    med_range_pct = sum(1 for v in values if lo <= v <= hi) / n * 100.0
    for threshold in sorted(set(int(t) for t in thresholds)):
        ge[str(threshold)] = _r1(sum(1 for v in values if v >= threshold) / n * 100.0)
    return {
        "avg": _r1(sum(values) / n),
        "var": _r2(statistics.pvariance(values)),
        "std": _r1(statistics.pstdev(values)),
        "drop": _r1(drop_per_hour),
        "min": _r1(min(values)),
        "median": _r1(median),
        "medRange": _r1(med_range_pct),
        "ge": ge,
    }


# ---- 组合入口（装配层复用；保证端点与 Excel/case 快照单一事实源）----


def frame_statistics(
    durations_ms: Sequence[float],
    duration_ms: float,
    ts_ms: Sequence[float] | None = None,
) -> dict[str, Any]:
    """全部逐帧耗时派生组（frameTime/jank/smooth/lowFps，契约 §12）。

    durations_ms 为空时四组数值字段均为 None；note 由装配层按数据基线附加。
    """
    values = _finite_numbers(durations_ms)
    return {
        "frameTime": frame_time_group(values, duration_ms),
        "jank": jank_group(values, duration_ms),
        "smooth": {"value": smooth_index(values, duration_ms, ts_ms)},
        "lowFps": {"p1": low_fps_p1(values)},
    }
