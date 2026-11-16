"""Perfetto trace_processor 封装与 frame_timeline 帧时间轴（CM-TASK-011/012）。

ADR-0001（accepted）：trace_processor 是可选增强依赖，不进入 pyproject 依赖、
不打包进 PyInstaller 产物。

- 二进制发现链（依次）：环境变量 ``PERFEVAL_TRACE_PROCESSOR`` → GUI 工具路径设置
  （经 ``configured_paths`` 注入；SettingsService 依赖 PySide6/QSettings 且属于
  ``perfeval.gui`` 包，collector 层不反向依赖 GUI，故此处留注入点，GUI 接线方
  传 ``{"trace_processor": settings.external_tool_path("trace_processor")}``）
  → ``PATH``（trace_processor_shell / trace_processor）→ 项目 ``tools/`` 目录。
- 双路径：Python ``perfetto`` 包（可选安装）优先使用其 TraceProcessor API（省去
  子进程管理），否则 subprocess 调用 shell 二进制 ``--query-string`` SQL 模式
  （v58+ 的 ``query`` 子命令作为兼容回退）。两条路径封在本模块后，上层无感。
- 语义：二进制缺失或执行失败抛 :class:`TraceProcessorUnavailable`，供上层记
  ``trace_processor.unavailable`` diagnostic 并降级（如 framestats 路径）；
  :func:`query_frame_timeline` 聚合入口不抛异常，返回带 ``diagnostics`` 的结果。

TASK-012：SQL 查询 frame_timeline（SurfaceFlinger 的 Actual/Expected Timeline
slice，见 ``FRAME_TIMELINE_SQL``），用 ``clock_snapshot`` 的 BOOTTIME→REALTIME
偏移把 trace 内 boot 时钟换算成 epoch 毫秒，输出逐帧 :class:`FrameRecord`
时间轴，供录屏掉帧跳转（Phase 4）与 FPS 来源链（TASK-013）消费。

golden 样本 ``perfeval/fixtures/android/golden/frame-timeline.pftrace``：Android 16
真机 ``perfetto -c - --txt``（android.surfaceflinger.frametimeline 数据源）3 秒
采集，47KB，jank_type 覆盖 None / SurfaceFlinger Stuffing / App Deadline Missed。
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import statistics
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

TRACE_PROCESSOR_ENV_VAR = "PERFEVAL_TRACE_PROCESSOR"
TRACE_PROCESSOR_SETTING_KEY = "trace_processor"
TRACE_PROCESSOR_BINARY_NAMES = ("trace_processor_shell", "trace_processor")
TOOLS_DIR_NAME = "tools"
UNAVAILABLE_DIAGNOSTIC = "trace_processor.unavailable"

ACTUAL_TIMELINE_TRACK_NAME = "Actual Timeline"
EXPECTED_TIMELINE_TRACK_NAME = "Expected Timeline"

# jank_type 取这些值（大小写不敏感）视为未掉帧；其余非空 jank_type 均为 jank 帧。
# 实测 Android 16：未掉帧为 "None"，掉帧如 "SurfaceFlinger Stuffing"、
# "App Deadline Missed"；保留历史口径 "Not janked"/"Ok" 以兼容旧版本。
NON_JANK_TYPES = frozenset({"", "none", "ok", "not janked", "unspecified"})

FRAME_TIMELINE_SQL = """
select
  s.ts as ts,
  s.dur as dur,
  t.name as track_name,
  s.name as surface_frame_token,
  p.upid as upid,
  p.pid as pid,
  p.name as process_name,
  ja.string_value as jank_type,
  pa.string_value as present_type,
  onf.int_value as on_time_finish,
  dfa.int_value as display_frame_token,
  ed.dur as expected_dur
from slice s
join track t on s.track_id = t.id
left join args da on da.arg_set_id = t.dimension_arg_set_id and da.key = 'upid'
left join process p on p.upid = da.int_value
left join args ja on ja.arg_set_id = s.arg_set_id and ja.key = 'Jank type'
left join args pa on pa.arg_set_id = s.arg_set_id and pa.key = 'Present type'
left join args onf on onf.arg_set_id = s.arg_set_id and onf.key = 'On time finish'
left join args dfa on dfa.arg_set_id = s.arg_set_id and dfa.key = 'Display frame token'
left join slice ed on ed.name = s.name
  and ed.track_id in (
    select t2.id from track t2
    where t2.name = 'Expected Timeline' and t2.dimension_arg_set_id = t.dimension_arg_set_id
  )
where t.name in ('Actual Timeline', 'Expected Timeline')
order by s.ts
"""

CLOCK_SNAPSHOT_SQL = """
select ts, clock_id, clock_name, clock_value, snapshot_id
from clock_snapshot
order by ts
"""

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+(?:[eE][+-]?\d+)?$")

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]
PackageQuery = Callable[[str], list[dict[str, Any]]]
Row = dict[str, Any]


class TraceProcessorUnavailable(RuntimeError):
    """trace_processor 缺失或查询执行失败。

    上层应捕获并记 ``trace_processor.unavailable`` diagnostic 后降级，
    不允许作为未捕获异常逸出到 runner。
    """


@dataclass(frozen=True)
class TraceProcessorResolution:
    """trace_processor 二进制发现结果；program 为 None 时附可读原因。"""

    program: str | None
    source: str | None  # env | settings | path | tools
    attempts: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return self.program is not None

    @property
    def unavailable_reason(self) -> str:
        if self.program is not None:
            return ""
        detail = "；".join(f"{name}={description}" for name, description in self.attempts)
        return (
            f"trace_processor 二进制未找到（{detail}）。可设置 {TRACE_PROCESSOR_ENV_VAR}、"
            f"GUI 工具路径设置（{TRACE_PROCESSOR_SETTING_KEY}）或放入项目 {TOOLS_DIR_NAME}/ 目录。"
        )


def resolve_trace_processor(
    *,
    environ: Mapping[str, str] | None = None,
    configured_paths: Mapping[str, str | None] | None = None,
    project_root: str | Path | None = None,
    which: Callable[[str], str | None] | None = None,
    cwd: str | Path | None = None,
) -> TraceProcessorResolution:
    """按 ADR-0001 发现链解析 trace_processor 二进制；全未命中时返回 program=None。"""
    env_values = dict(os.environ if environ is None else environ)
    find_program = which or shutil.which
    configured = dict(configured_paths or {})
    attempts: list[tuple[str, str]] = []

    # 1) 环境变量（显式指定但指向不可用文件时继续走链，attempt 留痕供 diagnostic）
    env_value = str(env_values.get(TRACE_PROCESSOR_ENV_VAR) or "").strip()
    if env_value:
        program = _executable_file(Path(env_value).expanduser())
        if program is not None:
            return TraceProcessorResolution(str(program), "env", tuple(attempts))
        attempts.append(("env", f"{env_value}（不可用）"))
    else:
        attempts.append(("env", "未设置"))

    # 2) GUI 工具路径设置（collector 层不 import GUI，由调用方注入持久化值）
    settings_value = str(configured.get(TRACE_PROCESSOR_SETTING_KEY) or "").strip()
    if settings_value:
        program = _executable_file(Path(settings_value).expanduser())
        if program is not None:
            return TraceProcessorResolution(str(program), "settings", tuple(attempts))
        attempts.append(("settings", f"{settings_value}（不可用）"))
    else:
        attempts.append(("settings", "未配置"))

    # 3) PATH
    for name in TRACE_PROCESSOR_BINARY_NAMES:
        found = find_program(name)
        if found:
            program = _executable_file(Path(found))
            if program is not None:
                return TraceProcessorResolution(str(program), "path", tuple(attempts))
    attempts.append(("path", "未找到"))

    # 4) 项目 tools/ 目录
    tools_hits: list[str] = []
    for base in _tools_dir_candidates(project_root, cwd):
        for name in TRACE_PROCESSOR_BINARY_NAMES:
            candidate = base / name
            tools_hits.append(str(candidate))
            program = _executable_file(candidate)
            if program is not None:
                return TraceProcessorResolution(str(program), "tools", tuple(attempts))
    attempts.append(("tools", f"未命中（尝试过 {tools_hits[0] if tools_hits else 'tools/'} 等）"))
    return TraceProcessorResolution(None, None, tuple(attempts))


class PerfettoTraceProcessor:
    """trace_processor 统一查询接口：perfetto 包优先，subprocess 兜底。

    上层通过 :meth:`query` 无感使用两条路径；二进制缺失或执行失败抛
    :class:`TraceProcessorUnavailable`。``notes`` 记录路径回退原因，供 diagnostic 汇总。

    perfetto 包会话底层是子进程句柄：支持 :meth:`close` 显式释放与上下文管理器
    （``with PerfettoTraceProcessor(...) as tp:``）；subprocess 路径无常驻句柄。
    """

    def __init__(
        self,
        resolution: TraceProcessorResolution | None = None,
        *,
        prefer_python_package: bool = True,
        timeout_s: float = 120.0,
        subprocess_runner: SubprocessRunner | None = None,
        package_loader: Callable[[], ModuleType | None] | None = None,
    ) -> None:
        self.resolution = resolution or resolve_trace_processor()
        self.prefer_python_package = prefer_python_package
        self.timeout_s = timeout_s
        self.notes: list[str] = []
        self._run = subprocess_runner or subprocess.run
        self._package_loader = package_loader or _load_perfetto_package
        self._package_sessions: dict[str, PackageQuery | None] = {}
        self._package_processors: list[Any] = []

    @property
    def program(self) -> str | None:
        return self.resolution.program

    @property
    def available(self) -> bool:
        return self.resolution.available

    def close(self) -> None:
        """关闭全部 perfetto 包会话并清空缓存（幂等；subprocess 路径无句柄可关）。"""
        for package_processor in self._package_processors:
            package_close = getattr(package_processor, "close", None)
            if not callable(package_close):
                continue
            try:
                package_close()
            except Exception:  # noqa: BLE001 - 单个会话关闭失败不影响其余清理
                self.notes.append("perfetto 包会话关闭失败（已忽略）")
        self._package_processors.clear()
        self._package_sessions.clear()

    def __enter__(self) -> "PerfettoTraceProcessor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def query(self, sql: str, trace_path: str | Path) -> list[Row]:
        """执行 SQL，返回字典行（[NULL] → None，数值自动转型）。"""
        trace = Path(trace_path)
        if not self.resolution.available:
            raise TraceProcessorUnavailable(self.resolution.unavailable_reason)
        if not trace.is_file():
            raise TraceProcessorUnavailable(f"trace 文件不存在：{trace}")
        if self.prefer_python_package:
            package_query = self._package_session_for(trace)
            if package_query is not None:
                try:
                    return package_query(sql)
                except Exception as exc:  # noqa: BLE001 - 包路径任意失败都回退 subprocess
                    self.notes.append(f"perfetto 包查询失败，回退 subprocess：{exc}")
                    self._package_sessions[str(trace)] = None  # 该 trace 不再重试包路径
        return self._query_via_subprocess(sql, trace)

    def _package_session_for(self, trace: Path) -> PackageQuery | None:
        key = str(trace)
        if key in self._package_sessions:
            return self._package_sessions[key]
        session: PackageQuery | None = None
        module = self._package_loader()
        if module is not None:
            session, package_processor, reason = _open_python_package_query(
                module, trace, self.resolution.program
            )
            if package_processor is not None:
                self._package_processors.append(package_processor)
            if session is None and reason:
                self.notes.append(f"perfetto 包不可用：{reason}")
        self._package_sessions[key] = session
        return session

    def _query_via_subprocess(self, sql: str, trace: Path) -> list[Row]:
        program = self.resolution.program
        assert program is not None  # available 已在 query() 校验
        # 形式一：现行/旧版共有的 --query-string flag；形式二：v58+ query 子命令回退。
        invocations: tuple[list[str], list[str]] = (
            [program, str(trace), "--query-string", sql],
            [program, "query", str(trace), sql],
        )
        last_error = ""
        for index, command in enumerate(invocations):
            try:
                completed = self._run(command, capture_output=True, text=True, timeout=self.timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise TraceProcessorUnavailable(
                    f"trace_processor 查询超时（>{self.timeout_s:.0f}s）：{trace}"
                ) from exc
            except OSError as exc:
                raise TraceProcessorUnavailable(f"trace_processor 无法执行：{program}（{exc}）") from exc
            if completed.returncode == 0:
                return parse_query_output(completed.stdout)
            last_error = completed.stderr or completed.stdout
            if index == 0:
                self.notes.append("--query-string 调用失败，尝试 query 子命令回退")
        tail = " ".join(str(last_error).split())[-400:]
        raise TraceProcessorUnavailable(f"trace_processor 查询执行失败：{tail or '无错误输出'}")


def parse_query_output(text: str) -> list[Row]:
    """解析 trace_processor 查询 CSV 输出（首行表头，[NULL] 表示空值）。"""
    rows: list[Row] = []
    header: list[str] | None = None
    for fields in csv.reader(io.StringIO(text)):
        if not fields or (len(fields) == 1 and not fields[0].strip()):
            continue
        if header is None:
            header = [name.strip() for name in fields]
            continue
        row: Row = {}
        for index, name in enumerate(header):
            raw = fields[index].strip() if index < len(fields) else ""
            row[name] = _coerce_sql_value(raw)
        rows.append(row)
    return rows


def _coerce_sql_value(raw: str) -> Any:
    if raw == "[NULL]":
        return None
    if _INT_RE.match(raw):
        return int(raw)
    if _FLOAT_RE.match(raw):
        return float(raw)
    return raw


def _load_perfetto_package() -> ModuleType | None:
    """可选导入 perfetto 包（零强制依赖）；未安装返回 None，走 subprocess 路径。"""
    try:
        from perfetto import trace_processor as module
    except Exception:  # noqa: BLE001 - 可选依赖缺失不允许影响调用方
        return None
    return module


def _open_python_package_query(
    module: ModuleType,
    trace_path: Path,
    program: str | None,
) -> tuple[PackageQuery | None, Any, str | None]:
    """用 perfetto 包 API 构造 query(sql)->rows 闭包；失败返回 (None, None, 原因)。

    第二返回值为打开的 TraceProcessor 实例（可能已半初始化），供调用方登记并在
    close() 时释放其子进程句柄；包的配置/构造签名在版本间有差异，逐个尝试常见
    形态，全部失败则由调用方回退 subprocess（两条路径语义一致，上层无感）。
    """
    config = None
    for kwargs in ({"trace_processor_bin": program}, {"bin": program}, {}):
        try:
            config = module.TraceProcessorConfig(**kwargs)
            break
        except TypeError:
            continue
    if config is None:
        return None, None, "TraceProcessorConfig 构造失败"
    processor = None
    for kwargs in (
        {"trace": str(trace_path), "config": config},
        {"file_path": str(trace_path), "config": config},
    ):
        try:
            processor = module.TraceProcessor(**kwargs)
            break
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001 - 打开 trace 失败交由上层回退
            return None, None, f"TraceProcessor 打开 trace 失败：{exc}"
    if processor is None:
        return None, None, "TraceProcessor 构造签名不匹配"

    def package_query(sql: str) -> list[Row]:
        return [_row_to_dict(row) for row in processor.query(sql)]

    return package_query, processor, None


def _row_to_dict(row: Any) -> Row:
    if isinstance(row, dict):
        return dict(row)
    as_dict = getattr(row, "_asdict", None)
    if callable(as_dict):
        return dict(as_dict())
    as_dict = getattr(row, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
        if isinstance(value, dict):
            return dict(value)
    fields = getattr(row, "_fields", None)
    if fields:
        return {name: getattr(row, name) for name in fields}
    return {"value": row}


# ---------------------------------------------------------------------------
# TASK-012：frame_timeline → 逐帧 epoch 对齐时间轴
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameRecord:
    """单帧记录：epoch 对齐的开始时间 + 时长 + jank 标记。

    ts_epoch_ms 在 trace 缺 clock_snapshot（无法 BOOTTIME→REALTIME 换算）时为
    None，此时仅 ts_boottime_ns 可用（不能与录屏 tsUtc 对齐）。

    消费注意（TASK-013 / Phase 4）：Actual Timeline 同时包含 SurfaceFlinger
    合成帧与应用提交的帧，按单应用口径统计 FPS/掉帧时需在消费侧按
    process（process_name/pid/upid）过滤（:func:`filter_frames_by_process`）；
    本模块不做该过滤以保持数据完整性。
    """

    frame_index: int
    ts_boottime_ns: int
    ts_epoch_ms: int | None
    duration_ms: float
    jank: bool
    expected_duration_ms: float | None = None
    jank_type: str | None = None
    present_type: str | None = None
    on_time_finish: bool | None = None
    process_name: str | None = None
    pid: int | None = None
    surface_frame_token: str | None = None
    display_frame_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "frameIndex": self.frame_index,
            "tsEpochMs": self.ts_epoch_ms,
            "durationMs": self.duration_ms,
            "jank": self.jank,
            "jankType": self.jank_type,
            "presentType": self.present_type,
            "onTimeFinish": self.on_time_finish,
            "expectedDurationMs": self.expected_duration_ms,
            "processName": self.process_name,
            "pid": self.pid,
            "surfaceFrameToken": self.surface_frame_token,
            "displayFrameToken": self.display_frame_token,
        }


@dataclass(frozen=True)
class FrameTimelineResult:
    """frame_timeline 查询结果；不可用时 frames 为空并附 diagnostics。"""

    frames: tuple[FrameRecord, ...]
    vsync_period_ms: float | None
    epoch_offset_ns: int | None
    diagnostics: tuple[str, ...] = ()
    available: bool = True

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def jank_count(self) -> int:
        return sum(1 for frame in self.frames if frame.jank)

    @property
    def jank_rate(self) -> float | None:
        if not self.frames:
            return None
        return self.jank_count / len(self.frames)


def parse_frame_timeline(
    rows: Iterable[Row],
    *,
    clock_rows: Iterable[Row] = (),
    diagnostics: Iterable[str] = (),
) -> FrameTimelineResult:
    """把 FRAME_TIMELINE_SQL / CLOCK_SNAPSHOT_SQL 的查询行换算成逐帧时间轴。

    纯函数，零设备/零二进制依赖。jank 判定：jank_type 非空且不在
    :data:`NON_JANK_TYPES`（实测口径见模块 docstring）。
    vsync_period_ms 取 Expected 时长（Expected track 行 + Actual join 命中值）
    去重后的中位数：同一帧的 expected_dur 会在两处出现，不去重会使中位数
    向重复值偏移。
    """
    notes = list(diagnostics)
    offset_ns = _epoch_offset_ns(clock_rows)
    if offset_ns is None:
        notes.append("epoch 对齐不可用：trace 缺少 clock_snapshot 的 BOOTTIME/REALTIME 对")

    actual_frames: list[FrameRecord] = []
    expected_durations_ms: list[float] = []
    seen_expected_ms: set[float] = set()
    pending: list[tuple[int, FrameRecord]] = []
    for row in rows:
        track_name = str(row.get("track_name") or "")
        ts_ns = _as_int(row.get("ts"))
        dur_ns = _as_int(row.get("dur"))
        if track_name == ACTUAL_TIMELINE_TRACK_NAME:
            jank_type = row.get("jank_type")
            # LEFT JOIN 未命中时 expected_dur 为 None（[NULL]），必须保留 None 语义，
            # 不能落进 _as_int 的 0 兜底污染 per-frame 值与 vsync 中位数。
            raw_expected = row.get("expected_dur")
            expected_ms = (
                round(dur_ns_to_ms(_as_int(raw_expected)), 3) if raw_expected is not None else None
            )
            if expected_ms is not None and expected_ms not in seen_expected_ms:
                seen_expected_ms.add(expected_ms)
                expected_durations_ms.append(expected_ms)
            pending.append(
                (
                    ts_ns,
                    FrameRecord(
                        frame_index=0,  # 排序后统一编号
                        ts_boottime_ns=ts_ns,
                        ts_epoch_ms=boottime_ns_to_epoch_ms(ts_ns, offset_ns),
                        duration_ms=round(dur_ns_to_ms(dur_ns), 3),
                        jank=is_jank_frame(jank_type),
                        expected_duration_ms=expected_ms,
                        jank_type=_as_str(jank_type),
                        present_type=_as_str(row.get("present_type")),
                        on_time_finish=_as_bool(row.get("on_time_finish")),
                        process_name=_as_str(row.get("process_name")),
                        pid=_as_optional_int(row.get("pid")),
                        surface_frame_token=_as_str(row.get("surface_frame_token")),
                        display_frame_token=_as_str(row.get("display_frame_token")),
                    ),
                )
            )
        elif track_name == EXPECTED_TIMELINE_TRACK_NAME:
            expected_ms = round(dur_ns_to_ms(dur_ns), 3)
            if expected_ms not in seen_expected_ms:
                seen_expected_ms.add(expected_ms)
                expected_durations_ms.append(expected_ms)

    pending.sort(key=lambda item: item[0])
    actual_frames = [
        replace_frame_index(record, index) for index, (_, record) in enumerate(pending)
    ]
    vsync_ms = round(statistics.median(expected_durations_ms), 3) if expected_durations_ms else None
    return FrameTimelineResult(
        frames=tuple(actual_frames),
        vsync_period_ms=vsync_ms,
        epoch_offset_ns=offset_ns,
        diagnostics=tuple(notes),
        available=True,
    )


def is_jank_frame(jank_type: Any) -> bool:
    """jank 判定口径：非空 jank_type 且不在 NON_JANK_TYPES 内即为 jank 帧。"""
    value = _as_str(jank_type)
    if value is None:
        return False
    return value.strip().casefold() not in NON_JANK_TYPES


def dur_ns_to_ms(dur_ns: int) -> float:
    return dur_ns / 1_000_000.0


def boottime_ns_to_epoch_ms(ts_boottime_ns: int, epoch_offset_ns: int | None) -> int | None:
    """trace 内 boot 时钟 ns → epoch 毫秒；offset 未知时返回 None。"""
    if epoch_offset_ns is None:
        return None
    return (ts_boottime_ns + epoch_offset_ns) // 1_000_000


def replace_frame_index(record: FrameRecord, index: int) -> FrameRecord:
    return FrameRecord(
        frame_index=index,
        ts_boottime_ns=record.ts_boottime_ns,
        ts_epoch_ms=record.ts_epoch_ms,
        duration_ms=record.duration_ms,
        jank=record.jank,
        expected_duration_ms=record.expected_duration_ms,
        jank_type=record.jank_type,
        present_type=record.present_type,
        on_time_finish=record.on_time_finish,
        process_name=record.process_name,
        pid=record.pid,
        surface_frame_token=record.surface_frame_token,
        display_frame_token=record.display_frame_token,
    )


def filter_frames_by_process(
    frames: Sequence[FrameRecord],
    package_name: str | None,
) -> tuple[FrameRecord, ...]:
    """按目标进程过滤帧时间轴（TASK-013 FPS 单应用口径）。

    FrameRecord 含 process_name/pid（FRAME_TIMELINE_SQL join process 表）；
    SurfaceFlinger 合成帧混在同名 ``Actual Timeline`` track 但 process 归属
    surfaceflinger，按 process_name 精确匹配目标包名即可剔除。package_name
    为 None（离线解析无包名上下文）时不过滤、原样返回。
    """
    if not package_name:
        return tuple(frames)
    return tuple(frame for frame in frames if frame.process_name == package_name)


def _as_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _epoch_offset_ns(clock_rows: Iterable[Row]) -> int | None:
    """从 clock_snapshot 行取同 snapshot 的 REALTIME - BOOTTIME 偏移（ns）。"""
    boots: dict[Any, int] = {}
    reals: dict[Any, int] = {}
    for row in clock_rows:
        name = str(row.get("clock_name") or "").strip().upper()
        value = row.get("clock_value")
        if value is None:
            continue
        snapshot = row.get("snapshot_id")
        try:
            value_ns = int(value)
        except (TypeError, ValueError):
            continue
        if name == "BOOTTIME":
            boots[snapshot] = value_ns
        elif name == "REALTIME":
            reals[snapshot] = value_ns
    for snapshot in reals:
        if snapshot in boots:
            return reals[snapshot] - boots[snapshot]
    return None


def query_frame_timeline(
    trace_path: str | Path,
    *,
    resolution: TraceProcessorResolution | None = None,
    processor: PerfettoTraceProcessor | None = None,
    prefer_python_package: bool = True,
    **resolve_kwargs: Any,
) -> FrameTimelineResult:
    """聚合入口：不抛异常；tp 缺失/执行失败时返回带 diagnostics 的空结果。

    供上层（TASK-013 FPS 来源链、diagnose 接线）消费：available=False 或
    frames 为空时按 diagnostics 记 ``trace_processor.unavailable`` 并降级。
    内部创建的 processor 由本函数负责 close（perfetto 包会话持有子进程句柄）；
    调用方传入的 processor 归调用方管理。diagnostics 在查询完成后收集，
    以覆盖查询期间产生的路径回退 note。
    """
    owns_processor = processor is None
    if processor is None:
        processor = PerfettoTraceProcessor(
            resolution or resolve_trace_processor(**resolve_kwargs),
            prefer_python_package=prefer_python_package,
        )
    try:
        if not processor.resolution.available:
            diagnostics = [*processor.notes, f"{UNAVAILABLE_DIAGNOSTIC}:{processor.resolution.unavailable_reason}"]
            return FrameTimelineResult(
                frames=(),
                vsync_period_ms=None,
                epoch_offset_ns=None,
                diagnostics=tuple(diagnostics),
                available=False,
            )
        try:
            frame_rows = processor.query(FRAME_TIMELINE_SQL, trace_path)
            clock_rows = processor.query(CLOCK_SNAPSHOT_SQL, trace_path)
        except TraceProcessorUnavailable as exc:
            # 查询后收集：包括查询期间产生的 --query-string 回退等 note
            diagnostics = [*processor.notes, f"{UNAVAILABLE_DIAGNOSTIC}:{exc}"]
            return FrameTimelineResult(
                frames=(),
                vsync_period_ms=None,
                epoch_offset_ns=None,
                diagnostics=tuple(diagnostics),
                available=False,
            )
        return parse_frame_timeline(frame_rows, clock_rows=clock_rows, diagnostics=processor.notes)
    finally:
        if owns_processor:
            processor.close()


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text or text == "[NULL]":
        return None
    return text


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None


def _executable_file(path: Path) -> Path | None:
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    except OSError:
        return None
    return None


def _tools_dir_candidates(
    project_root: str | Path | None,
    cwd: str | Path | None,
) -> list[Path]:
    if project_root is not None:
        return [Path(project_root).expanduser() / TOOLS_DIR_NAME]
    start = Path(cwd or os.getcwd()).expanduser().resolve(strict=False)
    candidates = [start / TOOLS_DIR_NAME]
    candidates.extend(parent / TOOLS_DIR_NAME for parent in list(start.parents)[:4])
    return candidates


__all__ = [
    "ACTUAL_TIMELINE_TRACK_NAME",
    "CLOCK_SNAPSHOT_SQL",
    "EXPECTED_TIMELINE_TRACK_NAME",
    "FRAME_TIMELINE_SQL",
    "FrameRecord",
    "FrameTimelineResult",
    "NON_JANK_TYPES",
    "PerfettoTraceProcessor",
    "TRACE_PROCESSOR_BINARY_NAMES",
    "TRACE_PROCESSOR_ENV_VAR",
    "TRACE_PROCESSOR_SETTING_KEY",
    "TOOLS_DIR_NAME",
    "TraceProcessorResolution",
    "TraceProcessorUnavailable",
    "UNAVAILABLE_DIAGNOSTIC",
    "boottime_ns_to_epoch_ms",
    "dur_ns_to_ms",
    "filter_frames_by_process",
    "is_jank_frame",
    "parse_frame_timeline",
    "parse_query_output",
    "query_frame_timeline",
    "resolve_trace_processor",
]
