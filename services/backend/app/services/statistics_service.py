"""statistics_service：perfdog.statistics.v1 装配层（契约 §12，ARCH §4.7）。

职责：从**事实源**取帧数据 → 调 app/stats/kernel 纯函数 → 附加 note/基线声明 →
输出契约 §12 载荷。单一事实源约束（契约 §13.1/§14、ARCH §4.7）：Excel Sum 行与
case 索引统计快照一律调用本模块，不得另算。

数据基线（契约 §12 `frameDataBasis`）：

- `frameEvents`：run 目录 timeline.ndjson 的逐帧 counter 事件
  （`frame.duration.detail.ms`，与 perfeval summary engine 同款判据 engine.py:319-380）；
  fps 点列 = 每 1s 窗口帧数（与 PerfDog 1 秒点列同节拍，interval 恒 1000）。
- `fpsSeries`：`frame.fps.mean` 轮询点列（会话内存缓冲 / samples.ndjson），
  点列原样，间隔如实声明（interval ≠1000ms 时 fps.note 声明节拍差，规则 2）。
- `none`：两者皆无——全组数值字段 None + 组级 note（不填假零值，[R§8.6]）。

durationMs = 帧点列覆盖时长，非会话总时长（契约 §12 规则 3）：
- fpsSeries：点列首尾点差（会话 golden 500ms×61 点 → 30000）；
- frameEvents：1s 桶覆盖时长 = 非空桶数 × 1000（run golden 62 桶 → 62000）；
  桶间空洞按采样间隙处理，不成 0fps 点（framestats 轮询的间隙不是冻结秒）。

real 逐帧数据来源：perfeval framestats 解析（parse_framestats_detail，
perfeval/collectors/android/gfxinfo.py:356）→ build_frame_detail_events
（gfxinfo.py:412）写入 timeline.ndjson；synthetic 帧序列见 app/stats/synthetic.py
（测试 fixture 与演示用，不注入采集链）。本模块只读 timeline/samples，直接兼容
两种来源——M1 会话只有 fps 轮询通道（registry.py:44），real FPS 通道落地前
会话统计恒为 fpsSeries/none 基线（契约 §12）。

power 统计组（2026-11-10 契约 §12 delta，Sum(Battery) + FPower）：

- ``power.sum``：Sum(Battery) = Σ power.energy.mah 逐窗增量（power-2 口径——
  real 采样器每条记录 = 单窗增量，collector_service.py synthetic 生成器同口径），
  会话累计；无数据 → null + note。
- ``power.frame``：FPower——能耗窗口按时间密度分摊到帧（帧事件来自
  _read_frame_events 既有判据），输出帧级能耗 mean/p95/total；估算口径
  （semantic=estimated 继承），note 恒带。
- 两子组均按会话全量数据计算，不随 §18 区间过滤（契约 §12 delta 声明）。
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

from perfeval.collectors.android.samples import read_samples
from perfeval.core.timeline import TimelineRowError, read_timeline

from ..errors import ApiError, validation_error
from ..stats import kernel
from .marker_service import read_markers_sidecar

SCHEMA_VERSION = "perfdog.statistics.v1"
FRAME_DETAIL_METRIC_ID = "frame.duration.detail.ms"
FPS_METRIC_ID = "frame.fps.mean"
POWER_ENERGY_METRIC_ID = "power.energy.mah"
DEFAULT_FPS_GE: tuple[int, ...] = (18, 25)  # 契约 §12 缺省阈值（TDR 口径 [F§3.1][I§2.9]）
FRAME_SERIES_INTERVAL_MS = 1000  # frameEvents 基线 1s 分桶（PerfDog 1 秒点列节拍）

# ---- note 原文（契约 §12 golden 逐字；修改示例必须连同契约与测试一起改）----

SMOOTH_NOTE_COMPUTED = (
    "近似口径：Smooth 官方未公开算法公式，按行为近似实现，数值与 PerfDog 不可直接对比"
    "（[F§3.5][F§10.1]）"
)
SMOOTH_NOTE_FPS_SERIES = (
    "近似口径：Smooth 官方未公开算法公式（[F§3.5][F§10.1]）；且 fpsSeries 基线无逐帧耗时，不计算"
)
SMOOTH_NOTE_NONE = (
    "近似口径：Smooth 官方未公开算法公式（[F§3.5][F§10.1]）；且无帧数据，不计算"
)
_FPS_BEAT_NOTE = (
    "fps 点列间隔 {interval}ms（frame.fps.mean 轮询），与 PerfDog 1 秒点列口径存在节拍差；"
    "Drop(FPS) 等相邻点统计受节拍影响，报告须声明间隔（[F§3.1]）"
)
_FPS_BEAT_NOTE_UNKNOWN = (
    "fps 点列间隔未知（不足两个点且无配置节拍），与 PerfDog 1 秒点列口径存在节拍差；"
    "Drop(FPS) 等相邻点统计受节拍影响，报告须声明间隔（[F§3.1]）"
)
FTIME_NOTE_FPS_SERIES = (
    "fpsSeries 基线无逐帧耗时（仅 frame.fps.mean 轮询序列），不计算；不填假零值（[R§8.6]）"
)
JANK_NOTE_FPS_SERIES = "Jank 族双条件判定需逐帧耗时（[F§3.3]），fpsSeries 基线不可计算"
LOW_FPS_NOTE_FPS_SERIES = (
    "1%Low 需逐帧耗时（帧耗时最长 1% 均值换算 fps，[F§3.5]），fpsSeries 基线不可计算"
)
FPS_NOTE_NONE = (
    "无任何帧数据（real 模式 FPS 通道未落地且未采集 frame.fps.mean）；不计算，不填假零值（[R§8.6]）"
)
FTIME_NOTE_NONE = "无逐帧耗时数据，不计算"
JANK_NOTE_NONE = "无逐帧耗时数据，不计算"
LOW_FPS_NOTE_NONE = "无逐帧耗时数据，不计算"

# ---- power 统计组 note（2026-11-10 契约 §12 delta，golden 逐字；同步同上）----

POWER_SUM_NOTE = (
    "Sum(Battery)=Σ power.energy.mah 逐窗增量求和（power-2 口径：每条记录=单窗增量）；"
    "能耗为估算口径（采样端 semantic=estimated），与 PerfDog Sum(Battery) 不可直接对比"
)
POWER_SUM_NOTE_NONE = (
    "无 power.energy.mah 数据点，Sum(Battery) 不计算；不填假零值（[R§8.6]）"
)
POWER_FRAME_NOTE = (
    "FPower：能耗窗口按时间密度分摊到帧的估算值（semantic=estimated 继承自"
    " power.energy.mah；分摊自第二条增量起的窗口，首条增量无窗口端点不计入）；"
    "数值与 PerfDog FPower 不可直接对比"
)
POWER_FRAME_NOTE_NONE = (
    "无能耗序列或无逐帧数据（或能耗与帧时域无重叠），FPower 分摊不计算；"
    "不填假零值（[R§8.6]）"
)


# ---- 参数解析（契约 §12 fpsGe / §18 fromMs,toMs）----


def parse_fps_ge(raw: str | None) -> list[int]:
    """`?fpsGe=18,25` → 升序去重阈值列表；缺省 (18, 25)。

    非法（空段/非整数/<=0）→ 400 validation_error（契约 §3：参数类型错误）。
    """
    if raw is None:
        return list(DEFAULT_FPS_GE)
    thresholds: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        try:
            value = int(token)
        except ValueError:
            raise ApiError(
                "validation_error",
                f"fpsGe 必须是逗号分隔的正整数，收到 {token!r}",
                details={"field": "fpsGe"},
            ) from None
        if value <= 0:
            raise ApiError(
                "validation_error",
                f"fpsGe 阈值必须为正整数，收到 {value}",
                details={"field": "fpsGe"},
            )
        if value not in thresholds:
            thresholds.append(value)
    return sorted(thresholds)


def parse_interval_ms(params: Any) -> tuple[int | None, int | None]:
    """`?fromMs=&toMs=` → 闭区间端点（契约 §18）；未给 → (None, None) = §12 现行为。

    校验（契约 §18）：非整数、负值、fromMs > toMs → 400 validation_error
    （``details.field``）。
    """
    from_ms = _parse_epoch_param(params.get("fromMs"), "fromMs")
    to_ms = _parse_epoch_param(params.get("toMs"), "toMs")
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise ApiError(
            "validation_error",
            f"fromMs ({from_ms}) 不得大于 toMs ({to_ms})",
            details={"field": "fromMs"},
        )
    return from_ms, to_ms


def _parse_epoch_param(raw: str | None, field: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise validation_error(f"{field} 必须是整数（epoch ms），收到 {raw!r}", field=field) from None
    if value < 0:
        raise validation_error(f"{field} 不得为负值", field=field)
    return value


def parse_by_label_segment(raw: str | None) -> bool:
    """`?byLabelSegment=1` → 是否按 label 段切分输出（R32 数据面，§18 实现期扩展）。

    取值 `1|true`（大小写不敏感）= 开、`0|false` = 关、缺参/空串 = 关（§12/§18
    现行为，既有 golden 形态不变，§11.1）；其余 → 400 validation_error。
    """
    if raw is None or raw == "":
        return False
    lowered = raw.strip().lower()
    if lowered in ("1", "true"):
        return True
    if lowered in ("0", "false"):
        return False
    raise ApiError(
        "validation_error",
        f"byLabelSegment 必须是 1/true/0/false，收到 {raw!r}",
        details={"field": "byLabelSegment"},
    )


# ---- 事实源读取（run 目录）----


def resolve_run_dir(runs_dir: Path, run_id: str) -> Path:
    """runId → run 目录；不存在或名称含路径分隔符/`.`/`..` → 404 report_not_found。

    runId 形如 `run-<caseName>`（亦接受裸 caseName）。统计端点（§12）与
    export.xlsx（§13.1）共用同一防护，保证两端口径一致。
    """
    name = run_id.removeprefix("run-")
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ApiError("report_not_found", f"run {run_id} 不存在")
    run_path = runs_dir / name
    if not run_path.is_dir():
        raise ApiError("report_not_found", f"run {run_id} 不存在")
    return run_path


def _iter_frame_payloads(timeline_path: Path) -> list[dict[str, Any]]:
    """timeline.ndjson → 逐帧事件 payload 列表（engine.py:319-380 同款判据）。

    接受 `frame.duration.detail.ms` counter 事件（real 路径来自
    gfxinfo.parse_framestats_detail → build_frame_detail_events；synthetic fixture
    事件形状一致）。timeline 解析失败视为无逐帧数据（降级到下一基线，不 500
    ——summary engine 对坏事件同为跳过策略）。
    """
    if not timeline_path.is_file():
        return []
    try:
        with timeline_path.open("r", encoding="utf-8") as source:
            events = read_timeline(source)
    except (TimelineRowError, OSError):
        return []
    payloads: list[dict[str, Any]] = []
    for event in events:
        # read_timeline 产出 TimelineEvent（dataclass）；Mapping 兼容直读路径
        payload = event.to_dict() if hasattr(event, "to_dict") else event
        if not isinstance(payload, dict) or payload.get("name") != FRAME_DETAIL_METRIC_ID:
            continue
        event_type = payload.get("type")
        if event_type is not None and event_type != "counter":
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        value = data.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            continue
        payloads.append({"payload": payload, "data": data, "value": float(value)})
    return payloads


def read_frame_detail(timeline_path: Path) -> list[dict[str, Any]]:
    """timeline.ndjson → 逐帧明细（供 §13.1 FrameInfo 明细表；与统计装配同一批事件）。

    每行：ts_ms（相对时戳，tsMonotonicNs/1e6，缺失回退 data.tsEpochMs、再回退 0）、
    duration_ms、frame_index（data.frameIndex，缺失 None）、ts_epoch_ms
    （data.tsEpochMs，clockDomain=frame_relative 的事件缺失 None——不编造 epoch）。
    按 ts_ms 升序；同一批事件在统计装配与 Excel writer 中顺序一致。
    """
    rows: list[dict[str, Any]] = []
    for item in _iter_frame_payloads(timeline_path):
        payload, data = item["payload"], item["data"]
        ts_ns = payload.get("tsMonotonicNs")
        if isinstance(ts_ns, (int, float)) and not isinstance(ts_ns, bool):
            ts_ms = float(ts_ns) / 1_000_000.0
        else:
            epoch_ms = data.get("tsEpochMs")
            ts_ms = (
                float(epoch_ms)
                if isinstance(epoch_ms, (int, float)) and not isinstance(epoch_ms, bool)
                else 0.0
            )
        frame_index = data.get("frameIndex")
        ts_epoch = data.get("tsEpochMs")
        rows.append(
            {
                "ts_ms": ts_ms,
                "duration_ms": item["value"],
                "frame_index": (
                    frame_index
                    if isinstance(frame_index, int) and not isinstance(frame_index, bool)
                    else None
                ),
                "ts_epoch_ms": (
                    float(ts_epoch)
                    if isinstance(ts_epoch, (int, float)) and not isinstance(ts_epoch, bool)
                    else None
                ),
            }
        )
    rows.sort(key=lambda row: row["ts_ms"])
    return rows


def _read_frame_events(timeline_path: Path) -> list[tuple[float, float]]:
    """timeline.ndjson → [(帧相对时戳 ms, 帧耗时 ms)]；read_frame_detail 的窄投影。"""
    return [
        (row["ts_ms"], row["duration_ms"]) for row in read_frame_detail(timeline_path)
    ]


def _read_fps_points(samples_path: Path) -> list[tuple[int, float]]:
    """samples.ndjson → [(tsMs, fps)]（read_samples 已丢弃非数值行，[R§2.10]）。"""
    if not samples_path.is_file():
        return []
    try:
        records = read_samples(samples_path)
    except OSError:
        return []
    points = [
        (int(record.ts_ms), float(record.value))
        for record in records
        if record.metric_id == FPS_METRIC_ID
    ]
    points.sort(key=lambda pair: pair[0])
    return points


def _read_energy_points(samples_path: Path) -> list[tuple[int, float]]:
    """samples.ndjson → [(tsMs, mAh)]（power.energy.mah 逐窗增量，power-2 口径）。"""
    if not samples_path.is_file():
        return []
    try:
        records = read_samples(samples_path)
    except OSError:
        return []
    points = [
        (int(record.ts_ms), float(record.value))
        for record in records
        if record.metric_id == POWER_ENERGY_METRIC_ID
    ]
    points.sort(key=lambda pair: pair[0])
    return points


# ---- power 统计组（2026-11-10 契约 §12 delta：Sum(Battery) + FPower 帧分摊）----


def _power_sum_section(energy_points: list[tuple[int, float]]) -> dict[str, Any]:
    """power.sum：Sum(Battery) = Σ power.energy.mah（逐窗增量求和，会话累计口径）。"""
    if not energy_points:
        return {"totalMah": None, "windowCount": 0, "note": POWER_SUM_NOTE_NONE}
    total = sum(mah for _, mah in energy_points)
    return {
        "totalMah": round(total, 3),
        "windowCount": len(energy_points),
        "note": POWER_SUM_NOTE,
    }


def _apportion_energy_to_frames(
    energy_points: list[tuple[int, float]],
    frame_events: list[tuple[float, float]],
) -> list[float]:
    """能耗窗口按时间密度分摊到帧（FPower，估算口径）。

    每条 energy 记录 = 单窗增量（power-2）：段 [ts_{i-1}, ts_i] 上的功率密度
    = mah_i / (ts_i - ts_{i-1})；帧能耗 = Σ 密度 × 帧区间 [ts_f, ts_f+duration_f]
    与段的交叠时长。首条增量无前趋端点不入分摊；能耗时线之外的帧不计入
    （首条时戳之前与末段右端点之后开始的帧——估算不外推，不造数）。
    帧完整铺满能耗窗口时 Σ 帧能耗 = Σ_{i≥2} mah_i（守恒）。
    """
    segments: list[tuple[float, float, float]] = []
    for index in range(1, len(energy_points)):
        start_ts, _ = energy_points[index - 1]
        end_ts, mah = energy_points[index]
        span = end_ts - start_ts
        if span <= 0:
            continue
        segments.append((start_ts, end_ts, mah / span))
    if not segments:
        return []
    first_energy_ts = energy_points[0][0]
    horizon_end = segments[-1][1]  # 能耗估算时域上界（末段右端点）
    shares: list[float] = []
    for frame_ts, frame_duration in frame_events:
        # 能耗时线之外的帧不计入（估算不外推，不造数）：时线之前无密度、
        # 时线之后（末段右端点以后开始的帧）无能耗窗口覆盖
        if frame_ts < first_energy_ts or frame_ts >= horizon_end:
            continue
        frame_start = frame_ts
        frame_end = frame_ts + frame_duration
        total = 0.0
        for seg_start, seg_end, density in segments:
            overlap = min(frame_end, seg_end) - max(frame_start, seg_start)
            if overlap > 0:
                total += density * overlap
        shares.append(total)
    return shares


def _p95(values: list[float]) -> float | None:
    """最近秩（nearest-rank）95 分位；空序列 → None。"""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _power_frame_section(
    energy_points: list[tuple[int, float]],
    frame_events: list[tuple[float, float]],
) -> dict[str, Any]:
    """power.frame：FPower 帧级能耗统计（mean/p95/total，估算口径，note 恒带）。"""
    if len(energy_points) < 2 or not frame_events:
        return {
            "meanMah": None,
            "p95Mah": None,
            "totalMah": None,
            "frameCount": 0,
            "note": POWER_FRAME_NOTE_NONE,
        }
    frame_energies = _apportion_energy_to_frames(energy_points, frame_events)
    if not frame_energies:
        return {
            "meanMah": None,
            "p95Mah": None,
            "totalMah": None,
            "frameCount": 0,
            "note": POWER_FRAME_NOTE_NONE,
        }
    total = sum(frame_energies)
    return {
        "meanMah": round(total / len(frame_energies), 3),
        "p95Mah": round(_p95(frame_energies) or 0.0, 3),
        "totalMah": round(total, 3),
        "frameCount": len(frame_energies),
        "note": POWER_FRAME_NOTE,
    }


def _power_section(
    energy_points: list[tuple[int, float]],
    frame_events: list[tuple[float, float]],
) -> dict[str, Any]:
    """契约 §12 power 组（sum + frame 两子组）；不随 §18 区间过滤（会话累计口径）。"""
    return {
        "sum": _power_sum_section(energy_points),
        "frame": _power_frame_section(energy_points, frame_events),
    }


# ---- 组内装配（note 附加规则见契约 §12 计算规则 1/2）----


def _fps_section(
    fps_values: list[float],
    duration_ms: int,
    interval_ms: int | None,
    fps_ge: list[int],
) -> dict[str, Any]:
    section = kernel.fps_group(fps_values, duration_ms, fps_ge)
    if interval_ms is not None and interval_ms != FRAME_SERIES_INTERVAL_MS:
        section["note"] = _FPS_BEAT_NOTE.format(interval=int(interval_ms))
    elif interval_ms is None:
        section["note"] = _FPS_BEAT_NOTE_UNKNOWN
    # interval == 1000：与 PerfDog 1 秒点列同节拍，无 note（absence = 精确口径）
    return section


def _frame_sections(
    durations_ms: list[float],
    duration_ms: int,
    ts_ms: list[float] | None,
    smooth_note: str,
) -> dict[str, dict[str, Any]]:
    sections = kernel.frame_statistics(durations_ms, duration_ms, ts_ms)
    sections["smooth"]["note"] = smooth_note
    return sections


def _none_frame_sections() -> dict[str, dict[str, Any]]:
    return {
        "frameTime": {**kernel.frame_time_group([], 0), "note": FTIME_NOTE_NONE},
        "jank": {**kernel.jank_group([], 0), "note": JANK_NOTE_NONE},
        "smooth": {"value": None, "note": SMOOTH_NOTE_NONE},
        "lowFps": {"p1": None, "note": LOW_FPS_NOTE_NONE},
    }


# ---- 两条装配路径（契约 §12 两端点）----


def build_run_statistics(
    runs_dir: Path,
    run_id: str,
    fps_ge: list[int],
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> dict[str, Any]:
    """runId 路径：读 run 目录（timeline.ndjson → samples.ndjson 兜底）。

    runId 形如 `run-<caseName>`（亦接受裸 caseName）；目录不存在或名称含路径
    分隔符/`.`/`..` → 404 report_not_found（resolve_run_dir，与 §13.1 同防护）。

    区间（契约 §18）：``fromMs``/``toMs`` 给出时先按各自时戳域闭区间过滤参与
    计算的帧序列 / fps 点列再按 §12 口径现算（**同一统计内核，不另算第二份**）；
    ``durationMs`` 改为区间内首尾点差（区间内无点 → 0）；顶层回显 fromMs/toMs。
    时戳域说明：后端自产 run 的 timeline/samples ts 均为 epoch ms（契约 §1）；
    perfeval 离线 run 的 timeline 帧时戳为相对域——过滤按各序列自身时戳域执行。

    power 组（2026-11-10 契约 §12 delta）：Sum(Battery) 与 FPower 帧分摊，按
    会话全量能耗/帧数据计算，**不随 §18 区间过滤**（会话累计口径，契约声明）；
    无能耗数据时组内数值 null + note（不填假零值）。
    """
    run_path = resolve_run_dir(runs_dir, run_id)
    has_interval = from_ms is not None or to_ms is not None

    frames = _read_frame_events(run_path / "timeline.ndjson")
    # power 组用未过滤帧事件（区间过滤前取值；下方区间路径会重绑 frames 变量）
    power_section = _power_section(
        _read_energy_points(run_path / "samples.ndjson"), frames
    )
    if frames:
        if has_interval:
            frames = _filter_pairs(frames, from_ms, to_ms)
        if not frames:
            return _interval_none_baseline(
                run_id=run_id, fps_ge=fps_ge, from_ms=from_ms, to_ms=to_ms,
                power_section=power_section,
            )
        durations = [duration for _, duration in frames]
        ts_ms = [ts for ts, _ in frames]
        origin = ts_ms[0]
        buckets: dict[int, int] = {}
        for ts in ts_ms:
            index = int((ts - origin) // FRAME_SERIES_INTERVAL_MS)
            buckets[index] = buckets.get(index, 0) + 1
        fps_values = [float(count) for _, count in sorted(buckets.items())]
        # 无区间：非空桶数×1000（§12 现行为）；有区间：区间内首尾点差（§18）
        duration_ms = (
            int(ts_ms[-1] - ts_ms[0]) if has_interval else len(buckets) * FRAME_SERIES_INTERVAL_MS
        )
        body: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "runId": run_id,
            "frameDataBasis": "frameEvents",
            "frameSeriesIntervalMs": FRAME_SERIES_INTERVAL_MS,
            "durationMs": duration_ms,
            "fps": kernel.fps_group(fps_values, duration_ms, fps_ge),
            **_frame_sections(durations, duration_ms, ts_ms, SMOOTH_NOTE_COMPUTED),
            "power": power_section,
        }
        return _echo_interval(body, from_ms, to_ms)

    fps_points = _read_fps_points(run_path / "samples.ndjson")
    if fps_points:
        if has_interval:
            fps_points = _filter_pairs(fps_points, from_ms, to_ms)
        if not fps_points:
            return _interval_none_baseline(
                run_id=run_id, fps_ge=fps_ge, from_ms=from_ms, to_ms=to_ms,
                power_section=power_section,
            )
        fps_values = [value for _, value in fps_points]
        duration_ms = int(fps_points[-1][0] - fps_points[0][0])
        interval_ms = _manifest_declared_interval_ms(run_path)
        if interval_ms is None:
            interval_ms = _measured_interval_ms(fps_points)
        body = {
            "schemaVersion": SCHEMA_VERSION,
            "runId": run_id,
            "frameDataBasis": "fpsSeries",
            "frameSeriesIntervalMs": interval_ms,
            "durationMs": duration_ms,
            "fps": _fps_section(fps_values, duration_ms, interval_ms, fps_ge),
            # fpsSeries 无逐帧耗时：frameTime/jank/lowFps 组级 note
            **_frame_sections_with_fps_series_notes(),
            "power": power_section,
        }
        return _echo_interval(body, from_ms, to_ms)
    if has_interval:
        return _interval_none_baseline(
            run_id=run_id, fps_ge=fps_ge, from_ms=from_ms, to_ms=to_ms,
            power_section=power_section,
        )
    return _none_baseline(run_id=run_id, fps_ge=fps_ge, power_section=power_section)


def _filter_pairs(
    pairs: list[tuple[float, float]], from_ms: int | None, to_ms: int | None
) -> list[tuple[float, float]]:
    """闭区间 [fromMs, toMs] 过滤（契约 §18，含端点）；输入须已按 ts 升序。"""
    return [
        (ts, value)
        for ts, value in pairs
        if (from_ms is None or ts >= from_ms) and (to_ms is None or ts <= to_ms)
    ]


def _echo_interval(
    body: dict[str, Any], from_ms: int | None, to_ms: int | None
) -> dict[str, Any]:
    """契约 §18：请求给出 fromMs/toMs 时顶层回显；未给不回显（§12 形态不变）。"""
    if from_ms is not None:
        body["fromMs"] = from_ms
    if to_ms is not None:
        body["toMs"] = to_ms
    return body


def _interval_none_baseline(
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    fps_ge: list[int],
    from_ms: int | None,
    to_ms: int | None,
    power_section: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """区间内无任何帧/fps 数据 → none 基线同形 + 区间回显（契约 §18）。"""
    body = _none_baseline(
        run_id=run_id, session_id=session_id, fps_ge=fps_ge,
        power_section=power_section,
    )
    return _echo_interval(body, from_ms, to_ms)


# ---- R32 数据面：label 段切分统计（§18 实现期收敛，`byLabelSegment` 查询参数）----


def _run_data_end_ms(run_path: Path) -> int | None:
    """run 数据结束时刻（末段右端点）：帧序列与 fps 点列末点时戳的较大者。

    帧时戳可能为浮点（相对域）→ 上界取 ceil 才能含末帧（闭区间含端点，与 §18
    过滤口径一致）；两序列皆空 → None（无数据，末段右端开）。
    """
    end: int | None = None
    frames = _read_frame_events(run_path / "timeline.ndjson")
    if frames:
        end = int(math.ceil(frames[-1][0]))
    fps_points = _read_fps_points(run_path / "samples.ndjson")
    if fps_points:
        candidate = fps_points[-1][0]
        end = candidate if end is None else max(end, candidate)
    return end


def build_label_segments(
    runs_dir: Path,
    run_id: str,
    fps_ge: list[int],
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> list[dict[str, Any]]:
    """label 段切分统计（R32 数据面）：sidecar label 序列 → 段边界 → 段内 §18 统计。

    段口径（契约 §15.1/§15.4 同源）：段 = 从该 label 的 ``tsMs`` 起、至下一个
    label 或数据结束；闭区间右端 = 下一个 label ``tsMs - 1``（恰在下一 label
    时刻的数据点归下一段）。细节（实现期决策，契约 §18 注记同步声明）：

    - **无 label** → ``[]``（无段可切，不伪造段；顶层 body 仍为整段统计）；
    - **首个 label 前的数据** 不属于任何 label 段（如实不伪造归属，前端 ``All``
      行由顶层 body 承载）；
    - **末段** 右端 = 数据结束时刻（帧/fps 末点较大者；run 无任何数据 →
      ``toMs: null``，段内为 none 基线）；
    - **与请求区间无交集的段不出行**（``fromMs``/``toMs`` 与段边界求交后为空）；
      同刻双 label 产生的零宽段同样不出行（下一段从该刻起完整承接）；
    - 每段 ``statistics`` = build_run_statistics(段边界)——**同一 M2 统计内核，
      不另算第二份**（契约 §18「复用统计内核」同款约束）；段内时戳域过滤与 §18
      同语义（按各序列自身时戳域执行）。
    """
    run_path = resolve_run_dir(runs_dir, run_id)
    sidecar = read_markers_sidecar(run_path)
    labels = [
        marker
        for marker in ((sidecar or {}).get("markers") or [])
        if isinstance(marker, dict)
        and marker.get("kind") == "label"
        and isinstance(marker.get("tsMs"), int)
        and not isinstance(marker.get("tsMs"), bool)
    ]
    if not labels:
        return []
    labels.sort(key=lambda marker: int(marker["tsMs"]))
    data_end_ms = _run_data_end_ms(run_path)
    segments: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        start = int(label["tsMs"])
        if index + 1 < len(labels):
            end: int | None = int(labels[index + 1]["tsMs"]) - 1
        else:
            end = data_end_ms
        # 请求区间裁剪（§18 区间参数与段边界求交）
        if from_ms is not None and start < from_ms:
            start = from_ms
        if to_ms is not None and end is not None and end > to_ms:
            end = to_ms
        if end is not None and start > end:
            continue  # 零宽/无交集段：不出行（段间空隙处不留空行）
        element: dict[str, Any] = {
            "name": str(label.get("name")),
            "fromMs": start,
            "toMs": end,
            "statistics": build_run_statistics(
                runs_dir, run_id, fps_ge, from_ms=start, to_ms=end
            ),
        }
        if isinstance(label.get("color"), str):
            element["color"] = label["color"]
        segments.append(element)
    return segments


def _frame_sections_with_fps_series_notes() -> dict[str, dict[str, Any]]:
    sections = _frame_sections([], 0, None, SMOOTH_NOTE_FPS_SERIES)
    sections["frameTime"]["note"] = FTIME_NOTE_FPS_SERIES
    sections["jank"]["note"] = JANK_NOTE_FPS_SERIES
    sections["lowFps"]["note"] = LOW_FPS_NOTE_FPS_SERIES
    return sections


def build_session_statistics(
    session: Any,
    fps_ge: list[int],
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> dict[str, Any]:
    """sessionId 路径：会话内存缓冲（monitoring/recording/stopped/saved 均可算）。

    会话侧只有 `frame.fps.mean` 轮询通道（M1，registry.py:44），恒为 fpsSeries 或
    none 基线（契约 §12）。间隔声明取会话**配置** intervalMs（registry 缺省 500，
    与 note 文案「轮询」语义一致、确定性可测）；metric 未在当前启用集时回退实测
    中位差。null 点（进程缺失瞬时缺口）不入统计（契约 §1：持久层只存数值点）。
    区间（契约 §18）：先按 epoch ms 闭区间过滤 fps 点列再现算；给出区间时顶层
    回显 fromMs/toMs；区间内无点 → none 基线同形 + 回显。
    """
    points = sorted(
        (int(row["tsMs"]), float(row["value"]))
        for row in session.records()
        if row["metricId"] == FPS_METRIC_ID
        and isinstance(row["value"], (int, float))
        and not isinstance(row["value"], bool)
    )
    # power 组（§12 delta）：能耗序列取会话缓冲；会话侧无逐帧数据（fpsSeries/none
    # 基线，§12），FPower 分摊恒不可计算 → null + note
    energy_points = sorted(
        (int(row["tsMs"]), float(row["value"]))
        for row in session.records()
        if row["metricId"] == POWER_ENERGY_METRIC_ID
        and isinstance(row["value"], (int, float))
        and not isinstance(row["value"], bool)
    )
    power_section = _power_section(energy_points, [])
    if from_ms is not None or to_ms is not None:
        points = _filter_pairs(points, from_ms, to_ms)
    if not points:
        if from_ms is not None or to_ms is not None:
            return _interval_none_baseline(
                session_id=session.id, fps_ge=fps_ge, from_ms=from_ms, to_ms=to_ms,
                power_section=power_section,
            )
        return _none_baseline(
            session_id=session.id, fps_ge=fps_ge, power_section=power_section
        )

    fps_values = [value for _, value in points]
    duration_ms = int(points[-1][0] - points[0][0])
    interval_ms: int | None = None
    configured = getattr(session, "metrics", {}).get(FPS_METRIC_ID)
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        interval_ms = configured
    elif len(points) >= 2:
        interval_ms = _measured_interval_ms(points)
    body = {
        "schemaVersion": SCHEMA_VERSION,
        "sessionId": session.id,
        "frameDataBasis": "fpsSeries",
        "frameSeriesIntervalMs": interval_ms,
        "durationMs": duration_ms,
        "fps": _fps_section(fps_values, duration_ms, interval_ms, fps_ge),
        **_frame_sections_with_fps_series_notes(),
        "power": power_section,
    }
    return _echo_interval(body, from_ms, to_ms)


def _none_baseline(
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    fps_ge: list[int],
    power_section: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """none 基线（契约 §12 第三个 golden：全组 None + note，不填假零值）。

    power 组如实计算（能耗序列独立于帧数据，§12 delta）——缺省 null 形态。
    """
    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "sessionId": session_id,
        "frameDataBasis": "none",
        "frameSeriesIntervalMs": None,
        "durationMs": 0,
        "fps": {
            **kernel.fps_group([], 0, fps_ge),
            "note": FPS_NOTE_NONE,
        },
        **_none_frame_sections(),
        "power": power_section if power_section is not None else _power_section([], []),
    }
    if run_id is None:
        payload.pop("runId")
    if session_id is None:
        payload.pop("sessionId")
    return payload


def _measured_interval_ms(points: list[tuple[int, float]]) -> int | None:
    """点列间隔的实测声明：相邻时戳差的中位数（四舍五入到整毫秒）。"""
    deltas = [later - earlier for (earlier, _), (later, _) in zip(points, points[1:]) if later > earlier]
    if not deltas:
        return None
    return int(round(statistics.median(deltas)))


# ---- case 索引统计快照（契约 §14；ARCH §4.7 单一事实源约束）----

# §14 stats 快照的 samples 派生键：metricId → (快照键, 聚合口径 max|mean)
# peakMemoryMb ← memory.pss.peak_mb 序列 max；avgAppCpuPct/avgPowerMa ← 序列 mean
CASE_SAMPLE_METRICS: tuple[tuple[str, str, str], ...] = (
    ("memory.pss.peak_mb", "peakMemoryMb", "max"),
    ("cpu.proc.pct", "avgAppCpuPct", "mean"),
    ("power.current.ma", "avgPowerMa", "mean"),
)
CASE_STATS_KEYS: tuple[str, ...] = (
    "avgFps",
    "smooth",
    "jankPer10min",
    "peakMemoryMb",
    "avgAppCpuPct",
    "avgPowerMa",
)


def build_case_stats_snapshot(runs_dir: Path, run_id: str) -> dict[str, float | None]:
    """run 目录 → §14 统计快照（登记/重建时刻冻结，与 §12 统计端点同源同值）。

    - `avgFps`/`smooth`/`jankPer10min` ← build_run_statistics（perfdog.statistics.v1，
      与 `GET /api/runs/{runId}/statistics` 同值）；
    - `peakMemoryMb`/`avgAppCpuPct`/`avgPowerMa` ← samples.ndjson 序列的 max/mean；
    - 无数据字段为 None，不填 0（[R§8.6]）。调用方保证 run 目录存在，否则 404
      report_not_found（build_run_statistics 的既有防护）。
    """
    statistics_body = build_run_statistics(runs_dir, run_id, list(DEFAULT_FPS_GE))
    fps_section = statistics_body.get("fps") or {}
    smooth_section = statistics_body.get("smooth") or {}
    jank_section = statistics_body.get("jank") or {}
    snapshot: dict[str, float | None] = {
        "avgFps": _number_or_none(fps_section.get("avg")),
        "smooth": _number_or_none(smooth_section.get("value")),
        "jankPer10min": _number_or_none(jank_section.get("per10min")),
    }
    for metric_id, key, aggregate in CASE_SAMPLE_METRICS:
        snapshot[key] = None
    sample_values: dict[str, list[float]] = {metric_id: [] for metric_id, _, _ in CASE_SAMPLE_METRICS}
    samples_path = resolve_run_dir(runs_dir, run_id) / "samples.ndjson"
    if samples_path.is_file():
        try:
            records = read_samples(samples_path)
        except OSError:
            records = []
        for record in records:
            bucket = sample_values.get(record.metric_id)
            if bucket is not None and isinstance(record.value, (int, float)) and not isinstance(record.value, bool):
                bucket.append(float(record.value))
    for metric_id, key, aggregate in CASE_SAMPLE_METRICS:
        values = sample_values[metric_id]
        if not values:
            continue  # 无数据 → None，不填 0
        snapshot[key] = max(values) if aggregate == "max" else sum(values) / len(values)
    return snapshot


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _manifest_declared_interval_ms(run_path: Path) -> int | None:
    """run 目录 manifest.json 声明的 frame.fps.mean intervalMs（本后端写盘即含）。

    声明节拍优先于实测（与会话路径同语义：「轮询点列间隔」指配置节拍）；
    旧 run 目录（[R§2.12] 兼容导入）可能无 manifest/该指标 → 回退实测。
    """
    manifest_path = run_path / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for entry in manifest.get("metrics") or []:
        if (
            isinstance(entry, dict)
            and entry.get("metricId") == FPS_METRIC_ID
            and isinstance(entry.get("intervalMs"), int)
            and not isinstance(entry.get("intervalMs"), bool)
            and entry["intervalMs"] > 0
        ):
            return entry["intervalMs"]
    return None
