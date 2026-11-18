"""R19/R20/R33 会话选项（契约 §21.5/§21.6/§21.7/§21.8）：fpsWindow / processName /
logLevel 回显、校验、fpsWindow 标注与 real 诊断、SSE log 流（级别过滤 + 生命周期）。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_session  # noqa: E402


@pytest.fixture()
def fast_client(tmp_path: Path) -> TestClient:
    """synthetic client：日志流节拍调快（0.01s/行），避免测试受真实节拍限制。"""
    from app.config import Settings
    from app.main import create_app
    from app.providers.synthetic import SyntheticProvider

    settings = Settings(
        provider="synthetic",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
        synthetic_log_pace_s=0.01,
    )
    app = create_app(settings, provider=SyntheticProvider())
    with TestClient(app) as c:
        c.runs_dir = settings.runs_dir  # type: ignore[attr-defined]
        yield c


# ---- 契约 §21.8 golden：三选项一次齐显 ----


def test_create_session_m6_options_echo(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [
            {"metricId": "cpu.proc.normalized.pct", "intervalMs": 1000},
            {"metricId": "frame.fps.mean", "intervalMs": 500},
        ],
        "interactive": False,
        "launchOnCreate": False,
        "fpsWindow": {"type": "surface_view"},
        "processName": "com.example.synthetic.game:sub",
        "logLevel": "debug",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 契约 §21.8 golden 回显（§6 既有字段不变）
    assert body["fpsWindow"] == {"type": "surface_view"}
    assert body["processName"] == "com.example.synthetic.game:sub"
    assert body["logLevel"] == "debug"
    assert body["recordScreen"] is False
    assert body["metrics"] == [
        {"metricId": "cpu.proc.normalized.pct", "intervalMs": 1000},
        {"metricId": "frame.fps.mean", "intervalMs": 500},
    ]
    client.post(f"/api/sessions/{body['id']}/discard")


def test_m6_options_default_echo(client: TestClient) -> None:
    """缺省回显：fpsWindow={"type":"auto"}、processName/logLevel=null（§11.1 始终返回）。"""
    body = make_session(client)
    assert body["fpsWindow"] == {"type": "auto"}
    assert body["processName"] is None
    assert body["logLevel"] is None
    client.post(f"/api/sessions/{body['id']}/discard")


# ---- 校验（契约 §21.5/§21.6/§21.7 错误行）----


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"fpsWindow": {"type": "surface"}}, "fpsWindow.type"),
        ({"fpsWindow": "surface_view"}, "fpsWindow.type"),
        ({"processName": "com.example.synthetic.game:unknown"}, "processName"),
        ({"processName": 42}, "processName"),
        ({"logLevel": "fatal"}, "logLevel"),
        ({"logLevel": ""}, "logLevel"),
    ],
)
def test_m6_options_validation_400(client: TestClient, payload: dict, field: str) -> None:
    body = {
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "interactive": False,
        "launchOnCreate": False,
    }
    body.update(payload)
    resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 400, payload
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == field


def test_process_name_main_process_accepted(client: TestClient) -> None:
    """processName 为主进程名时等价缺省（契约 §21.6），正常创建。"""
    resp = client.post("/api/sessions", json={
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "interactive": False,
        "launchOnCreate": False,
        "processName": "com.example.synthetic.game",
    })
    assert resp.status_code == 201
    assert resp.json()["processName"] == "com.example.synthetic.game"
    client.post(f"/api/sessions/{resp.json()['id']}/discard")


def test_process_name_validated_per_device(client: TestClient) -> None:
    """processName 校验来源 = 该设备的 apps processes[]（002 的 :sub pid 段独立）。"""
    resp = client.post("/api/sessions", json={
        "deviceId": "synthetic-002",
        "packageName": "com.example.synthetic.game",
        "interactive": False,
        "launchOnCreate": False,
        "processName": "com.example.synthetic.game:sub",
    })
    assert resp.status_code == 201
    client.post(f"/api/sessions/{resp.json()['id']}/discard")


# ---- R19：synthetic fpsWindow 枚举演示（frame.fps.mean tags 标注）----


def _collect_events(client: TestClient, sid: str, min_events: int) -> list[dict]:
    events: list[dict] = []
    with client.stream(
        "GET", f"/api/sessions/{sid}/events", headers={"Last-Event-ID": "0"}
    ) as resp:
        for line in resp.iter_lines():
            if line is None:
                continue
            text = line.rstrip("\r")
            if text.startswith("data:"):
                env = json.loads(text.removeprefix("data:").strip())
                events.append(env)
                if len(events) >= min_events:
                    break
    return events


def test_fps_window_tag_on_synthetic_fps(client: TestClient) -> None:
    sid = make_session(
        client,
        metrics=[{"metricId": "frame.fps.mean", "intervalMs": 100}],
        fpsWindow={"type": "surface_view"},
    )["id"]
    try:
        events = _collect_events(client, sid, 10)
        fps_samples = [
            e for e in events
            if e["type"] == "sample" and e["metricId"] == "frame.fps.mean"
        ]
        assert fps_samples, "synthetic frame.fps.mean 应出点"
        assert all(s["tags"].get("fpsWindow") == "surface_view" for s in fps_samples)
    finally:
        client.post(f"/api/sessions/{sid}/discard")


def test_fps_window_auto_mapping_synthetic(client: TestClient) -> None:
    """auto 缺省策略：synthetic.game → surface_view、synthetic.app → activity。"""
    from app.services.session_manager import resolve_fps_window

    assert resolve_fps_window(
        "auto", "com.example.synthetic.game"
    ) == "surface_view"
    assert resolve_fps_window(
        "auto", "com.example.synthetic.app"
    ) == "activity"
    assert resolve_fps_window("activity", "com.example.synthetic.game") == "activity"


# ---- R33：synthetic log 流（级别过滤）----


def test_log_stream_level_filter_and_envelope(fast_client: TestClient) -> None:
    """logLevel=warn → 只出 warn/error；信封字段对齐契约 §21.7 SSE log 行。"""
    session = make_session(fast_client, metrics=[], logLevel="warn")
    sid = session["id"]
    assert session["logLevel"] == "warn"
    try:
        events = _collect_events(fast_client, sid, 30)
        logs = [e for e in events if e["type"] == "log"]
        assert logs, "logLevel 提供应展开实时日志流"
        for env in logs:
            # 契约 §21.7 golden 载荷键：level/source/line（信封 v/seq/ts/sessionId）
            assert env["v"] == 1
            assert env["sessionId"] == sid
            assert isinstance(env["seq"], int)
            assert isinstance(env["ts"], int)
            assert env["level"] in ("warn", "error")
            assert isinstance(env["source"], str) and env["source"]
            assert isinstance(env["line"], str) and env["line"]
        # 确定性脚本样本齐全（warn 与 error 均应出现）
        levels = {e["level"] for e in logs}
        assert levels == {"warn", "error"}
    finally:
        fast_client.post(f"/api/sessions/{sid}/discard")


def test_log_stream_absent_without_log_level(client: TestClient) -> None:
    """缺省（未提供 logLevel）不发 log 消息（[I§2.1] ☐Log 复选语义）。"""
    # 带快节奏指标驱动事件流（metrics=[] 时仅 hello/phase，SSE 集合无法推进）
    session = make_session(
        client, metrics=[{"metricId": "cpu.total.pct", "intervalMs": 50}]
    )
    sid = session["id"]
    assert session["logLevel"] is None
    try:
        events = _collect_events(client, sid, 12)
        assert events, "采样事件应正常流动"
        assert all(e["type"] != "log" for e in events)
    finally:
        client.post(f"/api/sessions/{sid}/discard")


def test_log_stream_lifecycle_ends_on_record_stop(fast_client: TestClient) -> None:
    """日志流与采集线程同生命周期：record/stop 终止（契约 §21.7）。"""
    session = make_session(fast_client, metrics=[], logLevel="info")
    sid = session["id"]
    manager = fast_client.app.state.sessions
    internal = manager._sessions[sid]
    assert internal.log_channel is not None
    fast_client.post(f"/api/sessions/{sid}/record/start")
    fast_client.post(f"/api/sessions/{sid}/record/stop")
    assert internal.log_channel is None  # 通道已收口
    fast_client.post(f"/api/sessions/{sid}/save", json={})


# ---- 纯逻辑：logcat -v time 行解析与级别过滤 ----


def test_parse_logcat_time_line_and_filter() -> None:
    from app.services.log_stream import level_passes, parse_logcat_time_line

    line = "11-01 15:04:05.123  1234  5678 W ActivityManager: Background start not allowed\n"
    parsed = parse_logcat_time_line(line)
    assert parsed == ("warn", "ActivityManager", "Background start not allowed")
    # 级别过滤：warn 及以上放行（warn/error），debug 不放行
    assert level_passes("warn", "warn") is True
    assert level_passes("error", "warn") is True
    assert level_passes("debug", "warn") is False
    assert level_passes("info", "verbose") is True
    # 多行续行（无优先级字母）不伪造级别
    assert parse_logcat_time_line("    at com.example.Main.run(Main.java:1)") is None


def test_parse_logcat_threadtime_real_device_lines() -> None:
    """实机验证修复（2026-11-03）：真机/模拟器（均 Android 16）threadtime 实测行。

    Android 16 的 `-v time` 输出 `L/TAG(PID):` 简要格式（与解析正则不匹配 →
    整流静默丢弃），已改 `-v threadtime`；以下为 ANDROID-TEST-01 / emulator-5554
    实测原文行（含带点/带 @ 的 tag）。
    """
    from app.services.log_stream import parse_logcat_time_line

    phone = (
        "11-03 08:38:05.851  2786  2814 D ActivityManager: "
        "sync unfroze 27959 com.samsung.cmh for 19\n"
    )
    assert parse_logcat_time_line(phone) == (
        "debug", "ActivityManager", "sync unfroze 27959 com.samsung.cmh for 19",
    )
    emu = (
        "11-03 08:38:06.394 E/android.hardware.audio@7.1-impl.ranchu(  526): "
        "pcmWrite:260 failure: -1\n"
    )
    # time 简要格式（-v time，已弃用）不再匹配 → None（不伪造级别）；threadtime
    # 同内容行匹配
    assert parse_logcat_time_line(emu) is None
    emu_threadtime = (
        "11-03 08:38:06.394   526   601 E android.hardware.audio@7.1-impl.ranchu: "
        "pcmWrite:260 failure: -1\n"
    )
    assert parse_logcat_time_line(emu_threadtime) == (
        "error", "android.hardware.audio@7.1-impl.ranchu", "pcmWrite:260 failure: -1",
    )


def test_logcat_source_uses_threadtime(monkeypatch, tmp_path) -> None:
    """实机验证修复（2026-11-03）：real 日志流命令必须是 `logcat -v threadtime`。"""
    from unittest.mock import patch

    from app.services.log_stream import adb_logcat_time_source

    adb_path = tmp_path / "应用目录 with spaces" / "adb.exe"
    monkeypatch.setenv("PERFEVAL_ADB_PATH", str(adb_path))
    with patch("subprocess.Popen") as popen:
        popen.return_value.returncode = 0
        source_factory = adb_logcat_time_source("ANDROID-TEST-01")
        source = source_factory()
        assert source is not None
        source.close()
    cmd = popen.call_args[0][0]
    assert cmd == [str(adb_path), "-s", "ANDROID-TEST-01", "logcat", "-T", "1", "-v", "threadtime"]


def test_synthetic_log_source_shape() -> None:
    from app.services.log_stream import SyntheticLogSource, synthetic_log_line

    level, source, message = synthetic_log_line(2)
    assert level == "warn" and source == "ActivityManager"
    assert message == "Background start not allowed"
    src = SyntheticLogSource(pace_s=0.01, sleep=lambda _s: None)
    lines = [next(src.lines()) for _ in range(3)]
    for text in lines:
        assert " 12345  5678 " in text  # logcat -v time 同形态


# ---- 契约 §6.1：launchOnCreate（实机验证修复 2026-11-03：此前字段被 router
# 丢弃、app_prepare 从未执行——真机/模拟器实测踩中）----

def _ring_events(client: TestClient, sid: str) -> list[dict]:
    """读会话 bus 环形缓冲（SSE 重放源，同 test_real_m6_paths 的等价替身）。"""
    internal = client.app.state.sessions._sessions[sid]
    with internal.bus._lock:
        return [dict(env) for _seq, env in internal.bus._ring]


def _real_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """real 模式 client（fixture provider + stub 采集 adb，同 m4_fixtures 口径）。"""
    from m4_fixtures import fake_screenrecord_runner, make_real_client

    with make_real_client(
        tmp_path, monkeypatch, recorder_command_runner=fake_screenrecord_runner([], {})
    ) as c:
        yield c


def test_launch_on_create_runs_app_prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """launchOnCreate=true → provider.launch_app 执行 + phase started/completed 广播。"""
    calls: list[tuple[str, str]] = []

    for c in _real_client(tmp_path, monkeypatch):
        provider = c.app.state.provider

        def fake_launch(device_id: str, package_name: str) -> dict:
            calls.append((device_id, package_name))
            return {"component": f"{package_name}/.Main", "error": None}

        monkeypatch.setattr(provider, "launch_app", fake_launch)
        resp = c.post("/api/sessions", json={
            "deviceId": "fake-real-001",
            "packageName": "com.perfeval.fixture",
            "metrics": [{"metricId": "cpu.total.pct", "intervalMs": 1000}],
            "launchOnCreate": True,
        })
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        assert calls == [("fake-real-001", "com.perfeval.fixture")]
        # phase 事件经 bus 环形缓冲可读（SSE 重放等价）
        events = _ring_events(c, sid)
        phases = [(e["phase"], e["state"]) for e in events if e["type"] == "phase"
                  and e.get("phase") == "app_prepare"]
        assert ("app_prepare", "started") in phases
        assert ("app_prepare", "completed") in phases


def test_launch_on_create_failure_keeps_monitoring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """拉起失败 → phase failed + diagnostic error、会话保持 monitoring（不自动销毁）。"""
    for c in _real_client(tmp_path, monkeypatch):
        provider = c.app.state.provider

        def failing_launch(device_id: str, package_name: str) -> dict:
            return {"component": None, "error": "android.launch_activity_unresolved"}

        monkeypatch.setattr(provider, "launch_app", failing_launch)
        resp = c.post("/api/sessions", json={
            "deviceId": "fake-real-001",
            "packageName": "com.perfeval.fixture",
            "metrics": [],
            "launchOnCreate": True,
        })
        assert resp.status_code == 201
        sid = resp.json()["id"]
        assert resp.json()["state"] == "monitoring"
        events = _ring_events(c, sid)
        phases = [(e["phase"], e["state"]) for e in events if e["type"] == "phase"
                  and e.get("phase") == "app_prepare"]
        assert ("app_prepare", "failed") in phases
        diags = [e for e in events if e["type"] == "diagnostic"
                 and e.get("code") == "app.launch.failed"]
        assert len(diags) == 1 and diags[0]["severity"] == "error"


def test_launch_on_create_false_no_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省 false → 不拉起（monitoring 假设目标进程已在运行，契约 §6.1）。"""
    calls: list[tuple[str, str]] = []
    for c in _real_client(tmp_path, monkeypatch):
        provider = c.app.state.provider

        def fake_launch(device_id: str, package_name: str) -> dict:
            calls.append((device_id, package_name))
            return {"component": None, "error": None}

        monkeypatch.setattr(provider, "launch_app", fake_launch)
        resp = c.post("/api/sessions", json={
            "deviceId": "fake-real-001",
            "packageName": "com.perfeval.fixture",
            "metrics": [],
        })
        assert resp.status_code == 201
        assert calls == []


def test_launch_on_create_non_bool_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for c in _real_client(tmp_path, monkeypatch):
        resp = c.post("/api/sessions", json={
            "deviceId": "fake-real-001",
            "packageName": "com.perfeval.fixture",
            "metrics": [],
            "launchOnCreate": "yes",
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["details"]["field"] == "launchOnCreate"
