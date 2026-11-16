from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from perfeval.core.artifacts import ArtifactRecord, sha256_of_path
from .plan import SCREENRECORD_MIN_VALID_BYTES

from .gfxinfo import (
    build_frame_detail_events,
    parse_focused_window,
    parse_framestats_detail_events,
    parse_gfxinfo_events,
)
from .logcat import parse_logcat_events
from .meminfo import parse_meminfo_events
from .perfetto_trace import filter_frames_by_process, query_frame_timeline
from .samples import parse_samples_events, read_samples

ARTIFACT_METRICS = {
    "gfxinfo.txt": ["frame.jank_rate", "frame.duration.p95.ms"],
    "gfxinfo-framestats.txt": [
        "frame.jank_rate",
        "frame.duration.p95.ms",
        "frame.render",
        "frame.duration.detail.ms",
    ],
    "window.txt": [],
    "meminfo.txt": ["memory.pss.peak_mb", "memory.java_heap.mb"],
    "logcat.txt": ["startup.cold.ms", "stability.crash.count", "stability.anr.count"],
    "perfetto.pftrace": ["system.trace", "frame.duration.detail.ms"],
    "samples.ndjson": ["memory.pss.peak_mb"],
    # 录屏（CM-TASK-020）：不关联对比指标；内容属敏感 artifact，仅以相对路径引用
    # （SCREEN_RECORD_SYNC §3：LLM payload / 对比摘要不含录屏内容）
    "screen.mp4": [],
}

# --- TASK-013：FPS 来源优先级链（METRIC_SOURCES §1）---
# pftrace+trace_processor（tp）→ framestats 逐帧 → 聚合兜底（aggregate，降级 C）
FPS_SOURCE_TP = "tp"
FPS_SOURCE_FRAMESTATS = "framestats"
FPS_SOURCE_AGGREGATE = "aggregate"
TRACE_PROCESSOR_DIAGNOSTIC = "trace_processor.unavailable"
FPS_DEGRADED_DIAGNOSTIC = "fps.source.degraded"


def parse_android_artifacts(
    artifact_dir: str | Path,
    *,
    run_id: str,
    package_name: str | None = None,
    start_seq: int = 1,
    clock_anchors: dict | None = None,
) -> list[dict]:
    root = Path(artifact_dir)
    events: list[dict] = []
    seq = start_seq

    focused_window = None
    window_dump = root / "window.txt"
    if window_dump.exists():
        focused_window = parse_focused_window(
            _read_artifact_text(window_dump),
            package_name=package_name,
        )

    # TASK-013：FPS 来源优先级链（tp → framestats → 聚合兜底），逐帧事件 + 降级诊断。
    # 链路先行，且诊断事件无条件落 timeline（F1-fix：不随指标事件门控丢弃——
    # parsed/frame_detail 皆空时（如只有损坏 pftrace 且 tp 失败）诊断仍可见）
    # CM-TASK-021：boottime↔epoch 锚点透传，tp 帧事件升格为 epoch 对齐语义
    boottime_anchor = (clock_anchors or {}).get("boottimeAnchor")
    frame_detail, frame_chain_diagnostics = _frame_detail_chain(
        root,
        run_id=run_id,
        start_seq=seq,
        package_name=package_name,
        boottime_anchor=boottime_anchor,
    )
    events.extend(frame_detail)
    events.extend(frame_chain_diagnostics)
    seq += len(frame_detail) + len(frame_chain_diagnostics)

    # 聚合事件：无逐帧来源时（降级 C）在解析点标注 fpsSource=aggregate（METRIC_SOURCES §1）
    aggregate_fps_source = FPS_SOURCE_AGGREGATE if not frame_detail else None
    parsed: list[dict] = []
    framestats = root / "gfxinfo-framestats.txt"
    if framestats.exists():
        parsed = parse_gfxinfo_events(
            _read_artifact_text(framestats),
            run_id=run_id,
            start_seq=seq,
            window=focused_window,
            prefer_window=True,
            fps_source=aggregate_fps_source,
        )

    gfxinfo = root / "gfxinfo.txt"
    if not parsed and gfxinfo.exists():
        parsed = parse_gfxinfo_events(
            _read_artifact_text(gfxinfo),
            run_id=run_id,
            start_seq=seq,
            fps_source=aggregate_fps_source,
        )
    events.extend(parsed)
    seq += len(parsed)

    meminfo = root / "meminfo.txt"
    # 采样器原始记录（CM-001 §6）：离线 --android-artifacts 模式可由 samples.ndjson 重算
    sample_records: list = []
    has_pss_stream = False
    samples_file = root / "samples.ndjson"
    if samples_file.exists():
        sample_records = read_samples(samples_file)
        if sample_records:
            parsed = parse_samples_events(sample_records, run_id=run_id, start_seq=seq)
            has_pss_stream = any(event["name"] == "memory.pss.peak_mb" for event in parsed)
            # 周期采样已覆盖 PSS 序列时跳过 meminfo 结束快照；否则回退快照并标 final_snapshot
            events.extend(parsed)
            seq += len(parsed)

    if meminfo.exists():
        parsed = parse_meminfo_events(
            _read_artifact_text(meminfo),
            run_id=run_id,
            start_seq=seq,
            include_pss=not has_pss_stream,
            pss_semantic=None if has_pss_stream else "final_snapshot",
        )
        events.extend(parsed)
        seq += len(parsed)

    logcat = root / "logcat.txt"
    if logcat.exists():
        parsed = parse_logcat_events(
            _read_artifact_text(logcat),
            run_id=run_id,
            package_name=package_name,
            start_seq=seq,
        )
        events.extend(parsed)
    return events


def _read_artifact_text(path: Path) -> str:
    """Decode device-produced text without failing the entire collection on bad bytes."""

    return path.read_text(encoding="utf-8", errors="replace")


def _find_pftrace(root: Path) -> Path | None:
    """定位 perfetto trace：约定名 perfetto.pftrace 优先，否则取工件目录内首个 .pftrace。"""
    conventional = root / "perfetto.pftrace"
    if conventional.exists():
        return conventional
    for candidate in sorted(root.glob("*.pftrace")):
        if candidate.is_file():
            return candidate
    return None


def _frame_detail_chain(
    root: Path,
    *,
    run_id: str,
    start_seq: int,
    package_name: str | None,
    boottime_anchor: Mapping[str, Any] | None = None,
) -> tuple[list[dict], list[dict]]:
    """FPS 来源优先级链（CM-TASK-013，METRIC_SOURCES §1 三层降级）。

    1. ``.pftrace`` 存在且 trace_processor 可用 → ``perfetto.tp`` 逐帧事件；
       FrameRecord 按 process_name 过滤目标进程（SurfaceFlinger 合成帧混在同名
       Actual Timeline track，process 归属 surfaceflinger，可精确剔除）；
    2. 否则 framestats 存在且有逐帧行 → ``dumpsys.gfxinfo.framestats`` 逐帧事件；
    3. 都没有 → 聚合兜底（本函数返回空，调用方标注 fpsSource=aggregate）。

    每级降级产出 type=diagnostic 事件（trace_processor.unavailable /
    fps.source.degraded），随 timeline 落盘并由 summary 派生层并入 summary
    diagnostics。返回 (逐帧 counter 事件, 降级 diagnostic 事件)。
    """
    seq = start_seq
    detail: list[dict] = []
    diagnostics: list[dict] = []

    trace_path = _find_pftrace(root)
    if trace_path is not None:
        result = query_frame_timeline(trace_path)
        if not result.available:
            diagnostics.append(
                _frame_chain_diagnostic_event(
                    run_id,
                    seq,
                    name=TRACE_PROCESSOR_DIAGNOSTIC,
                    message="; ".join(result.diagnostics) or "trace_processor unavailable",
                )
            )
            seq += 1
        elif result.frames:
            filtered = filter_frames_by_process(result.frames, package_name)
            if filtered:
                detail = build_frame_detail_events(
                    {
                        "frames": [
                            {
                                "frame_index": frame.frame_index,
                                "frame_duration_ms": frame.duration_ms,
                                "jank": frame.jank,
                                "flag_names": [],
                                "ts_boottime_ns": frame.ts_boottime_ns,
                            }
                            for frame in filtered
                        ]
                    },
                    run_id=run_id,
                    start_seq=seq,
                    collector="perfetto.tp",
                    fps_source=FPS_SOURCE_TP,
                    boottime_anchor=boottime_anchor,
                )
                return detail, diagnostics
            diagnostics.append(
                _frame_chain_diagnostic_event(
                    run_id,
                    seq,
                    name=FPS_DEGRADED_DIAGNOSTIC,
                    message=(
                        "fps.source=framestats: frame timeline 无目标进程"
                        f" {package_name or '(未指定)'} 的帧（process 过滤后为空）"
                    ),
                )
            )
            seq += 1
        else:
            diagnostics.append(
                _frame_chain_diagnostic_event(
                    run_id,
                    seq,
                    name=FPS_DEGRADED_DIAGNOSTIC,
                    message="fps.source=framestats: frame timeline 为空（trace 无 FrameTimeline slice）",
                )
            )
            seq += 1

    framestats = root / "gfxinfo-framestats.txt"
    gfxinfo = root / "gfxinfo.txt"
    if framestats.exists():
        detail = parse_framestats_detail_events(
            _read_artifact_text(framestats),
            run_id=run_id,
            start_seq=seq,
        )
        if detail:
            return detail, diagnostics

    if trace_path is None and not framestats.exists() and gfxinfo.exists():
        # 降级 C 诊断仅在确实发生聚合兜底（存在聚合 frame 来源）时记录；
        # 无任何 frame artifact 的 run（如启动早期失败的空目录）不算降级
        diagnostics.append(
            _frame_chain_diagnostic_event(
                run_id,
                seq,
                name=FPS_DEGRADED_DIAGNOSTIC,
                message=(
                    "fps.source=aggregate: 无逐帧来源（pftrace+trace_processor / "
                    "framestats 均不可用），FPS 统计降级为聚合口径（METRIC_SOURCES 降级 C）"
                ),
            )
        )
    return detail, diagnostics


def _frame_chain_diagnostic_event(
    run_id: str,
    seq: int,
    *,
    name: str,
    message: str,
) -> dict:
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-android-frame-chain-diag-{seq:03d}",
        "type": "diagnostic",
        "name": name,
        "tsMonotonicNs": 0,
        "source": {
            "platform": "host",
            "collector": "android.artifacts",
            "evidenceLevel": "L0",
        },
        "data": {"severity": "warning", "code": name, "message": message},
    }


def index_android_artifacts(artifact_dir: str | Path) -> list[dict]:
    root = Path(artifact_dir)
    records: list[dict] = []
    for filename, metrics in ARTIFACT_METRICS.items():
        path = root / filename
        if not path.exists() or not path.is_file():
            continue
        # smoke-fix：空录屏（<1KB，ROM 限制等）无有效帧数据，不入索引——
        # 在线路径已在 runner 删除文件，此处兜底离线直读目录场景
        if filename == "screen.mp4" and path.stat().st_size < SCREENRECORD_MIN_VALID_BYTES:
            continue
        record = ArtifactRecord(
            artifact_id=path.stem.replace("-", "_"),
            path=str(path),
            sha256=sha256_of_path(path),
            size_bytes=path.stat().st_size,
            collector=_collector_for_filename(filename),
            metric_data={"relatedMetrics": metrics},
        )
        records.append(record.to_dict())
    # 录屏分段文件（screen-segNNN.mp4，CM-TASK-020）：同样进索引，不关联指标
    for path in sorted(root.glob("screen-*.mp4")):
        if not path.is_file():
            continue
        if path.stat().st_size < SCREENRECORD_MIN_VALID_BYTES:
            continue
        record = ArtifactRecord(
            artifact_id=path.stem.replace("-", "_"),
            path=str(path),
            sha256=sha256_of_path(path),
            size_bytes=path.stat().st_size,
            collector=_collector_for_filename(path.name),
            metric_data={"relatedMetrics": []},
        )
        records.append(record.to_dict())
    return records


def _collector_for_filename(filename: str) -> str:
    if filename.startswith("gfxinfo"):
        return "dumpsys.gfxinfo"
    if filename.startswith("meminfo"):
        return "dumpsys.meminfo"
    if filename.startswith("logcat"):
        return "logcat"
    if filename.startswith("window"):
        return "dumpsys.window"
    if filename.startswith("perfetto"):
        return "perfetto"
    if filename.startswith("samples"):
        return "samplers"
    if filename.startswith("screen"):
        return "screenrecord"
    return "android"
