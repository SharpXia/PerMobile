"""R52 打点通道（logcat ``PERFEVAL_EVENT`` → 会话 markers/事件流；契约 §15.5）。

- 解析分派逐字复用 perfeval ``parse_logcat``（perfeval/collectors/android/
  logcat.py:57-71）——本文件只测会话侧分派语义与生命周期接线，不改解析器契约；
- 真机 adb I/O 无设备：行源以注入迭代器替身（同 m4_fixtures「只替身传输层，
  全部逻辑走生产代码」口径）。
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services import custom_events, log_stream
from conftest import make_session
from m4_fixtures import make_real_client, make_recording_session, wait_until

# ---- 会话替身（handle_line 纯逻辑直测；与 Session 同形的最小面）----


class _StubBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, type_: str, **payload: Any) -> dict[str, Any]:
        envelope = {"type": type_, **payload}
        self.events.append(envelope)
        return envelope


class _StubSession:
    id = "sess-test"

    def __init__(self) -> None:
        self.bus = _StubBus()
        self.markers: list[dict[str, Any]] = []
        self.code_markers_list: list[dict[str, Any]] = []
        self.logcat_first_line: str | None = None
        self.stream_ended = False
        self._counter = itertools.count(1)

    def add_marker(self, kind: str, element: dict[str, Any]) -> dict[str, Any]:
        marker = {"id": f"mkr-{next(self._counter):02d}", "kind": kind, **element}
        self.markers.append(marker)
        return marker

    def add_code_marker(self, entry: dict[str, Any]) -> None:
        self.code_markers_list.append(entry)

    def logcat_stream_ended(self) -> None:
        self.stream_ended = True

    def log_stream_ended(self) -> None:
        self.stream_ended = True


def _line(event_json: str, epoch: str = "1757300005.000") -> str:
    return f"{epoch} D/PerfevalAgent: PERFEVAL_EVENT {event_json}"


# ---- 实机验证修复（2026-11-03）：自终止（close() 触发 terminate）不出异常诊断 ----


def test_line_source_self_terminated_no_diagnostic() -> None:
    """会话收口经生成器 close() → 我们 terminate 的 rc=-15 是正常终止。

    此前实现把自终止也当异常广播 custom.channel.unavailable（真机 record/stop
    实测复现误报）；修复后仅进程自行死亡（设备断开等）才广播。
    """
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import patch

    session = _StubSession()
    state: dict[str, Any] = {}
    process = SimpleNamespace(
        stdout=iter(["1757300005.000 D/PerfevalAgent: PERFEVAL_EVENT {}\n"]),
        stderr=None,
        poll=lambda: state.get("rc"),  # 存活 → close() 走 terminate 分支
        returncode=None,
        terminate=lambda: state.update(rc=-15),
        wait=lambda timeout=2.0: None,
        kill=lambda: None,
    )
    with patch("subprocess.Popen", return_value=process) as popen:
        source_factory = custom_events.adb_logcat_line_source(session, "S1")
        lines = source_factory()
        assert lines is not None
        list(lines)  # 消费完唯一一行 → for 循环结束 → finally 自终止
    # 实机验证修复（2026-11-03）：流式须带 -T 1（否则首 epoch 行是缓冲区最旧
    # 条目，t0 锚点早数小时——真机实测复现）
    cmd = popen.call_args[0][0]
    assert cmd[3:6] == ["logcat", "-T", "1"], f"流式 logcat 缺 -T 1: {cmd}"
    assert state.get("rc") == -15  # terminate() 已执行（SIGTERM 语义）
    unavailable = [e for e in session.bus.events if e.get("code") == "custom.channel.unavailable"]
    assert unavailable == []  # 自终止不出诊断（修复点）

    # 对照：进程自行死亡（rc=1，设备断开）→ 出诊断
    session2 = _StubSession()
    process2 = SimpleNamespace(
        stdout=iter([]),
        stderr=SimpleNamespace(read=lambda: "adb: device offline", close=lambda: None),
        poll=lambda: 1,  # 已死亡 → 不走 terminate 分支
        returncode=1,
        terminate=lambda: None,
        wait=lambda timeout=2.0: None,
        kill=lambda: None,
    )
    with patch("subprocess.Popen", return_value=process2):
        lines2 = custom_events.adb_logcat_line_source(session2, "S1")()
        list(lines2)
    unavailable2 = [e for e in session2.bus.events if e.get("code") == "custom.channel.unavailable"]
    assert len(unavailable2) == 1 and "code=1" in unavailable2[0]["message"]


@pytest.mark.parametrize("log_format", ["epoch", "threadtime"])
def test_stop_interrupts_silent_adb_child(monkeypatch, tmp_path, log_format: str) -> None:
    session = _StubSession()
    adb_path = tmp_path / "应用目录 with spaces" / "adb.exe"
    monkeypatch.setenv("PERFEVAL_ADB_PATH", str(adb_path))
    ready = threading.Event()
    popen = subprocess.Popen
    processes = []

    def fake_adb(command, **kwargs):
        assert command == [str(adb_path), "-s", "S1", "logcat", "-T", "1", "-v", log_format]
        process = popen(
            [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_adb)
    if log_format == "epoch":
        source = custom_events.adb_logcat_line_source(session, "S1")
        channel = custom_events.LogcatEventChannel(session, source)
    else:
        source = log_stream.adb_logcat_time_source("S1")
        channel = log_stream.LogStreamChannel(session, source)
    monkeypatch.setattr(channel, "handle_line", lambda _line: ready.set())
    try:
        channel.start()
        assert ready.wait(5)
        channel.stop()
        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert session.stream_ended
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        channel.stop()


# ---- 行解析分派（纯逻辑）----


def test_handle_line_dispatches_label_note_metric() -> None:
    session = _StubSession()
    channel = custom_events.LogcatEventChannel(session, lambda: iter(()))

    channel.handle_line(_line('{"type": "label", "name": "战斗场景", "color": "#FF5252"}'))
    channel.handle_line(_line('{"type": "note", "text": "此处加载卡顿明显"}', "1757300006.500"))
    channel.handle_line(
        _line('{"type": "metric", "name": "flow.step.login", "value": 1, "unit": "count"}',
              "1757300007.250")
    )

    # label/note → §15 标记管线（tsMs = 行首 epoch 时戳回填，logcat.py:66-68 契约）
    assert [m["kind"] for m in session.markers] == ["label", "note"]
    assert session.markers[0] == {
        "id": "mkr-01", "kind": "label", "tsMs": 1757300005000,
        "name": "战斗场景", "color": "#FF5252",
    }
    assert session.markers[1] == {
        "id": "mkr-02", "kind": "note", "tsMs": 1757300006500, "text": "此处加载卡顿明显",
    }
    # 两类标记均经 SSE type=marker 广播（契约 §15.2）
    marker_events = [e for e in session.bus.events if e["type"] == "marker"]
    assert [e["marker"]["kind"] for e in marker_events] == ["label", "note"]
    assert all(e["action"] == "add" for e in marker_events)

    # metric → SSE 新 type=customEvent（§8.1 兼容）+ clockAnchors.markers 代码打点
    custom_events_emitted = [e for e in session.bus.events if e["type"] == "customEvent"]
    assert custom_events_emitted == [
        {"type": "customEvent", "name": "flow.step.login", "value": 1,
         "unit": "count", "tsMs": 1757300007250}
    ]
    assert session.code_markers_list == [
        {"name": "flow.step.login", "epochMs": 1757300007250}
    ]


def test_handle_line_event_ts_epoch_ms_takes_precedence() -> None:
    session = _StubSession()
    channel = custom_events.LogcatEventChannel(session, lambda: iter(()))
    channel.handle_line(
        _line(
            '{"type": "metric", "name": "flow.step.scroll", "value": 2, "tsEpochMs": 1757300002999}'
        )
    )
    # 事件自带 tsEpochMs 时以事件为准（setdefault 语义，logcat.py:66-68）
    assert session.code_markers_list == [{"name": "flow.step.scroll", "epochMs": 1757300002999}]


def test_handle_line_skips_invalid_events_without_fabricating_ts() -> None:
    session = _StubSession()
    channel = custom_events.LogcatEventChannel(session, lambda: iter(()))

    # 无 PERFEVAL_EVENT 的普通行
    channel.handle_line("1757300005.000 D/PerfevalAgent: boot done")
    # 行首无 epoch 且事件未自带 tsEpochMs → 缺锚跳过（不伪造时戳）
    channel.handle_line("D/PerfevalAgent: PERFEVAL_EVENT {\"type\": \"label\", \"name\": \"L\"}")
    # 事件自带非法 tsEpochMs（布尔/字符串/负数）
    channel.handle_line(_line('{"type": "label", "name": "L", "tsEpochMs": true}'))
    channel.handle_line(_line('{"type": "label", "name": "L", "tsEpochMs": "1757300005"}'))
    channel.handle_line(_line('{"type": "label", "name": "L", "tsEpochMs": -1}'))
    # label 缺 name / note 缺 text / metric 缺 name / value 非有限数值 / 未知 type
    channel.handle_line(_line('{"type": "label"}'))
    channel.handle_line(_line('{"type": "note"}'))
    channel.handle_line(_line('{"type": "metric", "value": 1}'))
    channel.handle_line(_line('{"type": "metric", "name": "m", "value": "1"}'))
    channel.handle_line(_line('{"type": "metric", "name": "m", "value": null}'))
    channel.handle_line(_line('{"type": "flow", "name": "unknown"}'))

    assert session.markers == []
    assert session.code_markers_list == []
    assert session.bus.events == []


def test_handle_line_captures_first_epoch_line_for_t0() -> None:
    session = _StubSession()
    channel = custom_events.LogcatEventChannel(session, lambda: iter(()))
    # 首个带 epoch 时戳的行（不限 PERFEVAL_EVENT 行，SCREEN_RECORD_SYNC §2 t0 语义）
    channel.handle_line("1757300001.000 D/PerfevalAgent: boot done")
    assert session.logcat_first_line == "1757300001.000 D/PerfevalAgent: boot done"
    channel.handle_line(
        _line('{"type": "metric", "name": "flow.step.login", "value": 1}', "1757300005.000")
    )
    # 后续行不覆盖（首个即 t0）
    assert session.logcat_first_line == "1757300001.000 D/PerfevalAgent: boot done"
    # 无 epoch 前缀的行不触发捕获
    session2 = _StubSession()
    channel2 = custom_events.LogcatEventChannel(session2, lambda: iter(()))
    channel2.handle_line("D/PerfevalAgent: no epoch here")
    assert session2.logcat_first_line is None
    channel2.handle_line(
        _line('{"type": "metric", "name": "m", "value": 2}', "1757300006.000")
    )
    # PERFEVAL_EVENT 行裁掉 JSON 载荷（防 derive_clock_anchors 复析重收 markers）
    assert session2.logcat_first_line == "1757300006.000 D/PerfevalAgent: "


# ---- 生命周期（线程驱动；行源以替身注入）----


def test_channel_thread_consumes_lines_and_stop_closes_source() -> None:
    session = _StubSession()
    closed: list[bool] = []

    def source() -> Any:
        def lines() -> Any:
            try:
                yield _line('{"type": "label", "name": "线程段"}')
                while True:  # 模拟长流：stop() 中断后 close 触发 finally
                    yield "1757300005.000 D/PerfevalAgent: idle"
            finally:
                closed.append(True)

        return lines()

    channel = custom_events.LogcatEventChannel(session, source)
    channel.start()
    wait_until(lambda: len(session.markers) == 1)
    channel.stop()
    assert closed == [True]
    assert session.stream_ended is True


def test_channel_none_source_publishes_unavailable_diagnostic() -> None:
    session = _StubSession()
    channel = custom_events.LogcatEventChannel(session, lambda: None)
    channel.start()
    wait_until(lambda: any(e["type"] == "diagnostic" for e in session.bus.events))
    channel.stop()
    diagnostic = next(e for e in session.bus.events if e["type"] == "diagnostic")
    assert diagnostic["code"] == custom_events.CHANNEL_UNAVAILABLE_CODE
    assert diagnostic["severity"] == "warn"


def test_channel_bad_line_does_not_kill_stream(monkeypatch: Any) -> None:
    session = _StubSession()
    real_parse = custom_events.parse_logcat
    calls = {"n": 0}

    def flaky_parse(line: str, **kwargs: Any) -> Any:
        parsed = real_parse(line, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一行分派故障")
        return parsed

    monkeypatch.setattr(custom_events, "parse_logcat", flaky_parse)
    channel = custom_events.LogcatEventChannel(
        session,
        lambda: iter(
            [
                _line('{"type": "label", "name": "首行"}'),
                _line('{"type": "label", "name": "后续行"}'),
            ]
        ),
    )
    channel.start()
    wait_until(lambda: len(session.markers) == 1)
    channel.stop()
    assert [m["name"] for m in session.markers] == ["后续行"]


# ---- real 会话全链接线（record/start 起通道 → markers/事件流 → save 落盘）----


def test_real_session_channel_end_to_end(tmp_path: Any, monkeypatch: Any) -> None:
    lines = iter(
        [
            "1757300001.000 D/PerfevalAgent: boot done",
            _line('{"type": "label", "name": "战斗场景", "color": "#FF5252"}', "1757300002.000"),
            _line('{"type": "note", "text": "此处加载卡顿明显"}', "1757300003.000"),
            _line('{"type": "metric", "name": "flow.step.login", "value": 1}', "1757300003.500"),
        ]
    )
    monkeypatch.setattr(
        custom_events, "adb_logcat_line_source", lambda session, serial: lambda: lines
    )

    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=None) as client:
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]

        assert client.post(f"/api/sessions/{sid}/record/start").status_code == 200
        # 通道线程分派：2 个标记入会话内存（§15 标记管线）
        manager_session = client.app.state.sessions.get(sid)
        wait_until(lambda: len(manager_session.markers()) == 2)

        client.post(f"/api/sessions/{sid}/record/stop")
        resp = client.post(f"/api/sessions/{sid}/save", json={})
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["runId"]
        run_dir = client.runs_dir / run_id.removeprefix("run-")

        # ---- markers 侧车：通道 label/note 与 UI 打标同管线落盘（§15.1）----
        body = client.get(f"/api/runs/{run_id}/markers").json()
        assert [(m["kind"], m["name"] if "name" in m else m.get("text"))
                for m in body["markers"]] == [
            ("label", "战斗场景"),
            ("note", "此处加载卡顿明显"),
        ]
        assert (run_dir / "markers.ndjson").is_file()

        # ---- clockAnchors（§15.5）：t0 ← 通道首行 epoch；markers ← type=metric 打点 ----
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        anchors = manifest["context"]["clockAnchors"]
        assert anchors["t0EpochMs"] == 1757300001000
        assert anchors["t0Source"] == "logcat.epoch.first_line"
        assert anchors["markers"] == [
            {"name": "flow.step.login", "epochMs": 1757300003500}
        ]

        # ---- SSE 事件流：marker ×2 + customEvent ×1（事件缓冲重放取证）----
        session_obj = client.app.state.sessions.get(sid)
        envelopes = session_obj.bus.replay(0)[0]
        assert [e["type"] for e in envelopes if e["type"] in ("marker", "customEvent")] == [
            "marker", "marker", "customEvent",
        ]
        custom_event = next(e for e in envelopes if e["type"] == "customEvent")
        assert custom_event["name"] == "flow.step.login"
        assert custom_event["tsMs"] == 1757300003500

        # ---- R32 数据面闭环：通道 run 直接可按 label 段出统计（§18 byLabelSegment）----
        # stub 采集层无 fps 通道（fixture 无真机），向已落盘 run 目录补 fps 点列
        #（statistics 事实源是 run 目录文件，与通道行为无关；段边界断言用）
        from perfeval.collectors.android.samplers.base import SampleRecord
        from perfeval.collectors.android.samples import SamplesWriter

        writer = SamplesWriter(run_dir / "samples.ndjson")
        try:
            writer.append(
                [
                    SampleRecord(metric_id="frame.fps.mean", value=60.0, ts_ms=1757300004000),
                    SampleRecord(metric_id="frame.fps.mean", value=55.0, ts_ms=1757300005000),
                ]
            )
        finally:
            writer.close()
        body = client.get(
            f"/api/runs/{run_id}/statistics", params={"byLabelSegment": "1"}
        ).json()
        assert [s["name"] for s in body["labelSegments"]] == ["战斗场景"]
        # 末段右端 = 数据结束（fps 末点 1757300005000）；段内统计 = fpsSeries 基线
        assert (body["labelSegments"][0]["fromMs"], body["labelSegments"][0]["toMs"]) == (
            1757300002000, 1757300005000,
        )
        assert body["labelSegments"][0]["statistics"]["fps"]["avg"] == 57.5


def test_real_session_channel_none_source_degrades_to_diagnostic(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        custom_events, "adb_logcat_line_source", lambda session, serial: lambda: None
    )
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=None) as client:
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]
        assert client.post(f"/api/sessions/{sid}/record/start").status_code == 200
        session_obj = client.app.state.sessions.get(sid)
        wait_until(
            lambda: any(
                e["type"] == "diagnostic" and e["code"] == "custom.channel.unavailable"
                for e in session_obj.bus.replay(0)[0]
            )
        )
        # 通道故障不影响 recording 状态机（降级为诊断，不阻断采集）
        assert session_obj.state == "recording"


def test_synthetic_session_never_starts_channel(client: TestClient) -> None:
    session = make_session(client)
    client.post(f"/api/sessions/{session['id']}/record/start")
    session_obj = client.app.state.sessions.get(session["id"])
    assert session_obj.custom_events_channel is None
