"""perfeval 旧 jank 定义与 PerfDog 双条件口径的对照测试（M2 / R08 差异列）。

perfeval 口径（perfeval/collectors/android/gfxinfo.py）：
    jank 帧 = 帧耗时 > 1.5 × 刷新周期（_JANK_REFRESH_FACTOR=1.5，gfxinfo.py:29；
    阈值构造见 gfxinfo.py:324，刷新周期估计 gfxinfo.py:266-280 `_refresh_period_ms`，
    jank 标记 gfxinfo.py:338）。

perfdog.statistics.v1 口径（本仓库 app/stats/kernel.py，[F§3.3]）：
    Jank = 当前帧耗时 > 前三帧均值×2 且 > 两帧电影帧（≈83.33ms）；
    SmallJank = 条件 a 且 > 一帧电影帧（≈41.67ms）；BigJank = 条件 a 且 > 125ms。

本测试用同一段 ---PROFILEDATA--- 输入分别走 perfeval 解析器与统计内核，
断言两者判定结果的差异，并声明：**这是口径差异（两套定义并存），不是 bug**——
perfeval 的 1.5×刷新周期是文档口径（gfxinfo.py:359-368 docstring「文档口径」），
统计内核按 PerfDog 官方双条件精确复刻；summary 引擎的 jankSegments 消费的
frame["jank"] 标记仍属 perfeval 口径，统计内核不消费该标记（engine.py:361 只供
掉帧高亮，kernel.jank_classification 自行判定）。
"""

from __future__ import annotations

from typing import Any

from app.stats import jank_classification, jank_group
from perfeval.collectors.android.gfxinfo import parse_framestats_detail

# AOSP 经典 16 列 + FrameInterval（供 _refresh_period_ms 取 FrameInterval 中位数，
# gfxinfo.py:271-274）；本测试刷新周期 = 16666666ns ≈ 16.6667ms（60Hz）
_PROFILE_HEADER = (
    "Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,"
    "AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,"
    "IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DequeueBufferDuration,"
    "QueueBufferDuration,FrameInterval"
)
_REFRESH_NS = 16_666_666
_REFRESH_PERIOD_MS = _REFRESH_NS / 1_000_000.0  # ≈16.6667ms
_PERFEVAL_THRESHOLD_MS = _REFRESH_PERIOD_MS * 1.5  # ≈25.0ms（gfxinfo.py:324）


def _profiledata_text(durations_ms: list[float]) -> str:
    """构造 dumpsys gfxinfo framestats 的 ---PROFILEDATA--- 块。

    帧起点按 vsync 步进（16666666ns），FrameCompleted = IntendedVsync + 耗时×1e6，
    使 parse_framestats_detail 解析出的 frame_duration_ms 与输入一致。
    """
    lines = ["---PROFILEDATA---", _PROFILE_HEADER]
    vsync_ns = 0
    for duration_ms in durations_ms:
        completed_ns = vsync_ns + int(duration_ms * 1_000_000)
        row = [
            "0",                # Flags
            str(vsync_ns),      # IntendedVsync
            str(vsync_ns),      # Vsync
            "0",                # OldestInputEvent
            "0",                # NewestInputEvent
            "0",                # HandleInputStart
            "0",                # AnimationStart
            "0",                # PerformTraversalsStart
            "0",                # DrawStart
            "0",                # SyncQueued
            "0",                # SyncStart
            "0",                # IssueDrawCommandsStart
            "0",                # SwapBuffers
            str(completed_ns),  # FrameCompleted
            "0",                # DequeueBufferDuration
            "0",                # QueueBufferDuration
            str(_REFRESH_NS),   # FrameInterval
        ]
        lines.append(",".join(row))
        vsync_ns += _REFRESH_NS
    lines.append("---PROFILEDATA-END---")
    return "\n".join(lines) + "\n"


def test_perfeval_and_perfdog_jank_definitions_diverge() -> None:
    durations = [8.0, 8.0, 8.0, 30.0, 8.0, 100.0]
    parsed: dict[str, Any] = parse_framestats_detail(_profiledata_text(durations))

    # 同一组帧进入两个口径（解析出的帧耗时与输入一致，保证可比）
    assert parsed["frame_duration_ms"] == durations
    # refresh_period_basis / jank_threshold_ms 是 block 级字段（gfxinfo.py:342-352）
    assert parsed["blocks"][0]["refresh_period_basis"] == "frame_interval"
    assert parsed["blocks"][0]["refresh_period_ms"] == _REFRESH_PERIOD_MS

    # perfeval 口径：帧耗时 > 1.5×刷新周期（≈25ms）→ 30ms 与 100ms 均为 jank
    perfeval_jank_indexes = [
        frame["frame_index"] for frame in parsed["frames"] if frame["jank"]
    ]
    assert perfeval_jank_indexes == [3, 5]
    assert parsed["jank_frame_count"] == 2

    # PerfDog 双条件口径：30ms 满足条件 a（>前三帧均值 8×2）但不达两帧电影帧
    # 83.33ms → 非 Jank 亦非 SmallJank（30 < 41.67）；100ms 两条件均满足 → Jank
    perfdog = jank_group(durations, duration_ms=sum(durations))
    perfdog_flags = jank_classification(durations)
    perfdog_jank_indexes = [i for i, hit in enumerate(perfdog_flags["jank"]) if hit]
    assert perfdog_jank_indexes == [5]
    assert perfdog["jank"] == 1
    assert perfdog["small"] == 1  # 100ms > 41.67ms（30ms 不达）
    assert perfdog["big"] == 0  # 100ms < 125ms

    # 差异断言：同一输入，perfeval 判 2 次 jank、PerfDog 口径判 1 次——
    # 30ms 帧在 perfeval 口径下是 jank，在 PerfDog 口径下两者皆非。
    # 这是口径差异（1.5×刷新周期 vs 双条件电影帧），不是任何一方的实现错误。
    assert perfeval_jank_indexes != perfdog_jank_indexes
    assert set(perfdog_jank_indexes) < set(perfeval_jank_indexes)


def test_perfeval_threshold_matches_documented_rule() -> None:
    """perfeval 阈值口径自证：100ms 帧必为 jank、8ms 帧必非（1.5×16.6667≈25ms）。"""
    parsed = parse_framestats_detail(_profiledata_text([8.0, 8.0, 8.0, 100.0]))
    assert parsed["blocks"][0]["jank_threshold_ms"] == _PERFEVAL_THRESHOLD_MS
    flags = [frame["jank"] for frame in parsed["frames"]]
    assert flags == [False, False, False, True]
