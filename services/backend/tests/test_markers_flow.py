"""M4 markers（契约 §15：perfdog.markers.v1 sidecar；§15.2 会话端点/SSE；
§15.3 run 读回）。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import make_session


def _error(resp) -> dict:
    assert resp.headers["content-type"].startswith("application/json")
    return resp.json()["error"]


# ---- §15.2 会话 CRUD ----


def test_labels_notes_marks_crud_sorted_and_sse(client: TestClient) -> None:
    session = make_session(client)
    sid = session["id"]
    bus_q = client.app.state.sessions.get(sid).bus.subscribe()

    r1 = client.post(
        f"/api/sessions/{sid}/labels",
        json={"tsMs": 1798765435000, "name": "战斗场景", "color": "#FF5252"},
    )
    assert r1.status_code == 201, r1.text
    label = r1.json()
    assert label == {
        "id": "mkr-01",
        "tsMs": 1798765435000,
        "kind": "label",
        "name": "战斗场景",
        "color": "#FF5252",
    }
    r2 = client.post(
        f"/api/sessions/{sid}/notes",
        json={"tsMs": 1798765442000, "text": "此处加载卡顿明显"},
    )
    note = r2.json()
    assert note["id"] == "mkr-02"
    assert note["kind"] == "note"
    r3 = client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 1798765450000})
    mark = r3.json()
    assert mark["id"] == "mkr-03"
    assert mark == {"id": "mkr-03", "tsMs": 1798765450000, "kind": "mark"}

    # 无序插入 → GET 按 tsMs 升序
    r4 = client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 1})
    assert r4.status_code == 201
    body = client.get(f"/api/sessions/{sid}/markers").json()
    assert body["sessionId"] == sid
    assert [m["tsMs"] for m in body["markers"]] == [1, 1798765435000, 1798765442000, 1798765450000]
    ids = [m["id"] for m in body["markers"]]
    assert ids == ["mkr-04", "mkr-01", "mkr-02", "mkr-03"]

    # SSE marker add 事件（契约 §15.2）
    events = []
    while not bus_q.empty():
        events.append(bus_q.get())
    adds = [e for e in events if e["type"] == "marker"]
    assert len(adds) == 4
    assert all(e["action"] == "add" for e in adds)
    assert adds[0]["marker"]["id"] == "mkr-01"

    # DELETE 幂等（重复删除 / 未知 id 均 204）
    assert client.delete(f"/api/sessions/{sid}/markers/mkr-04").status_code == 204
    assert client.delete(f"/api/sessions/{sid}/markers/mkr-04").status_code == 204
    assert client.delete(f"/api/sessions/{sid}/markers/mkr-unknown").status_code == 204
    events.clear()
    while not bus_q.empty():
        events.append(bus_q.get())
    removes = [e for e in events if e["type"] == "marker"]
    assert len(removes) == 1
    assert removes[0]["action"] == "remove"
    assert removes[0]["marker"] == {"id": "mkr-04"}

    body = client.get(f"/api/sessions/{sid}/markers").json()
    assert len(body["markers"]) == 3

    # tsMs 缺省 = 服务端当前 epoch ms
    resp = client.post(f"/api/sessions/{sid}/marks", json={})
    assert resp.status_code == 201
    assert isinstance(resp.json()["tsMs"], int)


def test_marker_validation_errors(client: TestClient) -> None:
    session = make_session(client)
    sid = session["id"]
    # label 缺 name
    err = _error(client.post(f"/api/sessions/{sid}/labels", json={"tsMs": 1}))
    assert err["code"] == "validation_error" and err["details"]["field"] == "name"
    # note 缺/空 text
    err = _error(client.post(f"/api/sessions/{sid}/notes", json={"tsMs": 1}))
    assert err["details"]["field"] == "text"
    err = _error(client.post(f"/api/sessions/{sid}/notes", json={"text": "  "}))
    assert err["details"]["field"] == "text"
    # tsMs 非整数（字符串/浮点/布尔）
    for bad in ("abc", 1.5, True):
        err = _error(client.post(f"/api/sessions/{sid}/marks", json={"tsMs": bad}))
        assert err["code"] == "validation_error"
        assert err["details"]["field"] == "tsMs"
    # 未知会话
    assert client.post("/api/sessions/sess-99/labels", json={"name": "x"}).status_code == 404


def test_marker_state_gate_and_discard_semantics(client: TestClient) -> None:
    session = make_session(client)
    sid = session["id"]
    client.post(f"/api/sessions/{sid}/labels", json={"tsMs": 10, "name": "L"})
    client.post(f"/api/sessions/{sid}/record/start")
    # recording 可写
    assert client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 20}).status_code == 201
    client.post(f"/api/sessions/{sid}/record/stop")
    # stopped 可写
    assert client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 30}).status_code == 201
    # discard → 标记随会话消失（§15.1），状态门 409
    client.post(f"/api/sessions/{sid}/discard")
    err = _error(client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 40}))
    assert err["code"] == "session_state_conflict"
    assert client.delete(f"/api/sessions/{sid}/markers/mkr-01").status_code == 409
    # saved → 409
    session2 = make_session(client)
    client.post(f"/api/sessions/{session2['id']}/labels", json={"tsMs": 1, "name": "A"})
    client.post(f"/api/sessions/{session2['id']}/save", json={})
    err = _error(client.post(f"/api/sessions/{session2['id']}/marks", json={"tsMs": 2}))
    assert err["code"] == "session_state_conflict"
    # saved 后 GET 仍可读（任意状态可读）
    body = client.get(f"/api/sessions/{session2['id']}/markers").json()
    assert len(body["markers"]) == 1


# ---- §15.1 save 落盘 sidecar ----


def test_save_writes_sidecar_sorted_without_ids(client: TestClient) -> None:
    session = make_session(client)
    sid = session["id"]
    client.post(f"/api/sessions/{sid}/notes", json={"tsMs": 3000, "text": "n2"})
    client.post(f"/api/sessions/{sid}/labels", json={"tsMs": 1000, "name": "战斗", "color": "#FF5252"})
    client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 2000})

    resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": "PD_MARKERS"})
    run_id = resp.json()["runId"]
    sidecar = client.runs_dir / "PD_MARKERS" / "markers.ndjson"
    lines = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert [m["tsMs"] for m in lines] == [1000, 2000, 3000]
    assert lines[0] == {"tsMs": 1000, "kind": "label", "name": "战斗", "color": "#FF5252"}
    assert lines[1] == {"tsMs": 2000, "kind": "mark"}
    assert lines[2] == {"tsMs": 3000, "kind": "note", "text": "n2"}
    assert all("id" not in m and "_order" not in m for m in lines)

    # §15.3 run 读回：同源
    body = client.get(f"/api/runs/{run_id}/markers").json()
    assert body == {
        "schemaVersion": "perfdog.markers.v1",
        "runId": run_id,
        "markers": lines,
    }

    # save 后会话标记不再改动（状态门），再次 save 侧车不重建/不丢
    err = _error(client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 9}))
    assert err["code"] == "session_state_conflict"


def test_save_without_markers_writes_no_sidecar(client: TestClient) -> None:
    session = make_session(client)
    resp = client.post(f"/api/sessions/{session['id']}/save", json={"caseName": "PD_NOMKR"})
    run_id = resp.json()["runId"]
    assert not (client.runs_dir / "PD_NOMKR" / "markers.ndjson").exists()
    # §15.3：无 sidecar → 200 + 空列表（无标记是常态）
    body = client.get(f"/api/runs/{run_id}/markers").json()
    assert body == {"schemaVersion": "perfdog.markers.v1", "runId": run_id, "markers": []}


# ---- §15.2 run 侧扩展：save 期标记与 run 侧标注合并读回（§15.3 同源）----


def test_save_sidecar_then_run_side_append_merged_read_back(client: TestClient) -> None:
    """会话期标记（save 整体写）+ run 侧标注（增量追加）在同一 sidecar 合并、§15.3 读回。"""
    session = make_session(client)
    sid = session["id"]
    client.post(f"/api/sessions/{sid}/notes", json={"tsMs": 1000, "text": "会话期批注"})
    client.post(f"/api/sessions/{sid}/labels", json={"tsMs": 2000, "name": "战斗场景", "color": "#FF5252"})
    resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": "PD_RUN_MERGE"})
    run_id = resp.json()["runId"]

    # save 收口后会话侧不再可写（§15.2 状态门），run 侧标注仍可写
    err = _error(client.post(f"/api/sessions/{sid}/notes", json={"tsMs": 9, "text": "x"}))
    assert err["code"] == "session_state_conflict"
    r = client.post(f"/api/runs/{run_id}/notes", json={"tsMs": 3000, "text": "run 侧批注"})
    assert r.status_code == 201, r.text
    assert r.json() == {"tsMs": 3000, "kind": "note", "text": "run 侧批注"}  # 落盘同形（无 id）

    # §15.3 读回 = 会话期标记 + run 侧标注合并（tsMs 升序）
    body = client.get(f"/api/runs/{run_id}/markers").json()
    assert [(m["tsMs"], m["kind"]) for m in body["markers"]] == [
        (1000, "note"),
        (2000, "label"),
        (3000, "note"),
    ]
    assert body["markers"][2]["text"] == "run 侧批注"

    # sidecar 落盘契约（15.1）：升序、无 id/创建序占位
    lines = [json.loads(line) for line in (client.runs_dir / "PD_RUN_MERGE" / "markers.ndjson").read_text(encoding="utf-8").splitlines()]
    assert lines == body["markers"]
    assert all("id" not in m and "_order" not in m for m in lines)

    # §19 replay 聚合 markers 段同源（有标记 → 15.3 读回体）
    replay = client.get(f"/api/runs/{run_id}/replay").json()
    assert replay["markers"] == body


def test_run_markers_404_and_bad_line_tolerance(client: TestClient) -> None:
    assert client.get("/api/runs/run-PD_GHOST/markers").status_code == 404
    run_dir = client.runs_dir / "PD_BADLINE"
    run_dir.mkdir(parents=True)
    (run_dir / "markers.ndjson").write_text(
        '{"tsMs":1,"kind":"mark"}\nnot-json\n{"tsMs":2,"kind":"alien"}\n',
        encoding="utf-8",
    )
    body = client.get("/api/runs/run-PD_BADLINE/markers").json()
    assert body["markers"] == [{"tsMs": 1, "kind": "mark"}]


# ---- §19 回放聚合 markers 段（sidecar 同源）----


def test_replay_includes_markers_and_summary_reference(client: TestClient) -> None:
    import time

    session = make_session(client)
    sid = session["id"]
    time.sleep(0.25)  # 留出 synthetic 采样窗口 → save 必有数值样本 → summary 落盘
    client.post(f"/api/sessions/{sid}/labels", json={"tsMs": 1000, "name": "战斗", "color": "#FF5252"})
    client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 2000})
    resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": "PD_REPLAY_MKR"})
    run_id = resp.json()["runId"]

    replay = client.get(f"/api/runs/{run_id}/replay").json()
    assert replay["markers"] == {
        "schemaVersion": "perfdog.markers.v1",
        "runId": run_id,
        "markers": [
            {"tsMs": 1000, "kind": "label", "name": "战斗", "color": "#FF5252"},
            {"tsMs": 2000, "kind": "mark"},
        ],
    }
    # summary 引用不内联（契约 §19）：有数值样本 → summaryAvailable True
    assert replay["summary"]["summaryAvailable"] is True
    assert replay["summary"]["statisticsUrl"] == f"/api/runs/{run_id}/statistics"
    assert replay["clockAnchors"] is None  # synthetic 无锚点 → null（形状保留）
    # series 覆盖全部已采集指标（§7 形状）
    assert replay["series"], "synthetic 会话应有采样点"
    for entry in replay["series"]:
        assert set(entry) >= {"metricId", "unit", "points"}
        assert entry["points"], "series 点列非空"
    assert replay["durationMs"] > 0
