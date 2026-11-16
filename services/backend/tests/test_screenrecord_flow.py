"""M4 录屏全链（契约 §16.1/§17，fixture mp4 bytes；真机 adb I/O 用假传输层替身，
分段/watchdog/rollover/判废/锚点/落盘映射全部走生产代码——见 m4_fixtures 模块注）。"""

from __future__ import annotations

import json
import queue
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import make_session
from m4_fixtures import (
    FIXTURE_PNG,
    FakeClock,
    fake_screenrecord_runner,
    make_real_client,
    make_recording_session,
    make_segment_bytes,
    wait_until,
)

# ---- 全链：录制（watchdog rollover 两段 + <1KB 判废）→ save 改名 → 锚点 → Range 回读


def test_record_save_anchors_range_full_chain(tmp_path: Path, monkeypatch) -> None:
    executed: list[str] = []
    # 首段 screen.mp4 2KB、续段 screen-seg002.mp4 512B（<1KB 判废）、
    # 续段 screen-seg003.mp4 1.5KB（rollover 后仍有效）
    runner = fake_screenrecord_runner(
        executed, {"screen.mp4": 2048, "screen-seg002.mp4": 512, "screen-seg003.mp4": 1536}
    )
    clock = FakeClock()
    with make_real_client(
        tmp_path, monkeypatch, recorder_command_runner=runner, clock=clock
    ) as client:
        session = make_recording_session(client)
        sid = session["id"]
        assert session["recordScreen"] is True

        resp = client.post(f"/api/sessions/{sid}/record/start")
        assert resp.status_code == 200, resp.text
        # probe → clock → 启动首段（fake runner 命令序列，真实 _ScreenRecordSession 驱动）
        start_entry = executed[2]
        assert executed[:2] == ["probe", "clock"]
        assert start_entry.startswith("start:/sdcard/perfeval-PD_") and start_entry.endswith(".mp4")
        run_name = start_entry.removeprefix("start:/sdcard/perfeval-").removesuffix(".mp4")
        run_dir = client.runs_dir / run_name
        assert run_dir.is_dir()

        # 段内时间流逝超过 175s → watchdog 中途 rollover 起下一段（真实内核线程）
        clock.advance(176)
        wait_until(lambda: "start:/sdcard/perfeval-%s-seg002.mp4" % run_dir.name in executed)
        clock.advance(176)
        wait_until(lambda: "start:/sdcard/perfeval-%s-seg003.mp4" % run_dir.name in executed)

        resp = client.post(f"/api/sessions/{sid}/record/stop")
        assert resp.status_code == 200, resp.text
        # stop → pkill 收尾 + pull 各段（真实内核）
        assert executed.count("stop") >= 3
        assert "pull:screen.mp4" in executed
        assert "pull:screen-seg002.mp4" in executed
        assert "pull:screen-seg003.mp4" in executed
        # <1KB 判废：runner 删除无效段文件（perfeval _discard_empty_segment）
        assert not (run_dir / "artifacts" / "screen-seg002.mp4").exists()
        assert (run_dir / "artifacts" / "screen.mp4").stat().st_size == 2048
        assert (run_dir / "artifacts" / "screen-seg003.mp4").stat().st_size == 1536

        run_id = f"run-{run_dir.name}"
        resp = client.post(f"/api/sessions/{sid}/save", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["runId"] == run_id

        # ---- save 落位映射：screen*.mp4 → video*.mp4（契约 §16.1）----
        assert (run_dir / "artifacts" / "video.mp4").read_bytes() == make_segment_bytes("screen.mp4", 2048)
        assert (run_dir / "artifacts" / "video-seg003.mp4").read_bytes() == make_segment_bytes(
            "screen-seg003.mp4", 1536
        )
        assert not (run_dir / "artifacts" / "screen.mp4").exists()

        # ---- §17 索引：判废件不入索引，kind/collector/sha256 形状 ----
        body = client.get(f"/api/runs/{run_id}/artifacts").json()
        assert body["runId"] == run_id
        names = [a["name"] for a in body["artifacts"]]
        assert names == ["video.mp4", "video-seg003.mp4"]
        first = body["artifacts"][0]
        assert first["kind"] == "video"
        assert first["collector"] == "screenrecord"
        assert first["bytes"] == 2048
        assert len(first["sha256"]) == 64

        # ---- manifest clockAnchors：video 段 + samplers 段（四锚点形状原样，缺段缺省）----
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        anchors = manifest["context"]["clockAnchors"]
        assert anchors is not None
        video = anchors["video"]
        assert video["startEpochMs"] == 1757300002000
        assert video["firstFrameEpochMs"] == 1757300003000  # +1s 编码延迟（timebase.py:22）
        assert "encoding delay" in video["method"]
        assert video["timeLimitS"] == 175
        assert video["segments"] == 1  # extendable 计划预生成首段
        assert video["clockSource"] == "device_date"
        samplers = anchors["samplers"]
        assert samplers["startEpochMs"] == 1757300001000
        assert samplers["source"] == "device_date"
        assert samplers["boottimeNs"] == 812345678000000
        assert "method" in samplers
        # 会话后端不采 logcat：t0/markers 段缺省（不伪造）
        assert "t0EpochMs" not in anchors
        assert "markers" not in anchors

        # ---- §17 video 端点：200 全量 + Range 206/分片/416 ----
        fixture = make_segment_bytes("screen.mp4", 2048)
        resp = client.get(f"/api/runs/{run_id}/video")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("video/mp4")
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.content == fixture

        resp = client.get(
            f"/api/runs/{run_id}/video", headers={"Range": "bytes=0-9"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-9/{len(fixture)}"
        assert resp.headers["content-length"] == "10"
        assert resp.content == fixture[:10]

        resp = client.get(
            f"/api/runs/{run_id}/video", headers={"Range": "bytes=10-"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 10-{len(fixture) - 1}/{len(fixture)}"
        assert resp.content == fixture[10:]

        resp = client.get(
            f"/api/runs/{run_id}/video", headers={"Range": f"bytes=-{len(fixture) - 5}"}
        )
        assert resp.status_code == 206
        assert resp.content == fixture[-(len(fixture) - 5):]

        # ?name= 选段
        seg = make_segment_bytes("screen-seg003.mp4", 1536)
        resp = client.get(
            f"/api/runs/{run_id}/video",
            params={"name": "video-seg003.mp4"},
            headers={"Range": "bytes=0-3"},
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-3/{len(seg)}"
        assert resp.content == seg[:4]

        # 语法非法 / 起点越界 → 416 + bytes */total
        for bad in ("bytes=abc", "bytes=5-1", "items=0-9", "bytes=99999-", "bytes=-0"):
            resp = client.get(f"/api/runs/{run_id}/video", headers={"Range": bad})
            assert resp.status_code == 416, bad
            assert resp.headers["content-range"] == f"bytes */{len(fixture)}"

        # ?name= 不在索引（判废剔除）→ 404
        resp = client.get(f"/api/runs/{run_id}/video", params={"name": "video-seg002.mp4"})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "report_not_found"

        # ---- §17 artifacts/{name} 下载 ----
        resp = client.get(f"/api/runs/{run_id}/artifacts/video-seg003.mp4")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("video/mp4")
        assert resp.content == seg
        resp = client.get(f"/api/runs/{run_id}/artifacts/video-seg002.mp4")
        assert resp.status_code == 404
        resp = client.get(f"/api/runs/{run_id}/artifacts/..%2Fmanifest.json")
        assert resp.status_code == 404

        # ---- §19 回放聚合（无样本 run：series 空、artifacts/锚点/summary 引用在）----
        replay = client.get(f"/api/runs/{run_id}/replay").json()
        assert replay["runId"] == run_id
        assert replay["caseName"] == run_dir.name
        assert replay["durationMs"] == 0
        assert replay["series"] == []
        assert replay["downsampled"] is False
        assert replay["markers"] == []
        assert [a["name"] for a in replay["artifacts"]] == ["video.mp4", "video-seg003.mp4"]
        assert replay["clockAnchors"] == anchors
        assert replay["summary"] == {
            "summaryAvailable": False,  # 无数值样本 → 未写 summary.json
            "statisticsUrl": f"/api/runs/{run_id}/statistics",
        }

        # ---- run 详情（§9，2026-11-04 实装）：无数值样本 → summary null（不伪造）----
        resp = client.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200
        assert resp.json()["summary"] is None
        assert resp.json()["summaryAvailable"] is False


def test_recorder_unavailable_degrades_to_diagnostic(tmp_path: Path, monkeypatch) -> None:
    # 设备无 screenrecord：probe 失败 → 诊断降级，状态机照常（契约 §16.1）
    executed: list[str] = []

    def runner(command: list[str], timeout: float | None):
        result = type("_R", (), {})()
        result.stdout = ""
        result.returncode = 1
        result.stderr = "not found"
        return result

    with make_real_client(
        tmp_path, monkeypatch, recorder_command_runner=runner, clock=FakeClock()
    ) as client:
        session = make_recording_session(client)
        sid = session["id"]
        bus_q = client.app.state.sessions.get(sid).bus.subscribe()
        resp = client.post(f"/api/sessions/{sid}/record/start")
        assert resp.status_code == 200
        resp = client.post(f"/api/sessions/{sid}/record/stop")
        assert resp.status_code == 200
        events = []
        while not bus_q.empty():
            events.append(bus_q.get())
        diagnostics = [e for e in events if e["type"] == "diagnostic"]
        assert any(e["code"] == "screen.record.unavailable" for e in diagnostics)
        # 无有效段 → 锚点撤销 → manifest clockAnchors 仅含 samplers 段
        run_dir = client.runs_dir / next(iter(p.name for p in client.runs_dir.iterdir()))
        resp = client.post(f"/api/sessions/{sid}/save", json={})
        assert resp.status_code == 200
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        anchors = manifest["context"]["clockAnchors"]
        assert "video" not in anchors  # 全部段无效 → video 锚点撤销（runner.py:735-737）
        assert anchors["samplers"]["startEpochMs"] == 1757300001000


def test_video_endpoint_404_when_no_video_artifact(client: TestClient) -> None:
    session = make_session(client)
    resp = client.post(f"/api/sessions/{session['id']}/save", json={"caseName": "PD_NOVIDEO"})
    assert resp.status_code == 200
    resp = client.get("/api/runs/run-PD_NOVIDEO/video")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "report_not_found"
    resp = client.get("/api/runs/run-PD_GHOST/video")
    assert resp.status_code == 404


# ---- §16.2 截图 ----


def test_screenshot_real_lifecycle(tmp_path: Path, monkeypatch) -> None:
    runner = fake_screenrecord_runner([], {"screen.mp4": 4096})
    with make_real_client(
        tmp_path, monkeypatch, recorder_command_runner=runner, clock=FakeClock()
    ) as client:
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]
        # monitoring → 409（无 run 目录可落）
        resp = client.post(f"/api/sessions/{sid}/screenshot")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "session_state_conflict"

        client.post(f"/api/sessions/{sid}/record/start")
        run_dir = next(iter(p for p in client.runs_dir.iterdir() if p.is_dir()))
        monkeypatch.setattr(
            "app.routers.sessions.capture_screenshot_bytes", lambda *a, **k: FIXTURE_PNG
        )
        resp = client.post(f"/api/sessions/{sid}/screenshot")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["sessionId"] == sid
        name = body["artifact"]["name"]
        assert body["artifact"]["kind"] == "screenshot"
        assert body["artifact"]["bytes"] == len(FIXTURE_PNG)
        assert (run_dir / "artifacts" / name).read_bytes() == FIXTURE_PNG

        # stopped 后 save → §17 索引含截图（kind/collector=screen）
        client.post(f"/api/sessions/{sid}/record/stop")
        resp = client.post(f"/api/sessions/{sid}/save", json={})
        run_id = resp.json()["runId"]
        index = client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"]
        shots = [a for a in index if a["kind"] == "screenshot"]
        assert len(shots) == 1
        assert shots[0]["name"] == name
        assert shots[0]["collector"] == "screencap"
        assert shots[0]["bytes"] == len(FIXTURE_PNG)
        resp = client.get(f"/api/runs/{run_id}/artifacts/{name}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.content == FIXTURE_PNG

        # saved → 409
        resp = client.post(f"/api/sessions/{sid}/screenshot")
        assert resp.status_code == 409


def test_screenshot_synthetic_501_with_diagnostic(client: TestClient) -> None:
    session = make_session(client)
    sid = session["id"]
    bus_q = client.app.state.sessions.get(sid).bus.subscribe()
    resp = client.post(f"/api/sessions/{sid}/screenshot")
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "not_implemented"
    events = []
    while not bus_q.empty():
        events.append(bus_q.get())
    diag = [e for e in events if e["type"] == "diagnostic"]
    assert len(diag) == 1
    assert diag[0]["code"] == "screenshot.unavailable"
    assert diag[0]["severity"] == "info"


def test_screencap_command_failure_maps_to_artifact_pull_failed(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from app.errors import ApiError
    from app.services.screen_capture import capture_screenshot_bytes
    from perfeval.collectors.android.adb import AdbClient

    def fail_run(*args, **kwargs):
        result = subprocess.CompletedProcess(args[0], returncode=1, stdout=b"", stderr=b"err")
        return result

    monkeypatch.setattr("app.services.screen_capture.subprocess.run", fail_run)
    try:
        capture_screenshot_bytes(AdbClient("/fake-adb"), "fake-real-001")
        raise AssertionError("expected ApiError")
    except ApiError as exc:
        assert exc.code == "artifact_pull_failed"
        assert exc.status == 502
        assert exc.details == {"phase": "screenshot"}

    # 非 PNG 产物（魔数不符）→ 同码
    def garbage_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], returncode=0, stdout=b"not-png", stderr=b"")

    monkeypatch.setattr("app.services.screen_capture.subprocess.run", garbage_run)
    try:
        capture_screenshot_bytes(AdbClient("/fake-adb"), "fake-real-001")
        raise AssertionError("expected ApiError")
    except ApiError as exc:
        assert exc.code == "artifact_pull_failed"


def test_screencap_success_returns_png_bytes(monkeypatch) -> None:
    import subprocess

    from app.services.screen_capture import capture_screenshot_bytes
    from m4_fixtures import FIXTURE_PNG
    from perfeval.collectors.android.adb import AdbClient

    def ok_run(command, **kwargs):
        assert command[:4] == ["/fake-adb", "-s", "fake-real-001", "exec-out"]
        assert command[4:] == ["screencap", "-p"]
        return subprocess.CompletedProcess(command, returncode=0, stdout=FIXTURE_PNG, stderr=b"")

    monkeypatch.setattr("app.services.screen_capture.subprocess.run", ok_run)
    data = capture_screenshot_bytes(AdbClient("/fake-adb"), "fake-real-001")
    assert data == FIXTURE_PNG


# ---- §16.1 recordScreen 开关语义 ----


def test_record_screen_synthetic_diagnostic_on_create(client: TestClient) -> None:
    body = make_session(client, recordScreen=True)
    assert body["recordScreen"] is True
    # 创建时已广播的 diagnostic 经 SSE 重放面（bus.replay(0)）可读；同会话还有
    # SyntheticCollector 自身的 flow.synthetic.started，此处只断言录屏诊断
    events, _ = client.app.state.sessions.get(body["id"]).bus.replay(0)
    diag = [e for e in events if e["type"] == "diagnostic"]
    unavailable = [e for e in diag if e["code"] == "screenrecord.unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0]["severity"] == "info"

    # 缺省 false 回显（先释放设备占用）
    client.post(f"/api/sessions/{body['id']}/discard")
    plain = make_session(client)
    assert plain["recordScreen"] is False


def test_record_screen_non_bool_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/sessions",
        json={
            "deviceId": "synthetic-001",
            "packageName": "com.example.synthetic.game",
            "recordScreen": "yes",
        },
    )
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"]["field"] == "recordScreen"
