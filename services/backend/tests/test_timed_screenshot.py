"""M7 timed-screenshot 端点与循环测试（契约 §22.3，R38）。

如实标注：真机 adb I/O 无设备不可测——mock ``capture_screenshot_bytes``（§16.2
同一事实源的替身）+ 注入 capture/sleep 驱动**真实循环内核**（落盘命名/数量上限/
失败终止/诊断广播/重启幂等/record-stop 终止全部走生产代码）。real 路径未实测。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.errors import ApiError  # noqa: E402
from app.services.timed_screenshot import (  # noqa: E402
    MAX_CAPTURES,
    TimedScreenshotLoop,
    parse_interval_ms,
)
from m4_fixtures import (  # noqa: E402
    FIXTURE_PNG,
    fake_screenrecord_runner,
    make_real_client,
    make_recording_session,
    wait_until,
)
from m7_fixtures import drain_events  # noqa: E402


# ---- 实机验证修复（2026-11-03）：screencap 多屏 warning 前缀剥离 ----


def test_capture_screenshot_bytes_strips_display_warning_prefix() -> None:
    """SM_F9560 实测：多屏设备 `screencap -p` stdout 前置 347 字节 warning。

    按流内 PNG 签名定位剥离；无签名仍报 artifact_pull_failed。
    """
    from types import SimpleNamespace

    from app.services.screen_capture import capture_screenshot_bytes

    warning = (
        b"[Warning] Multiple displays were found, but no display id was specified! "
        b"Defaulting to display 0.\n"
    )
    dirty = warning + b"\x00" * 300 + FIXTURE_PNG  # 前缀污染 + PNG 签名在中段

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", "-s", serial, *args]

    completed = SimpleNamespace(returncode=0, stdout=dirty, stderr=b"")
    import app.services.screen_capture as mod

    orig_run = mod.subprocess.run
    mod.subprocess.run = lambda *a, **k: completed
    try:
        data = capture_screenshot_bytes(_Adb(), "RFCX701D8KR")
    finally:
        mod.subprocess.run = orig_run
    assert data == dirty[dirty.find(FIXTURE_PNG[:8]):]  # 签名起原样保留

    # 无签名（真实非 PNG）→ 拉流失败
    completed_no_png = SimpleNamespace(returncode=0, stdout=b"not a png at all", stderr=b"")
    mod.subprocess.run = lambda *a, **k: completed_no_png
    try:
        with pytest.raises(ApiError) as exc_info:
            capture_screenshot_bytes(_Adb(), "RFCX701D8KR")
    finally:
        mod.subprocess.run = orig_run
    assert exc_info.value.code == "artifact_pull_failed"


# ---- 参数校验（契约 §22.3：缺省 2000，允许 500–60000）----


def test_parse_interval_ms_defaults_and_bounds() -> None:
    assert parse_interval_ms(None) == 2000  # [F§7] 2s 一次
    assert parse_interval_ms(2000) == 2000
    assert parse_interval_ms(500) == 500 and parse_interval_ms(60000) == 60000
    for bad in (0, -1, 100, 60001, True, 2.5, "2000"):
        with pytest.raises(ApiError) as exc:
            parse_interval_ms(bad)
        assert exc.value.code == "validation_error"
        assert exc.value.details["field"] == "intervalMs"


def test_timed_screenshot_synthetic_501_with_diagnostic(client) -> None:
    """synthetic 模式 → 501 not_implemented + SSE timed-screenshot.unavailable(info)。"""
    body = {
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [{"metricId": "cpu.total.pct", "intervalMs": 60}],
        "interactive": False,
        "launchOnCreate": False,
    }
    sid = client.post("/api/sessions", json=body).json()["id"]
    bus_q = client.app.state.sessions.get(sid).bus.subscribe()
    resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={})
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "not_implemented"
    diag = [e for e in drain_events(bus_q) if e["type"] == "diagnostic"]
    assert len(diag) == 1
    assert diag[0]["code"] == "timed-screenshot.unavailable"
    assert diag[0]["severity"] == "info"


def test_timed_screenshot_param_validation_precedes_mode_gate(client) -> None:
    """校验顺序：参数（400）先于模式门（synthetic 501）。"""
    body = {
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [{"metricId": "cpu.total.pct", "intervalMs": 60}],
        "interactive": False,
        "launchOnCreate": False,
    }
    sid = client.post("/api/sessions", json=body).json()["id"]
    resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={"intervalMs": 10})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "intervalMs"


def test_timed_screenshot_session_not_found(client) -> None:
    resp = client.post("/api/sessions/sess-99/timed-screenshot", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"


def test_timed_screenshot_real_lifecycle(tmp_path, monkeypatch) -> None:
    """real recording 会话：202 形态、落盘命名/索引、重启幂等、record/stop 终止。"""
    runner = fake_screenrecord_runner([], {})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        monkeypatch.setattr(
            "app.services.timed_screenshot.capture_screenshot_bytes",
            lambda *a, **k: FIXTURE_PNG,
        )
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]

        # monitoring → 409（仅 recording 可启动）
        resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "session_state_conflict"

        client.post(f"/api/sessions/{sid}/record/start")
        run_dir = next(iter(p for p in client.runs_dir.iterdir() if p.is_dir()))

        # intervalMs 缺省 → 2000 回显；202 契约形态
        resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body == {"sessionId": sid, "intervalMs": 2000, "state": "started"}
        manager = client.app.state.sessions
        first_loop = manager.get(sid).timed_screenshot_loop
        assert first_loop is not None and first_loop.interval_ms == 2000
        wait_until(lambda: len(first_loop.captures) >= 2, timeout_s=10.0)
        first_loop.stop()  # 测试内先停，避免后台续拍干扰计数
        first_captures = list(first_loop.captures)

        # 落盘命名 screenshot-<tsMs>.png（§16.2 同款），字节为 mock 截图
        for name in first_captures:
            assert name.startswith("screenshot-") and name.endswith(".png")
            assert (run_dir / "artifacts" / name).read_bytes() == FIXTURE_PNG

        # 重启幂等：重复 POST 以新 intervalMs 重启（先停旧循环，不叠加）
        resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={"intervalMs": 500})
        assert resp.status_code == 202
        assert resp.json()["intervalMs"] == 500
        second_loop = manager.get(sid).timed_screenshot_loop
        assert second_loop is not first_loop and second_loop.interval_ms == 500
        assert not first_loop.running  # 旧循环已停（不叠加多循环）
        wait_until(lambda: len(second_loop.captures) >= 2, timeout_s=10.0)

        # record/stop 自动终止（契约 §22.3）
        client.post(f"/api/sessions/{sid}/record/stop")
        assert manager.get(sid).timed_screenshot_loop is None
        assert not second_loop.running
        total = len(first_captures) + len(second_loop.captures)

        # stopped → 409；save 后 §17 artifacts 索引含 screenshot（kind/collector）
        resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={})
        assert resp.status_code == 409
        resp = client.post(f"/api/sessions/{sid}/save", json={})
        run_id = resp.json()["runId"]
        index = client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"]
        shots = [a for a in index if a["kind"] == "screenshot"]
        assert len(shots) == total
        assert all(a["collector"] == "screencap" for a in shots)


def test_timed_screenshot_first_capture_failure_maps_502(tmp_path, monkeypatch) -> None:
    """首帧同步截取失败（契约 §22.3 502 artifact_pull_failed）+ error 诊断。"""
    runner = fake_screenrecord_runner([], {})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]
        client.post(f"/api/sessions/{sid}/record/start")

        def boom(*a, **k):
            raise ApiError("artifact_pull_failed", "screencap 执行失败: boom")

        monkeypatch.setattr(
            "app.services.timed_screenshot.capture_screenshot_bytes", boom
        )
        bus_q = client.app.state.sessions.get(sid).bus.subscribe()
        resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={})
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "artifact_pull_failed"
        # 循环未启动（首帧即败）
        assert client.app.state.sessions.get(sid).timed_screenshot_loop is None
        diag = [e for e in drain_events(bus_q) if e["type"] == "diagnostic"]
        assert any(
            e["code"] == "artifact_pull_failed" and e["severity"] == "error" for e in diag
        )


def test_timed_screenshot_mid_loop_failure_stops_and_broadcasts(
    tmp_path, monkeypatch
) -> None:
    """循环中单帧失败 → 整循环终止 + error 诊断（契约 §22.3）。"""
    runner = fake_screenrecord_runner([], {})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]
        client.post(f"/api/sessions/{sid}/record/start")

        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ApiError("artifact_pull_failed", "screencap 返回 1")
            return FIXTURE_PNG

        monkeypatch.setattr(
            "app.services.timed_screenshot.capture_screenshot_bytes", flaky
        )
        bus_q = client.app.state.sessions.get(sid).bus.subscribe()
        resp = client.post(f"/api/sessions/{sid}/timed-screenshot", json={"intervalMs": 500})
        assert resp.status_code == 202
        manager = client.app.state.sessions
        loop = manager.get(sid).timed_screenshot_loop
        wait_until(lambda: not loop.running, timeout_s=10.0)
        assert len(loop.captures) == 1  # 首帧成功、第二帧失败即终止
        diag = [e for e in drain_events(bus_q) if e["type"] == "diagnostic"]
        assert any(
            e["code"] == "artifact_pull_failed" and e["severity"] == "error" for e in diag
        )
        # 循环自终止后不再续拍
        count_after = calls["n"]
        time.sleep(0.2)
        assert calls["n"] == count_after


# ---- 循环内核单元（注入 capture/sleep，无真机）----


class _FakeSession:
    def __init__(self) -> None:
        from app.sse import SseBus

        self.id = "sess-xx"
        self.device_id = "fake-real-001"
        self.bus = SseBus(self.id)


def test_loop_cap_enforces_max_captures(tmp_path: Path) -> None:
    """数量上限防滥用：达上限自然结束 + info 诊断（timed_screenshot 模块）。"""
    session = _FakeSession()
    bus_q = session.bus.subscribe()

    def fast_sleep(seconds: float) -> bool:
        # 极短等待：产物名 screenshot-<tsMs>.png 以毫秒为唯一性（契约 §17 索引
        # 正则口径，真实间隔 ≥500ms 不会同 ms 冲突；无 sleep 连拍会同 ms 互覆）
        time.sleep(0.003)
        return False

    loop = TimedScreenshotLoop(
        session,
        interval_ms=2000,
        artifact_dir=tmp_path / "artifacts",
        capture=lambda: FIXTURE_PNG,
        sleep=fast_sleep,
        max_captures=3,
    )
    loop.start()
    wait_until(lambda: not loop.running, timeout_s=5.0)  # 自然结束（非外部 stop）
    assert len(loop.captures) == 3
    assert loop.stop_reason == "max_captures"
    diag = [e for e in drain_events(bus_q) if e["type"] == "diagnostic"]
    assert len(diag) == 1 and diag[0]["severity"] == "info"
    assert diag[0]["code"] == "timed-screenshot.unavailable"
    names = sorted(p.name for p in (tmp_path / "artifacts").glob("*.png"))
    assert len(names) == 3
    assert MAX_CAPTURES >= 1  # 上限常量在位（防滥用）


def test_loop_respects_stop_event_during_sleep(tmp_path: Path) -> None:
    """缺省 sleep = stop_event.wait：stop 中断等待，不再续拍（契约自动终止语义）。"""
    session = _FakeSession()
    loop = TimedScreenshotLoop(
        session,
        interval_ms=2000,  # 真实等待：stop 必然落在 sleep 窗口内
        artifact_dir=tmp_path / "artifacts",
        capture=lambda: FIXTURE_PNG,
    )
    loop.start()
    wait_until(lambda: len(loop.captures) >= 1, timeout_s=5.0)
    loop.stop()  # stop_event.wait 被中断 → 循环退出
    assert len(loop.captures) == 1
    time.sleep(0.2)
    assert len(loop.captures) == 1  # stop 后不续拍
