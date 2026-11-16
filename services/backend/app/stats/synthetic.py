"""确定性种子帧序列（synthetic 模式统计输入源；ARCH §7「synthetic provider 用
确定性种子，测试可复现」）。

定位：perfdog.statistics.v1 内核的**输入生成器**——real 数据来自 perfeval
framestats 解析（`perfeval.collectors.android.gfxinfo.parse_framestats_detail`，
gfxinfo.py:356；ask 中提到的 "framestats.py" 实际函数名以 gfxinfo.py 为准），
synthetic 数据来自本模块：同一 seed 恒产出同一序列（sha256 种子 + random.Random），
golden 测试与 synthetic 模式 fixture 共用。

注意：本模块**不**向采集链注入假数据（统计层只认事实源 timeline.ndjson /
samples.ndjson / 会话缓冲，[P§5.5]）；它服务于测试 fixture 与 synthetic 演示路径。
"""

from __future__ import annotations

import hashlib
import random
from typing import NamedTuple


class FrameEvent(NamedTuple):
    """一帧：ts_ms 为帧起点（相对基准，单调不减），duration_ms 为帧耗时。"""

    ts_ms: float
    duration_ms: float


def generate_frame_sequence(seed: str, count: int) -> list[float]:
    """确定性帧耗时序列（ms）。

    波形设计（覆盖统计内核各分支，供 golden 与演示使用）：
    - 基线 120fps（8.33ms 附近小幅抖动）；
    - 每 37 帧一次中等尖刺（40-70ms：SmallJank 区间，多数不达 Jank 阈值）；
    - 每 211 帧一次严重尖刺（120-220ms：Jank/BigJank 区间）。
    """
    if count < 0:
        raise ValueError("count 必须 >= 0")
    digest = hashlib.sha256(f"frame-seq|{seed}".encode("utf-8")).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    values: list[float] = []
    for i in range(count):
        if i % 211 == 210:
            duration = 120.0 + rng.uniform(0.0, 100.0)
        elif i % 37 == 36:
            duration = 40.0 + rng.uniform(0.0, 30.0)
        else:
            duration = 8.33 + rng.uniform(-0.6, 0.9)
        values.append(round(duration, 4))
    return values


def generate_frame_events(seed: str, count: int) -> list[FrameEvent]:
    """确定性 (帧起点, 帧耗时) 序列：下一帧起点 = 上一帧起点 + 上一帧耗时。

    语义对齐 framestats：帧耗时即显示间隔，帧起点按显示时间连续推进；
    ts_ms 相对首帧为 0（frame_relative 时钟域，gfxinfo.py:406-409）。
    """
    events: list[FrameEvent] = []
    clock_ms = 0.0
    for duration in generate_frame_sequence(seed, count):
        events.append(FrameEvent(ts_ms=round(clock_ms, 4), duration_ms=duration))
        clock_ms += duration
    return events
