"""时间对齐（CM-TASK-021，SCREEN_RECORD_SYNC §2）。

三方时钟锚点：
- ``t0``：logcat -v epoch 首条时间戳（性能时间轴原点，设备墙钟）；
- flow marker：PERFEVAL_EVENT 行的 epoch（跳转目标 + 时间轴竖线）；
- video：screenrecord 启动时刻（设备墙钟），首个可见帧按 +1s 编码延迟估算。

全部锚点写入 manifest ``context.clockAnchors``，离线可复算；视频跳转
``video.currentTime = (eventEpochMs - videoFirstFrameEpochMs) / 1000``。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .logcat import parse_logcat

TIMELINE_SCHEMA_VERSION = "perfeval.timeline.v1"

# screenrecord 启动编码延迟：首个可见帧按启动时刻 +1s 估算（SCREEN_RECORD_SYNC §2）
VIDEO_FIRST_FRAME_DELAY_MS = 1000

# 独立 `date +%s%3N` 失败时的兜底输出解析（含 /proc/uptime 双行输出）
_EPOCH_MS_RE = re.compile(r"\b(\d{12,15})\b")
_UPTIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*$", re.MULTILINE)


def parse_device_clock_output(stdout: str, *, host_epoch_ms: int | None = None) -> dict[str, Any] | None:
    """解析 `adb shell date +%s%3N; cat /proc/uptime` 输出为 boottime↔epoch 锚点。

    返回 ``{"epochMs": int, "boottimeNs": int, "source": ...}``；
    source=device_date（date 成功解析）/ host_epoch_approx（date 不可解析、
    epoch 走 host 兜底，误差含主从时钟偏差）；uptime 可用时附 boottimeNs；
    全部不可用返回 None。
    """
    epoch_ms: int | None = None
    date_parsed = False
    for token in (stdout or "").split():
        match = _EPOCH_MS_RE.fullmatch(token.strip())
        if match is not None:
            epoch_ms = int(match.group(1))
            date_parsed = True
            break
    uptime_match = _UPTIME_RE.search(stdout or "")
    boottime_ns: int | None = None
    if uptime_match is not None:
        boottime_ns = int(float(uptime_match.group(1)) * 1_000_000_000)
    if epoch_ms is None and host_epoch_ms is not None:
        epoch_ms = host_epoch_ms
    if epoch_ms is None and boottime_ns is None:
        return None
    anchor: dict[str, Any] = {
        "source": "device_date" if date_parsed else "host_epoch_approx"
    }
    if epoch_ms is not None:
        anchor["epochMs"] = epoch_ms
    if boottime_ns is not None:
        anchor["boottimeNs"] = boottime_ns
    return anchor


def frame_epoch_ms(frame: Mapping[str, Any], boottime_anchor: Mapping[str, Any] | None) -> int | None:
    """用 boottime↔epoch 锚点把帧的 ts_boottime_ns 换算为 epoch 毫秒。"""
    if boottime_anchor is None:
        return None
    epoch_ms = boottime_anchor.get("epochMs")
    boottime_ns = boottime_anchor.get("boottimeNs")
    ts_boottime_ns = frame.get("ts_boottime_ns")
    if (
        not isinstance(epoch_ms, (int, float))
        or not isinstance(boottime_ns, (int, float))
        or not isinstance(ts_boottime_ns, (int, float))
    ):
        return None
    return int(epoch_ms + (ts_boottime_ns - boottime_ns) / 1_000_000)


def derive_clock_anchors(
    *,
    logcat_text: str | None,
    video_anchors: Mapping[str, Any] | None = None,
    markers: list[dict[str, Any]] | None = None,
    samplers_anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """汇总三方锚点为 manifest ``context.clockAnchors``；无任何锚点时返回 None。

    video_anchors（runner 侧记录）：
    - ``startEpochMs``：screenrecord 启动命令发出时刻（设备墙钟 epoch ms）；
    - ``firstFrameEpochMs``：startEpochMs + 编码延迟估算（误差口径见 method）；
    - ``method`` / ``timeLimitS`` / ``segments``：口径声明与录制计划。

    samplers_anchor（B1，TASK-023-fix R1）：``SamplingScheduler.start()`` 同一刻的
    设备时钟读数（parse_device_clock_output 产出）——采样序列 series ts 的原点是
    start()（host 单调 elapsed），与 t0（logcat 首行）相差 setup 间隙，报告侧
    marker 竖线必须以本锚点换算才能与采样曲线同轴。
    """
    t0_epoch_ms: int | None = None
    parsed_markers: list[dict[str, Any]] = list(markers or [])
    if logcat_text:
        parsed = parse_logcat(logcat_text)
        t0_epoch_ms = parsed.get("first_epoch_ms")
        if not parsed_markers:
            for event in parsed["custom_events"]:
                if not isinstance(event, dict):
                    continue
                name = event.get("name")
                epoch_ms = event.get("tsEpochMs")
                if isinstance(name, str) and name and isinstance(epoch_ms, (int, float)):
                    parsed_markers.append({"name": name, "epochMs": int(epoch_ms)})
    if t0_epoch_ms is None and not video_anchors and not samplers_anchor:
        return None

    anchors: dict[str, Any] = {}
    if t0_epoch_ms is not None:
        anchors["t0EpochMs"] = t0_epoch_ms
        anchors["t0Source"] = "logcat.epoch.first_line"
        anchors["clockDomain"] = "device_epoch"
    if video_anchors:
        video = dict(video_anchors)
        if t0_epoch_ms is not None and isinstance(video.get("firstFrameEpochMs"), int):
            video["offsetMs"] = int(video["firstFrameEpochMs"]) - t0_epoch_ms
        anchors["video"] = video
    if parsed_markers:
        anchors["markers"] = parsed_markers
    if samplers_anchor:
        anchors["samplers"] = _normalize_samplers_anchor(samplers_anchor)
    return anchors or None


# B1（TASK-023-fix2 R2）：producer（parse_device_clock_output）产出键名是
# epochMs，报告侧 consumer（summary_report._samplers_clock_anchor）读
# startEpochMs——逐字透传会键名断链（真实链路锚点永远读 None，竖线永不渲染）。
# 落盘时统一映射为 startEpochMs（与 video.startEpochMs 命名对齐）并附误差口径。
SAMPLERS_ANCHOR_METHOD = "samplerStart+deviceClockRead（读数晚于 SamplingScheduler.start() 约 0.1s，marker 竖线相对曲线左偏）"


def _normalize_samplers_anchor(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """把设备时钟读数映射为 clockAnchors.samplers 落盘形状。

    - ``startEpochMs``：取 producer 的 epochMs（容忍已映射形态的 startEpochMs）；
    - ``source`` / ``boottimeNs``：原样保留（离线可复算）；
    - ``method``：误差口径声明（读数晚于 start() 约 0.1s，方向左偏）。
    无有效 epochMs 时返回仅含 method 的占位（报告侧读不到 startEpochMs 即
    优雅不渲染竖线，不报错）。
    """
    normalized: dict[str, Any] = {}
    epoch_ms = anchor.get("epochMs", anchor.get("startEpochMs"))
    if isinstance(epoch_ms, (int, float)) and not isinstance(epoch_ms, bool):
        normalized["startEpochMs"] = int(epoch_ms)
    source = anchor.get("source")
    if source:
        normalized["source"] = str(source)
    boottime_ns = anchor.get("boottimeNs")
    if isinstance(boottime_ns, (int, float)) and not isinstance(boottime_ns, bool):
        normalized["boottimeNs"] = int(boottime_ns)
    normalized.setdefault("method", SAMPLERS_ANCHOR_METHOD)
    return normalized


def marker_events_from_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 timeline 事件提取 flow marker（跳转目标，按时间排序）。

    F-fix（R1-B2）：仅接受 logcat 来源事件（flow marker 经 PERFEVAL_EVENT 上报，
    parse_logcat_events 产出 collector=logcat）——排除 tp/framestats 的逐帧
    counter 事件，否则录屏+tp 主路径下每帧事件混入 markers（上千条）导致
    manifest/summary 膨胀、报告标记行失效。
    """
    markers: list[dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {"metric", "counter"}:
            continue
        source = event.get("source")
        collector = source.get("collector") if isinstance(source, Mapping) else None
        if collector != "logcat":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        epoch_ms = data.get("tsEpochMs")
        name = event.get("name")
        if isinstance(epoch_ms, (int, float)) and isinstance(name, str) and name:
            markers.append({"name": name, "epochMs": int(epoch_ms)})
    markers.sort(key=lambda marker: marker["epochMs"])
    return markers
