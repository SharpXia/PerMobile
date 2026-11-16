"""run 侧标注（契约 §15.2 run 侧扩展：POST /runs/{id}/labels|notes|marks，
report-annotate-frontend 流）。

- POST 追加 → §15.3 读回含新标注（无 sidecar 时增量写建立文件）；
- body 校验复用 §15.2 会话端点同一实现（validate_marker_body）→ 非法 body 400；
- 未知 run → 404；tsMs 缺省 = 服务端当前 epoch ms；
- 增量写 = 读 sidecar → 追加 → 按 15.1 排序 → 临时文件+rename 原子替换；
  既有坏行按 §15.3 读回容错口径在增量重写时剔除（非静默丢数据，读回同口径）。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import make_session


def _error(resp) -> dict:
    assert resp.headers["content-type"].startswith("application/json")
    return resp.json()["error"]


def test_run_side_note_append_and_read_back(client: TestClient) -> None:
    session = make_session(client)
    resp = client.post(f"/api/sessions/{session['id']}/save", json={"caseName": "PD_RUNANN"})
    run_id = resp.json()["runId"]
    # save 无标记 → 无 sidecar；POST 追加建立文件
    r = client.post(f"/api/runs/{run_id}/notes", json={"tsMs": 5000, "text": "此处启动慢"})
    assert r.status_code == 201, r.text
    assert r.json() == {"tsMs": 5000, "kind": "note", "text": "此处启动慢"}  # 无 id（§15.1 落盘同形）
    body = client.get(f"/api/runs/{run_id}/markers").json()
    assert body == {
        "schemaVersion": "perfdog.markers.v1",
        "runId": run_id,
        "markers": [{"tsMs": 5000, "kind": "note", "text": "此处启动慢"}],
    }

    # 二次追加（乱序 tsMs + 同 tsMs 追加）→ 排序按 15.1：tsMs 升序、同 tsMs 按创建序
    client.post(f"/api/runs/{run_id}/marks", json={"tsMs": 1000})
    client.post(f"/api/runs/{run_id}/labels", json={"tsMs": 3000, "name": "战斗场景", "color": "#FF5252"})
    client.post(f"/api/runs/{run_id}/notes", json={"tsMs": 5000, "text": "同刻后写"})
    body = client.get(f"/api/runs/{run_id}/markers").json()
    kinds = [(m["tsMs"], m["kind"]) for m in body["markers"]]
    assert kinds == [(1000, "mark"), (3000, "label"), (5000, "note"), (5000, "note")]
    assert body["markers"][1]["name"] == "战斗场景" and body["markers"][1]["color"] == "#FF5252"
    assert body["markers"][2]["text"] == "此处启动慢"  # 同 tsMs：先写的在前（创建序）
    assert body["markers"][3]["text"] == "同刻后写"

    # sidecar 与 §15.3 读回同源；落盘无 id/创建序占位；增量写为整文件重写（4 行而非行数翻倍）
    lines = [json.loads(line) for line in (client.runs_dir / "PD_RUNANN" / "markers.ndjson").read_text(encoding="utf-8").splitlines()]
    assert lines == body["markers"]
    assert len(lines) == 4
    assert all("id" not in m and "_order" not in m for m in lines)


def test_run_side_annotation_validation_and_404(client: TestClient) -> None:
    # 未知 run → 404 report_not_found（resolve_run_dir 防护同 §12/§13.1 口径）
    assert client.post("/api/runs/run-PD_GHOST/notes", json={"text": "x"}).status_code == 404
    assert client.post("/api/runs/run-PD_GHOST/labels", json={"name": "x"}).status_code == 404
    assert client.post("/api/runs/run-PD_GHOST/marks", json={"tsMs": 1}).status_code == 404
    # runId 归一为 ".." → 404（resolve_run_dir 拒绝，不写文件）
    assert client.post("/api/runs/run-../notes", json={"text": "x"}).status_code == 404

    session = make_session(client)
    resp = client.post(f"/api/sessions/{session['id']}/save", json={"caseName": "PD_RUNANN3"})
    run_id = resp.json()["runId"]

    # note 缺/空 text（契约 15.2 错误清单）
    err = _error(client.post(f"/api/runs/{run_id}/notes", json={"tsMs": 1}))
    assert err["code"] == "validation_error" and err["details"]["field"] == "text"
    err = _error(client.post(f"/api/runs/{run_id}/notes", json={"text": "  "}))
    assert err["details"]["field"] == "text"
    # label 缺 name
    err = _error(client.post(f"/api/runs/{run_id}/labels", json={"tsMs": 1}))
    assert err["details"]["field"] == "name"
    # tsMs 非整数（字符串/浮点/布尔）
    for bad in ("abc", 1.5, True):
        err = _error(client.post(f"/api/runs/{run_id}/marks", json={"tsMs": bad}))
        assert err["code"] == "validation_error" and err["details"]["field"] == "tsMs"
    # 400 后 sidecar 不被写出（校验在写盘前）
    assert not (client.runs_dir / "PD_RUNANN3" / "markers.ndjson").exists()

    # tsMs 缺省 = 服务端当前 epoch ms（与 §15.2 会话端点同语义）
    r = client.post(f"/api/runs/{run_id}/marks", json={})
    assert r.status_code == 201
    assert isinstance(r.json()["tsMs"], int) and r.json()["kind"] == "mark"


def test_run_side_append_atomic_rewrite_drops_bad_lines(client: TestClient) -> None:
    """增量写 = 整文件重写（临时文件+rename）：坏行按 §15.3 读回口径剔除，无 .tmp 残留。"""
    run_dir = client.runs_dir / "PD_RUNANN4"
    run_dir.mkdir(parents=True)
    (run_dir / "markers.ndjson").write_text(
        '{"tsMs":1,"kind":"mark"}\nnot-json\n{"tsMs":2,"kind":"alien"}\n',
        encoding="utf-8",
    )
    run_id = "run-PD_RUNANN4"
    r = client.post(f"/api/runs/{run_id}/notes", json={"tsMs": 2, "text": "n"})
    assert r.status_code == 201, r.text

    sidecar_text = (run_dir / "markers.ndjson").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in sidecar_text.splitlines()]
    # not-json 与 kind=alien 两坏行被剔除（读回容错同口径）；合法行保留 + 新标注
    assert lines == [{"tsMs": 1, "kind": "mark"}, {"tsMs": 2, "kind": "note", "text": "n"}]
    body = client.get(f"/api/runs/{run_id}/markers").json()
    assert [m["tsMs"] for m in body["markers"]] == [1, 2]
    # 原子替换后无临时文件残留
    assert not (run_dir / "markers.ndjson.tmp").exists()
