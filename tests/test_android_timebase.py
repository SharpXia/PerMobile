"""CM-TASK-021 时间对齐测试：t0/marker/video 锚点、tp 帧 epoch 升格、manifest context。

SCREEN_RECORD_SYNC §2：t0=logcat -v epoch 首条时间戳；视频首帧=启动时刻+1s 编码
延迟估算；三方锚点写入 manifest context.clockAnchors 离线可复算。
"""

from __future__ import annotations

import json
from pathlib import Path

from perfeval.collectors.android import AndroidCollectionResult, AndroidPreflightResult
from perfeval.collectors.android.adb import AdbClient
from perfeval.collectors.android.flow import InteractionCommand
from perfeval.collectors.android.plan import build_screen_record_plan
from perfeval.collectors.android.gfxinfo import (
    CLOCK_DOMAIN_EPOCH,
    CLOCK_DOMAIN_FRAME_RELATIVE,
    build_frame_detail_events,
)
from perfeval.collectors.android.logcat import parse_logcat
from perfeval.collectors.android.timebase import (
    derive_clock_anchors,
    frame_epoch_ms,
    marker_events_from_timeline,
    parse_device_clock_output,
)
from perfeval.cli.commands import run as run_command
from perfeval.cli.main import main
from perfeval.core.schema import Manifest

from test_android_flow import _Completed, _completed, _screen_record_executor


# --- logcat：t0 与 marker epoch ---

def test_parse_logcat_extracts_first_epoch_and_marker_epoch() -> None:
    text = "\n".join(
        [
            "1757300000.123 I/ActivityManager(  1): Start proc for com.perfeval.fixture",
            "1757300001.500 D/PerfevalAgent(1234): PERFEVAL_EVENT {\"type\": \"metric\", \"name\": \"flow.step.scroll\", \"value\": 1}",
            "1757300002.000 D/PerfevalAgent(1234): PERFEVAL_EVENT {\"type\": \"metric\", \"name\": \"flow.step.scroll_end\", \"value\": 2, \"tsEpochMs\": 1757300002999}",
        ]
    )
    parsed = parse_logcat(text)
    # t0 = 首条 epoch 时间戳
    assert parsed["first_epoch_ms"] == 1757300000123
    # marker 行 epoch 注入 tsEpochMs；事件自带 tsEpochMs 时以事件为准
    assert parsed["custom_events"][0]["tsEpochMs"] == 1757300001500
    assert parsed["custom_events"][1]["tsEpochMs"] == 1757300002999
    # 无 epoch 前缀的 logcat（旧格式）：first_epoch_ms 为 None，不崩溃
    assert parse_logcat("D/Tag( 1): hello")["first_epoch_ms"] is None


# --- timebase：设备时钟解析与帧 epoch 换算 ---

def test_parse_device_clock_output() -> None:
    stdout = "1757300000123\n3456.789 3456.789\n"
    anchor = parse_device_clock_output(stdout)
    assert anchor == {"source": "device_date", "epochMs": 1757300000123, "boottimeNs": 3456789000000}

    # F-fix（R1-N1）：date 输出损坏 → host epoch 兜底时 source 标 host_epoch_approx
    anchor = parse_device_clock_output("bad", host_epoch_ms=1757300009999)
    assert anchor == {"source": "host_epoch_approx", "epochMs": 1757300009999}
    # uptime 可解析但 date 损坏：boottime 保留 + host 兜底标注
    anchor = parse_device_clock_output("bad\n3456.789 3456.789\n", host_epoch_ms=1757300009999)
    assert anchor == {
        "source": "host_epoch_approx",
        "epochMs": 1757300009999,
        "boottimeNs": 3456789000000,
    }
    # 全部不可用
    assert parse_device_clock_output("", host_epoch_ms=None) is None


def test_frame_epoch_ms_boottime_conversion() -> None:
    anchor = {"epochMs": 1757300000123, "boottimeNs": 1_000_000_000}
    assert frame_epoch_ms({"ts_boottime_ns": 2_500_000_000}, anchor) == 1757300001623
    # 无锚点 / 字段缺失 → None
    assert frame_epoch_ms({"ts_boottime_ns": 1}, None) is None
    assert frame_epoch_ms({}, anchor) is None


# --- derive_clock_anchors：manifest clockAnchors 结构 ---

def test_derive_clock_anchors_combines_t0_video_markers() -> None:
    logcat_text = "\n".join(
        [
            "1757300000.000 I/System: boot",
            "1757300005.250 D/PerfevalAgent: PERFEVAL_EVENT {\"type\": \"metric\", \"name\": \"flow.step.login\", \"value\": 1}",
        ]
    )
    anchors = derive_clock_anchors(
        logcat_text=logcat_text,
        video_anchors={
            "startEpochMs": 1757300001100,
            "firstFrameEpochMs": 1757300002100,
            "method": "videoStart+1000ms encoding delay (SCREEN_RECORD_SYNC §2)",
            "timeLimitS": 42,
            "segments": 1,
            "clockSource": "device_date",
        },
    )
    assert anchors is not None
    assert anchors["t0EpochMs"] == 1757300000000
    assert anchors["t0Source"] == "logcat.epoch.first_line"
    # 视频偏移 = 首帧 epoch - t0（报告跳转 video.currentTime 的基准）
    assert anchors["video"]["offsetMs"] == 2100
    assert anchors["video"]["firstFrameEpochMs"] == 1757300002100
    # marker epoch 从 logcat 行内提取
    assert anchors["markers"] == [{"name": "flow.step.login", "epochMs": 1757300005250}]


def test_derive_clock_anchors_none_without_inputs() -> None:
    assert derive_clock_anchors(logcat_text=None, video_anchors=None) is None
    assert derive_clock_anchors(logcat_text="no epoch prefix", video_anchors=None) is None


def test_marker_events_from_timeline_sorts_and_filters() -> None:
    def _logcat_metric(name: str, epoch: int) -> dict:
        return {
            "type": "metric",
            "name": name,
            "source": {"platform": "android", "collector": "logcat", "evidenceLevel": "L1"},
            "data": {"tsEpochMs": epoch},
        }

    events = [
        _logcat_metric("b.step", 200),
        _logcat_metric("a.step", 100),
        {"type": "metric", "name": "no.epoch", "source": {"platform": "android", "collector": "logcat", "evidenceLevel": "L1"}, "data": {}},
        {"type": "diagnostic", "name": "ignored", "source": {"platform": "android", "collector": "logcat", "evidenceLevel": "L0"}, "data": {"tsEpochMs": 50}},
        # F-fix（R1-B2）：tp/framestats 逐帧 counter 事件不是 flow marker，必须排除
        *[
            {
                "type": "counter",
                "name": "frame.duration.detail.ms",
                "source": {"platform": "android", "collector": "perfetto.tp", "evidenceLevel": "L1"},
                "data": {"tsEpochMs": 1757300000000 + i, "frameIndex": i},
            }
            for i in range(40)
        ],
    ]
    assert marker_events_from_timeline(events) == [
        {"name": "a.step", "epochMs": 100},
        {"name": "b.step", "epochMs": 200},
    ]


# --- gfxinfo：frame.duration.detail.ms 时间语义升格（旧消费方不回归） ---

def _tp_frames() -> dict:
    return {
        "frames": [
            {"frame_index": i, "frame_duration_ms": 16.0, "jank": i >= 2, "flag_names": [], "ts_boottime_ns": 1_000_000_000 + i * 16_000_000}
            for i in range(5)
        ]
    }


def test_build_frame_detail_events_with_boottime_anchor_upgrades_to_epoch() -> None:
    anchor = {"epochMs": 1757300000000, "boottimeNs": 1_000_000_000}
    events = build_frame_detail_events(
        _tp_frames(),
        run_id="run-tp",
        collector="perfetto.tp",
        fps_source="tp",
        boottime_anchor=anchor,
    )
    first = events[0]["data"]
    assert first["clockDomain"] == CLOCK_DOMAIN_EPOCH
    assert first["tsEpochMs"] == 1757300000000
    assert first["tsBoottimeNs"] == 1_000_000_000
    assert events[2]["data"]["tsEpochMs"] == 1757300000032
    # 旧字段不回归：tsMonotonicNs 相对首帧、value/jank/frameIndex/fpsSource 原样
    assert events[0]["tsMonotonicNs"] == 0
    assert events[2]["tsMonotonicNs"] == 32_000_000
    assert events[2]["data"]["value"] == 16.0
    assert events[2]["data"]["jank"] is True


def test_build_frame_detail_events_without_anchor_marks_frame_relative() -> None:
    events = build_frame_detail_events(
        _tp_frames(),
        run_id="run-rel",
        collector="dumpsys.gfxinfo.framestats",
        fps_source="framestats",
    )
    for event in events:
        assert event["data"]["clockDomain"] == CLOCK_DOMAIN_FRAME_RELATIVE
        assert "tsEpochMs" not in event["data"]
        assert "tsBoottimeNs" not in event["data"]


# --- engine：jankSegments 附段起止 epoch（仅当帧事件可对齐时） ---

def _frame_events(*, with_epoch: bool) -> list[dict]:
    events = []
    for index in range(8):
        data = {
            "value": 40.0 if 2 <= index <= 5 else 16.0,
            "unit": "ms",
            "jank": 2 <= index <= 5,
            "frameIndex": index,
            "fpsSource": "tp",
        }
        if with_epoch:
            data["tsEpochMs"] = 1757300000000 + index * 16
        events.append(
            {
                "schemaVersion": "perfeval.timeline.v1",
                "seq": index + 1,
                "runId": "run",
                "eventId": f"run-e{index}",
                "type": "counter",
                "name": "frame.duration.detail.ms",
                "tsMonotonicNs": index * 16_000_000,
                "source": {"platform": "android", "collector": "perfetto.tp", "evidenceLevel": "L1"},
                "data": data,
            }
        )
    return events


def test_jank_segments_carry_epoch_times_when_aligned() -> None:
    from perfeval.core.summary.engine import _jank_segments

    segments = _jank_segments(
        list(range(8)),
        [False, False, True, True, True, True, False, False],
        epoch_times=[1757300000000 + i * 16 for i in range(8)],
    )
    assert segments == [
        {
            "startIndex": 2,
            "endIndex": 5,
            "frameCount": 4,
            "startTsEpochMs": 1757300000032,
            "endTsEpochMs": 1757300000080,
        }
    ]


def test_jank_segments_without_epoch_keep_legacy_shape() -> None:
    from perfeval.core.summary.engine import _jank_segments

    segments = _jank_segments(
        [4, 5, 6],
        [True, True, True],
        epoch_times=[None, None, None],
    )
    assert segments == [{"startIndex": 4, "endIndex": 6, "frameCount": 3}]


# --- runner 集成：collect 产出 clock_anchors ---

def test_android_run_executor_collects_clock_anchors(tmp_path) -> None:
    logcat_text = "\n".join(
        [
            "1757300000.000 I/ActivityTaskManager: display",
            "1757300003.000 D/PerfevalAgent: PERFEVAL_EVENT {\"type\": \"metric\", \"name\": \"flow.step.login\", \"value\": 1}",
            "",
        ]
    )

    def fake_runner(command, timeout):
        joined = " ".join(command)
        # collect() 开头会清理旧 artifact，pftrace 需随命令持续补写
        pftrace = tmp_path / "run" / "artifacts" / "android" / "perfetto.pftrace"
        pftrace.parent.mkdir(parents=True, exist_ok=True)
        pftrace.write_bytes(b"fake-pftrace")
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "date +%s%3N" in joined:
            return _completed("1757300001100\n100.500 100.500\n")
        if "nohup screenrecord" in joined:
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            return _completed("")
        if "pull" in joined:
            Path(command[-1]).write_bytes(b"fake-mp4" + b"\x00" * 2048)  # >=1KB 有效帧数据
            return _completed("")
        if "logcat -d" in joined:
            return _completed(logcat_text)
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-align",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        screen_record_config={"enabled": True},
    )

    anchors = result.clock_anchors
    assert anchors is not None
    assert anchors["t0EpochMs"] == 1757300000000
    video = anchors["video"]
    assert video["startEpochMs"] == 1757300001100
    # 首帧 = 启动 + 1s 编码延迟；偏移 = 首帧 - t0
    assert video["firstFrameEpochMs"] == 1757300002100
    assert video["offsetMs"] == 2100
    assert video["clockSource"] == "device_date"
    assert anchors["markers"] == [{"name": "flow.step.login", "epochMs": 1757300003000}]


def test_android_run_executor_without_screen_record_has_no_clock_anchors(tmp_path) -> None:
    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "logcat -d" in joined:
            return _completed("1757300000.000 I/System: ready\n")
        return _completed("")

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-noalign",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
    )
    # 未启用录屏：t0（logcat epoch）仍可用，但无 video/video 偏移锚点
    anchors = result.clock_anchors
    assert anchors is not None
    assert anchors["t0EpochMs"] == 1757300000000
    assert "video" not in anchors


# --- CLI：clockAnchors 落 manifest context 与 summary ---

def test_run_cli_writes_clock_anchors_to_manifest_and_summary(tmp_path, monkeypatch) -> None:
    scenario = tmp_path / "scenario-align.yaml"
    scenario.write_text(
        "\n".join(
            [
                "schemaVersion: perfeval.scenario.v1",
                "scenarioId: cm021_align",
                "description: clock anchors passthrough",
                "apps:",
                "  - id: target",
                "    platform: android",
                "    packageName: com.example.app",
                "run:",
                "  repeats: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    anchors = {
        "t0EpochMs": 1757300000000,
        "t0Source": "logcat.epoch.first_line",
        "clockDomain": "device_epoch",
        "video": {
            "startEpochMs": 1757300001100,
            "firstFrameEpochMs": 1757300002100,
            "offsetMs": 2100,
        },
        "markers": [{"name": "flow.step.login", "epochMs": 1757300003000}],
    }
    monkeypatch.setattr(
        run_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )

    def fake_collect(**kwargs):
        return AndroidCollectionResult(
            run_id=str(kwargs["run_id"]),
            artifact_dir=Path(kwargs["run_dir"]) / "artifacts" / "android",
            events=[],
            artifacts=[],
            commands=(),
            diagnostics=(),
            clock_anchors=dict(anchors),
        )

    monkeypatch.setattr(run_command, "collect_android_run", fake_collect)

    run_root = tmp_path / "runs"
    code = main(
        [
            "run",
            str(scenario),
            "--repeat",
            "1",
            "--out",
            str(run_root),
            "--android-device-run",
            "--package-name",
            "com.example.app",
        ]
    )
    assert code == 0

    manifest = json.loads((run_root / "cm021_align" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["context"]["clockAnchors"] == anchors
    # schema 可解析（向后兼容：context 可选）
    Manifest.from_dict(manifest)

    summary = json.loads((run_root / "cm021_align" / "summary.json").read_text(encoding="utf-8"))
    assert summary["clockAnchors"] == anchors

    repeat_manifest = json.loads(
        (run_root / "cm021_align" / "repeats" / "r001" / "manifest.json").read_text(encoding="utf-8")
    )
    assert repeat_manifest["context"]["clockAnchors"] == anchors


def test_manifest_roundtrip_without_context() -> None:
    payload = {
        "schemaVersion": "perfeval.manifest.v1",
        "runId": "run-legacy",
        "createdAtUtc": "2026-09-08T00:00:00Z",
        "mode": "android",
        "scenario": {"scenarioId": "s", "description": "d"},
        "app": {"platform": "android", "appId": "a"},
        "device": {"platform": "android", "deviceId": "d", "serialOrUdid": "d"},
        "environment": {"hostOs": "Darwin", "hostArch": "arm64", "toolVersion": "0"},
        "run": {
            "repeatIndex": 0,
            "warmupIndex": 0,
            "startedAtUtc": "2026-09-08T00:00:00Z",
            "status": "completed",
            "evidenceLevel": ["L0"],
        },
    }
    manifest = Manifest.from_dict(payload)
    assert manifest.context is None
    assert "context" not in manifest.to_dict()


# --- F-fix（R1-B2）：runner 级集成——tp 帧事件（带 tsEpochMs）不得混入 anchors.markers ---

def test_android_run_executor_markers_exclude_tp_frame_events(tmp_path, monkeypatch) -> None:
    """录屏+tp 主路径：逐帧 counter 事件带 tsEpochMs，markers 只剩 flow 步骤。"""
    import perfeval.collectors.android.collector as collector_mod
    from perfeval.collectors.android.perfetto_trace import FrameRecord, FrameTimelineResult

    logcat_text = "\n".join(
        [
            "1757300000.000 I/ActivityTaskManager: display",
            "1757300003.000 D/PerfevalAgent: PERFEVAL_EVENT {\"type\": \"metric\", \"name\": \"flow.step.login\", \"value\": 1}",
            "1757300006.000 D/PerfevalAgent: PERFEVAL_EVENT {\"type\": \"metric\", \"name\": \"flow.step.scroll\", \"value\": 2}",
            "",
        ]
    )

    def fake_runner(command, timeout):
        joined = " ".join(command)
        # collect() 开头会清理旧 artifact，pftrace 需随命令持续补写
        pftrace = tmp_path / "run" / "artifacts" / "android" / "perfetto.pftrace"
        pftrace.parent.mkdir(parents=True, exist_ok=True)
        pftrace.write_bytes(b"fake-pftrace")
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "date +%s%3N" in joined:
            return _completed("1757300001100\n100.500 100.500\n")
        if "nohup screenrecord" in joined:
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            return _completed("")
        if "pull" in joined:
            Path(command[-1]).write_bytes(b"fake-mp4" + b"\x00" * 2048)  # >=1KB 有效帧数据
            return _completed("")
        if "logcat -d" in joined:
            return _completed(logcat_text)
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    # 假 tp：200 帧、帧事件带 ts_boottime_ns（配合 screenrecord boottime 锚点 → tsEpochMs）
    tp_frames = tuple(
        FrameRecord(
            frame_index=i,
            ts_boottime_ns=100_000_000 + i * 16_000_000,
            ts_epoch_ms=None,
            duration_ms=16.0,
            jank=False,
            process_name="com.perfeval.fixture",
        )
        for i in range(200)
    )
    monkeypatch.setattr(
        collector_mod,
        "query_frame_timeline",
        lambda trace_path, **kwargs: FrameTimelineResult(
            frames=tp_frames,
            vsync_period_ms=16.0,
            epoch_offset_ns=None,
            available=True,
        ),
    )

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-b2",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        screen_record_config={"enabled": True},
    )

    # tp 帧事件确实带 tsEpochMs（前置条件成立，修复才有意义）
    frame_events = [e for e in result.events if e["name"] == "frame.duration.detail.ms"]
    assert len(frame_events) == 200
    assert all("tsEpochMs" in e["data"] for e in frame_events)

    anchors = result.clock_anchors
    assert anchors is not None
    # markers == flow 步骤数（2），不含任何帧事件
    assert [m["name"] for m in anchors["markers"]] == ["flow.step.login", "flow.step.scroll"]


def test_android_run_executor_rollover_stop_failure_diagnosed(tmp_path) -> None:
    """F-fix（R1-N7）：rollover 收尾 pkill 异常返回码（>1）不留静默。"""
    from perfeval.collectors.android.runner import _ScreenRecordSession

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined:
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            return _Completed(stdout="", returncode=2, stderr="pkill: permission denied")
        return _completed("")

    adb = AdbClient("/adb")
    plan = build_screen_record_plan(
        adb=adb,
        run_id="run-stopfail",
        config={"enabled": True},
        interaction_commands=[InteractionCommand(name="idle", wait_s=400.0)],
        post_launch_wait_s=0.0,
        artifact_dir=str(tmp_path),
    )
    session = _ScreenRecordSession(_screen_record_executor(fake_runner), plan, finalize_wait_s=0.0)
    diagnostics: list[str] = []
    session.start_first(diagnostics)
    session.segment_started_mono -= plan.time_limit_s + 1
    session.maybe_rollover(diagnostics)

    assert any(item.startswith("screen.record.stop_failed:rollover:2:") for item in diagnostics)


# --- F-fix（R1-N4）：离线 --android-artifacts 从源 run manifest 读回 clockAnchors ---

def test_run_cli_offline_reads_back_clock_anchors(tmp_path) -> None:
    scenario = tmp_path / "scenario-offline.yaml"
    scenario.write_text(
        "\n".join(
            [
                "schemaVersion: perfeval.scenario.v1",
                "scenarioId: cm021_offline_anchor",
                "description: offline anchors readback",
                "apps:",
                "  - id: target",
                "    platform: android",
                "    packageName: com.example.app",
                "run:",
                "  repeats: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    anchors = {
        "t0EpochMs": 1757300000000,
        "t0Source": "logcat.epoch.first_line",
        "clockDomain": "device_epoch",
        "video": {
            "startEpochMs": 1757300001100,
            "firstFrameEpochMs": 1757300002100,
            "offsetMs": 2100,
            "method": "videoStart+1000ms encoding delay (SCREEN_RECORD_SYNC §2)",
        },
        "markers": [{"name": "flow.step.login", "epochMs": 1757300003000}],
    }
    # 源 run 目录结构：<src>/manifest.json + <src>/artifacts/android/*
    src = tmp_path / "src-run"
    artifact_dir = src / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (src / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.manifest.v1",
                "runId": "src-run",
                "context": {"clockAnchors": anchors},
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "screen.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)  # >=1KB
    (artifact_dir / "logcat.txt").write_text(
        "1757300000.000 I/System: ready\n",
        encoding="utf-8",
    )

    out = tmp_path / "runs"
    code = main(
        [
            "run",
            str(scenario),
            "--out",
            str(out),
            "--android-artifacts",
            str(artifact_dir),
        ]
    )
    assert code == 0

    run_dir = out / "cm021_offline_anchor"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["clockAnchors"] == anchors
    # 新 manifest 同步携带（泛化写回逻辑）
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["context"]["clockAnchors"] == anchors

    # 离线报告可精确跳转（视频 + 锚点齐备）
    report_html = (run_dir / "report" / "report.html").read_text(encoding="utf-8")
    assert "录屏回放" in report_html
    assert 'data-video-jump="0.900"' in report_html


# --- B1（TASK-023-fix R1）：samplers 锚点（SamplingScheduler.start() 同刻设备时钟） ---


def test_derive_clock_anchors_carries_samplers_anchor() -> None:
    logcat_text = "1757300000.000 I/System: boot"
    anchors = derive_clock_anchors(
        logcat_text=logcat_text,
        samplers_anchor={"source": "device_date", "epochMs": 1757300002000, "boottimeNs": 812345678000000},
    )
    assert anchors is not None
    assert anchors["t0EpochMs"] == 1757300000000
    # 采样序列 ts 原点（scheduler.start()）↔ epoch 换算锚点随 manifest 落盘；
    # producer 键 epochMs 统一映射为 startEpochMs（B1-fix2 防断链），附误差口径
    from perfeval.collectors.android.timebase import SAMPLERS_ANCHOR_METHOD

    assert anchors["samplers"] == {
        "startEpochMs": 1757300002000,
        "source": "device_date",
        "boottimeNs": 812345678000000,
        "method": SAMPLERS_ANCHOR_METHOD,
    }


def test_derive_clock_anchors_samplers_only_is_not_none() -> None:
    """logcat 解析失败/无视频时，仅 samplers 锚点也应落盘（报告竖线数据源）。"""
    anchors = derive_clock_anchors(
        logcat_text=None,
        samplers_anchor={"source": "host_epoch_approx", "epochMs": 1757300002000},
    )
    assert anchors["samplers"]["startEpochMs"] == 1757300002000
    assert anchors["samplers"]["source"] == "host_epoch_approx"
    assert "method" in anchors["samplers"]


def test_parse_device_clock_output_feeds_samplers_anchor() -> None:
    anchor = parse_device_clock_output(
        "1757300002000\n812345.678 12345.6\n", host_epoch_ms=1757300000999
    )
    assert anchor == {
        "source": "device_date",
        "epochMs": 1757300002000,
        "boottimeNs": 812345678000000,
    }


# --- smoke-fix：离线 --android-artifacts 空录屏与在线路径一致降级 ---

def test_run_cli_offline_empty_screen_recording_degrades(tmp_path) -> None:
    """离线模式 0 字节 screen.mp4：不拷贝、记 *.unavailable diagnostic、
    报告不渲染 video 标签（即便源 manifest 残留 video 锚点——防御组合）。"""
    scenario = tmp_path / "scenario-empty-sr.yaml"
    scenario.write_text(
        "\n".join(
            [
                "schemaVersion: perfeval.scenario.v1",
                "scenarioId: smokefix_empty_sr",
                "description: offline empty screen recording",
                "apps:",
                "  - id: target",
                "    platform: android",
                "    packageName: com.example.app",
                "run:",
                "  repeats: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    anchors = {
        "t0EpochMs": 1757300000000,
        "t0Source": "logcat.epoch.first_line",
        "clockDomain": "device_epoch",
        # 模拟锚点残留的最坏情况：video 锚点在，但录屏实际为空
        "video": {
            "startEpochMs": 1757300001100,
            "firstFrameEpochMs": 1757300002100,
            "offsetMs": 2100,
        },
        "markers": [],
    }
    src = tmp_path / "src-run"
    artifact_dir = src / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (src / "manifest.json").write_text(
        json.dumps({"schemaVersion": "perfeval.manifest.v1", "runId": "src", "context": {"clockAnchors": anchors}}),
        encoding="utf-8",
    )
    (artifact_dir / "screen.mp4").write_bytes(b"")  # 0 字节空录屏
    (artifact_dir / "logcat.txt").write_text("1757300000.000 I/System: ready\n", encoding="utf-8")

    out = tmp_path / "runs"
    code = main(["run", str(scenario), "--out", str(out), "--android-artifacts", str(artifact_dir)])
    assert code == 0

    run_dir = out / "smokefix_empty_sr"
    # 1) diagnostic：与在线路径同一 *.unavailable 语义
    diagnostics = [
        json.loads(line)
        for line in (run_dir / "diagnostics.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        str(item.get("name")) == "screen.record.unavailable"
        and "empty recording" in str(item.get("data", {}).get("message", ""))
        for item in diagnostics
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    # 2) 空录屏不入 artifact 索引（在线/离线一致）
    assert not [a for a in summary.get("artifacts", []) if str(a.get("path", "")).endswith(".mp4")]
    # 3) 读回的 video 锚点仍在 summary（防御第二层在报告侧生效）
    assert summary["clockAnchors"]["video"]["offsetMs"] == 2100
    # 4) 报告不渲染可播放 video 标签（artifact 缺失过滤），保留降级文案
    report_html = (run_dir / "report" / "report.html").read_text(encoding="utf-8")
    assert "<video" not in report_html
    assert "screen.record.unavailable:empty recording" in report_html


def test_index_skips_undersized_screen_recording(tmp_path) -> None:
    """index_android_artifacts 兜底：<1KB 的 screen*.mp4 不入索引，>=1KB 正常。"""
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "screen.mp4").write_bytes(b"")  # 0 字节
    (artifact_dir / "screen-seg002.mp4").write_bytes(b"x" * 512)  # <1KB
    (artifact_dir / "screen-seg003.mp4").write_bytes(b"x" * 2048)  # >=1KB

    from perfeval.collectors.android import index_android_artifacts

    paths = [a["path"] for a in index_android_artifacts(artifact_dir)]
    mp4_paths = [p for p in paths if p.endswith(".mp4")]
    assert mp4_paths == [str(artifact_dir / "screen-seg003.mp4")]
