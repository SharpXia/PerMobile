"""会话状态机与错误码（契约 §6 / ARCH §4.4）。"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from conftest import make_session


def _error_body(resp) -> dict:
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) >= {"code", "message"}
    return body["error"]


def test_create_session_201_shape(client: TestClient) -> None:
    body = make_session(client)
    assert body["id"].startswith("sess-")
    assert body["state"] == "monitoring"
    assert body["deviceId"] == "synthetic-001"
    assert body["packageName"] == "com.example.synthetic.game"
    assert body["metrics"] == [{"metricId": "cpu.total.pct", "intervalMs": 60}]
    assert isinstance(body["createdAt"], int)
    assert set(body) >= {"id", "state", "deviceId", "packageName", "metrics", "createdAt"}


def test_create_session_validation_errors(client: TestClient) -> None:
    # 未知设备
    err = _error_body(client.post("/api/sessions", json={"deviceId": "ghost", "packageName": "com.example.synthetic.game"}))
    assert err["code"] == "device_not_found"
    # synthetic 包名白名单（契约 §6）
    err = _error_body(client.post("/api/sessions", json={"deviceId": "synthetic-001", "packageName": "com.foo.bar"}))
    assert err["code"] == "validation_error"
    assert err["details"]["field"] == "packageName"
    # 未注册指标
    err = _error_body(client.post("/api/sessions", json={
        "deviceId": "synthetic-001", "packageName": "com.example.synthetic.game",
        "metrics": [{"metricId": "nope.metric", "intervalMs": 1000}],
    }))
    assert err["code"] == "validation_error"
    # 非 JSON 请求体 → 契约错误体（覆盖 FastAPI 默认 422）
    err = _error_body(client.post("/api/sessions", content=b"not json", headers={"content-type": "application/json"}))
    assert err["code"] == "validation_error"


def test_device_busy_conflict(client: TestClient) -> None:
    make_session(client)
    err = _error_body(client.post("/api/sessions", json={
        "deviceId": "synthetic-001", "packageName": "com.example.synthetic.app",
    }))
    assert err["code"] == "device_busy"
    assert err["details"]["sessionId"].startswith("sess-")


def test_full_state_machine_flow(client: TestClient) -> None:
    session = make_session(client)
    sid = session["id"]

    # monitoring -> recording
    resp = client.post(f"/api/sessions/{sid}/record/start")
    assert resp.status_code == 200
    assert resp.json()["state"] == "recording"
    # 重复 start -> 409
    err = _error_body(client.post(f"/api/sessions/{sid}/record/start"))
    assert err["code"] == "session_state_conflict"
    assert err["details"]["state"] == "recording"

    # recording -> stopped（重复 stop 幂等）
    time.sleep(0.3)  # 留出采样窗口，验证在线落盘
    resp = client.post(f"/api/sessions/{sid}/record/stop")
    assert resp.status_code == 200
    assert resp.json()["state"] == "stopped"
    resp = client.post(f"/api/sessions/{sid}/record/stop")
    assert resp.status_code == 200
    assert resp.json()["state"] == "stopped"
    # stopped -> saved
    resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": "PD_TEST_FLOW"})
    assert resp.status_code == 200
    saved = resp.json()
    assert saved["state"] == "saved"
    assert saved["runId"] == "run-PD_TEST_FLOW"
    assert saved["runPath"].endswith("PD_TEST_FLOW")
    # 重复 save -> 409
    err = _error_body(client.post(f"/api/sessions/{sid}/save", json={}))
    assert err["code"] == "session_state_conflict"

    # 落盘结构（ARCH §4.5）
    run_dir = client.runs_dir / "PD_TEST_FLOW"
    assert (run_dir / "samples.ndjson").exists()
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "timeline.ndjson").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["schemaVersion"] == "perfeval.manifest.v1"
    assert "clockAnchors" in manifest["context"]
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["schemaVersion"] == "perfeval.summary.v1"
    samples_line = (run_dir / "samples.ndjson").read_text().splitlines()[0]
    record = json.loads(samples_line)
    assert set(record) == {"tsMs", "metricId", "value", "tags"}  # perfeval SampleRecord 同构


def test_recording_cannot_save(client: TestClient) -> None:
    sid = make_session(client)["id"]
    client.post(f"/api/sessions/{sid}/record/start")
    err = _error_body(client.post(f"/api/sessions/{sid}/save", json={}))
    assert err["code"] == "session_state_conflict"
    client.post(f"/api/sessions/{sid}/record/stop")
    client.post(f"/api/sessions/{sid}/discard")
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 409


def test_monitoring_stop_conflict(client: TestClient) -> None:
    """monitoring 态 stop → 409（应走 discard/save，契约 §6）。"""
    sid = make_session(client)["id"]
    err = _error_body(client.post(f"/api/sessions/{sid}/record/stop"))
    assert err["code"] == "session_state_conflict"
    assert err["details"]["state"] == "monitoring"


def test_monitoring_direct_save_writes_buffer(client: TestClient) -> None:
    """monitoring 直接收口：内存缓冲写盘（契约 §6.1 监控态收口）。"""
    sid = make_session(client)["id"]
    time.sleep(0.3)  # 等 1-2 个 60ms 采样点
    resp = client.post(f"/api/sessions/{sid}/save", json={})
    assert resp.status_code == 200
    assert resp.json()["state"] == "saved"
    assert resp.json()["runId"].startswith("run-PD_")
    run_dir = client.runs_dir / resp.json()["runId"].removeprefix("run-")
    assert (run_dir / "samples.ndjson").exists()


def test_save_case_name_validation_blocks_path_escape(client: TestClient) -> None:
    """caseName is used as a run directory and must stay within runs_dir."""
    sid = make_session(client)["id"]
    for case_name in ("../outside", "nested/name", r"..\\outside", "..", 123):
        resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": case_name})
        assert resp.status_code == 400, (case_name, resp.text)
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["details"]["field"] == "caseName"
    assert not (client.runs_dir.parent / "outside").exists()
    client.post(f"/api/sessions/{sid}/discard")


def test_monitoring_save_duplicate_case_name_keeps_both_runs(client: TestClient) -> None:
    first_sid = make_session(client)["id"]
    first = client.post(
        f"/api/sessions/{first_sid}/save", json={"caseName": "PD_DUPLICATE"}
    )
    assert first.status_code == 200, first.text
    first_run = client.runs_dir / "PD_DUPLICATE"
    first_manifest_before = (first_run / "manifest.json").read_text()

    second_sid = make_session(client)["id"]
    second = client.post(
        f"/api/sessions/{second_sid}/save", json={"caseName": "PD_DUPLICATE"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["runId"] == "run-PD_DUPLICATE_2"
    assert (client.runs_dir / "PD_DUPLICATE_2" / "manifest.json").exists()
    assert (first_run / "manifest.json").read_text() == first_manifest_before


def test_discard_and_delete(client: TestClient) -> None:
    sid = make_session(client)["id"]
    time.sleep(0.15)
    resp = client.post(f"/api/sessions/{sid}/discard")
    assert resp.status_code == 204
    resp = client.post(f"/api/sessions/{sid}/discard")  # 幂等
    assert resp.status_code == 204
    # deleted 列表中消失
    resp = client.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 204
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_delete_blocked_in_active_states(client: TestClient) -> None:
    sid = make_session(client)["id"]
    err = _error_body(client.delete(f"/api/sessions/{sid}"))
    assert err["code"] == "session_state_conflict"  # monitoring 不可删


def test_session_not_found(client: TestClient) -> None:
    err = _error_body(client.get("/api/sessions/sess-99"))
    assert err["code"] == "session_not_found"
    err = _error_body(client.post("/api/sessions/sess-99/record/start"))
    assert err["code"] == "session_not_found"


def test_stats_snapshot_and_session_list(client: TestClient) -> None:
    sid = make_session(client)["id"]
    time.sleep(0.3)
    detail = client.get(f"/api/sessions/{sid}").json()
    stats = detail["stats"]["cpu.total.pct"]
    assert stats["count"] >= 1
    assert isinstance(stats["latest"], float)
    listing = client.get("/api/sessions").json()
    assert any(s["id"] == sid for s in listing["sessions"])


def test_patch_metrics_enable_disable(client: TestClient) -> None:
    sid = make_session(client)["id"]
    time.sleep(0.1)
    resp = client.patch(f"/api/sessions/{sid}/metrics", json={
        "enable": [{"metricId": "frame.fps.mean", "intervalMs": 100}],
        "disable": ["cpu.total.pct"],
    })
    assert resp.status_code == 200
    metric_ids = {m["metricId"] for m in resp.json()["metrics"]}
    assert "frame.fps.mean" in metric_ids and "cpu.total.pct" not in metric_ids
    # 双空 -> 400
    err = _error_body(client.patch(f"/api/sessions/{sid}/metrics", json={}))
    assert err["code"] == "validation_error"
    # unavailable 指标 enable -> 409 metric_unavailable
    err = _error_body(client.patch(f"/api/sessions/{sid}/metrics", json={
        "enable": [{"metricId": "gpu.busy.pct"}],
    }))
    assert err["code"] == "metric_unavailable"
    assert err["details"]["metricId"] == "gpu.busy.pct"
    # stopped 态 -> 409
    client.post(f"/api/sessions/{sid}/record/start")
    client.post(f"/api/sessions/{sid}/record/stop")
    err = _error_body(client.patch(f"/api/sessions/{sid}/metrics", json={"disable": ["frame.fps.mean"]}))
    assert err["code"] == "session_state_conflict"


def test_interval_query_and_summary(client: TestClient) -> None:
    sid = make_session(client)["id"]
    time.sleep(0.4)
    resp = client.get(f"/api/sessions/{sid}/metrics?metricIds=cpu.total.pct")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"sessionId", "fromMs", "toMs", "series", "downsampled"}
    series = body["series"][0]
    assert series["metricId"] == "cpu.total.pct"
    assert series["unit"] == "%"
    assert series["points"] and set(series["points"][0]) == {"ts", "value"}

    resp = client.get(f"/api/sessions/{sid}/summary")
    assert resp.status_code == 200
    assert resp.json()["schemaVersion"] == "perfeval.summary.v1"  # 原样透出，不加壳


def test_relative_ts_normalized_to_epoch(tmp_path) -> None:
    """perfeval 采样器时戳是相对 elapsed ms；入会话缓冲前必须归一 epoch ms（契约 §1），
    否则 5 分钟有界缓冲会把真实数据全部误剪掉（M1 冒烟实测踩中）。"""
    from perfeval.collectors.android.samples import SampleRecord

    from app.services.session_manager import Session
    from app.sse import now_ms

    created = now_ms()
    session = Session(
        session_id="sess-t", device_id="d", package_name="p",
        metrics=[{"metricId": "cpu.total.pct", "intervalMs": 1000}],
        provider_mode="real", created_at=created, runs_dir=tmp_path,
    )
    session.append_record(SampleRecord(metric_id="cpu.total.pct", value=1.0, ts_ms=1010))
    row = session.records()[0]
    assert row["tsMs"] == created + 1010  # 相对时戳 → epoch
    assert session.stats_snapshot()["cpu.total.pct"]["count"] == 1
    # 已是 epoch 的时戳原样保留（synthetic 路径）
    session.append_record(SampleRecord(metric_id="cpu.total.pct", value=2.0, ts_ms=created + 5))
    assert session.records()[-1]["tsMs"] == created + 5

def test_record_started_at_exposed_for_resume(client: TestClient) -> None:
    """契约 §6.2：detail 回显 recordStartedAt（null = 未进入 recording）。

    页面刷新后前端凭它还原「记录 xx:xx」计时，避免把已录制时长显示成 0。
    """
    body = make_session(client)
    assert body["recordStartedAt"] is None  # monitoring：不伪造 0
    started = client.post(f"/api/sessions/{body['id']}/record/start")
    assert started.status_code == 200, started.text
    started_at = started.json()["recordStartedAt"]
    assert isinstance(started_at, int) and started_at > 0
    stopped = client.post(f"/api/sessions/{body['id']}/record/stop").json()
    assert stopped["recordStartedAt"] == started_at  # 停止后冻结原值
    listed = client.get("/api/sessions").json()["sessions"]
    assert listed[0]["recordStartedAt"] == started_at


def test_close_all_stops_collectors_on_shutdown(client: TestClient) -> None:
    """后端退出兜底：close_all 停掉全部会话采集（不留孤儿采集线程/子进程）。"""
    sid = make_session(client)["id"]
    manager = client.app.state.sessions  # type: ignore[attr-defined]
    session = manager.get(sid)
    assert session.collector is not None
    manager.close_all()
    assert session.collector is None
    # 幂等：lifespan 退出会再调一次，不抛异常
    manager.close_all()
