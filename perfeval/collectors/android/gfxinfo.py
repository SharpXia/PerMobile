from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ._events import metric_event
from .timebase import frame_epoch_ms


_INT_RE = re.compile(r"(-?\d+)")
_FLOAT_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
_WINDOW_HEADER_RE = re.compile(r"^\s*Window:\s*(\S+)\s*$", re.MULTILINE)
_COMPONENT_RE = re.compile(r"([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)")
_PROFILEDATA_OPEN_RE = re.compile(r"^\s*---PROFILEDATA---\s*$")
_PROFILEDATA_END_RE = re.compile(r"^\s*---PROFILEDATA-END---\s*$")
_WINDOW_LINE_RE = re.compile(r"^\s*Window:\s*(\S.*?)\s*$")
_GFX_PROCESS_LINE_RE = re.compile(r"^\s*\*\* Graphics info for pid \d+ \[(.+?)\] \*\*")
_INT_CELL_RE = re.compile(r"-?\d+")
_FLOAT_CELL_RE = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_HEX_CELL_RE = re.compile(r"[0-9A-Fa-f]+")

# Flags 位含义对照 METRIC_SOURCES（P0-1 主路径 A）：0x1 window、0x2 first_draw。
_FRAME_FLAG_NAMES: tuple[tuple[int, str], ...] = ((0x1, "window"), (0x2, "first_draw"))
_FRAME_REQUIRED_COLUMNS = ("Flags", "IntendedVsync", "FrameCompleted")
_NS_PER_MS = 1_000_000.0
_JANK_REFRESH_FACTOR = 1.5


def _first_number(text: str) -> float | None:
    match = _FLOAT_RE.search(text)
    return float(match.group(1)) if match else None


def parse_focused_window(text: str, *, package_name: str | None = None) -> str | None:
    """Extract the foreground Android activity component from dumpsys window."""
    candidates: list[str] = []
    for marker in ("mCurrentFocus=", "topResumedActivity=", "mResumedActivity=", "mFocusedApp="):
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if marker not in line or line.endswith("=null"):
                continue
            match = _COMPONENT_RE.search(line)
            if match is None:
                continue
            component = _normalise_component(match.group(1))
            if package_name and not component.startswith(f"{package_name}/"):
                continue
            candidates.append(component)
        if candidates:
            return candidates[-1]
    return None


def _normalise_component(component: str) -> str:
    package, separator, activity = component.partition("/")
    if not separator:
        return component
    if activity.startswith("."):
        activity = f"{package}{activity}"
    return f"{package}/{activity}"


def _select_gfxinfo_scope(
    text: str,
    *,
    window: str | None,
    prefer_window: bool,
) -> tuple[str, str | None]:
    headers = list(_WINDOW_HEADER_RE.finditer(text))
    if not headers:
        return text, None

    selected_index: int | None = None
    normalised_window = _normalise_component(window) if window else None
    if normalised_window is not None:
        for index, header in enumerate(headers):
            if _normalise_component(header.group(1)) == normalised_window:
                selected_index = index

    if selected_index is None and prefer_window:
        # dumpsys gfxinfo emits ViewRoots in creation order.  The last section
        # is the best available proxy for the target reached by an interactive
        # flow when older artifacts do not contain a foreground-window dump.
        selected_index = len(headers) - 1

    if selected_index is None:
        return text[: headers[0].start()], None

    header = headers[selected_index]
    end = headers[selected_index + 1].start() if selected_index + 1 < len(headers) else len(text)
    return text[header.end() : end], _normalise_component(header.group(1))


def parse_gfxinfo(
    text: str,
    *,
    window: str | None = None,
    prefer_window: bool = False,
) -> dict[str, Any]:
    scoped_text, selected_window = _select_gfxinfo_scope(
        text,
        window=window,
        prefer_window=prefer_window,
    )
    metrics: dict[str, Any] = {}
    for raw_line in scoped_text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if lower.startswith("total frames rendered"):
            value = _first_number(line)
            if value is not None:
                metrics["total_frames"] = int(value)
        elif lower.startswith("janky frames") and "legacy" not in lower:
            matches = _FLOAT_RE.findall(line)
            if matches:
                metrics["janky_frames"] = int(float(matches[0]))
            if len(matches) >= 2:
                metrics["jank_rate"] = float(matches[1]) / 100.0
        elif "percentile" in lower and "gpu percentile" not in lower:
            value = _first_number(line)
            if value is None:
                continue
            duration_match = re.search(r":\s*(-?\d+(?:\.\d+)?)\s*ms", line, re.IGNORECASE)
            duration = float(duration_match.group(1)) if duration_match else None
            if duration is None:
                continue
            percentile = int(value)
            metrics[f"p{percentile}_ms"] = duration
        elif lower.startswith("number missed vsync"):
            value = _first_number(line)
            if value is not None:
                metrics["missed_vsync_count"] = int(value)
        elif lower.startswith("number slow ui thread"):
            value = _first_number(line)
            if value is not None:
                metrics["slow_ui_thread_count"] = int(value)
    if "jank_rate" not in metrics and metrics.get("total_frames"):
        metrics["jank_rate"] = float(metrics.get("janky_frames", 0)) / float(metrics["total_frames"])
    if selected_window is not None:
        metrics["window"] = selected_window
    return metrics


def parse_gfxinfo_events(
    text: str,
    *,
    run_id: str,
    start_seq: int = 1,
    window: str | None = None,
    prefer_window: bool = False,
    fps_source: str | None = None,
) -> list[dict[str, Any]]:
    parsed = parse_gfxinfo(text, window=window, prefer_window=prefer_window)
    events: list[dict[str, Any]] = []
    seq = start_seq
    mapping = [
        ("frame.total_count", "total_frames", "count"),
        ("frame.janky_count", "janky_frames", "count"),
        ("frame.jank_rate", "jank_rate", "ratio"),
        ("frame.duration.p50.ms", "p50_ms", "ms"),
        ("frame.duration.p90.ms", "p90_ms", "ms"),
        ("frame.duration.p95.ms", "p95_ms", "ms"),
        ("frame.duration.p99.ms", "p99_ms", "ms"),
        ("frame.missed_vsync_count", "missed_vsync_count", "count"),
        ("frame.slow_ui_thread_count", "slow_ui_thread_count", "count"),
    ]
    for metric_name, parsed_key, unit in mapping:
        if parsed_key not in parsed:
            continue
        data: dict[str, Any] = {"window": parsed["window"]} if "window" in parsed else {}
        if fps_source is not None:
            data["fpsSource"] = fps_source
        events.append(
            metric_event(
                seq=seq,
                run_id=run_id,
                name=metric_name,
                value=parsed[parsed_key],
                unit=unit,
                collector="dumpsys.gfxinfo",
                data=data or None,
            )
        )
        seq += 1
    return events


def _split_profile_cells(line: str) -> list[str]:
    cells = [cell.strip() for cell in line.split(",")]
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _parse_frame_cell(cell: str, *, hex_ok: bool = False) -> int | float | None:
    """Parse one ``---PROFILEDATA---`` cell; NaN/blank/illegal text yields None."""
    if _INT_CELL_RE.fullmatch(cell):
        return int(cell)
    if _FLOAT_CELL_RE.fullmatch(cell):
        value = float(cell)
        return value if math.isfinite(value) else None
    if hex_ok and _HEX_CELL_RE.fullmatch(cell):
        # AOSP dumps Flags via %X, so bare hex digits are legal there.
        return int(cell, 16)
    return None


def _frame_flag_names(flags: int) -> list[str]:
    names: list[str] = []
    for bit, name in _FRAME_FLAG_NAMES:
        if flags & bit:
            names.append(name)
    # 未知高位以 0xN 透传，供报告区分 OEM 扩展位。
    for bit in range(2, 32):
        marker = 1 << bit
        if flags & marker:
            names.append(f"0x{marker:x}")
    return names


def _iter_profile_blocks(lines: list[str]) -> Iterator[dict[str, Any]]:
    window: str | None = None
    process: str | None = None
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        process_match = _GFX_PROCESS_LINE_RE.match(line)
        if process_match is not None:
            process = process_match.group(1)
            window = None
            index += 1
            continue
        window_match = _WINDOW_LINE_RE.match(line)
        if window_match is not None:
            window = window_match.group(1)
            index += 1
            continue
        if _PROFILEDATA_OPEN_RE.match(line):
            body: list[str] = []
            index += 1
            while index < total:
                inner = lines[index]
                stripped = inner.strip()
                # Blocks end at ---PROFILEDATA-END---, at the next opening
                # marker (some ROMs, e.g. One UI, repeat ---PROFILEDATA--- as
                # the closing marker), or at the first non-CSV line.  Blank
                # lines inside a block are ignored.
                if (
                    _PROFILEDATA_OPEN_RE.match(inner)
                    or _PROFILEDATA_END_RE.match(inner)
                    or (stripped and "," not in stripped)
                ):
                    break
                if stripped:
                    body.append(inner)
                index += 1
            yield {"window": window, "process": process, "lines": body}
            continue
        index += 1


def _refresh_period_ms(
    rows: list[dict[str, int | float]],
    header: list[str],
) -> tuple[float | None, str | None]:
    """Estimate the display refresh period (ms) for jank thresholding."""
    if "FrameInterval" in header:
        intervals = [row["FrameInterval"] for row in rows if row["FrameInterval"] > 0]
        if intervals:
            return statistics.median(intervals) / _NS_PER_MS, "frame_interval"
    stamp_column = "Vsync" if "Vsync" in header else "IntendedVsync"
    stamps = [row[stamp_column] for row in rows]
    deltas = [later - earlier for earlier, later in zip(stamps, stamps[1:]) if later > earlier]
    if deltas:
        return statistics.median(deltas) / _NS_PER_MS, "vsync_delta"
    return None, None


def _parse_profile_block(
    block: dict[str, Any],
    block_index: int,
) -> tuple[dict[str, Any] | None, int]:
    lines = block["lines"]
    if not lines:
        return None, 0
    header = _split_profile_cells(lines[0])
    if any(column not in header for column in _FRAME_REQUIRED_COLUMNS):
        return None, 0
    rows: list[dict[str, int | float]] = []
    skipped_rows = 0
    for line in lines[1:]:
        cells = _split_profile_cells(line)
        if len(cells) != len(header):
            skipped_rows += 1
            continue
        values: dict[str, int | float] = {}
        for column, cell in zip(header, cells):
            parsed = _parse_frame_cell(cell, hex_ok=(column == "Flags"))
            if parsed is None:
                values = {}
                break
            values[column] = parsed
        if not values:
            skipped_rows += 1
            continue
        # Uncompleted frame slots (FrameCompleted == 0) produce non-positive
        # durations and are treated as illegal rows.
        if values["FrameCompleted"] - values["IntendedVsync"] <= 0:
            skipped_rows += 1
            continue
        rows.append(values)

    refresh_period_ms, refresh_basis = _refresh_period_ms(rows, header)
    durations_ms = [
        (row["FrameCompleted"] - row["IntendedVsync"]) / _NS_PER_MS for row in rows
    ]
    if refresh_period_ms is None and durations_ms:
        refresh_period_ms = statistics.median(durations_ms)
        refresh_basis = "median_frame_duration"
    threshold_ms = refresh_period_ms * _JANK_REFRESH_FACTOR if refresh_period_ms is not None else None

    frames: list[dict[str, Any]] = []
    for row, duration_ms in zip(rows, durations_ms):
        flags = int(row["Flags"])
        frames.append(
            {
                "frame_index": 0,
                "block_index": block_index,
                "window": block["window"],
                "process": block["process"],
                "flags": flags,
                "flag_names": _frame_flag_names(flags),
                "frame_duration_ms": duration_ms,
                "jank": threshold_ms is not None and duration_ms > threshold_ms,
                "raw_ns": {column: row[column] for column in header},
            }
        )
    parsed_block = {
        "block_index": block_index,
        "window": block["window"],
        "process": block["process"],
        "columns": list(header),
        "refresh_period_ms": refresh_period_ms,
        "refresh_period_basis": refresh_basis if frames else None,
        "jank_threshold_ms": threshold_ms if frames else None,
        "frame_count": len(frames),
        "frames": frames,
    }
    return parsed_block, skipped_rows


def parse_framestats_detail(text: str) -> dict[str, Any]:
    """Parse per-frame ``---PROFILEDATA---`` blocks from dumpsys gfxinfo framestats.

    口径（对齐 METRIC_SOURCES.md P0-1 主路径 A）：
    - 表头驱动解析，兼容 AOSP 经典 16 列布局与新版 ROM（如 One UI 24 列、
      块尾省略 ---PROFILEDATA-END--- 而以另一行 ---PROFILEDATA--- 收尾）；
    - ``frame_duration_ms = (FrameCompleted - IntendedVsync) / 1e6``；
    - jank 帧 = 帧耗时 > 1.5 × 刷新周期（文档口径）。刷新周期依次取
      FrameInterval 列中位数 → Vsync 步进中位数，两者皆不可得时兜底取
      帧耗时中位数；实际依据记录在 block["refresh_period_basis"]；
    - Flags 位透传（0x1 window、0x2 first_draw，未知位以 0xN 命名）；
    - NaN / 列数不符 / FrameCompleted <= IntendedVsync 的行跳过并计数；
    - 时间戳保留纳秒原值（frame["raw_ns"]），帧耗时换算为毫秒。

    本函数只产出逐帧数据（frame_duration_ms 列表 + 每帧元数据），供 FPS 派生
    与掉帧区间定位使用；不做 summary/报告接线。
    """
    blocks: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    skipped_rows = 0
    skipped_blocks = 0
    block_index = 0
    for raw_block in _iter_profile_blocks(text.splitlines()):
        parsed_block, block_skipped_rows = _parse_profile_block(raw_block, block_index)
        skipped_rows += block_skipped_rows
        if parsed_block is None:
            skipped_blocks += 1
            continue
        for frame in parsed_block["frames"]:
            frame["frame_index"] = len(frames)
            frames.append(frame)
        blocks.append(parsed_block)
        block_index += 1
    return {
        "block_count": len(blocks),
        "blocks": blocks,
        "frame_count": len(frames),
        "frames": frames,
        "frame_duration_ms": [frame["frame_duration_ms"] for frame in frames],
        "jank_frame_count": sum(1 for frame in frames if frame["jank"]),
        "skipped_row_count": skipped_rows,
        "skipped_block_count": skipped_blocks,
    }


# --- TASK-009：逐帧数据 → frame.duration.detail.ms counter 事件（FPS 派生输入） ---

FRAME_DETAIL_METRIC_ID = "frame.duration.detail.ms"
TIMELINE_SCHEMA_VERSION = "perfeval.timeline.v1"

# CM-TASK-021 clockDomain 语义：epoch=帧起点已对齐设备墙钟（可对齐录屏/跳转）；
# frame_relative=仅相对首帧偏移（framestats 无 wall-clock，掉帧只高亮不跳转）
CLOCK_DOMAIN_EPOCH = "epoch"
CLOCK_DOMAIN_FRAME_RELATIVE = "frame_relative"


def build_frame_detail_events(
    parsed: dict[str, Any],
    *,
    run_id: str,
    start_seq: int = 1,
    collector: str,
    fps_source: str,
    boottime_anchor: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把 parse_framestats_detail / 帧时间轴结果转为逐帧 counter 事件序列。

    口径（METRIC_SOURCES §4）：事件名 ``frame.duration.detail.ms``、type=counter、
    evidenceLevel=L1；``tsMonotonicNs`` 取相对首帧的纳秒偏移（旧消费方——FPS 派生
    /jankSegments/分布——语义不变）；帧序、jank 判定与 Flags 位保留在 data
    （frameIndex/jank/flagNames），``fpsSource`` 标注来源（tp/framestats）。

    CM-TASK-021 时间对齐升格：``data.clockDomain`` 显式标注时钟域——tp 帧携带
    ``ts_boottime_ns`` 且提供 boottime↔epoch 锚点时换算 ``data.tsEpochMs``
    （domain=epoch，可对齐录屏/报告跳转）并保留 ``data.tsBoottimeNs`` 原值供
    离线复算；framestats 无 wall-clock 时 domain=frame_relative（报告掉帧只
    高亮不跳转）。
    """
    frames = parsed.get("frames") or []
    if not frames:
        return []
    origin_ns = _frame_origin_ns(frames)
    events: list[dict[str, Any]] = []
    seq = start_seq
    for frame in frames:
        # counter 事件手动构造（METRIC_SOURCES §4：frame.duration.detail.ms 为 counter）
        epoch_ms = frame_epoch_ms(frame, boottime_anchor)
        ts_boottime_ns = frame.get("ts_boottime_ns")
        data: dict[str, Any] = {
            "value": round(float(frame["frame_duration_ms"]), 6),
            "unit": "ms",
            "direction": "lower_better",
            "frameIndex": int(frame.get("frame_index", 0)),
            "jank": bool(frame.get("jank")),
            "flagNames": list(frame.get("flag_names") or []),
            "fpsSource": fps_source,
        }
        if epoch_ms is not None:
            data["tsEpochMs"] = epoch_ms
            data["clockDomain"] = CLOCK_DOMAIN_EPOCH
            if isinstance(ts_boottime_ns, (int, float)):
                data["tsBoottimeNs"] = int(ts_boottime_ns)
        else:
            data["clockDomain"] = CLOCK_DOMAIN_FRAME_RELATIVE
        events.append(
            {
                "schemaVersion": TIMELINE_SCHEMA_VERSION,
                "seq": seq,
                "runId": run_id,
                "eventId": f"{run_id}-android-{collector}-{seq:03d}",
                "type": "counter",
                "name": FRAME_DETAIL_METRIC_ID,
                "tsMonotonicNs": max(0, _frame_ts_ns(frame, origin_ns)),
                "source": {
                    "platform": "android",
                    "collector": collector,
                    "evidenceLevel": "L1",
                },
                "data": data,
            }
        )
        seq += 1
    return events


def _frame_origin_ns(frames: Sequence[Mapping[str, Any]]) -> int | None:
    for frame in frames:
        raw_ns = frame.get("raw_ns")
        if isinstance(raw_ns, Mapping) and isinstance(raw_ns.get("IntendedVsync"), (int, float)):
            return int(raw_ns["IntendedVsync"])
        ts_ns = frame.get("ts_boottime_ns")
        if isinstance(ts_ns, (int, float)):
            return int(ts_ns)
    return None


def _frame_ts_ns(frame: Mapping[str, Any], origin_ns: int | None) -> int:
    if origin_ns is None:
        return 0
    raw_ns = frame.get("raw_ns")
    if isinstance(raw_ns, Mapping) and isinstance(raw_ns.get("IntendedVsync"), (int, float)):
        return int(raw_ns["IntendedVsync"]) - origin_ns
    ts_ns = frame.get("ts_boottime_ns")
    if isinstance(ts_ns, (int, float)):
        return int(ts_ns) - origin_ns
    return 0


def parse_framestats_detail_events(
    text: str,
    *,
    run_id: str,
    start_seq: int = 1,
    fps_source: str = "framestats",
) -> list[dict[str, Any]]:
    """解析 framestats 文本并产出逐帧 counter 事件（collector 接线入口）。"""
    parsed = parse_framestats_detail(text)
    return build_frame_detail_events(
        parsed,
        run_id=run_id,
        start_seq=start_seq,
        collector="dumpsys.gfxinfo.framestats",
        fps_source=fps_source,
    )
