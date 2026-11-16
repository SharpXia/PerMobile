"""深度分析数据面（M7 R50，契约 §22.1）。

数据面**复用 perfeval ``perfetto_trace.py``，不复制实现**（ARCH §1 路线 A 同仓库
import）：FrameTimeline SQL 与解析走 ``query_frame_timeline``（FRAME_TIMELINE_SQL
+ ``parse_frame_timeline``，perfetto_trace.py:57-86/:474），帧元素 = ``FrameRecord
.to_dict()`` 形状原样透出（perfetto_trace.py:432-446，不加壳不改名）。trace_processor
是可选增强依赖（ADR-0001）：二进制缺失/查询失败时 ``query_frame_timeline`` 不抛异常，
返回 ``available=False`` 空结果——本服务照契约转 200 空结构 + note（无数据是常态，
不回退伪造，[R§8.6]）。

M7 实现期新增（契约 §22.1 如实声明，perfeval 现有 SQL 仅 FrameTimeline）：

- **CPU 调度片段** ``SCHED_SQL``：trace_processor ``thread_state`` 表（含 on-CPU
  ``Running`` 行与 ``R``/``S`` 等等待/睡眠行）按线程条带聚合，取单帧时间窗
  ``[ts_boottime_ns, ts_boottime_ns + dur_ns]`` 内的片段；``startEpochMs`` 经
  clock_snapshot 的 BOOTTIME→REALTIME 偏移换算为 §1 epoch ms 口径（偏移不可得时
  为 null，不猜）。状态映射 ``THREAD_STATE_MAP``：trace_processor 原始串 → 展示态
  （Running/Runnable/Sleeping/Uninterruptible/Stopped/Zombie/Idle）；未列出的
  原始串原样透传（不臆测映射）。未闭合片段（``dur <= 0``）不产出负时长条目。
- **run 内 trace 工件定位** ``resolve_trace_artifact``：``artifacts/trace.pftrace``
  → run 根 ``trace.pftrace`` → ``artifacts/`` 下任一 ``*.pftrace``（按名升序取首）。
  定位顺序是本模块的数据面约定（契约未固化文件名，如实声明）。
- **simpleperf 火焰图数据面**（flamegraph-compare-missing 缺口，2026-11-10，M8+）：
  run 内 ``simpleperf-report.txt``（diagnose --mode cpu/full 落盘）经 perfeval
  ``parse_simpleperf_report_text``（报告面同源单一实现）解析为 stat 卡 + 火焰图树
  （flat 表 → Command→SharedObject→Symbol overhead 分布树；-g → 简报调用树）。
  帧**列表概览**（缺省形态）带 ``simpleperf`` 数据面；无工件 / 不可解析 →
  ``available=false`` + note（不回退伪造，[R§8.6]）。单帧 Overview 卡形态不带
  该字段（帧卡聚焦单帧窗口）。

单帧 Overview 卡口径如实声明：卡片只含 FrameTimeline 帧字段与 CPU 调度片段；
[I§2.7] 引擎模式 stat/profile 数据卡依赖 UE/Unity 引擎集成（R59，M8+），不在本期。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from perfeval.core.report.summary_report import parse_simpleperf_report_text

from ..errors import validation_error
from .statistics_service import resolve_run_dir

SOURCE_FRAME_TIMELINE = "perfetto.frame_timeline"

SOURCE_SIMPLEPERF = "simpleperf.report.txt"

SIMPLEPERF_REPORT_MAIN_NAME = "simpleperf-report.txt"

SIMPLEPERF_EMPTY_NOTE = (
    "run 无 simpleperf report 工件（未运行 diagnose android --mode cpu/full 或采集失败），"
    "CPU 热点火焰图不可用；不回退伪造（[R§8.6]）"
)

SIMPLEPERF_UNPARSED_NOTE = (
    "simpleperf report 存在但未见可解析的样本表行（可能为失败占位文本），"
    "CPU 热点火焰图不可用；不回退伪造（[R§8.6]）"
)

# 契约 §22.1 golden 空结构 note（逐字同源；修改契约示例必须连同本常量一起改）
EMPTY_NOTE = (
    "run 无 perfetto trace 工件（未采集 android.surfaceflinger.frametimeline"
    " 或 trace_processor 不可用），深度分析数据面为空；不回退伪造帧数据（[R§8.6]）"
)

TRACE_MAIN_NAME = "trace.pftrace"

# thread_state 原始串 → 展示态（M7 数据面约定；未列出 → 原样透传）
THREAD_STATE_MAP: dict[str, str] = {
    "Running": "Running",
    "R": "Runnable",
    "R+": "Runnable",
    "S": "Sleeping",
    "D": "Uninterruptible",
    "DK": "Uninterruptible",
    "DF": "Uninterruptible",
    "T": "Stopped",
    "t": "Stopped",
    "Z": "Zombie",
    "I": "Idle",
}

# 单帧 CPU 调度片段查询（M7 实现期新增，契约 §22.1；thread_state 含 on-CPU
# Running 与等待态行，sched_slice 仅 Running）。窗口参数由本模块解析出的帧数据
# 构造（int 强制，无外部输入直拼）。
SCHED_SQL_TEMPLATE = """
select
  tstate.ts as ts,
  tstate.dur as dur,
  tstate.state as raw_state,
  tstate.cpu as cpu,
  t.tid as tid,
  t.name as thread_name,
  p.pid as pid,
  p.name as process_name
from thread_state tstate
join thread t on tstate.utid = t.utid
left join process p on t.upid = p.upid
where tstate.ts >= {start_ns} and tstate.ts < {end_ns}
order by tstate.ts, t.tid
"""


def resolve_trace_artifact(run_path: Path) -> Path | None:
    """run 目录 → perfetto trace 工件路径；无 → None（空结构路径）。

    定位顺序（本模块数据面约定）：``artifacts/trace.pftrace`` → run 根
    ``trace.pftrace`` → ``artifacts/*.pftrace`` 名升序首个。
    """
    candidates = [run_path / "artifacts" / TRACE_MAIN_NAME, run_path / TRACE_MAIN_NAME]
    artifacts_dir = run_path / "artifacts"
    if artifacts_dir.is_dir():
        candidates.extend(sorted(artifacts_dir.glob("*.pftrace")))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def resolve_simpleperf_report(run_path: Path) -> Path | None:
    """run 目录 → simpleperf report 工件路径；无 → None（不可用数据面路径）。

    定位顺序（与 ``resolve_trace_artifact`` 同构约定）：
    ``artifacts/simpleperf-report.txt`` → run 根 → ``artifacts/`` 下任一同名
    （按名升序取首；0 字节文件按缺失处理）。
    """
    candidates = [
        run_path / "artifacts" / SIMPLEPERF_REPORT_MAIN_NAME,
        run_path / SIMPLEPERF_REPORT_MAIN_NAME,
    ]
    artifacts_dir = run_path / "artifacts"
    if artifacts_dir.is_dir():
        candidates.extend(sorted(artifacts_dir.glob(SIMPLEPERF_REPORT_MAIN_NAME)))
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def build_simpleperf_face(run_path: Path) -> dict[str, Any]:
    """run 目录 → simpleperf 数据面（stat 卡 + 火焰图树，契约 §22.1 M8+ 扩展）。

    解析复用 perfeval 报告面单一实现（``parse_simpleperf_report_text``）；无工件 →
    available=false + note；工件存在但不可解析 → available=true + stat/flamegraph
    为 None + note（不回退伪造，[R§8.6]）。
    """
    report_path = resolve_simpleperf_report(run_path)
    if report_path is None:
        return {
            "available": False,
            "source": None,
            "stat": None,
            "flamegraph": None,
            "note": SIMPLEPERF_EMPTY_NOTE,
        }
    source = report_path.relative_to(run_path).as_posix()
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "available": False,
            "source": None,
            "stat": None,
            "flamegraph": None,
            "note": SIMPLEPERF_UNPARSED_NOTE,
        }
    face = parse_simpleperf_report_text(text)
    if not face["entryCount"] and not face["events"]:
        return {
            "available": True,
            "source": source,
            "stat": None,
            "flamegraph": None,
            "note": SIMPLEPERF_UNPARSED_NOTE,
        }
    stat = {
        "arch": face["arch"],
        "events": face["events"],
        "mode": face["mode"],
        "basis": face["basis"],
        "entryCount": face["entryCount"],
        "maxDepth": face["maxDepth"],
        "unparsedLines": face["unparsedLines"],
    }
    flamegraph = (
        {"tree": face["tree"], "basis": face["basis"], "maxDepth": face["maxDepth"]}
        if face["tree"] is not None
        else None
    )
    return {
        "available": True,
        "source": source,
        "stat": stat,
        "flamegraph": flamegraph,
        "note": None,
    }


def empty_body(run_id: str, *, simpleperf_face: dict[str, Any] | None = None) -> dict[str, Any]:
    """契约 §22.1 空结构（200 + note）：无 trace 工件或 trace_processor 不可用。

    ``simpleperf_face``：调用方持 run 目录时传入 ``build_simpleperf_face`` 结果
    （trace 与 simpleperf 独立采集，空 trace ≠ 空 simpleperf）；None → 按缺失
    如实呈现（不伪造）。
    """
    return {
        "runId": run_id,
        "traceAvailable": False,
        "source": None,
        "frames": [],
        "frameCount": 0,
        "jankCount": 0,
        "jankRate": None,
        "note": EMPTY_NOTE,
        "simpleperf": simpleperf_face
        or {
            "available": False,
            "source": None,
            "stat": None,
            "flamegraph": None,
            "note": SIMPLEPERF_EMPTY_NOTE,
        },
    }


def build_deep_analysis(
    runs_dir: Path,
    run_id: str,
    frame_index: int | None = None,
    *,
    trace_processor_environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """契约 §22.1 装配：帧列表概览（缺省）或单帧 Overview 卡（``frame_index``）。

    ``trace_processor_environ``：trace_processor 发现链的 environ 注入面（perfeval
    ``resolve_trace_processor(environ=...)`` 同参，测试以假二进制驱动 SQL 路径）；
    None 时用进程环境。

    run 不存在由 :func:`resolve_run_dir` 抛 404 report_not_found；frameIndex 越界
    （traceAvailable=true 且无该帧）→ 400 validation_error（契约 §22.1）。
    """
    from perfeval.collectors.android.perfetto_trace import (
        PerfettoTraceProcessor,
        query_frame_timeline,
        resolve_trace_processor,
    )

    run_path = resolve_run_dir(runs_dir, run_id)
    trace_path = resolve_trace_artifact(run_path)
    simpleperf_face = build_simpleperf_face(run_path)
    if trace_path is None:
        return empty_body(run_id, simpleperf_face=simpleperf_face)

    resolution = resolve_trace_processor(environ=trace_processor_environ)
    processor = PerfettoTraceProcessor(resolution)
    try:
        if not processor.resolution.available:
            return empty_body(run_id, simpleperf_face=simpleperf_face)
        result = query_frame_timeline(trace_path, processor=processor)
        if not result.available or not result.frames:
            # trace 可定位但查询失败/无 frame_timeline 数据 → 空结构 + note
            return empty_body(run_id, simpleperf_face=simpleperf_face)

        frames = [frame.to_dict() for frame in result.frames]
        if frame_index is None:
            jank_count = result.jank_count
            return {
                "runId": run_id,
                "traceAvailable": True,
                "source": SOURCE_FRAME_TIMELINE,
                "vsyncPeriodMs": result.vsync_period_ms,
                "frameCount": len(frames),
                "jankCount": jank_count,
                "jankRate": (
                    round(jank_count / len(frames), 3) if frames else None
                ),
                "frames": frames,
                "note": None,
                # simpleperf 火焰图数据面（M8+ 缺口 flamegraph-compare-missing，
                # §22 delta 由契约流登记）：帧列表概览形态带该字段
                "simpleperf": simpleperf_face,
            }

        if not isinstance(frame_index, int) or isinstance(frame_index, bool):
            raise validation_error("frameIndex 必须是整数", field="frameIndex")
        if frame_index < 0 or frame_index >= len(result.frames):
            raise validation_error(
                f"frameIndex {frame_index} 越界（trace 共 {len(result.frames)} 帧）",
                field="frameIndex",
            )
        frame = result.frames[frame_index]
        slices, note = _cpu_scheduling(processor, trace_path, frame, result.epoch_offset_ns)
        return {
            "runId": run_id,
            "traceAvailable": True,
            "frame": frame.to_dict(),
            "cpuScheduling": slices,
            "note": note,
        }
    finally:
        processor.close()


def _cpu_scheduling(
    processor: Any,
    trace_path: Path,
    frame: Any,
    epoch_offset_ns: int | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """单帧时间窗内 thread_state 片段 → 契约 §22.1 cpuScheduling 条目。

    窗口 = ``[frame.ts_boottime_ns, ts + duration]``（帧自身区间；「该帧附近」的
    数据面口径，如实声明）。查询失败（trace_processor 执行异常）→ ``([], note)``
    降级——帧卡主体（FrameTimeline）不受影响，调度片段缺失如实声明。
    """
    from perfeval.collectors.android.perfetto_trace import (
        TraceProcessorUnavailable,
        boottime_ns_to_epoch_ms,
        dur_ns_to_ms,
    )

    start_ns = int(frame.ts_boottime_ns)
    end_ns = start_ns + int(round(frame.duration_ms * 1_000_000))
    sql = SCHED_SQL_TEMPLATE.format(start_ns=start_ns, end_ns=end_ns)
    try:
        rows = processor.query(sql, trace_path)
    except TraceProcessorUnavailable as exc:
        return [], (
            "CPU 调度片段查询失败（trace_processor 执行异常），帧卡主体不受影响；"
            f"不回退伪造调度片段（[R§8.6]）：{exc}"
        )

    slices: list[dict[str, Any]] = []
    for row in rows:
        dur_ns = int(row.get("dur") or 0)
        if dur_ns <= 0:
            continue  # 未闭合/零长片段不产出（不伪造负时长）
        raw_state = row.get("raw_state")
        state = THREAD_STATE_MAP.get(str(raw_state), str(raw_state))
        slices.append(
            {
                "core": _optional_int(row.get("cpu")),
                "tid": _optional_int(row.get("tid")),
                "threadName": row.get("thread_name"),
                "pid": _optional_int(row.get("pid")),
                "state": state,
                # BOOTTIME→REALTIME 偏移不可得时为 null（§1 epoch ms 口径，不猜）
                "startEpochMs": boottime_ns_to_epoch_ms(
                    int(row.get("ts") or 0), epoch_offset_ns
                ),
                "durationMs": round(dur_ns_to_ms(dur_ns), 3),
            }
        )
    return slices, None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
