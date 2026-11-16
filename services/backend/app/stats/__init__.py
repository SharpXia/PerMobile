"""perfdog.statistics.v1 统计层（app/stats，M2）。

- kernel.py：纯函数统计内核（Jank 族/Stutter/FrameTime/Smooth/1%Low/FPS 矩阵，
  公式逐条对照 research/perfdog-features.md [F§3.1-3.5]）；
- synthetic.py：确定性种子帧序列（synthetic 模式输入源，golden 可复现）。

装配层（statistics_service）从事实源（run 目录 timeline.ndjson / samples.ndjson /
会话内存缓冲）取数后调用内核；本包不做 I/O。
"""

from .kernel import (
    BIG_JANK_THRESHOLD_MS,
    JANK_THRESHOLD_MS,
    SMALL_JANK_THRESHOLD_MS,
    fps_group,
    frame_statistics,
    frame_time_group,
    jank_classification,
    jank_group,
    low_fps_p1,
    smooth_index,
)
from .synthetic import FrameEvent, generate_frame_events, generate_frame_sequence

__all__ = [
    "BIG_JANK_THRESHOLD_MS",
    "JANK_THRESHOLD_MS",
    "SMALL_JANK_THRESHOLD_MS",
    "FrameEvent",
    "fps_group",
    "frame_statistics",
    "frame_time_group",
    "generate_frame_events",
    "generate_frame_sequence",
    "jank_classification",
    "jank_group",
    "low_fps_p1",
    "smooth_index",
]
