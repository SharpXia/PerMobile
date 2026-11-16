"""perfetto_trace 单测：零设备依赖、零真二进制依赖（fake binary 回放 canned CSV）。

覆盖：发现链各级命中/优先级/全未命中、subprocess 查询解析与执行失败降级、
perfetto 包路径注入与回退、frame_timeline → 逐帧 epoch 时间轴换算。
golden 样本 smoke 仅在真机 trace_processor 存在时运行，否则 skip。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from perfeval.collectors.android.perfetto_trace import (
    CLOCK_SNAPSHOT_SQL,
    FRAME_TIMELINE_SQL,
    UNAVAILABLE_DIAGNOSTIC,
    FrameRecord,
    PerfettoTraceProcessor,
    TraceProcessorUnavailable,
    is_jank_frame,
    parse_frame_timeline,
    parse_query_output,
    query_frame_timeline,
    resolve_trace_processor,
)

FAKE_FRAME_ROWS = (
    '"ts","dur","track_name","surface_frame_token","upid","pid","process_name",'
    '"jank_type","present_type","on_time_finish","display_frame_token","expected_dur"\n'
    "1000000000,8333333,\"Expected Timeline\",\"11\",3,2000,\"com.example.app\","
    '"[NULL]","[NULL]","[NULL]",11,"[NULL]"\n'
    "1016000000,8333333,\"Expected Timeline\",\"12\",3,2000,\"com.example.app\","
    '"[NULL]","[NULL]","[NULL]",12,"[NULL]"\n'
    "1000000000,16500000,\"Actual Timeline\",\"11\",3,2000,\"com.example.app\","
    '"None","On-time Present",1,11,8333333\n'
    "1016000000,33000000,\"Actual Timeline\",\"12\",3,2000,\"com.example.app\","
    '"App Deadline Missed","Late Present",0,12,8333333\n'
    "1032000000,8300000,\"Actual Timeline\",\"13\",3,2000,\"com.example.app\","
    '"SurfaceFlinger Stuffing","Late Present",1,13,8333333\n'
)

FAKE_CLOCK_ROWS = (
    '"ts","clock_id","clock_name","clock_value","snapshot_id"\n'
    "1000000000,6,\"BOOTTIME\",1000000000,0\n"
    "1000000000,1,\"REALTIME\",1700000000000000000,0\n"
)


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_binary(directory: Path, *, mode: str = "ok") -> Path:
    """生成 fake trace_processor_shell：按 SQL 内容回放 canned CSV（真实二进制同为 CSV 输出）。"""
    directory.mkdir(parents=True, exist_ok=True)
    frames_csv = directory / "frames.csv"
    clock_csv = directory / "clock.csv"
    frames_csv.write_text(FAKE_FRAME_ROWS, encoding="utf-8")
    clock_csv.write_text(FAKE_CLOCK_ROWS, encoding="utf-8")
    if mode == "always_fail":
        script = '#!/bin/sh\necho "boom" >&2\nexit 3\n'
    elif mode == "legacy_fail":
        # 模拟只支持 v58+ query 子命令的新二进制：--query-string 形式报未知 flag
        script = (
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "--query-string" ]; then echo "unknown flag: --query-string" >&2; exit 1; fi\n'
            "done\n"
            'if [ "$1" = "query" ]; then\n'
            '  case "$3" in\n'
            f'    *clock_snapshot*) cat "{clock_csv}" ;;\n'
            f"    *'from slice'*) cat \"{frames_csv}\" ;;\n"
            "  esac\n"
            "fi\n"
        )
    else:
        script = (
            "#!/bin/sh\n"
            'prev=""\nsql=""\n'
            'for arg in "$@"; do\n'
            '  if [ "$prev" = "--query-string" ]; then sql="$arg"; fi\n'
            '  prev="$arg"\n'
            "done\n"
            'case "$sql" in\n'
            f'  *clock_snapshot*) cat "{clock_csv}" ;;\n'
            f"  *'from slice'*) cat \"{frames_csv}\" ;;\n"
            '  *) echo "unexpected query" >&2; exit 9 ;;\n'
            "esac\n"
        )
    return _write_executable(directory / "trace_processor_shell", script)


def _fake_trace(tmp_path: Path) -> Path:
    """query() 有 trace 文件存在性校验；fake binary 不读内容，占位即可。"""
    trace = tmp_path / "trace.pftrace"
    trace.write_bytes(b"\x0a\x00fake\n")
    return trace


def _processor_with_fake(
    tmp_path: Path,
    *,
    mode: str = "ok",
    **kwargs: Any,
) -> PerfettoTraceProcessor:
    fake = _fake_binary(tmp_path, mode=mode)
    _fake_trace(tmp_path)
    defaults: dict[str, Any] = {
        "resolution": resolve_trace_processor(
            environ={"PERFEVAL_TRACE_PROCESSOR": str(fake)},
            which=lambda name: None,
        ),
        "prefer_python_package": False,
        "subprocess_runner": subprocess.run,
        "package_loader": lambda: None,
    }
    defaults.update(kwargs)
    return PerfettoTraceProcessor(**defaults)


# ---------------------------------------------------------------------------
# 发现链（TASK-011）
# ---------------------------------------------------------------------------


def test_resolve_env_hit(tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path)
    resolution = resolve_trace_processor(
        environ={"PERFEVAL_TRACE_PROCESSOR": str(fake)},
        which=lambda name: None,
    )
    assert resolution.available
    assert resolution.source == "env"
    assert resolution.program == str(fake)


def test_resolve_settings_injection_hit(tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path)
    resolution = resolve_trace_processor(
        environ={},
        configured_paths={"trace_processor": str(fake)},
        which=lambda name: None,
    )
    assert resolution.available
    assert resolution.source == "settings"


def test_resolve_env_beats_settings_and_path(tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path / "env")
    resolution = resolve_trace_processor(
        environ={"PERFEVAL_TRACE_PROCESSOR": str(fake)},
        configured_paths={"trace_processor": str(tmp_path / "settings" / "tp")},
        which=lambda name: str(tmp_path / "from-path"),
    )
    assert resolution.source == "env"


def test_resolve_settings_beats_path(tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path / "settings")
    resolution = resolve_trace_processor(
        environ={},
        configured_paths={"trace_processor": str(fake)},
        which=lambda name: str(tmp_path / "from-path"),
    )
    assert resolution.source == "settings"


def test_resolve_path_hit(tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path)
    resolution = resolve_trace_processor(
        environ={},
        which=lambda name: str(tmp_path / name),
    )
    assert resolution.available
    assert resolution.source == "path"
    assert resolution.program == str(tmp_path / "trace_processor_shell")


def test_resolve_tools_dir_hit(tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path / "tools")
    resolution = resolve_trace_processor(
        environ={},
        which=lambda name: None,
        project_root=tmp_path,
    )
    assert resolution.available
    assert resolution.source == "tools"
    assert resolution.program == str(fake)


def test_resolve_all_miss_reports_reason(tmp_path: Path) -> None:
    resolution = resolve_trace_processor(
        environ={},
        configured_paths={},
        which=lambda name: None,
        project_root=tmp_path,
    )
    assert not resolution.available
    assert resolution.source is None
    assert {name for name, _ in resolution.attempts} == {"env", "settings", "path", "tools"}
    reason = resolution.unavailable_reason
    assert "PERFEVAL_TRACE_PROCESSOR" in reason
    assert "trace_processor" in reason


def test_resolve_env_missing_file_falls_through_to_path(tmp_path: Path) -> None:
    fallback = _write_executable(tmp_path / "from-path", "#!/bin/sh\n")
    resolution = resolve_trace_processor(
        environ={"PERFEVAL_TRACE_PROCESSOR": str(tmp_path / "missing")},
        which=lambda name: str(fallback),
    )
    assert resolution.available
    assert resolution.source == "path"
    assert any(name == "env" and "不可用" in text for name, text in resolution.attempts)


def test_resolve_skips_non_executable_candidates(tmp_path: Path) -> None:
    not_executable = tmp_path / "trace_processor_shell"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    not_executable.chmod(0o644)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "trace_processor_shell").write_text("#!/bin/sh\n", encoding="utf-8")
    (tools_dir / "trace_processor_shell").chmod(0o644)
    resolution = resolve_trace_processor(
        environ={"PERFEVAL_TRACE_PROCESSOR": str(not_executable)},
        which=lambda name: None,
        project_root=tmp_path,
    )
    assert not resolution.available
    assert any(name == "env" and "不可用" in text for name, text in resolution.attempts)
    assert any(name == "tools" for name, _ in resolution.attempts)


# ---------------------------------------------------------------------------
# subprocess 查询路径（TASK-011）
# ---------------------------------------------------------------------------


def test_parse_query_output_coerces_types() -> None:
    rows = parse_query_output('"a","b","c"\n1,2.500000,"x,y"\n7,"[NULL]","z"\n')
    assert rows[0] == {"a": 1, "b": 2.5, "c": "x,y"}
    assert rows[1] == {"a": 7, "b": None, "c": "z"}


def test_query_subprocess_parses_csv_rows(tmp_path: Path) -> None:
    processor = _processor_with_fake(tmp_path)
    rows = processor.query(FRAME_TIMELINE_SQL, tmp_path / "trace.pftrace")
    assert len(rows) == 5
    assert rows[2]["jank_type"] == "None"
    assert rows[2]["dur"] == 16500000
    assert isinstance(rows[2]["dur"], int)
    assert rows[3]["jank_type"] == "App Deadline Missed"
    assert rows[3]["on_time_finish"] == 0
    clock_rows = processor.query(CLOCK_SNAPSHOT_SQL, tmp_path / "trace.pftrace")
    assert clock_rows[0]["clock_name"] == "BOOTTIME"


def test_query_raises_unavailable_when_binary_missing(tmp_path: Path) -> None:
    processor = PerfettoTraceProcessor(
        resolve_trace_processor(environ={}, which=lambda name: None, project_root=tmp_path),
        package_loader=lambda: None,
    )
    with pytest.raises(TraceProcessorUnavailable) as exc_info:
        processor.query("select 1", tmp_path / "trace.pftrace")
    assert "PERFEVAL_TRACE_PROCESSOR" in str(exc_info.value)


def test_query_execution_failure_degrades_to_unavailable(tmp_path: Path) -> None:
    processor = _processor_with_fake(tmp_path, mode="always_fail")
    with pytest.raises(TraceProcessorUnavailable) as exc_info:
        processor.query(FRAME_TIMELINE_SQL, tmp_path / "trace.pftrace")
    assert "boom" in str(exc_info.value)


def test_query_timeout_maps_to_unavailable(tmp_path: Path) -> None:
    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="trace_processor_shell", timeout=30)

    processor = _processor_with_fake(tmp_path, subprocess_runner=runner)
    with pytest.raises(TraceProcessorUnavailable) as exc_info:
        processor.query(FRAME_TIMELINE_SQL, tmp_path / "trace.pftrace")
    assert "超时" in str(exc_info.value)


def test_query_legacy_flag_falls_back_to_query_subcommand(tmp_path: Path) -> None:
    processor = _processor_with_fake(tmp_path, mode="legacy_fail")
    rows = processor.query(FRAME_TIMELINE_SQL, tmp_path / "trace.pftrace")
    assert len(rows) == 5
    assert any("query 子命令回退" in note for note in processor.notes)


# ---------------------------------------------------------------------------
# perfetto 包可选路径（TASK-011）
# ---------------------------------------------------------------------------


class _FakePackageModule:
    """模拟 perfetto.trace_processor 模块的最小 API 面（含会话 close 语义）。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None, query_error: Exception | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.query_error = query_error
        self.config_kwargs: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.close_count = 0

    def TraceProcessorConfig(self, **kwargs: Any) -> dict[str, Any]:
        self.config_kwargs = kwargs
        return {"config": kwargs}

    def TraceProcessor(self, **kwargs: Any) -> "_FakePackageModule":
        self.calls.append("open")
        return self

    def query(self, sql: str) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if self.query_error is not None:
            raise self.query_error
        return self.rows

    def close(self) -> None:
        self.close_count += 1


def test_package_loader_preferred_when_available(tmp_path: Path) -> None:
    module = _FakePackageModule(rows=[{"a": 1}])
    runner_calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        runner_calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "should not run")

    processor = _processor_with_fake(
        tmp_path,
        prefer_python_package=True,
        subprocess_runner=runner,
        package_loader=lambda: module,
    )
    rows = processor.query("select 1 as a", tmp_path / "trace.pftrace")
    assert rows == [{"a": 1}]
    # 走包路径：二进制路径注入包配置，subprocess 未被调用
    assert module.calls == ["open", "select 1 as a"]
    assert module.config_kwargs == {"trace_processor_bin": processor.resolution.program}
    assert runner_calls == []


def test_package_query_failure_falls_back_to_subprocess(tmp_path: Path) -> None:
    module = _FakePackageModule(query_error=RuntimeError("package pipe broken"))
    processor = _processor_with_fake(
        tmp_path,
        prefer_python_package=True,
        package_loader=lambda: module,
    )
    rows = processor.query(FRAME_TIMELINE_SQL, tmp_path / "trace.pftrace")
    assert len(rows) == 5
    assert any("回退 subprocess" in note for note in processor.notes)
    # 失败会话按 trace 缓存为 None：后续查询直走 subprocess，不再触碰包
    second = processor.query(FRAME_TIMELINE_SQL, tmp_path / "trace.pftrace")
    assert len(second) == 5
    assert module.calls == ["open", FRAME_TIMELINE_SQL]


def test_package_loader_absent_uses_subprocess_without_noise(tmp_path: Path) -> None:
    processor = _processor_with_fake(tmp_path, prefer_python_package=True)
    rows = processor.query(FRAME_TIMELINE_SQL, tmp_path / "trace.pftrace")
    assert len(rows) == 5
    assert not any("perfetto 包" in note for note in processor.notes)


# ---------------------------------------------------------------------------
# frame_timeline → 逐帧 epoch 时间轴（TASK-012）
# ---------------------------------------------------------------------------


def test_query_frame_timeline_builds_epoch_aligned_frames(tmp_path: Path) -> None:
    processor = _processor_with_fake(tmp_path)
    result = query_frame_timeline(tmp_path / "trace.pftrace", processor=processor)
    assert result.available
    assert result.frame_count == 3
    # offset = REALTIME - BOOTTIME = 1699999999000000000ns
    assert result.epoch_offset_ns == 1699999999000000000
    # epoch ms = (ts + offset) // 1e6
    epochs = [frame.ts_epoch_ms for frame in result.frames]
    assert epochs == [1700000000000, 1700000000016, 1700000000032]
    assert [frame.frame_index for frame in result.frames] == [0, 1, 2]
    assert [frame.duration_ms for frame in result.frames] == [16.5, 33.0, 8.3]
    assert [frame.jank for frame in result.frames] == [False, True, True]
    assert [frame.jank_type for frame in result.frames] == [
        "None",
        "App Deadline Missed",
        "SurfaceFlinger Stuffing",
    ]
    assert result.vsync_period_ms == 8.333
    assert result.jank_count == 2
    assert result.jank_rate == pytest.approx(2 / 3)
    assert result.frames[1].on_time_finish is False
    assert result.frames[0].to_dict()["tsEpochMs"] == 1700000000000


def test_jank_classification_covers_observed_and_legacy_types() -> None:
    assert is_jank_frame("None") is False
    assert is_jank_frame("none") is False
    assert is_jank_frame("") is False
    assert is_jank_frame("[NULL]") is False
    assert is_jank_frame(None) is False
    assert is_jank_frame("Ok") is False
    assert is_jank_frame("Not janked") is False
    assert is_jank_frame("App Deadline Missed") is True
    assert is_jank_frame("SurfaceFlinger Stuffing") is True
    assert is_jank_frame("Janky frames") is True


def test_parse_frame_timeline_without_clock_snapshot_keeps_boottime_only() -> None:
    rows = [
        {
            "ts": 100,
            "dur": 16000000,
            "track_name": "Actual Timeline",
            "jank_type": "None",
            "expected_dur": None,
        }
    ]
    result = parse_frame_timeline(rows, clock_rows=[])
    assert result.available
    assert result.epoch_offset_ns is None
    frame = result.frames[0]
    assert frame.ts_epoch_ms is None
    assert frame.ts_boottime_ns == 100
    assert frame.duration_ms == 16.0
    assert any("epoch 对齐不可用" in note for note in result.diagnostics)


def test_parse_frame_timeline_orders_frames_and_numbers_them() -> None:
    rows = [
        {"ts": 300, "dur": 1_000_000, "track_name": "Actual Timeline", "jank_type": "None"},
        {"ts": 100, "dur": 2_000_000, "track_name": "Actual Timeline", "jank_type": "None"},
        {"ts": 200, "dur": 3_000_000, "track_name": "Actual Timeline", "jank_type": "None"},
    ]
    result = parse_frame_timeline(rows)
    assert [frame.ts_boottime_ns for frame in result.frames] == [100, 200, 300]
    assert [frame.frame_index for frame in result.frames] == [0, 1, 2]
    assert result.frames[0].duration_ms == 2.0


def test_frame_record_to_dict_uses_camel_case_contract() -> None:
    record = FrameRecord(
        frame_index=3,
        ts_boottime_ns=1_000,
        ts_epoch_ms=1_700_000_000_000,
        duration_ms=16.5,
        jank=True,
        jank_type="App Deadline Missed",
        present_type="Late Present",
        on_time_finish=False,
        expected_duration_ms=8.333,
        process_name="com.example.app",
        pid=12345,
        surface_frame_token="12",
        display_frame_token="12",
    )
    assert record.to_dict() == {
        "frameIndex": 3,
        "tsEpochMs": 1_700_000_000_000,
        "durationMs": 16.5,
        "jank": True,
        "jankType": "App Deadline Missed",
        "presentType": "Late Present",
        "onTimeFinish": False,
        "expectedDurationMs": 8.333,
        "processName": "com.example.app",
        "pid": 12345,
        "surfaceFrameToken": "12",
        "displayFrameToken": "12",
    }


def test_query_frame_timeline_unavailable_semantics_without_binary(tmp_path: Path) -> None:
    result = query_frame_timeline(
        tmp_path / "trace.pftrace",
        resolution=resolve_trace_processor(environ={}, which=lambda name: None, project_root=tmp_path),
        prefer_python_package=False,
    )
    assert not result.available
    assert result.frames == ()
    assert any(note.startswith(UNAVAILABLE_DIAGNOSTIC) for note in result.diagnostics)


def test_query_frame_timeline_execution_failure_degrades_without_raising(tmp_path: Path) -> None:
    result = query_frame_timeline(
        tmp_path / "trace.pftrace",
        processor=_processor_with_fake(tmp_path, mode="always_fail"),
    )
    assert not result.available
    assert result.frames == ()
    assert any(note.startswith(UNAVAILABLE_DIAGNOSTIC) and "boom" in note for note in result.diagnostics)


def test_parse_frame_timeline_missing_expected_dur_keeps_none_out_of_vsync() -> None:
    """LEFT JOIN 未命中（expected_dur 为 NULL）不得落成 0.0 污染 per-frame 值与中位数。"""
    rows = [
        {"ts": 100, "dur": 8333333, "track_name": "Expected Timeline"},
        {"ts": 1000, "dur": 16500000, "track_name": "Actual Timeline", "jank_type": "None", "expected_dur": None},
        {"ts": 2000, "dur": 16500000, "track_name": "Actual Timeline", "jank_type": "None"},
    ]
    result = parse_frame_timeline(rows)
    assert [frame.expected_duration_ms for frame in result.frames] == [None, None]
    # vsync 只由 Expected track 行得出，不受 0 值拖累
    assert result.vsync_period_ms == 8.333


def test_vsync_median_dedupes_expected_durations() -> None:
    """同一帧的 expected_dur 会同时出现在 Expected 行与 Actual join 值，去重后中位数才准。"""
    rows = [
        {"ts": 10, "dur": 8333333, "track_name": "Expected Timeline"},
        {"ts": 20, "dur": 16666667, "track_name": "Expected Timeline"},
        {"ts": 30, "dur": 16666667, "track_name": "Expected Timeline"},
        {"ts": 100, "dur": 16500000, "track_name": "Actual Timeline", "jank_type": "None", "expected_dur": 8333333},
        {"ts": 200, "dur": 33000000, "track_name": "Actual Timeline", "jank_type": "None", "expected_dur": 16666667},
    ]
    result = parse_frame_timeline(rows)
    # 去重后 distinct = {8.333, 16.667}，中位数 12.5；不去重则为重复值主导的 16.667
    assert result.vsync_period_ms == 12.5


def test_query_frame_timeline_diagnostics_include_query_time_notes(tmp_path: Path) -> None:
    """diagnostics 在查询后收集：--query-string 回退等查询期 note 不丢失。"""
    result = query_frame_timeline(
        tmp_path / "trace.pftrace",
        processor=_processor_with_fake(tmp_path, mode="legacy_fail"),
    )
    assert result.available
    assert any("query 子命令回退" in note for note in result.diagnostics)


def test_processor_close_closes_package_sessions_and_reopens(tmp_path: Path) -> None:
    module = _FakePackageModule(rows=[{"a": 1}])
    processor = _processor_with_fake(
        tmp_path,
        prefer_python_package=True,
        package_loader=lambda: module,
    )
    assert processor.query("select 1 as a", tmp_path / "trace.pftrace") == [{"a": 1}]
    processor.close()
    assert module.close_count == 1
    assert processor._package_sessions == {}
    assert processor._package_processors == []
    # close 后会话缓存清空：再次查询按需重建（幂等 close）
    assert processor.query("select 1 as a", tmp_path / "trace.pftrace") == [{"a": 1}]
    assert module.calls.count("open") == 2
    processor.close()
    assert module.close_count == 2


def test_processor_context_manager_closes_sessions(tmp_path: Path) -> None:
    module = _FakePackageModule(rows=[{"a": 1}])
    with _processor_with_fake(
        tmp_path,
        prefer_python_package=True,
        package_loader=lambda: module,
    ) as processor:
        assert processor.query("select 1 as a", tmp_path / "trace.pftrace") == [{"a": 1}]
    assert module.close_count == 1


def test_query_frame_timeline_closes_internal_but_not_caller_processor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import perfeval.collectors.android.perfetto_trace as perfetto_trace_module

    module = _FakePackageModule(rows=[{"a": 1}])
    monkeypatch.setattr(perfetto_trace_module, "_load_perfetto_package", lambda: module)
    trace = _fake_trace(tmp_path)

    # 内部创建的 processor：返回前关闭包会话（释放子进程句柄）
    result = query_frame_timeline(
        trace,
        resolution=resolve_trace_processor(
            environ={"PERFEVAL_TRACE_PROCESSOR": str(_fake_binary(tmp_path / "bin"))},
            which=lambda name: None,
        ),
    )
    assert result.available
    assert module.close_count == 1

    # 调用方传入的 processor：归调用方管理，聚合入口不得代为关闭
    caller_processor = _processor_with_fake(tmp_path, prefer_python_package=True, package_loader=lambda: module)
    query_frame_timeline(trace, processor=caller_processor)
    assert module.close_count == 1


# ---------------------------------------------------------------------------
# golden 样本 smoke（真二进制存在时才运行，否则 skip）
# ---------------------------------------------------------------------------

GOLDEN_TRACE = Path(__file__).resolve().parent.parent / "perfeval/fixtures/android/golden/frame-timeline.pftrace"
KNOWN_GOLDEN_JANK_TYPES = {
    "None",
    "SurfaceFlinger Stuffing",
    "App Deadline Missed",
}


def _real_trace_processor() -> str | None:
    env_value = os.environ.get("PERFEVAL_TRACE_PROCESSOR")
    if env_value and Path(env_value).is_file():
        return env_value
    return shutil.which("trace_processor_shell") or shutil.which("trace_processor")


def test_golden_trace_path_is_repo_anchored() -> None:
    """golden 路径必须锚定测试文件位置：非仓库根目录跑 pytest 时不得静默 skip。"""
    assert GOLDEN_TRACE.is_absolute()
    assert GOLDEN_TRACE.is_file()
    assert GOLDEN_TRACE.name == "frame-timeline.pftrace"


@pytest.mark.skipif(
    not GOLDEN_TRACE.is_file() or _real_trace_processor() is None,
    reason="需要 golden .pftrace 与本机 trace_processor（协调者设备 smoke 阶段覆盖）",
)
def test_golden_frame_timeline_smoke() -> None:
    processor = PerfettoTraceProcessor(
        resolve_trace_processor(environ=dict(os.environ)),
        package_loader=lambda: None,
    )
    result = query_frame_timeline(GOLDEN_TRACE, processor=processor)
    assert result.available
    assert result.frame_count > 0
    assert result.vsync_period_ms is not None and result.vsync_period_ms > 0
    first = result.frames[0]
    assert first.ts_epoch_ms is not None
    # golden 采集于 2026-09：epoch 必须落在合理区间（而非 boot 时钟的 1e11ns 量级）
    assert first.ts_epoch_ms > 1_700_000_000_000
    assert {frame.jank_type for frame in result.frames} <= KNOWN_GOLDEN_JANK_TYPES
    assert 0.0 <= (result.jank_rate or 0.0) <= 1.0
