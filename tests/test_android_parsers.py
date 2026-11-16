from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from perfeval.collectors.android import (
    parse_adb_devices,
    parse_android_artifacts,
    parse_focused_window,
    parse_framestats_detail,
    parse_framestats_detail_events,
    parse_gfxinfo,
    parse_gfxinfo_events,
    parse_logcat,
    parse_logcat_events,
    parse_meminfo,
    parse_meminfo_events,
)
from perfeval.core.timeline import TimelineEvent


FIXTURE_ROOT = Path("perfeval/fixtures/android/golden")

# AOSP 经典 16 列布局（libs/hwui FrameInfo::dump）。
CANONICAL_FRAMESTATS_COLUMNS = (
    "Flags",
    "IntendedVsync",
    "Vsync",
    "OldestInputEvent",
    "NewestInputEvent",
    "HandleInputStart",
    "AnimationStart",
    "PerformTraversalsStart",
    "DrawStart",
    "SyncQueued",
    "SyncStart",
    "IssueDrawCommandsStart",
    "SwapBuffers",
    "FrameCompleted",
    "DequeueBufferDuration",
    "QueueBufferDuration",
)


def test_parse_adb_devices() -> None:
    devices = parse_adb_devices(
        """
        List of devices attached
        emulator-5554 device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a
        offline-1 offline
        """
    )

    assert devices[0].serial == "emulator-5554"
    assert devices[0].state == "device"
    assert devices[0].model == "sdk_gphone64_arm64"
    assert devices[1].state == "offline"


def test_gfxinfo_parser_extracts_frame_metrics() -> None:
    text = (FIXTURE_ROOT / "gfxinfo.txt").read_text(encoding="utf-8")
    parsed = parse_gfxinfo(text)

    assert parsed["total_frames"] == 100
    assert parsed["janky_frames"] == 12
    assert parsed["jank_rate"] == 0.12
    assert parsed["p95_ms"] == 24

    events = parse_gfxinfo_events(text, run_id="run-android")
    names = {event["name"] for event in events}
    assert "frame.jank_rate" in names
    assert "frame.duration.p95.ms" in names


def test_gfxinfo_parser_ignores_legacy_and_gpu_summary() -> None:
    text = """
    Total frames rendered: 624
    Janky frames: 0 (0.00%)
    Janky frames (legacy): 624 (100.00%)
    50th percentile: 40ms
    90th percentile: 73ms
    95th percentile: 89ms
    99th percentile: 113ms
    50th gpu percentile: 3ms
    90th gpu percentile: 3ms
    95th gpu percentile: 3ms
    99th gpu percentile: 4ms
    """

    parsed = parse_gfxinfo(text)

    assert parsed["total_frames"] == 624
    assert parsed["janky_frames"] == 0
    assert parsed["jank_rate"] == 0.0
    assert parsed["p50_ms"] == 40
    assert parsed["p90_ms"] == 73
    assert parsed["p95_ms"] == 89
    assert parsed["p99_ms"] == 113


def test_gfxinfo_parser_selects_focused_window_from_framestats() -> None:
    text = """
    Total frames rendered: 1000
    Janky frames: 200 (20.00%)
    95th percentile: 40ms
    Window: com.example.app/com.example.app.HiddenActivity
    Total frames rendered: 500
    Janky frames: 400 (80.00%)
    95th percentile: 80ms
    Window: com.example.app/com.example.app.TargetActivity
    Total frames rendered: 100
    Janky frames: 5 (5.00%)
    95th percentile: 16ms
    """

    parsed = parse_gfxinfo(
        text,
        window="com.example.app/.TargetActivity",
        prefer_window=True,
    )

    assert parsed["total_frames"] == 100
    assert parsed["janky_frames"] == 5
    assert parsed["jank_rate"] == 0.05
    assert parsed["p95_ms"] == 16
    assert parsed["window"] == "com.example.app/com.example.app.TargetActivity"


def test_parse_focused_window_ignores_null_and_other_packages() -> None:
    text = """
      mCurrentFocus=null
      mCurrentFocus=Window{123 u0 com.other.app/com.other.app.MainActivity}
      mCurrentFocus=Window{456 u0 com.example.app/com.example.app.TargetActivity}
      mFocusedApp=ActivityRecord{789 u0 com.example.app/.TargetActivity t42}
    """

    assert parse_focused_window(text, package_name="com.example.app") == (
        "com.example.app/com.example.app.TargetActivity"
    )


def test_meminfo_parser_extracts_memory_metrics() -> None:
    text = (FIXTURE_ROOT / "meminfo.txt").read_text(encoding="utf-8")
    parsed = parse_meminfo(text)

    assert parsed["pss_mb"] == 88.0
    assert parsed["java_heap_mb"] == 50.0
    assert parsed["native_heap_mb"] == 22.906
    assert parsed["graphics_mb"] == 12.056

    events = parse_meminfo_events(text, run_id="run-android")
    names = {event["name"] for event in events}
    assert "memory.pss.peak_mb" in names
    assert "memory.java_heap.mb" in names


def test_logcat_parser_extracts_stability_markers() -> None:
    crash = (FIXTURE_ROOT / "logcat-crash.txt").read_text(encoding="utf-8")
    anr = (FIXTURE_ROOT / "logcat-anr.txt").read_text(encoding="utf-8")

    assert parse_logcat(crash, package_name="com.perfeval.fixture")["crash_count"] == 1
    assert parse_logcat(anr, package_name="com.perfeval.fixture")["anr_count"] == 1

    events = parse_logcat_events(crash + "\n" + anr, run_id="run-android", package_name="com.perfeval.fixture")
    by_name = {event["name"]: event for event in events}
    assert by_name["stability.crash.count"]["data"]["value"] == 1
    assert by_name["stability.anr.count"]["data"]["value"] == 1
    assert "stability.crash.detected" in by_name


def test_logcat_parser_ignores_crashes_from_other_packages() -> None:
    text = "\n".join(
        [
            "E AndroidRuntime: FATAL EXCEPTION: main",
            "E AndroidRuntime: Process: com.other.app, PID: 123",
        ]
    )

    parsed = parse_logcat(text, package_name="com.perfeval.fixture")

    assert parsed["crash_count"] == 0


def test_logcat_parser_extracts_startup_marker() -> None:
    startup = (FIXTURE_ROOT / "logcat-startup.txt").read_text(encoding="utf-8")

    events = parse_logcat_events(startup, run_id="run-android", package_name="com.perfeval.fixture")
    by_name = {event["name"]: event for event in events}
    assert "startup.cold.ms" in by_name
    assert by_name["startup.cold.ms"]["data"]["value"] == 1234
    assert by_name["startup.cold.ms"]["data"]["unit"] == "ms"
    assert by_name["startup.cold.ms"]["data"]["direction"] == "lower_better"
    assert by_name["startup.cold.ms"]["source"]["evidenceLevel"] == "L1"


def test_parse_android_artifact_directory(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    for filename in ("gfxinfo.txt", "meminfo.txt"):
        (artifact_dir / filename).write_text((FIXTURE_ROOT / filename).read_text(encoding="utf-8"), encoding="utf-8")
    logcat_text = (
        (FIXTURE_ROOT / "logcat-startup.txt").read_text(encoding="utf-8")
        + "\n"
        + (FIXTURE_ROOT / "logcat-crash.txt").read_text(encoding="utf-8")
    )
    (artifact_dir / "logcat.txt").write_text(logcat_text, encoding="utf-8")

    events = parse_android_artifacts(artifact_dir, run_id="run-android", package_name="com.perfeval.fixture")
    names = {event["name"]: event for event in events}
    assert "frame.jank_rate" in names
    assert "memory.pss.peak_mb" in names
    assert "stability.crash.count" in names
    assert "startup.cold.ms" in names
    assert names["startup.cold.ms"]["data"]["value"] == 1234


def test_parse_android_artifacts_tolerates_malformed_device_text(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "gfxinfo.txt").write_bytes(
        b"Total frames rendered: 10\nJanky frames: 1 (10.00%)\ninvalid:\xe6\n"
    )
    (artifact_dir / "logcat.txt").write_bytes(
        b"E AndroidRuntime: FATAL EXCEPTION: main\ninvalid:\xff\n"
    )

    events = parse_android_artifacts(
        artifact_dir,
        run_id="run-malformed-utf8",
    )

    names = {event["name"] for event in events}
    assert "frame.jank_rate" in names
    assert "stability.crash.count" in names


def test_parse_android_artifacts_prefers_focused_framestats_window(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "gfxinfo.txt").write_text(
        "Total frames rendered: 1000\nJanky frames: 200 (20.00%)\n95th percentile: 40ms\n",
        encoding="utf-8",
    )
    (artifact_dir / "gfxinfo-framestats.txt").write_text(
        """
        Total frames rendered: 1000
        Janky frames: 200 (20.00%)
        95th percentile: 40ms
        Window: com.example.app/com.example.app.HiddenActivity
        Total frames rendered: 500
        Janky frames: 400 (80.00%)
        95th percentile: 80ms
        Window: com.example.app/com.example.app.TargetActivity
        Total frames rendered: 100
        Janky frames: 5 (5.00%)
        95th percentile: 16ms
        """,
        encoding="utf-8",
    )
    (artifact_dir / "window.txt").write_text(
        "mCurrentFocus=Window{456 u0 com.example.app/com.example.app.TargetActivity}\n",
        encoding="utf-8",
    )

    events = parse_android_artifacts(
        artifact_dir,
        run_id="run-focused",
        package_name="com.example.app",
    )
    by_name = {event["name"]: event for event in events}

    assert by_name["frame.jank_rate"]["data"]["value"] == 0.05
    assert by_name["frame.duration.p95.ms"]["data"]["value"] == 16
    assert by_name["frame.jank_rate"]["data"]["window"] == (
        "com.example.app/com.example.app.TargetActivity"
    )


def test_meminfo_light_golden_parses_total_fallback() -> None:
    text = (FIXTURE_ROOT / "meminfo-light.txt").read_text(encoding="utf-8")

    parsed = parse_meminfo(text)

    # 无 App Summary 段时回退 TOTAL 行（KB -> MB）
    assert parsed["pss_mb"] == 89.0


def test_parse_meminfo_events_marks_final_snapshot_semantic() -> None:
    text = (FIXTURE_ROOT / "meminfo.txt").read_text(encoding="utf-8")

    events = parse_meminfo_events(text, run_id="run-snapshot", pss_semantic="final_snapshot")
    by_name = {event["name"]: event for event in events}

    assert by_name["memory.pss.peak_mb"]["data"]["semantic"] == "final_snapshot"
    assert "semantic" not in by_name["memory.java_heap.mb"]["data"]

    # 周期采样已覆盖 PSS 序列时跳过结束快照
    events = parse_meminfo_events(text, run_id="run-stream", include_pss=False)
    names = {event["name"] for event in events}
    assert "memory.pss.peak_mb" not in names
    assert "memory.java_heap.mb" in names


def test_parse_android_artifacts_suppresses_pss_snapshot_when_stream_present(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "meminfo.txt").write_text(
        (FIXTURE_ROOT / "meminfo.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (artifact_dir / "samples.ndjson").write_text(
        (FIXTURE_ROOT / "samples.ndjson").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    events = parse_android_artifacts(artifact_dir, run_id="run-suppressed")

    pss_events = [event for event in events if event["name"] == "memory.pss.peak_mb"]
    # PSS 全部来自采样 counter 事件，meminfo 结束快照被抑制
    assert len(pss_events) == 8
    assert all(event["type"] == "counter" for event in pss_events)
    assert all(event["data"]["value"] == value for event, value in zip(pss_events, [312.0, 318.5, 324.25, 321.0, 336.75, 329.5, 325.125, 319.875]))

    java_heap = [event for event in events if event["name"] == "memory.java_heap.mb"]
    assert len(java_heap) == 1
    assert "semantic" not in java_heap[0]["data"]

    for event in events:
        TimelineEvent.from_dict(event)


def test_parse_android_artifacts_marks_pss_snapshot_without_samples(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "meminfo.txt").write_text(
        (FIXTURE_ROOT / "meminfo.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    events = parse_android_artifacts(artifact_dir, run_id="run-fallback")

    pss_event = next(event for event in events if event["name"] == "memory.pss.peak_mb")
    assert pss_event["type"] == "metric"
    assert pss_event["data"]["semantic"] == "final_snapshot"
    assert pss_event["data"]["value"] == 88.0


def test_framestats_detail_golden_full_parse() -> None:
    text = (FIXTURE_ROOT / "gfxinfo-framestats-detail.txt").read_text(encoding="utf-8")

    parsed = parse_framestats_detail(text)

    # 真机 24 列段：空块(ShellDropTarget) + NavigationBar0(7 帧) + NotificationShade(7 帧)；
    # 经典 16 列段 7 帧（NaN 行与 FrameCompleted<IntendedVsync 行跳过），
    # 3 个 ROM 收尾用的孤立 ---PROFILEDATA--- 块无表头被跳过。
    assert parsed["frame_count"] == 21
    assert parsed["block_count"] == 4
    assert parsed["skipped_row_count"] == 2
    assert parsed["skipped_block_count"] == 3
    assert len(parsed["frame_duration_ms"]) == 21
    assert parsed["jank_frame_count"] == 9

    # ns -> ms 换算抽查：NavigationBar0 与经典 16 列块
    assert parsed["frame_duration_ms"][0] == pytest.approx(1.763908)
    assert parsed["frame_duration_ms"][3] == pytest.approx(49.597455)
    assert parsed["frame_duration_ms"][7] == pytest.approx(11.711141)
    assert parsed["frame_duration_ms"][10] == pytest.approx(460.79349)
    assert parsed["frame_duration_ms"][14] == pytest.approx(3.0)
    assert parsed["frame_duration_ms"][17] == pytest.approx(40.0)

    # jank = 帧耗时 > 1.5 × 刷新周期（24 列块走 FrameInterval，16 列块走 Vsync 步进）
    jank_indices = {frame["frame_index"] for frame in parsed["frames"] if frame["jank"]}
    assert jank_indices == {3, 4, 5, 6, 10, 11, 12, 13, 17}
    nav_block = parsed["blocks"][1]
    shade_block = parsed["blocks"][2]
    classic_block = parsed["blocks"][3]
    assert nav_block["refresh_period_basis"] == "frame_interval"
    assert nav_block["jank_threshold_ms"] == pytest.approx(12.4992855)
    assert shade_block["refresh_period_basis"] == "frame_interval"
    assert shade_block["jank_threshold_ms"] == pytest.approx(12.497067)
    assert classic_block["refresh_period_basis"] == "vsync_delta"
    assert classic_block["jank_threshold_ms"] == pytest.approx(25.0000005)

    # Flags 位与元数据透传
    assert parsed["frames"][10]["flags"] == 1
    assert parsed["frames"][10]["flag_names"] == ["window"]
    assert parsed["frames"][14]["flag_names"] == ["window"]
    assert parsed["frames"][15]["flag_names"] == ["first_draw"]
    assert parsed["frames"][0]["flag_names"] == []
    assert parsed["frames"][0]["window"] == "NavigationBar0"
    assert parsed["frames"][0]["process"] == "com.android.systemui"
    assert parsed["frames"][14]["window"] == "com.example.app/.MainActivity"
    assert parsed["frames"][14]["process"] == "com.example.app"

    # 原始时间戳保留纳秒原值
    assert parsed["frames"][14]["raw_ns"]["IntendedVsync"] == 100000000
    assert parsed["frames"][14]["raw_ns"]["FrameCompleted"] == 103000000
    assert parsed["frames"][17]["raw_ns"]["FrameCompleted"] == 190000001


def test_framestats_detail_sixteen_column_layout() -> None:
    text = """
    Window: com.example.app/.MainActivity
    ---PROFILEDATA---
    Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DequeueBufferDuration,QueueBufferDuration,
    0,100000000,100000000,0,0,100200000,100210000,100233334,100400000,100500000,100550000,100600000,100700000,103000000,1200,30000,
    0,116666667,116666667,0,0,116866667,116876667,116900001,117066667,117166667,117216667,117266667,117366667,156666668,1200,30000,
    0,nan,nan,nan,nan,nan,nan,nan,nan,nan,nan,nan,nan,nan,nan,nan,
    ---PROFILEDATA-END---
    """

    parsed = parse_framestats_detail(text)

    assert parsed["frame_count"] == 2
    assert parsed["skipped_row_count"] == 1
    block = parsed["blocks"][0]
    assert tuple(block["columns"]) == CANONICAL_FRAMESTATS_COLUMNS
    for frame in parsed["frames"]:
        assert tuple(frame["raw_ns"]) == CANONICAL_FRAMESTATS_COLUMNS
        assert all(isinstance(value, int) for value in frame["raw_ns"].values())
    # 16 列布局无 FrameInterval：刷新周期取 Vsync 步进中位数（16.666667ms）
    assert block["refresh_period_basis"] == "vsync_delta"
    assert block["jank_threshold_ms"] == pytest.approx(25.0000005)
    # 第二帧 40ms > 25ms 记 jank，第一帧 3ms 不记
    assert [frame["jank"] for frame in parsed["frames"]] == [False, True]


def test_framestats_detail_jank_threshold_follows_refresh_period() -> None:
    # FrameInterval 列存在时刷新周期取其中位数（8333333ns -> 12.5ms 阈值）
    interval_text = """
    ---PROFILEDATA---
    Flags,IntendedVsync,FrameCompleted,FrameInterval,
    0,100000000,130000000,8333333,
    0,200000000,205000000,8333333,
    ---PROFILEDATA-END---
    """
    interval_parsed = parse_framestats_detail(interval_text)
    assert interval_parsed["blocks"][0]["refresh_period_basis"] == "frame_interval"
    assert interval_parsed["blocks"][0]["jank_threshold_ms"] == pytest.approx(12.5)
    assert [frame["jank"] for frame in interval_parsed["frames"]] == [True, False]

    # 单帧且无 FrameInterval/Vsync 列时兜底取帧耗时中位数
    fallback_text = """
    ---PROFILEDATA---
    Flags,IntendedVsync,FrameCompleted,
    0,100000000,110000000,
    ---PROFILEDATA-END---
    """
    fallback_parsed = parse_framestats_detail(fallback_text)
    assert fallback_parsed["blocks"][0]["refresh_period_basis"] == "median_frame_duration"
    assert fallback_parsed["blocks"][0]["jank_threshold_ms"] == pytest.approx(15.0)
    assert fallback_parsed["frames"][0]["jank"] is False


def test_framestats_detail_tolerates_malformed_blocks() -> None:
    text = """
    Window: com.example.app/.MainActivity
    ---PROFILEDATA---
    Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DequeueBufferDuration,QueueBufferDuration,
    ---PROFILEDATA-END---
    ---PROFILEDATA---
    Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DequeueBufferDuration,QueueBufferDuration,
    0,100000000,100000000,0,0,100200000,100210000,100233334,100400000,100500000,100550000,100600000,100700000,101200000,1200,30000,
    0,116666667,116666667,0,0,116866667,
    0,116666667,116666667,extra,cells,beyond,header,count,0,0,0,0,0,0,0,0,0,0,
    0,116666667,nan,0,0,0,0,0,0,0,0,0,0,130000000,0,0,
    ---PROFILEDATA-END---
    ---PROFILEDATA---
    Unsupported,Header,Columns,
    0,100000000,103000000,
    ---PROFILEDATA-END---
    """

    parsed = parse_framestats_detail(text)

    # 空块保留但 0 帧；仅 1 条合法帧行，列数不足/超列/NaN 行各跳过 1 条，
    # 缺必需列的表头块整体跳过（其行不计数）。
    assert parsed["frame_count"] == 1
    assert parsed["skipped_row_count"] == 3
    assert parsed["skipped_block_count"] == 1
    assert parsed["blocks"][0]["frame_count"] == 0
    assert parsed["frame_duration_ms"] == [pytest.approx(1.2)]
    assert parsed["frames"][0]["jank"] is False


def test_framestats_detail_handles_absent_blocks() -> None:
    # 无 PROFILEDATA 的聚合输出与空文本都必须零帧返回，不抛异常
    aggregate_only = parse_framestats_detail((FIXTURE_ROOT / "gfxinfo.txt").read_text(encoding="utf-8"))
    assert aggregate_only["frame_count"] == 0
    assert aggregate_only["block_count"] == 0
    assert aggregate_only["frames"] == []
    assert aggregate_only["frame_duration_ms"] == []
    assert aggregate_only["jank_frame_count"] == 0

    empty = parse_framestats_detail("")
    assert empty["frame_count"] == 0
    assert empty["blocks"] == []


# --- CM-TASK-009：framestats 逐帧事件产出与 collector 接线 ---


def test_framestats_detail_events_from_golden_match_fps_contract() -> None:
    text = (FIXTURE_ROOT / "gfxinfo-framestats-detail.txt").read_text(encoding="utf-8")

    events = parse_framestats_detail_events(text, run_id="run-f", start_seq=9)

    assert len(events) == 21
    first = events[0]
    assert first["schemaVersion"] == "perfeval.timeline.v1"
    assert first["seq"] == 9
    assert first["type"] == "counter"
    assert first["name"] == "frame.duration.detail.ms"
    assert first["source"] == {
        "platform": "android",
        "collector": "dumpsys.gfxinfo.framestats",
        "evidenceLevel": "L1",
    }
    # METRIC_SOURCES §4 帧事件契约：jank 判定、帧序与来源标注进 data
    assert first["data"]["value"] == pytest.approx(1.763908)
    assert first["data"]["unit"] == "ms"
    assert first["data"]["direction"] == "lower_better"
    assert first["data"]["frameIndex"] == 0
    assert first["data"]["jank"] is False
    assert first["data"]["fpsSource"] == "framestats"
    assert events[3]["data"]["jank"] is True
    assert events[10]["data"]["flagNames"] == ["window"]
    # tsMonotonicNs 取相对首帧的 IntendedVsync 偏移
    assert first["tsMonotonicNs"] == 0
    assert events[1]["tsMonotonicNs"] == 88185185062497 - 88185176746824
    for event in events:
        TimelineEvent.from_dict(event)


def test_parse_android_artifacts_emits_frame_detail_events(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    shutil.copyfile(
        FIXTURE_ROOT / "gfxinfo-framestats-detail.txt",
        artifact_dir / "gfxinfo-framestats.txt",
    )

    events = parse_android_artifacts(artifact_dir, run_id="run-wire")

    detail = [event for event in events if event["name"] == "frame.duration.detail.ms"]
    assert len(detail) == 21
    # 聚合事件与逐帧事件共存；链路事件先行，seq 连续不重叠
    aggregate = [event for event in events if event["name"] == "frame.jank_rate"]
    assert len(aggregate) == 1
    assert detail[0]["seq"] < detail[-1]["seq"] < aggregate[0]["seq"]
    # 所有事件 seq 从 1 起单调连续
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    for event in events:
        TimelineEvent.from_dict(event)


def test_parse_android_artifacts_without_profiledata_emits_no_detail_events(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "gfxinfo-framestats.txt").write_text(
        "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n",
        encoding="utf-8",
    )

    events = parse_android_artifacts(artifact_dir, run_id="run-agg")

    assert not [event for event in events if event["name"] == "frame.duration.detail.ms"]


# --- CM-TASK-013：FPS 来源优先级链（pftrace+tp → framestats → 聚合兜底） ---


def _tp_frame(frame_index, duration_ms, *, jank, process_name, pid=None):
    from perfeval.collectors.android.perfetto_trace import FrameRecord

    return FrameRecord(
        frame_index=frame_index,
        ts_boottime_ns=1_000_000_000 + frame_index * 16_666_667,
        ts_epoch_ms=1_700_000_000_000 + frame_index * 16,
        duration_ms=duration_ms,
        jank=jank,
        process_name=process_name,
        pid=pid,
    )


def _patch_tp_result(monkeypatch, result) -> None:
    from perfeval.collectors.android import collector as collector_module

    monkeypatch.setattr(collector_module, "query_frame_timeline", lambda trace_path: result)


def test_frame_chain_prefers_trace_processor_and_filters_target_process(
    tmp_path: Path, monkeypatch
) -> None:
    from perfeval.collectors.android.perfetto_trace import FrameTimelineResult

    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "perfetto.pftrace").write_bytes(b"fake-trace")
    shutil.copyfile(
        FIXTURE_ROOT / "gfxinfo-framestats-detail.txt",
        artifact_dir / "gfxinfo-framestats.txt",
    )
    # SurfaceFlinger 合成帧与目标应用帧混在同一 Actual Timeline track
    _patch_tp_result(
        monkeypatch,
        FrameTimelineResult(
            frames=(
                _tp_frame(0, 5.0, jank=False, process_name="com.example.app", pid=4321),
                _tp_frame(1, 8.0, jank=False, process_name="surfaceflinger", pid=999),
                _tp_frame(2, 40.0, jank=True, process_name="com.example.app", pid=4321),
                _tp_frame(3, 33.0, jank=True, process_name="surfaceflinger", pid=999),
                _tp_frame(4, 41.0, jank=True, process_name="com.example.app", pid=4321),
            ),
            vsync_period_ms=16.667,
            epoch_offset_ns=1_700_000_000_000_000_000,
        ),
    )

    events = parse_android_artifacts(
        artifact_dir, run_id="run-tp", package_name="com.example.app"
    )

    detail = [event for event in events if event["name"] == "frame.duration.detail.ms"]
    # 只有目标进程的帧进入 FPS 口径（SurfaceFlinger 合成帧被剔除，帧序保留原值）
    assert [event["data"]["frameIndex"] for event in detail] == [0, 2, 4]
    assert [event["data"]["value"] for event in detail] == [5.0, 40.0, 41.0]
    assert all(event["data"]["fpsSource"] == "tp" for event in detail)
    assert all(event["source"]["collector"] == "perfetto.tp" for event in detail)
    assert all(event["data"]["jank"] is (event["data"]["frameIndex"] != 0) for event in detail)
    # tp 命中时不产生降级诊断
    assert not [event for event in events if event["type"] == "diagnostic"]
    for event in events:
        TimelineEvent.from_dict(event)


def test_frame_chain_falls_back_to_framestats_when_tp_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    from perfeval.collectors.android.perfetto_trace import FrameTimelineResult

    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "perfetto.pftrace").write_bytes(b"fake-trace")
    shutil.copyfile(
        FIXTURE_ROOT / "gfxinfo-framestats-detail.txt",
        artifact_dir / "gfxinfo-framestats.txt",
    )
    _patch_tp_result(
        monkeypatch,
        FrameTimelineResult(
            frames=(),
            vsync_period_ms=None,
            epoch_offset_ns=None,
            diagnostics=("trace_processor.unavailable:trace_processor 二进制未找到",),
            available=False,
        ),
    )

    events = parse_android_artifacts(
        artifact_dir, run_id="run-fallback", package_name="com.example.app"
    )

    detail = [event for event in events if event["name"] == "frame.duration.detail.ms"]
    assert len(detail) == 21
    assert all(event["data"]["fpsSource"] == "framestats" for event in detail)
    degraded = [event for event in events if event["type"] == "diagnostic"]
    assert [event["name"] for event in degraded] == ["trace_processor.unavailable"]
    assert "二进制未找到" in degraded[0]["data"]["message"]

    # 降级诊断并入 summary diagnostics，fpsSource 标注 framestats
    from perfeval.core.summary import summarize_timeline

    summary = summarize_timeline("run-fallback", events)
    assert summary["fpsSource"] == "framestats"
    codes = [entry.get("data", {}).get("code") for entry in summary["diagnostics"]]
    assert "trace_processor.unavailable" in codes
    assert summary["metrics"]["frame.fps.mean"]["n"] == 21


def test_frame_chain_aggregate_fallback_annotates_fps_source(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "gfxinfo.txt").write_text(
        "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n",
        encoding="utf-8",
    )

    events = parse_android_artifacts(artifact_dir, run_id="run-aggregate")

    # 无逐帧来源：聚合 frame 事件标注 fpsSource=aggregate（降级 C）
    frame_events = [
        event for event in events if str(event.get("name", "")).startswith("frame.")
    ]
    assert frame_events
    assert all(event["data"]["fpsSource"] == "aggregate" for event in frame_events)
    degraded = [event for event in events if event["type"] == "diagnostic"]
    assert [event["name"] for event in degraded] == ["fps.source.degraded"]

    from perfeval.core.summary import summarize_timeline

    summary = summarize_timeline("run-aggregate", events)
    assert summary["fpsSource"] == "aggregate"
    assert "frame.fps.mean" not in summary["metrics"]
    assert "jankSegments" not in summary


def test_frame_chain_degrades_to_framestats_when_process_filter_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    from perfeval.collectors.android.perfetto_trace import FrameTimelineResult

    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "perfetto.pftrace").write_bytes(b"fake-trace")
    shutil.copyfile(
        FIXTURE_ROOT / "gfxinfo-framestats-detail.txt",
        artifact_dir / "gfxinfo-framestats.txt",
    )
    _patch_tp_result(
        monkeypatch,
        FrameTimelineResult(
            frames=(_tp_frame(0, 5.0, jank=False, process_name="surfaceflinger", pid=999),),
            vsync_period_ms=16.667,
            epoch_offset_ns=None,
        ),
    )

    events = parse_android_artifacts(
        artifact_dir, run_id="run-empty-filter", package_name="com.example.app"
    )

    detail = [event for event in events if event["name"] == "frame.duration.detail.ms"]
    assert detail
    assert all(event["data"]["fpsSource"] == "framestats" for event in detail)
    degraded = [event for event in events if event["type"] == "diagnostic"]
    assert [event["name"] for event in degraded] == ["fps.source.degraded"]
    assert "process 过滤后为空" in degraded[0]["data"]["message"]


def test_frame_chain_diagnostics_survive_without_metric_events(
    tmp_path: Path, monkeypatch
) -> None:
    """F1-fix：parsed/frame_detail 皆空（只有损坏 pftrace 且 tp 失败）时，
    trace_processor.unavailable 诊断事件仍独立落 timeline，不被门控丢弃。"""
    from perfeval.collectors.android.perfetto_trace import FrameTimelineResult
    from perfeval.core.summary import summarize_timeline

    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "perfetto.pftrace").write_bytes(b"corrupt-trace")
    _patch_tp_result(
        monkeypatch,
        FrameTimelineResult(
            frames=(),
            vsync_period_ms=None,
            epoch_offset_ns=None,
            diagnostics=("trace_processor.unavailable:trace 文件损坏，无法解析",),
            available=False,
        ),
    )

    events = parse_android_artifacts(artifact_dir, run_id="run-corrupt")

    # 无任何指标事件，但降级诊断事件独立落盘
    metric_events = [event for event in events if event["type"] in ("metric", "counter")]
    assert metric_events == []
    diagnostics = [event for event in events if event["type"] == "diagnostic"]
    assert [event["name"] for event in diagnostics] == ["trace_processor.unavailable"]
    assert "trace 文件损坏" in diagnostics[0]["data"]["message"]
    assert diagnostics[0]["data"]["severity"] == "warning"
    for event in events:
        TimelineEvent.from_dict(event)

    # 诊断事件经 summary 派生层并入 summary diagnostics
    summary = summarize_timeline("run-corrupt", events)
    codes = [entry.get("data", {}).get("code") for entry in summary["diagnostics"]]
    assert "trace_processor.unavailable" in codes


def test_logcat_epoch_android16_first_line_timestamp() -> None:
    """实机 golden（2026-11-03，SM_F9560 / Android 16）：`logcat -T 5 -v epoch -d`。

    Android 16 真机 epoch 行形如 `1793719324.237  6168  6168 W tag: msg`——
    perfeval `_line_epoch_ms` 前缀解析与「首条 epoch 时间戳 = t0 原点」口径在
    新 ROM 上成立（backend 流式通道同期修复：不带 -T 1 时首行是缓冲区最旧
    条目，t0 早数小时）。
    """
    text = (FIXTURE_ROOT / "logcat-epoch-android16.txt").read_text(encoding="utf-8")

    parsed = parse_logcat(text, package_name="com.perfeval.fixture")

    first = parsed["first_epoch_ms"]
    assert first is not None
    # 行首 1793719324.237 → 1793719324237（秒*1000 + 毫秒）
    assert first == 1793719324237
    # 纯真机日志（无注入事件）：crash/anr 零计数、无 custom_events
    assert parsed["crash_count"] == 0
    assert parsed["custom_events"] == []
