"""契约一致性测试（test_contract_examples，ARCH §7）：契约示例 JSON 作 golden 逐端点对拍。

golden 值取自 API_CONTRACT.md §2/§4/§5/§6/§8；结构必须一致，按契约 §11.1 允许
服务端附加字段/类型（消费方忽略未知字段）。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_session

# ---- 契约示例（API_CONTRACT.md 原文，golden 源）----


def _assert_subset(expected: dict, actual: dict, path: str = "") -> None:
    """递归校验 golden 字段/类型/值一致（额外键放行）。"""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: 期望对象"
        for key, sub in expected.items():
            assert key in actual, f"{path}.{key} 缺失（契约要求字段）"
            _assert_subset(sub, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: 期望数组"
        for idx, sub in enumerate(expected):
            assert len(actual) > idx, f"{path}[{idx}] 缺失"
            _assert_subset(sub, actual[idx], f"{path}[{idx}]")
    elif expected is not None:
        assert actual == expected, f"{path}: {actual!r} != golden {expected!r}"


def test_contract_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    _assert_subset(
        {
            "status": "ok",
            "version": "1.0.0",
            "backend": {"framework": "fastapi"},
            "providers": {"devices": "synthetic", "metrics": "synthetic"},
        },
        body,
    )


def test_contract_devices(client: TestClient) -> None:
    body = client.get("/api/devices").json()
    _assert_subset(
        {
            "mode": "synthetic",
            "devices": [
                {
                    "id": "synthetic-001",
                    "platform": "android",
                    "model": "Synthetic Device 001",
                    "serial": "synthetic-001",
                    "state": "online",
                    "connection": "usb",
                    "info": {
                        "os": "Android 14 (synthetic)",
                        "cpu": "8 cores (synthetic)",
                        "resolution": "1080x2400",
                        "batteryLevel": 87,
                        "refreshRateHz": 120,
                    },
                }
            ],
        },
        body,
    )


def test_contract_apps(client: TestClient) -> None:
    body = client.get("/api/devices/synthetic-001/apps").json()
    _assert_subset(
        {
            "deviceId": "synthetic-001",
            "apps": [
                {
                    "packageName": "com.example.synthetic.game",
                    "pid": 12345,
                    "running": True,
                    "isForeground": True,
                    "processes": [
                        {"pid": 12345, "name": "com.example.synthetic.game", "isMain": True}
                    ],
                }
            ],
        },
        body,
    )


def test_contract_metrics_entries(client: TestClient) -> None:
    body = client.get("/api/metrics").json()
    by_id = {m["metricId"]: m for m in body["metrics"]}
    # 契约 §5 示例三条逐字段
    _assert_subset(
        {"metricId": "cpu.total.pct", "unit": "%", "direction": "lower_better",
         "evidenceLevel": "L1", "available": True},
        by_id["cpu.total.pct"],
    )
    _assert_subset(
        {"metricId": "frame.fps.mean", "unit": "fps", "direction": "higher_better",
         "evidenceLevel": "L1", "available": True},
        by_id["frame.fps.mean"],
    )
    _assert_subset(
        {"metricId": "gpu.busy.pct", "unit": "%", "direction": "lower_better",
         "evidenceLevel": "L2", "available": False},
        by_id["gpu.busy.pct"],
    )


def test_contract_create_session_and_errors(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [
            {"metricId": "cpu.total.pct", "intervalMs": 1000},
            {"metricId": "memory.pss.peak_mb", "intervalMs": 2000},
        ],
        "interactive": False,
        "launchOnCreate": False,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("sess-")
    assert body["state"] == "monitoring"
    assert body["deviceId"] == "synthetic-001"
    assert body["packageName"] == "com.example.synthetic.game"
    assert body["metrics"] == [
        {"metricId": "cpu.total.pct", "intervalMs": 1000},
        {"metricId": "memory.pss.peak_mb", "intervalMs": 2000},
    ]
    assert isinstance(body["createdAt"], int)
    # 契约 §3 错误体 golden：非法迁移（stopped 后再 record/start）
    client.post(f"/api/sessions/{body['id']}/record/start")
    client.post(f"/api/sessions/{body['id']}/record/stop")
    err = client.post(f"/api/sessions/{body['id']}/record/start")
    assert err.status_code == 409
    err_body = err.json()
    assert set(err_body) == {"error"}
    assert set(err_body["error"]) >= {"code", "message", "details"}
    assert err_body["error"]["code"] == "session_state_conflict"
    assert err_body["error"]["details"]["state"] == "stopped"


def test_contract_save_response(client: TestClient, tmp_path) -> None:
    sid = make_session(client)["id"]
    time.sleep(0.2)
    resp = client.post(f"/api/sessions/{sid}/save", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"sessionId", "runId", "state", "runPath"}
    assert body["sessionId"] == sid
    assert body["state"] == "saved"
    assert body["runId"].startswith("run-PD_")
    assert body["runPath"].startswith("/")


def test_contract_metrics_interval_query(client: TestClient) -> None:
    sid = make_session(client)["id"]
    time.sleep(0.3)
    body = client.get(f"/api/sessions/{sid}/metrics").json()
    assert body["sessionId"] == sid
    assert isinstance(body["fromMs"], int) and isinstance(body["toMs"], int)
    assert body["downsampled"] is False
    series = body["series"]
    assert series and isinstance(series, list)
    entry = series[0]
    assert set(entry) >= {"metricId", "unit", "points"}
    assert all(set(p) == {"ts", "value"} for p in entry["points"])


def test_contract_sse_stream_golden(client: TestClient) -> None:
    """契约 §8.4 示例流：hello/sample/session.state 信封逐字段对拍。"""
    sid = make_session(client)["id"]
    client.post(f"/api/sessions/{sid}/record/start")
    events = []
    # record/start 在连接前 → 带 Last-Event-ID="0" 重放历史（含 session.state）
    with client.stream("GET", f"/api/sessions/{sid}/events", headers={"Last-Event-ID": "0"}) as resp:
        current = None
        for line in resp.iter_lines():
            if line is None:
                continue
            text = line.rstrip("\r")
            if text.startswith("event:"):
                current = text.removeprefix("event:").strip()
            elif text.startswith("data:"):
                env = json.loads(text.removeprefix("data:").strip())
                env["_event"] = current
                events.append(env)
                if len(events) >= 6:
                    break
            elif text.startswith(":"):
                continue
    types = [e["type"] for e in events]
    assert types[0] == "hello"
    assert "sample" in types
    assert "session.state" in types
    for env in events:
        # 契约 §8.2 信封 golden
        assert env["v"] == 1
        assert env["type"] == env["_event"]
        assert env["sessionId"] == sid
        assert isinstance(env["seq"], int)
        assert isinstance(env["ts"], int)
    sample = next(e for e in events if e["type"] == "sample")
    # 契约 §8.3 sample 载荷 golden：metricId/value/unit/tags
    for key in ("metricId", "value", "unit", "tags"):
        assert key in sample
    state = next(e for e in events if e["type"] == "session.state")
    assert state["state"] == "recording"


# ---- 契约 §12 统计量端点 golden（perfdog.statistics.v1，M2，契约 §11.5 新增用例）----
#
# 数值口径说明：§12 示例中的 fps/jank 数值是「完整形态」示意值，契约未给出产生它
# 的输入帧序列，无法也不应以构造数据凑值；逐位数值 golden 由固定输入的
# test_statistics.py / test_statistics_endpoints.py 承担（[R§5] fixture 对拍）。
# 本文件同步的是示例中**与数据无关的可判定部分**：schemaVersion/基线声明/间隔、
# null 字段、note 原文与键集（_assert_subset 对 None 期望只校验键存在）。


def _frame_timeline_events(run_id: str, count: int) -> list[dict]:
    """与 build_frame_detail_events（gfxinfo.py:412-478）同形的逐帧 counter 事件。"""
    from app.stats import generate_frame_events

    events = []
    for seq, frame in enumerate(generate_frame_events("contract-golden", count), start=1):
        events.append(
            {
                "schemaVersion": "perfeval.timeline.v1",
                "seq": seq,
                "runId": run_id,
                "eventId": f"{run_id}-frame-{seq:03d}",
                "type": "counter",
                "name": "frame.duration.detail.ms",
                "tsMonotonicNs": int(frame.ts_ms * 1_000_000),
                "source": {
                    "platform": "android",
                    "collector": "synthetic.fixture",
                    "evidenceLevel": "L1",
                },
                "data": {
                    "value": float(frame.duration_ms),
                    "unit": "ms",
                    "direction": "lower_better",
                    "frameIndex": seq - 1,
                    "jank": False,
                    "clockDomain": "frame_relative",
                    "fpsSource": "framestats",
                },
            }
        )
    return events


def test_contract_run_statistics_frame_events_golden(client: TestClient) -> None:
    from perfeval.core.timeline import write_timeline

    from app.services.statistics_service import SMOOTH_NOTE_COMPUTED

    run_dir = client.runs_dir / "PD_20261031_120001"
    run_dir.mkdir(parents=True)
    with (run_dir / "timeline.ndjson").open("w", encoding="utf-8") as sink:
        write_timeline(_frame_timeline_events("run-PD_20261031_120001", 120), sink)

    resp = client.get("/api/runs/run-PD_20261031_120001/statistics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 契约 §12 run golden 骨架（frameDataBasis="frameEvents"，逐帧齐全时的完整形态）
    _assert_subset(
        {
            "schemaVersion": "perfdog.statistics.v1",
            "runId": "run-PD_20261031_120001",
            "frameDataBasis": "frameEvents",
            "frameSeriesIntervalMs": 1000,
        },
        body,
    )
    assert isinstance(body["durationMs"], int) and body["durationMs"] > 0
    fps = body["fps"]
    assert set(fps) >= {"avg", "var", "std", "drop", "min", "median", "medRange", "ge"}
    assert list(fps["ge"]) == ["18", "25"]  # 缺省阈值；键按数值升序
    for key in ("avg", "var", "std", "drop", "min", "median", "medRange"):
        assert isinstance(fps[key], (int, float)), key
    frame_time = body["frameTime"]
    assert set(frame_time) >= {"avg", "std", "var", "delta"}
    assert all(isinstance(frame_time[k], (int, float)) for k in ("avg", "std", "var", "delta"))
    jank = body["jank"]
    assert set(jank) >= {"small", "big", "jank", "stutter", "per10min"}
    assert all(isinstance(jank[k], int) for k in ("small", "big", "jank"))
    assert all(isinstance(jank[k], (int, float)) for k in ("stutter", "per10min"))
    assert isinstance(body["lowFps"]["p1"], (int, float))
    # smooth.note 恒带近似口径原文；可精确复刻组不带 note（契约 §12 规则 1）
    assert body["smooth"]["note"] == SMOOTH_NOTE_COMPUTED
    assert "note" not in fps and "note" not in frame_time
    assert "note" not in jank and "note" not in body["lowFps"]


def test_contract_session_statistics_fps_series_golden(client: TestClient) -> None:
    import time as _time

    from app.services.statistics_service import (
        FTIME_NOTE_FPS_SERIES,
        JANK_NOTE_FPS_SERIES,
        LOW_FPS_NOTE_FPS_SERIES,
        SMOOTH_NOTE_FPS_SERIES,
    )

    sid = make_session(client, metrics=[{"metricId": "frame.fps.mean", "intervalMs": 500}])["id"]
    _time.sleep(1.3)
    resp = client.get(f"/api/sessions/{sid}/statistics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 契约 §12 session golden（frameDataBasis="fpsSeries"，synthetic 会话典型形态）
    _assert_subset(
        {
            "schemaVersion": "perfdog.statistics.v1",
            "sessionId": sid,
            "frameDataBasis": "fpsSeries",
            "frameSeriesIntervalMs": 500,
            "frameTime": {
                "avg": None, "std": None, "var": None, "delta": None,
                "note": FTIME_NOTE_FPS_SERIES,
            },
            "jank": {
                "small": None, "big": None, "jank": None, "stutter": None, "per10min": None,
                "note": JANK_NOTE_FPS_SERIES,
            },
            "smooth": {"value": None, "note": SMOOTH_NOTE_FPS_SERIES},
            "lowFps": {"p1": None, "note": LOW_FPS_NOTE_FPS_SERIES},
        },
        body,
    )
    # fps 组数值为数据依赖（契约未给输入序列），按类型与 note 原文对拍
    fps = body["fps"]
    assert set(fps) >= {"avg", "var", "std", "drop", "min", "median", "medRange", "ge", "note"}
    assert list(fps["ge"]) == ["18", "25"]
    assert isinstance(fps["avg"], (int, float)) and isinstance(fps["drop"], (int, float))
    assert fps["note"] == (
        "fps 点列间隔 500ms（frame.fps.mean 轮询），与 PerfDog 1 秒点列口径存在节拍差；"
        "Drop(FPS) 等相邻点统计受节拍影响，报告须声明间隔（[F§3.1]）"
    )


def test_contract_run_statistics_none_baseline_golden(client: TestClient) -> None:
    from app.services.statistics_service import (
        FPS_NOTE_NONE,
        FTIME_NOTE_NONE,
        JANK_NOTE_NONE,
        LOW_FPS_NOTE_NONE,
        SMOOTH_NOTE_NONE,
    )

    (client.runs_dir / "PD_20261031_120002").mkdir(parents=True)
    resp = client.get("/api/runs/run-PD_20261031_120002/statistics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runId"] == "run-PD_20261031_120002"
    # 契约 §12 frameDataBasis="none" golden：全组 null + note（None 期望只校验键存在）
    _assert_subset(
        {
            "schemaVersion": "perfdog.statistics.v1",
            "frameDataBasis": "none",
            "frameSeriesIntervalMs": None,
            "durationMs": 0,
            "fps": {
                "avg": None, "var": None, "std": None, "drop": None, "min": None,
                "median": None, "medRange": None,
                "ge": {"18": None, "25": None},
                "note": FPS_NOTE_NONE,
            },
            "frameTime": {
                "avg": None, "std": None, "var": None, "delta": None,
                "note": FTIME_NOTE_NONE,
            },
            "jank": {
                "small": None, "big": None, "jank": None, "stutter": None, "per10min": None,
                "note": JANK_NOTE_NONE,
            },
            "smooth": {"value": None, "note": SMOOTH_NOTE_NONE},
            "lowFps": {"p1": None, "note": LOW_FPS_NOTE_NONE},
        },
        body,
    )


# ---- 契约 §13.1/§13.2 Excel 导出与 save 导出扩展 golden（M2，契约 §11.5 新增用例）----


def test_contract_save_export_excel_golden(client: TestClient) -> None:
    """契约 §13.2 golden：save exportExcel=true 成功形态——响应在 §6 四字段之上新增
    `exportPath`；`exportError` 仅失败时出现、与 `exportPath` 互斥。"""
    from pathlib import Path

    sid = make_session(client)["id"]
    time.sleep(0.2)
    resp = client.post(
        f"/api/sessions/{sid}/save",
        json={"caseName": "PD_20261031_120001", "exportExcel": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_subset(
        {
            "sessionId": sid,
            "runId": "run-PD_20261031_120001",
            "state": "saved",
        },
        body,
    )
    assert "exportPath" in body, "exportExcel=true 成功时必须出现 exportPath（契约 §13.2）"
    assert "exportError" not in body  # 二者互斥
    export_path = Path(body["exportPath"])
    assert export_path.is_absolute()
    assert export_path.name == "PD_20261031_120001.xlsx"  # <runs-root>/<caseName>.xlsx
    assert export_path.is_file()


def test_contract_export_xlsx_endpoint(client: TestClient) -> None:
    """契约 §13.1 端点形态：200 二进制 + Content-Type + attachment 文件名
    （run 目录名 + .xlsx）；工作簿内部结构断言在 test_excel_export.py。"""
    sid = make_session(client)["id"]
    time.sleep(0.2)
    saved = client.post(
        f"/api/sessions/{sid}/save", json={"caseName": "PD_20261031_120001"}
    ).json()
    resp = client.get(f"/api/runs/{saved['runId']}/export.xlsx")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert resp.headers["content-disposition"] == (
        'attachment; filename="PD_20261031_120001.xlsx"'
    )
    assert resp.content[:2] == b"PK"  # xlsx（zip 容器）magic

    missing = client.get("/api/runs/run-NO_SUCH_RUN/export.xlsx")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "report_not_found"


# ---- 契约 §14 case 库 golden（M3，契约 §11.5 新增用例）----
#
# 数值口径说明：§14 示例中的 stats 数值（avgFps=112.3 等）是「完整形态」示意值，
# 契约未给出产生它的输入序列，无法也不应以构造数据凑值（与 §12 同一处理原则，
# 见本文件 §12 注释）；本文件同步示例中**可复现与可判定部分**：runId/caseName/
# device/packageName/summaryAvailable（synthetic save 路径可复现）、键集与类型。
# stats 快照与统计端点同源的数值对拍在 test_case_index.py。

CASE_STATS_KEYS = {"avgFps", "smooth", "jankPer10min", "peakMemoryMb", "avgAppCpuPct", "avgPowerMa"}


def _save_contract_run(client: TestClient) -> dict:
    sid = make_session(client)["id"]
    time.sleep(0.2)
    resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": "PD_20261031_120001"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_contract_cases_list_golden(client: TestClient) -> None:
    """契约 §14 GET /api/cases golden：save 成功自动登记（§6 注），结构与示例一致。"""
    saved = _save_contract_run(client)
    resp = client.get("/api/cases")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # golden 顶层键：cases 数组 + total（筛选后总数）
    assert set(body) >= {"cases", "total"}
    assert body["total"] == 1
    _assert_subset(
        {
            "runId": "run-PD_20261031_120001",
            "caseName": "PD_20261031_120001",
            "device": "synthetic-001",
            "packageName": "com.example.synthetic.game",
            "summaryAvailable": True,
        },
        body["cases"][0],
    )
    row = body["cases"][0]
    assert isinstance(row["createdAt"], int)
    assert isinstance(row["path"], str) and row["path"].startswith("/")  # 绝对路径
    assert row["path"] == saved["runPath"]
    assert set(row["stats"]) == CASE_STATS_KEYS  # 快照键集；无数据字段为 null，不填 0
    assert all(
        row["stats"][key] is None or isinstance(row["stats"][key], (int, float))
        for key in CASE_STATS_KEYS
    )


def test_contract_cases_rebuild_golden(client: TestClient) -> None:
    """契约 §14 POST /api/cases/rebuild golden：幂等重建，{"rebuiltAt", "total"}。"""
    _save_contract_run(client)
    for _ in range(2):  # 两次调用结果一致（幂等）
        resp = client.post("/api/cases/rebuild")
        assert resp.status_code == 200, resp.text
        _assert_subset({"total": 1}, resp.json())
        assert isinstance(resp.json()["rebuiltAt"], int)


def test_contract_cases_patch_rename_golden(client: TestClient) -> None:
    """契约 §14 PATCH /api/cases/{runId} golden：返回更新后的 case 对象
    （结构与 GET 元素一致）；只改显示名，runId 与 run 目录不变。"""
    _save_contract_run(client)
    resp = client.patch("/api/cases/run-PD_20261031_120001", json={"caseName": "一轮回归基线"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_subset(
        {
            "runId": "run-PD_20261031_120001",
            "caseName": "一轮回归基线",
            "device": "synthetic-001",
            "packageName": "com.example.synthetic.game",
            "summaryAvailable": True,
        },
        body,
    )
    assert isinstance(body["createdAt"], int)
    assert body["path"].startswith("/") and body["path"].endswith("/PD_20261031_120001")
    assert set(body["stats"]) == CASE_STATS_KEYS
    # 错误：404 report_not_found、400 validation_error（caseName 缺失/空/非字符串）
    missing = client.patch("/api/cases/run-NOPE", json={"caseName": "x"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "report_not_found"
    for payload in ({}, {"caseName": ""}, {"caseName": 42}):
        bad = client.patch("/api/cases/run-PD_20261031_120001", json=payload)
        assert bad.status_code == 400, payload
        assert bad.json()["error"]["code"] == "validation_error"


def test_contract_cases_delete(client: TestClient) -> None:
    """契约 §14 DELETE /api/cases/{runId}：204（事实源 run 目录 + 索引行 +
    同名 .xlsx 一次删净，与契约文本同口径）；已删除再删 → 404 report_not_found。"""
    _save_contract_run(client)
    first = client.delete("/api/cases/run-PD_20261031_120001")
    assert first.status_code == 204
    assert first.content == b""
    second = client.delete("/api/cases/run-PD_20261031_120001")
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "report_not_found"


# ---- 契约 §9 compare 数据面（M3 落地；契约 §11.5 新增用例）----


def test_contract_reports_compare_passthrough(client: TestClient) -> None:
    """契约 §9 POST /api/reports/compare：perfeval.compare.v1 原样透出（不加壳），
    顶层附可选 meta（§9 M3 扩展，§11.1 不升版本）。前提错配由引擎结果字段承载
    （HTTP 仍 200）；<1 条 baseline → 400；run 缺失 → 404 report_not_found。"""
    ids = []
    for case_name in ("PD_20261031_120001", "PD_20261031_120002"):
        sid = make_session(client)["id"]
        time.sleep(0.2)
        saved = client.post(f"/api/sessions/{sid}/save", json={"caseName": case_name}).json()
        ids.append(saved["runId"])
    baseline_id, candidate_id = ids

    resp = client.post(
        "/api/reports/compare", json={"baselineRunIds": [baseline_id], "candidateRunId": candidate_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # perfeval.compare.v1 透传体（[R§3.3] compare_summaries 的顶层键）
    assert body["schemaVersion"] == "perfeval.compare.v1"
    for key in ("compareId", "mode", "baselineGroup", "candidateGroup", "status", "metrics", "reliability"):
        assert key in body, key
    assert isinstance(body["status"], str) and body["status"] in ("pass", "warn", "fail", "unknown", "invalid")
    # §9 M3 扩展可选 meta：各 runId 的 caseName/device/时长 + 可比前提提示
    meta = body["meta"]
    assert [entry["runId"] for entry in meta["baseline"]] == [baseline_id]
    assert meta["candidate"][0]["runId"] == candidate_id
    assert meta["baseline"][0]["caseName"] == "PD_20261031_120001"
    assert isinstance(meta["candidate"][0]["durationMs"], int)
    assert isinstance(meta["preconditionNote"], str)

    # <1 条 baseline → 400 validation_error
    bad = client.post("/api/reports/compare", json={"baselineRunIds": [], "candidateRunId": candidate_id})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "validation_error"
    # run 缺失 → 404 report_not_found
    gone = client.post(
        "/api/reports/compare", json={"baselineRunIds": ["run-NOPE"], "candidateRunId": candidate_id}
    )
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "report_not_found"


def test_contract_runs_list_golden(client: TestClient) -> None:
    """契约 §9 GET /api/runs golden：runs 目录实时轻索引（saved 会话出现）。

    golden 修正注（M8+ 实装同步，契约 §11.4）：原示例 ``device`` 写作展示名
    "Synthetic Device 001"，实装按 manifest `device` 字段（device_id 字符串，
    §14 /api/cases 行同源）透出——示例已随本测试改为 "synthetic-001"。
    """
    saved = _save_contract_run(client)
    resp = client.get("/api/runs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["runs"], list) and len(body["runs"]) == 1
    _assert_subset(
        {
            "runId": "run-PD_20261031_120001",
            "caseName": "PD_20261031_120001",
            "device": "synthetic-001",
            "packageName": "com.example.synthetic.game",
            "summaryAvailable": True,
        },
        body["runs"][0],
    )
    row = body["runs"][0]
    assert isinstance(row["createdAt"], int)
    assert row["path"] == saved["runPath"]
    # 轻索引不携带统计快照（与 §14 /api/cases 的分工，契约 §14 注）
    assert "stats" not in row


def test_contract_run_detail_golden(client: TestClient) -> None:
    """契约 §9 GET /api/runs/{runId} golden（M8+ 实装）：轻索引行 + manifest 原样
    + summary 原样 + artifacts 索引（§17 同源）。"""
    saved = _save_contract_run(client)
    resp = client.get(f"/api/runs/{saved['runId']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_subset(
        {
            "runId": "run-PD_20261031_120001",
            "caseName": "PD_20261031_120001",
            "device": "synthetic-001",
            "packageName": "com.example.synthetic.game",
            "summaryAvailable": True,
        },
        body,
    )
    # manifest 原样（perfeval.manifest.v1）与 summary 原样（perfeval.summary.v1）
    assert body["manifest"]["schemaVersion"] == "perfeval.manifest.v1"
    assert body["manifest"]["device"] == "synthetic-001"
    assert isinstance(body["summary"], dict)
    assert body["summary"]["schemaVersion"] == "perfeval.summary.v1"
    assert isinstance(body["artifacts"], list)
    # 404：不存在 run / 非法 runId
    for bad in ("run-NOPE", "run-..", "run-a/b"):
        gone = client.get(f"/api/runs/{bad}")
        assert gone.status_code == 404, bad
        assert gone.json()["error"]["code"] == "report_not_found"
# ---- 契约 §15-§19 M4/M5 golden 语料（2026-10-31 契约扩展，契约 §11.5 新增用例）----
#
# 同步原则（与 §12/§13/§14 先例一致，见 ARCHITECTURE.md §7 第 8 条）：M4/M5 端点
# 尚未实现（契约先行），本段先把契约 §15-§19 的示例 JSON 固化为**语料常量**并校验
# 「可 json.loads + 结构不变量」；端点行为对拍（响应与语料匹配、落盘/Range/聚合）
# 随 M4/M5 实现落地后以新增用例补入。语料值与契约文档逐字对应——改契约示例必须
# 连同本段一起改（契约 §11.4）。

MARKER_KINDS = ("label", "note", "mark")

MARKERS_GOLDEN: dict = {
    # 契约 §15.2 三端点请求/响应 golden
    "label_request": {"tsMs": 1798765435000, "name": "战斗场景", "color": "#FF5252"},
    "label_response": {
        "id": "mkr-01", "tsMs": 1798765435000, "kind": "label",
        "name": "战斗场景", "color": "#FF5252",
    },
    "note_request": {"tsMs": 1798765442000, "text": "此处加载卡顿明显"},
    "note_response": {"id": "mkr-02", "tsMs": 1798765442000, "kind": "note",
                      "text": "此处加载卡顿明显"},
    "mark_request": {"tsMs": 1798765450000},
    "mark_response": {"id": "mkr-03", "tsMs": 1798765450000, "kind": "mark"},
    # 契约 §15.2 GET /api/sessions/{id}/markers golden
    "session_list": {
        "sessionId": "sess-01",
        "markers": [
            {"id": "mkr-01", "tsMs": 1798765435000, "kind": "label",
             "name": "战斗场景", "color": "#FF5252"},
            {"id": "mkr-02", "tsMs": 1798765442000, "kind": "note",
             "text": "此处加载卡顿明显"},
            {"id": "mkr-03", "tsMs": 1798765450000, "kind": "mark"},
        ],
    },
    # 契约 §15.3 GET /api/runs/{runId}/markers golden（落盘元素无 id）
    "run_readback": {
        "schemaVersion": "perfdog.markers.v1",
        "runId": "run-PD_20261031_120001",
        "markers": [
            {"tsMs": 1798765435000, "kind": "label", "name": "战斗场景", "color": "#FF5252"},
            {"tsMs": 1798765442000, "kind": "note", "text": "此处加载卡顿明显"},
            {"tsMs": 1798765450000, "kind": "mark"},
        ],
    },
    # 契约 §15.1 sidecar 行示例（markers.ndjson 逐行形态）
    "sidecar_lines": [
        '{"tsMs":1798765435000,"kind":"label","name":"战斗场景","color":"#FF5252"}',
        '{"tsMs":1798765442000,"kind":"note","text":"此处加载卡顿明显"}',
        '{"tsMs":1798765450000,"kind":"mark"}',
    ],
    # 契约 §15.2 SSE marker 信封（data 行 JSON）
    "sse_marker_data": (
        '{"v":1,"seq":8,"ts":1798765435000,"type":"marker","sessionId":"sess-01",'
        '"action":"add","marker":{"id":"mkr-01","tsMs":1798765435000,"kind":"label",'
        '"name":"战斗场景","color":"#FF5252"}}'
    ),
}

SCREEN_RECORD_GOLDEN: dict = {
    # 契约 §16.1 POST /api/sessions 的 M4 扩展（§6 既有示例不变）
    "create_request": {
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [
            {"metricId": "cpu.total.pct", "intervalMs": 1000},
            {"metricId": "memory.pss.peak_mb", "intervalMs": 2000},
        ],
        "interactive": False,
        "launchOnCreate": False,
        "recordScreen": True,
    },
    "create_response": {
        "id": "sess-02",
        "state": "monitoring",
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [
            {"metricId": "cpu.total.pct", "intervalMs": 1000},
            {"metricId": "memory.pss.peak_mb", "intervalMs": 2000},
        ],
        "createdAt": 1798765432101,
        "recordScreen": True,
    },
    # 契约 §16.2 screenshot 201 响应 golden
    "screenshot_response": {
        "sessionId": "sess-01",
        "artifact": {"name": "screenshot-1798765440000.png", "kind": "screenshot",
                     "bytes": 482133},
    },
}

ARTIFACTS_GOLDEN: dict = {
    # 契约 §17 GET /api/runs/{runId}/artifacts golden
    "index": {
        "runId": "run-PD_20261031_120001",
        "artifacts": [
            {"name": "video.mp4", "kind": "video", "bytes": 10485760,
             "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
             "collector": "screenrecord"},
            {"name": "video-seg002.mp4", "kind": "video", "bytes": 8388608,
             "sha256": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
             "collector": "screenrecord"},
            {"name": "screenshot-1798765440000.png", "kind": "screenshot", "bytes": 482133,
             "sha256": "ef797c8118f02dfb649607dd5d3f8c7623048c9c063d532cc95c5ed7a898a64f",
             "collector": "screencap"},
        ],
    },
}

STATISTICS_RANGE_GOLDEN: dict = {
    # 契约 §18 区间统计 golden（frameEvents 基线区间形态；数值为示意值，
    # 可判定部分为 schemaVersion/基线/区间回显/durationMs/note 原文）
    "body": {
        "schemaVersion": "perfdog.statistics.v1",
        "runId": "run-PD_20261031_120001",
        "frameDataBasis": "frameEvents",
        "frameSeriesIntervalMs": 1000,
        "fromMs": 1798765440000,
        "toMs": 1798765460000,
        "durationMs": 20000,
        "fps": {
            "avg": 113.7, "var": 22.1, "std": 4.7, "drop": 90.0,
            "min": 89.5, "median": 115.0, "medRange": 98.3,
            "ge": {"18": 100.0, "25": 100.0},
        },
        "frameTime": {"avg": 8.8, "std": 0.4, "var": 0.16, "delta": 0.0},
        "jank": {"small": 0, "big": 0, "jank": 0, "stutter": 0.0, "per10min": 0.0},
        "smooth": {"value": 5.9,
                   "note": "近似口径：Smooth 官方未公开算法公式，按行为近似实现，"
                           "数值与 PerfDog 不可直接对比（[F§3.5][F§10.1]）"},
        "lowFps": {"p1": 15.2},
    },
}

REPLAY_GOLDEN: dict = {
    # 契约 §19 GET /api/runs/{runId}/replay golden
    "body": {
        "runId": "run-PD_20261031_120001",
        "caseName": "PD_20261031_120001",
        "durationMs": 62000,
        "series": [
            {"metricId": "frame.fps.mean", "unit": "fps",
             "points": [{"ts": 1798765432101, "value": 112.3},
                        {"ts": 1798765433102, "value": 111.8}]},
        ],
        "downsampled": False,
        "markers": {
            "schemaVersion": "perfdog.markers.v1",
            "markers": [
                {"tsMs": 1798765435000, "kind": "label", "name": "战斗场景",
                 "color": "#FF5252"},
                {"tsMs": 1798765450000, "kind": "mark"},
            ],
        },
        "artifacts": [ARTIFACTS_GOLDEN["index"]["artifacts"][0]],
        "clockAnchors": None,
        "summary": {"summaryAvailable": True,
                    "statisticsUrl": "/api/runs/run-PD_20261031_120001/statistics"},
    },
}

M4M5_GOLDEN_CORPUS: dict = {
    "markers": MARKERS_GOLDEN,
    "screen_record": SCREEN_RECORD_GOLDEN,
    "artifacts": ARTIFACTS_GOLDEN,
    "statistics_range": STATISTICS_RANGE_GOLDEN,
    "replay": REPLAY_GOLDEN,
}


def test_m4m5_golden_corpus_is_valid_json(client: TestClient) -> None:
    """契约 §15-§19 示例必须可 json.loads（硬性要求）：全部语料经
    dumps→loads 往返逐值相等；sidecar 行与 SSE data 行逐行 json.loads。"""
    for section_name, section in M4M5_GOLDEN_CORPUS.items():
        assert json.loads(json.dumps(section)) == section, section_name
    for line in MARKERS_GOLDEN["sidecar_lines"]:
        parsed = json.loads(line)
        assert parsed["kind"] in MARKER_KINDS
    sse = json.loads(MARKERS_GOLDEN["sse_marker_data"])
    assert sse["type"] == "marker" and sse["v"] == 1
    assert sse["action"] in ("add", "remove")


def test_markers_golden_schema(client: TestClient) -> None:
    """契约 §15.1/§15.3 结构不变量：落盘元素无 id；label 带 name、note 带 text、
    mark 无附加字段；run 读回按 tsMs 升序且 schemaVersion 定名。"""
    for element in MARKERS_GOLDEN["run_readback"]["markers"]:
        assert "id" not in element, "落盘元素不得携带 session-scoped id（契约 §15.1）"
        assert element["kind"] in MARKER_KINDS
    label = next(e for e in MARKERS_GOLDEN["run_readback"]["markers"] if e["kind"] == "label")
    note = next(e for e in MARKERS_GOLDEN["run_readback"]["markers"] if e["kind"] == "note")
    mark = next(e for e in MARKERS_GOLDEN["run_readback"]["markers"] if e["kind"] == "mark")
    assert isinstance(label["name"], str) and label["name"]
    assert isinstance(note["text"], str) and note["text"]
    assert set(mark) == {"tsMs", "kind"}
    ts_list = [e["tsMs"] for e in MARKERS_GOLDEN["run_readback"]["markers"]]
    assert ts_list == sorted(ts_list)
    # 会话侧列表（含 id）与 run 读回一一对应：id 之外字段一致
    for with_id, without_id in zip(MARKERS_GOLDEN["session_list"]["markers"],
                                   MARKERS_GOLDEN["run_readback"]["markers"]):
        assert {k: v for k, v in with_id.items() if k != "id"} == without_id
    assert MARKERS_GOLDEN["run_readback"]["schemaVersion"] == "perfdog.markers.v1"


def test_artifacts_golden_schema(client: TestClient) -> None:
    """契约 §17 索引结构不变量：kind 枚举、bytes 正整数、sha256 64 位 hex、
    录屏落位命名 video.mp4 / video-segNNN.mp4（§16.1 显式映射）。"""
    import re

    index = ARTIFACTS_GOLDEN["index"]
    video_names = []
    for entry in index["artifacts"]:
        assert entry["kind"] in ("video", "screenshot", "data")
        assert isinstance(entry["bytes"], int) and entry["bytes"] > 0
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert isinstance(entry["collector"], str) and entry["collector"]
    for entry in index["artifacts"]:
        if entry["kind"] == "video":
            video_names.append(entry["name"])
            assert entry["name"] == "video.mp4" or re.fullmatch(
                r"video-seg\d{3}\.mp4", entry["name"]
            ), entry["name"]
    assert video_names, "录屏 run 的 artifacts 索引必须含 video 条目（契约 §17）"


def test_statistics_range_golden_schema(client: TestClient) -> None:
    """契约 §18 区间统计可判定部分：区间回显 fromMs/toMs、durationMs=区间首尾差、
    smooth.note 与 §12 同一常量（单一事实源）。"""
    from app.services.statistics_service import SMOOTH_NOTE_COMPUTED

    body = STATISTICS_RANGE_GOLDEN["body"]
    assert body["schemaVersion"] == "perfdog.statistics.v1"
    assert body["durationMs"] == body["toMs"] - body["fromMs"]
    assert body["smooth"]["note"] == SMOOTH_NOTE_COMPUTED
    assert set(body["fps"]["ge"]) == {"18", "25"}


def test_replay_golden_schema(client: TestClient) -> None:
    """契约 §19 聚合结构不变量：series 为 §7 形状、markers 为 §15 sidecar 体、
    artifacts 与 §17 索引同源、summary 为引用不内联、clockAnchors 允许 null。"""
    body = REPLAY_GOLDEN["body"]
    assert set(body) >= {"runId", "caseName", "durationMs", "series", "downsampled",
                         "markers", "artifacts", "clockAnchors", "summary"}
    assert body["markers"]["schemaVersion"] == "perfdog.markers.v1"
    for marker in body["markers"]["markers"]:  # 与 §15 sidecar 同源（语料的子集）
        assert marker in MARKERS_GOLDEN["run_readback"]["markers"]
    for entry in body["artifacts"]:
        assert entry in ARTIFACTS_GOLDEN["index"]["artifacts"]  # 与索引同源同形
    for point in body["series"][0]["points"]:
        assert set(point) == {"ts", "value"}
    assert body["summary"]["statisticsUrl"].startswith("/api/runs/")
    assert "fps" not in body["summary"]  # 引用不内联：统计走 §12 端点（单一事实源）


# ==========================================================================
# §11.5 契约扩展（§15-§19，2026-10-31）端点级 golden：新增用例，既有 golden 不改。
# 传输层证据：端点实际响应须满足契约示例的可判定子集（fixture 字节数/sha256 与
# 示例示意值不可复算一致，按 §11.1 只断言结构/枚举/口径字段）。
# ==========================================================================


def test_contract_screen_record_create_echo(client: TestClient) -> None:
    """契约 §16.1：recordScreen 请求扩展 → 201 会话对象回显 recordScreen。"""
    resp = client.post("/api/sessions", json=SCREEN_RECORD_GOLDEN["create_request"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    _assert_subset(
        {"recordScreen": True, "state": "monitoring", "deviceId": "synthetic-001"},
        body,
    )
    # 缺省回显 false（§11.1 始终返回）；先释放设备占用
    client.post(f"/api/sessions/{body['id']}/discard")
    plain = make_session(client)
    assert plain["recordScreen"] is False


def test_contract_markers_endpoints_golden(tmp_path, monkeypatch) -> None:
    """契约 §15.2/§15.3：labels/notes/marks 201 回执、会话列表与 run 读回同源。"""
    from m4_fixtures import fake_screenrecord_runner, make_real_client, make_recording_session

    runner = fake_screenrecord_runner([], {"screen.mp4": 4096})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]
        r1 = client.post(
            f"/api/sessions/{sid}/labels",
            json={"tsMs": 1798765435000, "name": "战斗场景", "color": "#FF5252"},
        )
        r2 = client.post(
            f"/api/sessions/{sid}/notes",
            json={"tsMs": 1798765442000, "text": "此处加载卡顿明显"},
        )
        r3 = client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 1798765450000})
        assert (r1.status_code, r2.status_code, r3.status_code) == (201, 201, 201)
        label, note, mark = r1.json(), r2.json(), r3.json()
        # 201 回执 = 会话列表 golden 元素（含 id；id 为服务端分配句柄形态 mkr-NN）
        _assert_subset(MARKERS_GOLDEN["session_list"]["markers"][0], label)
        _assert_subset(MARKERS_GOLDEN["session_list"]["markers"][1], note)
        _assert_subset(MARKERS_GOLDEN["session_list"]["markers"][2], mark)
        assert label["id"].startswith("mkr-")

        # §15.2 会话列表 golden（升序、含 id）
        listing = client.get(f"/api/sessions/{sid}/markers").json()
        _assert_subset(
            {"sessionId": sid, "markers": MARKERS_GOLDEN["session_list"]["markers"]},
            listing,
        )

        # §15.3 run 读回 golden（sidecar 落盘无 id；runId 以实际 run 断言）
        resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": "PD_GOLDEN_MKR"})
        run_id = resp.json()["runId"]
        body = client.get(f"/api/runs/{run_id}/markers").json()
        assert body["runId"] == run_id
        _assert_subset(
            {
                "schemaVersion": "perfdog.markers.v1",
                "markers": MARKERS_GOLDEN["run_readback"]["markers"],
            },
            body,
        )


def test_contract_artifacts_and_video_golden(tmp_path, monkeypatch) -> None:
    """契约 §17：索引元素形状（name/kind/collector）、video 端点 Range 头。"""
    from m4_fixtures import (
        fake_screenrecord_runner,
        make_real_client,
        make_recording_session,
    )

    runner = fake_screenrecord_runner([], {"screen.mp4": 2048})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        session = make_recording_session(client)
        sid = session["id"]
        client.post(f"/api/sessions/{sid}/record/start")
        client.post(f"/api/sessions/{sid}/record/stop")
        run_id = client.post(f"/api/sessions/{sid}/save", json={}).json()["runId"]

        index = client.get(f"/api/runs/{run_id}/artifacts").json()
        assert index["runId"] == run_id
        _assert_subset({"name": "video.mp4", "kind": "video", "collector": "screenrecord"},
                       index["artifacts"][0])
        names = [a["name"] for a in index["artifacts"]]
        assert names == ["video.mp4"] or names[0] == "video.mp4"  # 首段在前（§17 示例顺序）

        # video 端点：200 全量 + Accept-Ranges；Range → 206 + Content-Range
        resp = client.get(f"/api/runs/{run_id}/video")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("video/mp4")
        assert resp.headers["accept-ranges"] == "bytes"
        total = int(resp.headers["content-length"])
        resp = client.get(f"/api/runs/{run_id}/video", headers={"Range": "bytes=0-0"})
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-0/{total}"
        # 分段选择 ?name=（分段 run 的契约用法）
        resp = client.get(
            f"/api/runs/{run_id}/video", params={"name": "video.mp4"},
            headers={"Range": f"bytes=-{total}"},
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-{total - 1}/{total}"


def test_contract_statistics_range_golden(client: TestClient) -> None:
    """契约 §18 golden（frameEvents 基线区间形态）：回显/durationMs/note 原文。"""
    from perfeval.core.timeline import write_timeline

    run_dir = client.runs_dir / "PD_GOLDEN_RANGE"
    run_dir.mkdir(parents=True)
    # frameEvents 基线：帧时戳用 §18 golden 的区间（epoch 域），末帧落在 toMs 上
    # → durationMs = 区间首尾点差 = 20000
    origin, span = 1798765440000, 1798765460000
    payloads = []
    durations = [8.4, 8.5, 8.3]
    ts = [origin, origin + 9990, span]  # 首帧=fromMs、末帧=toMs
    for seq, (t, d) in enumerate(zip(ts, durations), start=1):
        payloads.append(
            {
                "schemaVersion": "perfeval.timeline.v1",
                "seq": seq,
                "runId": "run-PD_GOLDEN_RANGE",
                "eventId": f"run-PD_GOLDEN_RANGE-frame-{seq:03d}",
                "type": "counter",
                "name": "frame.duration.detail.ms",
                "tsMonotonicNs": int(t) * 1_000_000,
                "source": {"platform": "android", "collector": "synthetic.fixture",
                           "evidenceLevel": "L1"},
                "data": {"value": d, "unit": "ms", "direction": "lower_better"},
            }
        )
    with (run_dir / "timeline.ndjson").open("w", encoding="utf-8") as sink:
        write_timeline(payloads, sink)

    body = client.get(
        "/api/runs/run-PD_GOLDEN_RANGE/statistics",
        params={"fromMs": origin, "toMs": span},
    ).json()
    expected_keys = {
        "schemaVersion", "runId", "frameDataBasis", "frameSeriesIntervalMs",
        "fromMs", "toMs", "durationMs",
    }
    assert expected_keys <= set(body)
    assert body["fromMs"] == origin and body["toMs"] == span
    assert body["durationMs"] == 20000  # 区间首尾点差（§18）
    assert body["frameDataBasis"] == "frameEvents"
    assert body["smooth"]["note"] == STATISTICS_RANGE_GOLDEN["body"]["smooth"]["note"]
    # 校验（§18）：fromMs>toMs → 400 + details.field
    err = client.get(
        "/api/runs/run-PD_GOLDEN_RANGE/statistics",
        params={"fromMs": span, "toMs": origin},
    )
    assert err.status_code == 400
    assert err.json()["error"]["code"] == "validation_error"


def test_contract_replay_golden(tmp_path, monkeypatch) -> None:
    """契约 §19：聚合体一次拉取——series/«markers»/artifacts/clockAnchors/summary。"""
    from m4_fixtures import fake_screenrecord_runner, make_real_client, make_recording_session

    runner = fake_screenrecord_runner([], {"screen.mp4": 2048})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        session = make_recording_session(client, record_screen=False)
        sid = session["id"]
        client.post(f"/api/sessions/{sid}/labels",
                    json={"tsMs": 1798765435000, "name": "战斗场景", "color": "#FF5252"})
        client.post(f"/api/sessions/{sid}/marks", json={"tsMs": 1798765450000})
        resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": "PD_GOLDEN_REPLAY"})
        run_id = resp.json()["runId"]

        body = client.get(f"/api/runs/{run_id}/replay").json()
        # §19 golden：markers 段元素与示例逐字段（label/mark 两元素；实现按
        # 「与 §15.3 读回体同形同源」附加 runId，subset 断言放行）
        _assert_subset(
            {
                "runId": run_id,
                "caseName": "PD_GOLDEN_REPLAY",
                "downsampled": False,
                "markers": REPLAY_GOLDEN["body"]["markers"],
                "clockAnchors": None,
                "summary": {
                    "summaryAvailable": False,
                    "statisticsUrl": f"/api/runs/{run_id}/statistics",
                },
            },
            body,
        )
        assert body["markers"]["schemaVersion"] == "perfdog.markers.v1"
        assert body["markers"]["runId"] == run_id  # 同源读回体（§15.3 形状）
        assert body["artifacts"] == []  # record_screen=False：无录屏工件
        for entry in body["series"]:
            assert set(entry) >= {"metricId", "unit", "points"}


# ---- 契约 §20-§22 M6/M7 golden 语料（2026-11-01 契约扩展，契约 §11.5 新增用例）----
#
# 同步原则与 §15-§19 先例一致（见本文件 M4/M5 段注释）：M6/M7 端点尚未实现（契约
# 先行），本段先把契约 §20-§22 示例 JSON 固化为**语料常量**并校验「可 json.loads +
# 结构不变量」；端点行为对拍（capabilities∩profile 交集判定、wifi-mode 501/诊断、
# registry 新指标可用性、startup/deep-analysis/diagnostics 形状与空结构 note、
# timed-screenshot 501/409/400）随 M6/M7 实现落地后以新增用例补入。语料值与契约
# 文档逐字对应——改契约示例必须连同本段一起改（契约 §11.4）。

REASON_CODES = ("kgsl_missing", "power_wifi_only", "not_landed", "platform_unsupported")
CONNECTION_MODES = ("usb", "wifi")
LOG_LEVELS = ("verbose", "debug", "info", "warn", "error")

CAPABILITY_GOLDEN: dict = {
    # 契约 §20.1 GET /api/metrics 每指标可选 capabilities（含 reason 码形态）
    "metric_gpu_busy": {
        "metricId": "gpu.busy.pct", "unit": "%", "direction": "lower_better",
        "evidenceLevel": "L2", "defaultIntervalMs": 2000, "available": False,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android",
                         "reason": "kgsl_missing"},
    },
    # 契约 §21.2 CPU 高阶 3 指标（§5 注册表扩展，capabilities + description）
    "metric_cpu_freq": {
        "metricId": "cpu.freq.mhz", "unit": "MHz", "direction": "none",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
    "metric_cpu_normalized": {
        "metricId": "cpu.proc.normalized.pct", "unit": "%", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 1000, "available": True,
        "description": ("规范化=未规范化×(Σ当前频率/Σ最大频率)（[F§3.6]），官方建议默认"
                        "展示规范化值；与 cpu.proc.pct 未规范化口径不可直接对比"),
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
    "metric_cpu_thread": {
        "metricId": "cpu.proc.thread.pct", "unit": "%", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
    # 契约 §21.3 内存 Detail 勾选组（官方损耗提示文案进 description）
    "metric_memory_detail_native": {
        "metricId": "memory.detail.native_mb", "unit": "MB", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
        "description": ("官方提示：极限测试（超高帧率）建议不勾选 Memory Detail，"
                        "部分低端机型有性能损耗（[F§3.8][I§2.4]）"),
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
    "memory_detail_group": [
        "memory.detail.native_mb", "memory.detail.dalvik_mb",
        "memory.detail.gfx_mb", "memory.detail.gl_mb",
    ],
    # 契约 §20.2 GET /api/devices 每设备可选 profile（synthetic 多设备形态）
    "devices": {
        "mode": "synthetic",
        "devices": [
            {
                "id": "synthetic-001", "platform": "android",
                "model": "Synthetic Device 001", "serial": "synthetic-001",
                "state": "online", "connection": "usb",
                "profile": {"usb": True, "wifi": False, "gpuSource": None,
                            "cpuCores": 8, "refreshRateHz": 120},
                "info": {"os": "Android 14 (synthetic)", "cpu": "8 cores (synthetic)",
                         "resolution": "1080x2400", "batteryLevel": 87, "refreshRateHz": 120},
            },
            {
                "id": "synthetic-002", "platform": "android",
                "model": "Synthetic Device 002", "serial": "synthetic-002",
                "state": "online", "connection": "usb",
                "profile": {"usb": True, "wifi": False, "gpuSource": "kgsl",
                            "cpuCores": 8, "refreshRateHz": 90},
                "info": {"os": "Android 14 (synthetic)", "cpu": "8 cores (synthetic)",
                         "resolution": "1080x2400", "batteryLevel": 64, "refreshRateHz": 90},
            },
        ],
    },
}

WIFI_GOLDEN: dict = {
    # 契约 §21.1 POST /api/devices/{deviceId}/wifi-mode 请求/响应 golden（real-only）
    "enable_request": {"action": "enable"},
    "enable_response": {"deviceId": "R3CN80ABCDEF", "action": "enable",
                        "state": "tcpip_enabled", "port": 5555},
    "connect_request": {"action": "connect", "host": "192.168.1.23"},
    "connect_response": {"deviceId": "R3CN80ABCDEF-wifi", "serial": "R3CN80ABCDEF",
                         "action": "connect", "host": "192.168.1.23", "port": 5555,
                         "connection": "wifi"},
}

APPS_SUBPROCESS_GOLDEN: dict = {
    # 契约 §21.6 synthetic game 包子进程演示（§4 apps 既有首元素不变）
    "deviceId": "synthetic-001",
    "apps": [
        {
            "packageName": "com.example.synthetic.game",
            "name": "Synthetic Game",
            "pid": 12345,
            "running": True,
            "isForeground": True,
            "processes": [
                {"pid": 12345, "name": "com.example.synthetic.game", "isMain": True},
                {"pid": 12347, "name": "com.example.synthetic.game:sub", "isMain": False},
            ],
        }
    ],
}

SESSION_M6_GOLDEN: dict = {
    # 契约 §21.8 POST /api/sessions M6 扩展请求/响应（fpsWindow/processName/logLevel）
    "create_request": {
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
    },
    "create_response": {
        "id": "sess-03",
        "state": "monitoring",
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [
            {"metricId": "cpu.proc.normalized.pct", "intervalMs": 1000},
            {"metricId": "frame.fps.mean", "intervalMs": 500},
        ],
        "createdAt": 1798765432101,
        "recordScreen": False,
        "fpsWindow": {"type": "surface_view"},
        "processName": "com.example.synthetic.game:sub",
        "logLevel": "debug",
    },
    # 契约 §21.7 SSE log 示例行（data: 一行 JSON）
    "sse_log_data": (
        '{"v":1,"seq":9,"ts":1798765438000,"type":"log","sessionId":"sess-01",'
        '"level":"warn","source":"ActivityManager","line":"Background start not allowed"}'
    ),
}

STARTUP_GOLDEN: dict = {
    # 契约 §21.4 GET /api/runs/{runId}/startup golden（TTID/TTFD null + note 口径声明；
    # 2026-11-10 S3 落地等价采集后 note 更新——原文与 reports.py STARTUP_NOTE 逐字同源）
    "body": {
        "runId": "run-PD_20261101_100001",
        "totalTimeMs": 842,
        "ttidMs": None,
        "ttfdMs": None,
        "note": ("口径声明：TotalTime 来自 am start -W（perfeval startup.cold.ms 同源，"
                 "[R§2.5]）；TTID 为启动捕获作业的 logcat『Displayed』行等价采集"
                 "（ActivityTaskManager/ActivityManager 同解析，run 级旁路测量，不随"
                 "实时监控会话自动执行）；TTFD 需应用代码调用 reportFullyDrawn()（[F§3.11]）"
                 "产生『Fully drawn』行，未调用则 ttfdMs 为 null 而非 0——两值为 logcat 行"
                 "解析口径，与 PerfDog 官方（FrameMetrics/reportFullyDrawn 回调链）语义"
                 "近似、数值不可直接对比"),
    },
    # 契约 §21.4 POST /api/runs/{runId}/startup/capture 请求 golden（2026-11-10 S3）
    "capture_request": {"deviceSerial": "R3CN80ABCDEF", "packageName": "com.example.app"},
}

SIMPLEPERF_FACE_BASIS = (
    "flat:overhead 分布树（落盘 report 无调用栈，Command→SharedObject→Symbol 分组呈现）"
)

# 契约 §22.1 simpleperf 数据面 golden（2026-11-10 S5 delta）
SIMPLEPERF_FACE_GOLDEN: dict = {
    "available": True,
    "source": "artifacts/simpleperf-report.txt",
    "stat": {
        "arch": "arm64",
        "events": [{"event": "cpu-clock", "samples": 4, "eventCount": 8000000}],
        "mode": "flat",
        "basis": SIMPLEPERF_FACE_BASIS,
        "entryCount": 3,
        "maxDepth": 4,
        "unparsedLines": 0,
    },
    "flamegraph": {
        "basis": SIMPLEPERF_FACE_BASIS,
        "maxDepth": 4,
        "tree": {
            "name": "ALL", "value": 100.0,
            "children": [
                {"name": "com.game", "value": 100.0,
                 "children": [
                     {"name": "libgame.so", "value": 80.0,
                      "children": [
                          {"name": "GameThread::Update", "value": 50.0, "children": []},
                          {"name": "RenderScene", "value": 30.0, "children": []},
                      ]},
                     {"name": "[kernel.kallsyms]", "value": 20.0,
                      "children": [
                          {"name": "freeze_slab", "value": 20.0, "children": []},
                      ]},
                 ]},
            ],
        },
    },
    "note": None,
}

DEEP_ANALYSIS_GOLDEN: dict = {
    # 契约 §22.1 GET /api/runs/{runId}/deep-analysis：帧列表概览（完整小样）
    "list": {
        "runId": "run-PD_20261101_100001",
        "traceAvailable": True,
        "source": "perfetto.frame_timeline",
        "vsyncPeriodMs": 8.33,
        "frameCount": 2,
        "jankCount": 1,
        "jankRate": 0.5,
        "frames": [
            {"frameIndex": 0, "tsEpochMs": 1798765432101, "durationMs": 8.9,
             "jank": False, "jankType": None, "presentType": "Present On Time Finish",
             "onTimeFinish": True, "expectedDurationMs": 8.33,
             "processName": "com.example.game", "pid": 12345},
            {"frameIndex": 501, "tsEpochMs": 1798765439210, "durationMs": 33.3,
             "jank": True, "jankType": "App Deadline Missed",
             "presentType": "Present On Time Finish", "onTimeFinish": False,
             "expectedDurationMs": 8.33, "processName": "com.example.game", "pid": 12345},
        ],
        # §22.1 S5 delta：帧列表概览带 simpleperf 数据面（§11.1 可选响应字段，
        # 与 deep_analysis.build_simpleperf_face 同形；数值与 test_deep_analysis.py
        # SIMPLEPERF_FLAT_REPORT fixture 手算口径一致）
        "simpleperf": SIMPLEPERF_FACE_GOLDEN,
        "note": None,
    },
    # ?frameIndex=501 单帧 Overview 卡 + CPU 调度片段
    "frame_detail": {
        "runId": "run-PD_20261101_100001",
        "traceAvailable": True,
        "frame": {"frameIndex": 501, "tsEpochMs": 1798765439210, "durationMs": 33.3,
                  "jank": True, "jankType": "App Deadline Missed",
                  "presentType": "Present On Time Finish", "onTimeFinish": False,
                  "expectedDurationMs": 8.33, "processName": "com.example.game",
                  "pid": 12345},
        "cpuScheduling": [
            {"core": 4, "tid": 24271, "threadName": "GameThread", "pid": 12345,
             "state": "Running", "startEpochMs": 1798765439212, "durationMs": 12.4},
            {"core": 5, "tid": 24272, "threadName": "RenderThread", "pid": 12345,
             "state": "Runnable", "startEpochMs": 1798765439225, "durationMs": 2.1},
        ],
        "note": None,
    },
    # 无 perfetto trace 空结构（200 + note，不回退伪造）
    "empty": {
        "runId": "run-PD_20261101_100002",
        "traceAvailable": False,
        "source": None,
        "frames": [],
        "frameCount": 0,
        "jankCount": 0,
        "jankRate": None,
        # §22.1 S5 delta：空 trace ≠ 空 simpleperf（trace 与 simpleperf 独立采集）
        "simpleperf": {
            "available": False,
            "source": None,
            "stat": None,
            "flamegraph": None,
            "note": ("run 无 simpleperf report 工件（未运行 diagnose android --mode"
                     " cpu/full 或采集失败），CPU 热点火焰图不可用；不回退伪造"
                     "（[R§8.6]）"),
        },
        "note": ("run 无 perfetto trace 工件（未采集 android.surfaceflinger.frametimeline"
                 " 或 trace_processor 不可用），深度分析数据面为空；不回退伪造帧数据"
                 "（[R§8.6]）"),
    },
}

DIAGNOSTICS_GOLDEN: dict = {
    # 契约 §22.2 GET /api/runs/{runId}/diagnostics golden（默认 -15%/1000ms）
    "body": {
        "runId": "run-PD_20261101_100001",
        "anchor": "frame.fps.mean",
        "params": {"dropThresholdPct": 15, "minDurationMs": 1000},
        "dropIntervals": [
            {"metricId": "frame.fps.mean", "fromMs": 1798765440000,
             "toMs": 1798765450500, "durationMs": 10500, "dropPct": 22.4,
             "baseline": 112.3, "trough": 87.2},
        ],
        "correlations": [
            {"metricId": "cpu.total.pct", "pearson": 0.82, "sampleCount": 21},
            {"metricId": "memory.pss.peak_mb", "pearson": -0.31, "sampleCount": 21},
        ],
        "candidates": [
            {"rank": 1, "metricId": "cpu.total.pct", "score": 0.82,
             "evidence": "FPS 下降区间内 CPU 负载同比上升，皮尔逊相关系数 0.82"},
            {"rank": 2, "metricId": "memory.pss.peak_mb", "score": -0.31,
             "evidence": "内存与 FPS 负相关（-0.31），弱候选"},
        ],
        "note": None,
    },
    # 锚指标无数据空结构
    "empty": {
        "runId": "run-PD_20261101_100002",
        "anchor": "frame.fps.mean",
        "params": {"dropThresholdPct": 15, "minDurationMs": 1000},
        "dropIntervals": [],
        "correlations": [],
        "candidates": [],
        "note": ("run 无锚指标 frame.fps.mean 数据点，根因诊断不可计算；不填假零值"
                 "（[R§8.6]）"),
    },
}

TIMED_SCREENSHOT_GOLDEN: dict = {
    # 契约 §22.3 POST /api/sessions/{sessionId}/timed-screenshot（real-only）
    "request": {"intervalMs": 2000},
    "response": {"sessionId": "sess-01", "intervalMs": 2000, "state": "started"},
}

M6M7_GOLDEN_CORPUS: dict = {
    "capability": CAPABILITY_GOLDEN,
    "wifi": WIFI_GOLDEN,
    "apps_subprocess": APPS_SUBPROCESS_GOLDEN,
    "session_m6": SESSION_M6_GOLDEN,
    "startup": STARTUP_GOLDEN,
    "deep_analysis": DEEP_ANALYSIS_GOLDEN,
    "diagnostics": DIAGNOSTICS_GOLDEN,
    "timed_screenshot": TIMED_SCREENSHOT_GOLDEN,
}


def test_m6m7_golden_corpus_is_valid_json(client: TestClient) -> None:
    """契约 §20-§22 示例必须可 json.loads（硬性要求）：全部语料经
    dumps→loads 往返逐值相等；SSE log 行逐行 json.loads。"""
    for section_name, section in M6M7_GOLDEN_CORPUS.items():
        assert json.loads(json.dumps(section)) == section, section_name
    sse = json.loads(SESSION_M6_GOLDEN["sse_log_data"])
    assert sse["type"] == "log" and sse["v"] == 1
    assert sse["level"] in LOG_LEVELS
    for key in ("source", "line"):
        assert isinstance(sse[key], str) and sse[key]


def test_capability_golden_schema(client: TestClient) -> None:
    """契约 §20.1/§20.2/§21.2/§21.3 结构不变量：capabilities/profile 形状、reason
    码枚举、注册表新增 metricId、交集判定的 golden 分支（kgsl_missing 只出现在
    gpu.busy.pct）。"""
    entries = [CAPABILITY_GOLDEN[key] for key in
               ("metric_gpu_busy", "metric_cpu_freq", "metric_cpu_normalized",
                "metric_cpu_thread", "metric_memory_detail_native")]
    for entry in entries:
        caps = entry["capabilities"]
        assert set(caps["modes"]) <= set(CONNECTION_MODES) and caps["modes"]
        assert caps["platform"] == "android"
        reason = caps.get("reason")
        if reason is not None:
            assert reason in REASON_CODES
    # 交集判定 golden 分支：gpu.busy.pct 对 gpuSource=null 设备不可用且 reason 为
    # kgsl_missing（契约 §20.3 判定模型第 3 条）；其余示例指标不带 reason
    assert CAPABILITY_GOLDEN["metric_gpu_busy"]["capabilities"]["reason"] == "kgsl_missing"
    assert CAPABILITY_GOLDEN["metric_gpu_busy"]["available"] is False
    for key in ("metric_cpu_freq", "metric_cpu_normalized", "metric_cpu_thread",
                "metric_memory_detail_native"):
        assert "reason" not in CAPABILITY_GOLDEN[key]["capabilities"]
    # §5 注册表新增 metricId 全集（CPU 3 + 内存 Detail 4）
    assert CAPABILITY_GOLDEN["memory_detail_group"] == [
        "memory.detail.native_mb", "memory.detail.dalvik_mb",
        "memory.detail.gfx_mb", "memory.detail.gl_mb",
    ]
    assert {e["metricId"] for e in entries[:-1]} >= {
        "cpu.freq.mhz", "cpu.proc.normalized.pct", "cpu.proc.thread.pct",
    }
    # 官方损耗提示与规范化口径声明必须进 description（[F§3.8][I§2.4] 开销提示内建）
    assert "Memory Detail" in CAPABILITY_GOLDEN["metric_memory_detail_native"]["description"]
    assert "规范化" in CAPABILITY_GOLDEN["metric_cpu_normalized"]["description"]
    # profile 形状（契约 §20.2）：gpuSource ∈ kgsl|null；synthetic 多设备 ≥2（R22）
    devices = CAPABILITY_GOLDEN["devices"]["devices"]
    assert len(devices) >= 2
    assert [d["id"] for d in devices] == ["synthetic-001", "synthetic-002"]  # 首位保护既有 golden
    for device in devices:
        profile = device["profile"]
        assert isinstance(profile["usb"], bool) and isinstance(profile["wifi"], bool)
        assert profile["gpuSource"] in ("kgsl", None)
        assert isinstance(profile["cpuCores"], int) and profile["cpuCores"] > 0
        assert isinstance(profile["refreshRateHz"], int) and profile["refreshRateHz"] > 0
    # 第二台 gpuSource="kgsl"：演示交集判定两条分支（§20.2/§20.3）
    assert devices[0]["profile"]["gpuSource"] is None
    assert devices[1]["profile"]["gpuSource"] == "kgsl"


def test_wifi_golden_schema(client: TestClient) -> None:
    """契约 §21.1 结构不变量：action 枚举、connect 必带 host、响应 connection
    扩展值 "wifi" 与端口 5555（adb tcpip 约定）。"""
    assert WIFI_GOLDEN["enable_request"]["action"] in ("enable", "connect")
    assert WIFI_GOLDEN["connect_request"]["action"] == "connect"
    assert WIFI_GOLDEN["connect_request"]["host"]
    assert WIFI_GOLDEN["enable_response"]["state"] == "tcpip_enabled"
    assert WIFI_GOLDEN["enable_response"]["port"] == 5555
    connect = WIFI_GOLDEN["connect_response"]
    assert connect["connection"] == "wifi"  # §4 connection 字段扩展语义
    assert connect["port"] == 5555 and connect["host"] == WIFI_GOLDEN["connect_request"]["host"]
    assert connect["serial"] == "R3CN80ABCDEF"  # 同 serial：USB/WiFi 各一行（[I§2.2]）


def test_session_m6_golden_schema(client: TestClient) -> None:
    """契约 §21.5/§21.6/§21.7/§21.8 结构不变量：fpsWindow/processName/logLevel
    回显、枚举与校验来源（processName ∈ apps processes[].name）、§6 基础字段不变。"""
    request = SESSION_M6_GOLDEN["create_request"]
    response = SESSION_M6_GOLDEN["create_response"]
    assert request["fpsWindow"]["type"] in ("activity", "surface_view", "auto")
    assert request["logLevel"] in LOG_LEVELS
    # processName 必须属于 apps golden 的 processes[].name（§21.6 校验来源）
    process_names = [p["name"] for a in APPS_SUBPROCESS_GOLDEN["apps"] for p in a["processes"]]
    assert request["processName"] in process_names
    assert APPS_SUBPROCESS_GOLDEN["apps"][0]["processes"][0] == {
        "pid": 12345, "name": "com.example.synthetic.game", "isMain": True,
    }  # §4 既有首元素不变（契约 §21.6）
    # 回显字段（§11.1 始终返回）+ recordScreen 缺省回显（§16.1 既有规则）
    for key in ("fpsWindow", "processName", "logLevel", "recordScreen"):
        assert response[key] == request.get(key, False if key == "recordScreen" else None)
    # §6 基础字段不受扩展影响
    assert response["id"] == "sess-03" and response["state"] == "monitoring"
    assert response["metrics"] == request["metrics"]
    assert isinstance(response["createdAt"], int)


def test_startup_golden_schema(client: TestClient) -> None:
    """契约 §21.4 结构不变量：TTID/TTFD 不可观测 ⇒ null + note 显式口径声明；
    TotalTime 为 am start -W 来源；note 原文与 reports.py STARTUP_NOTE 逐字同源。"""
    from app.routers.reports import STARTUP_NOTE

    body = STARTUP_GOLDEN["body"]
    assert body["ttidMs"] is None and body["ttfdMs"] is None
    assert isinstance(body["note"], str) and body["note"]
    for token in ("reportFullyDrawn", "[F§3.11]", "am start -W"):
        assert token in body["note"]
    assert body["note"] == STARTUP_NOTE  # 契约 golden 与实现同源（单一事实源）
    assert isinstance(body["totalTimeMs"], int) and body["totalTimeMs"] > 0
    # capture 端点（§21.4 2026-11-10 S3）：deviceSerial 必填形态
    assert STARTUP_GOLDEN["capture_request"]["deviceSerial"]


# ---- 契约 §9 compare S3 扩展 golden（2026-11-10 N 路/budget/窗口，§11.5 新增用例）----

COMPARE_S3_GOLDEN: dict = {
    # 契约 §9 POST /api/reports/compare 请求扩展示例（值经 S1 排版对齐语料位）
    "request": {
        "baselineRunIds": ["run-A"],
        "candidateRunIds": ["run-B", "run-C"],
        "mode": "same_app_devices",
        "budget": {
            "defaults": {"minRuns": 1, "recommendedRuns": 1},
            "metrics": {
                "startup.cold.ms": {"warnRegressionPct": 0.08, "failRegressionPct": 0.15}
            },
        },
        "fromMs": 1793600000000,
        "toMs": 1793600060000,
    },
}


def test_compare_s3_golden_schema(client: TestClient) -> None:
    """契约 §9 S3 扩展结构不变量：请求扩展字段形态 + 校验行（budget 非对象 /
    candidateRunIds 超 9 / fromMs>toMs → 400 validation_error；校验先于 run
    解析，可用不存在 runId 断言）。"""
    request = COMPARE_S3_GOLDEN["request"]
    assert isinstance(request["baselineRunIds"], list) and request["baselineRunIds"]
    assert isinstance(request["candidateRunIds"], list) and 2 <= len(request["candidateRunIds"]) <= 9
    assert isinstance(request["budget"], dict)
    assert isinstance(request["budget"]["metrics"], dict)
    for rule in request["budget"]["metrics"].values():
        assert isinstance(rule, dict)
        for value in rule.values():
            assert not isinstance(value, (dict, list))  # 叶值标量
    assert request["fromMs"] < request["toMs"]

    base = {"baselineRunIds": ["run-NOPE"]}
    # budget 非对象 → 400（校验先于 run 解析）
    bad_budget = client.post("/api/reports/compare", json={**base, "budget": "x"})
    assert bad_budget.status_code == 400
    assert bad_budget.json()["error"]["code"] == "validation_error"
    # candidateRunIds 超 9 条 → 400
    too_many = client.post(
        "/api/reports/compare",
        json={**base, "candidateRunIds": [f"run-{i}" for i in range(10)]},
    )
    assert too_many.status_code == 400
    assert too_many.json()["error"]["code"] == "validation_error"
    # fromMs > toMs → 400（§18 同校验）
    bad_window = client.post(
        "/api/reports/compare", json={**base, "fromMs": 2, "toMs": 1}
    )
    assert bad_window.status_code == 400
    assert bad_window.json()["error"]["code"] == "validation_error"


def test_deep_analysis_golden_schema(client: TestClient) -> None:
    """契约 §22.1 结构不变量：帧元素 = FrameRecord.to_dict 形状（perfetto_trace.py
    数据面，不加壳）；列表自洽（frameCount == len(frames)、jankCount 实数）；
    调度片段 epoch ms；空结构 note 恒带。"""
    listing = DEEP_ANALYSIS_GOLDEN["list"]
    frame_keys = {"frameIndex", "tsEpochMs", "durationMs", "jank", "jankType",
                  "presentType", "onTimeFinish", "expectedDurationMs",
                  "processName", "pid"}
    for frame in listing["frames"]:
        assert set(frame) == frame_keys  # FrameRecord.to_dict 键集（perfetto_trace.py:433-448）
    assert listing["frameCount"] == len(listing["frames"])
    assert listing["jankCount"] == sum(1 for f in listing["frames"] if f["jank"])
    assert abs(listing["jankRate"] - listing["jankCount"] / listing["frameCount"]) < 1e-9
    assert listing["traceAvailable"] is True and listing["source"] == "perfetto.frame_timeline"
    assert DEEP_ANALYSIS_GOLDEN["frame_detail"]["frame"] == listing["frames"][1]  # 单帧卡同源
    detail = DEEP_ANALYSIS_GOLDEN["frame_detail"]
    assert detail["traceAvailable"] is True
    assert set(detail["frame"]) == frame_keys
    for slice_ in detail["cpuScheduling"]:
        assert set(slice_) >= {"core", "tid", "threadName", "state",
                               "startEpochMs", "durationMs"}
        assert isinstance(slice_["startEpochMs"], int)  # §1 epoch ms 口径
        assert slice_["durationMs"] > 0
    empty = DEEP_ANALYSIS_GOLDEN["empty"]
    assert empty["traceAvailable"] is False and empty["frames"] == []
    assert empty["frameCount"] == 0 and empty["jankRate"] is None
    assert isinstance(empty["note"], str) and empty["note"]
    assert "不回退伪造" in empty["note"]
    # §22.1 S5 delta：simpleperf 火焰图数据面（帧列表概览与空结构都带该可选字段；
    # 单帧 Overview 卡不带——同 deep_analysis.build_deep_analysis 实装形态）
    face_keys = {"available", "source", "stat", "flamegraph", "note"}
    for body in (DEEP_ANALYSIS_GOLDEN["list"], empty):
        face = body["simpleperf"]
        assert set(face) == face_keys
    face = DEEP_ANALYSIS_GOLDEN["list"]["simpleperf"]
    assert face["available"] is True and face["source"] == "artifacts/simpleperf-report.txt"
    stat = face["stat"]
    assert set(stat) == {"arch", "events", "mode", "basis", "entryCount",
                         "maxDepth", "unparsedLines"}
    assert stat["basis"].startswith("flat:") and stat["entryCount"] == 3
    flame = face["flamegraph"]
    assert set(flame) == {"basis", "maxDepth", "tree"}
    tree = flame["tree"]
    assert tree["name"] == "ALL" and tree["value"] == 100.0
    assert set(tree) == {"name", "value", "children"}
    assert sum(c["value"] for c in tree["children"][0]["children"]) == 100.0
    empty_face = empty["simpleperf"]
    assert empty_face["available"] is False
    assert empty_face["source"] is None
    assert empty_face["stat"] is None and empty_face["flamegraph"] is None
    assert "不回退伪造" in empty_face["note"]


def test_diagnostics_golden_schema(client: TestClient) -> None:
    """契约 §22.2 结构不变量：默认参数回显 -15%/1000ms（[F§9] analyze_root_cause
    默认口径）、区间自洽（durationMs/toMs/baseline-trough→dropPct）、皮尔逊 ∈ [-1,1]、
    候选按 |score| 降序且 score 同源 pearson、空结构 note 恒带。"""
    body = DIAGNOSTICS_GOLDEN["body"]
    assert body["params"] == {"dropThresholdPct": 15, "minDurationMs": 1000}
    assert body["anchor"] == "frame.fps.mean"
    for interval in body["dropIntervals"]:
        assert interval["durationMs"] == interval["toMs"] - interval["fromMs"]
        implied = (interval["baseline"] - interval["trough"]) / interval["baseline"] * 100
        assert abs(interval["dropPct"] - implied) < 0.1
        assert interval["dropPct"] >= body["params"]["dropThresholdPct"]  # 命中阈值才检出
    pearson_by_metric = {}
    for corr in body["correlations"]:
        assert -1.0 <= corr["pearson"] <= 1.0
        assert isinstance(corr["sampleCount"], int) and corr["sampleCount"] > 0
        pearson_by_metric[corr["metricId"]] = corr["pearson"]
    scores = [abs(c["score"]) for c in body["candidates"]]
    assert scores == sorted(scores, reverse=True)  # 按 |score| 降序
    for cand in body["candidates"]:
        assert cand["rank"] == body["candidates"].index(cand) + 1
        assert cand["metricId"] in pearson_by_metric
        assert cand["score"] == pearson_by_metric[cand["metricId"]]  # score 主导项 = pearson
        assert isinstance(cand["evidence"], str) and cand["evidence"]
    empty = DIAGNOSTICS_GOLDEN["empty"]
    assert empty["dropIntervals"] == [] and empty["correlations"] == []
    assert empty["candidates"] == []
    assert empty["params"] == body["params"]  # 参数回显与默认一致
    assert isinstance(empty["note"], str) and "不填假零值" in empty["note"]


def test_timed_screenshot_golden_schema(client: TestClient) -> None:
    """契约 §22.3 结构不变量：intervalMs 缺省 2000（[F§7] 官方 2s 一次）、
    允许区间 500–60000、202 Accepted 形态。"""
    request = TIMED_SCREENSHOT_GOLDEN["request"]
    response = TIMED_SCREENSHOT_GOLDEN["response"]
    assert request["intervalMs"] == 2000  # [F§7]：超低消耗定时截图，2s 一次
    assert 500 <= request["intervalMs"] <= 60000
    assert response["intervalMs"] == request["intervalMs"]
    assert response["state"] == "started"
    assert response["sessionId"] == "sess-01"


# ---- 契约 §23 M-iOS golden 语料（2026-11-03 契约扩展，契约 §11.5 新增用例）----
#
# 同步原则与 §15-§22 先例一致：§23 示例 JSON 固化为语料常量并校验「可 json.loads +
# 结构不变量」；simulator 模式端点行为对拍（设备枚举/交集判定/录屏/日志流）在
# test_simulator_provider.py / test_simulator_flow.py 以 fixture 驱动（无模拟器
# 常驻依赖），live smoke 单独标注（需 booted 模拟器，无则 skip——§23.5 如实声明）。

SIMULATOR_UDID = "00000000-0000-4000-8000-000000000065"

SIMULATOR_GOLDEN: dict = {
    # 契约 §23.1 GET /api/devices（simulator 模式形态；UDID 取自本机实测设备）
    "devices": {
        "mode": "simulator",
        "devices": [
            {
                "id": SIMULATOR_UDID,
                "platform": "ios",
                "model": "iPhone 18 Pro",
                "serial": SIMULATOR_UDID,
                "state": "online",
                "connection": "simulator",
                "profile": {"usb": False, "wifi": False, "simulator": True,
                            "gpuSource": None, "cpuCores": 14, "refreshRateHz": None},
                "info": {
                    "os": "iOS 27.0 (Simulator)",
                    "cpu": "14 cores (host)",
                    "resolution": None,
                    "batteryLevel": None,
                    "refreshRateHz": None,
                },
            }
        ],
    },
    # 契约 §23.1 POST /api/devices/{deviceId}/sim-boot 响应
    "sim_boot": {
        "deviceId": SIMULATOR_UDID, "action": "boot", "state": "online",
    },
    # 契约 §23.1 GET /api/devices/{deviceId}/apps（simulator 形态）
    "apps": {
        "deviceId": SIMULATOR_UDID,
        "apps": [
            {
                "packageName": "com.apple.Preferences",
                "name": "Settings",
                "pid": 81296,
                "running": True,
                "isForeground": True,
                "processes": [
                    {"pid": 81296, "name": "com.apple.Preferences", "isMain": True}
                ],
            }
        ],
    },
    # 契约 §23.2 GET /api/metrics（iOS 指标条目形态，description 携带宿主口径声明）
    "metrics": {
        "mode": "simulator",
        "metrics": [
            {
                "metricId": "cpu.proc.host.pct", "unit": "%", "direction": "lower_better",
                "evidenceLevel": "L1", "defaultIntervalMs": 1000, "available": True,
                "description": (
                    "模拟器宿主口径：被测 App 进程在本机（macOS）的 CPU 占用率"
                    "（ps %CPU，衰减均值，多核可 >100%）；与真机 iOS（Xcode 使用率/核数、"
                    "PerfDog 未规范化口径）不可直接对比（[F§3.6] 平台口径差异）"
                ),
                "capabilities": {"modes": ["simulator"], "platform": "ios"},
            },
            {
                "metricId": "memory.rss.mb", "unit": "MB", "direction": "lower_better",
                "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
                "description": (
                    "模拟器宿主口径：被测 App 进程在本机（macOS）的常驻内存 RSS；"
                    "与 Android PSS（memory.pss.peak_mb）及真机 iOS FootPrint "
                    "（[F§3.8]）口径均不同，数值不可直接对比"
                ),
                "capabilities": {"modes": ["simulator"], "platform": "ios"},
            },
        ],
    },
}

IOS_METRIC_IDS = ("cpu.proc.host.pct", "memory.rss.mb")
SIMULATOR_MODES = ("usb", "wifi", "simulator")

M_IOS_GOLDEN_CORPUS: dict = {"simulator": SIMULATOR_GOLDEN}


def test_mios_golden_corpus_is_valid_json(client: TestClient) -> None:
    """契约 §23 示例必须可 json.loads（硬性要求）：语料 dumps→loads 往返逐值相等。"""
    for section_name, section in M_IOS_GOLDEN_CORPUS.items():
        assert json.loads(json.dumps(section)) == section, section_name


def test_simulator_golden_schema(client: TestClient) -> None:
    """契约 §23.0/§23.1/§23.2 结构不变量：platform/connection/modes 枚举扩展、
    profile.simulator 标志、宿主口径 description、FPS 不造数（platform_unsupported）。"""
    devices = SIMULATOR_GOLDEN["devices"]["devices"]
    for device in devices:
        assert device["platform"] == "ios"
        assert device["connection"] == "simulator"
        assert device["state"] in ("online", "offline")
        profile = device["profile"]
        assert profile["simulator"] is True
        assert profile["usb"] is False and profile["wifi"] is False
        assert profile["gpuSource"] is None  # 模拟器无 kgsl（§23.1）
        assert profile["refreshRateHz"] is None  # 无公开读数，不猜
        info = device["info"]
        assert info["os"].endswith("(Simulator)")
        assert info["cpu"].endswith("(host)")  # 宿主口径声明
        assert info["resolution"] is None and info["batteryLevel"] is None
    assert SIMULATOR_GOLDEN["sim_boot"] == {
        "deviceId": SIMULATOR_UDID, "action": "boot", "state": "online",
    }
    for app in SIMULATOR_GOLDEN["apps"]["apps"]:
        # 模拟器单进程模型（§23.1）：processes 恒单元素 isMain=True
        assert app["processes"] == [
            {"pid": app["pid"], "name": app["packageName"], "isMain": True}
        ]
    for metric in SIMULATOR_GOLDEN["metrics"]["metrics"]:
        caps = metric["capabilities"]
        assert set(caps["modes"]) <= set(SIMULATOR_MODES)
        assert caps["modes"] == ["simulator"]
        assert caps["platform"] == "ios"
        assert "宿主口径" in metric["description"]
        assert "不可直接对比" in metric["description"]
    # §23.2 FPS 不造数：frame.fps.mean 不在 iOS 注册条目中
    ios_ids = {m["metricId"] for m in SIMULATOR_GOLDEN["metrics"]["metrics"]}
    assert ios_ids == set(IOS_METRIC_IDS)
    assert "frame.fps.mean" not in ios_ids


def test_contract_simulator_registry_live(client: TestClient) -> None:
    """§23.2 golden 落点对拍（synthetic client 上的注册表静态面）：iOS 指标已注册、
    modes/platform 与语料一致；android 设备上 iOS 指标 available=false 且
    reason=platform_unsupported（§23.0 跨平台判定）。"""
    from app.registry import get_metric, is_registered

    for metric_id in IOS_METRIC_IDS:
        assert is_registered(metric_id)
        spec = get_metric(metric_id)
        assert spec.platform == "ios"
        assert spec.modes == ("simulator",)
        assert spec.simulator_available is True
        # 合成/真机模式不可用（simulator-only 通道）
        assert spec.synthetic_available is False and spec.real_available is False
        # android 设备 profile 交集判定 → platform_unsupported（§23.0）
        from app.services.capability_matrix import device_profile, judge_metric

        available, reason = judge_metric(
            spec, "real", device_profile(
                usb=True, wifi=False, gpu_source="kgsl",
                cpu_cores=8, refresh_rate_hz=120,
            )
        )
        assert available is False and reason == "platform_unsupported"


# ---- 契约 §21.9/§21.3.1 + §12 power 统计组（2026-11-10 delta，契约 §11.5 新增用例）----
#
# 同步原则与 §20-§23 先例一致：示例 JSON 固化为语料常量（值与契约文档逐字对应，
# 改契约示例必须连同本段一起改，契约 §11.4）并校验「可 json.loads + 结构不变量」；
# 注册表行与门禁的实装对拍以新增用例落点校验（test_contract_registry_expansion_live）。

REGISTRY_EXPANSION_GOLDEN: dict = {
    # 契约 §21.9 GPU 频率与温度分量（kgsl 缺失设备的交集判定分支）
    "gpu_freq_kgsl_missing": {
        "metricId": "gpu.freq.mhz", "unit": "MHz", "direction": "none",
        "evidenceLevel": "L2", "defaultIntervalMs": 2000, "available": False,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android",
                         "reason": "kgsl_missing"},
    },
    "thermal_cpu": {
        "metricId": "thermal.cpu.c", "unit": "°C", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
    "thermal_gpu": {
        "metricId": "thermal.gpu.c", "unit": "°C", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
    "thermal_btemp": {
        "metricId": "thermal.btemp.c", "unit": "°C", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
    # 契约 §21.3.1 内存三指标（明细段探测缺失设备的门禁分支）
    "memory_swap_not_landed": {
        "metricId": "memory.swap.mb", "unit": "MB", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": False,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android",
                         "reason": "not_landed"},
    },
    "memory_vss_not_landed": {
        "metricId": "memory.vss.mb", "unit": "MB", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": False,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android",
                         "reason": "not_landed"},
    },
    "memory_available": {
        "metricId": "memory.available.mb", "unit": "MB", "direction": "lower_better",
        "evidenceLevel": "L1", "defaultIntervalMs": 2000, "available": True,
        "capabilities": {"modes": ["usb", "wifi"], "platform": "android"},
    },
}

POWER_GROUP_GOLDEN: dict = {
    # 契约 §12 power 统计组（§11.1 兼容新增；数值为示意值，可判定部分为键集/note 原文）
    "computed": {
        "power": {
            "sum": {
                "totalMah": 43.7,
                "windowCount": 62,
                "note": ("Sum(Battery)=Σ power.energy.mah 逐窗增量求和（power-2 口径：每条记录=单窗增量）；"
                         "能耗为估算口径（采样端 semantic=estimated），与 PerfDog Sum(Battery) 不可直接对比"),
            },
            "frame": {
                "meanMah": 0.705,
                "p95Mah": 1.204,
                "totalMah": 42.3,
                "frameCount": 60,
                "note": ("FPower：能耗窗口按时间密度分摊到帧的估算值（semantic=estimated 继承自"
                         " power.energy.mah；分摊自第二条增量起的窗口，首条增量无窗口端点不计入）；"
                         "数值与 PerfDog FPower 不可直接对比"),
            },
        },
    },
    "none": {
        "power": {
            "sum": {
                "totalMah": None,
                "windowCount": 0,
                "note": "无 power.energy.mah 数据点，Sum(Battery) 不计算；不填假零值（[R§8.6]）",
            },
            "frame": {
                "meanMah": None,
                "p95Mah": None,
                "totalMah": None,
                "frameCount": 0,
                "note": ("无能耗序列或无逐帧数据（或能耗与帧时域无重叠），FPower 分摊不计算；"
                         "不填假零值（[R§8.6]）"),
            },
        },
    },
}

DELTA_GOLDEN_CORPUS: dict = {
    "registry_expansion": REGISTRY_EXPANSION_GOLDEN,
    "power_group": POWER_GROUP_GOLDEN,
}

REGISTRY_EXPANSION_GOLDEN_KEY_BY_ID: dict = {
    entry["metricId"]: entry for entry in REGISTRY_EXPANSION_GOLDEN.values()
}


def test_delta_golden_corpus_is_valid_json(client: TestClient) -> None:
    """契约 §21.9/§21.3.1/§12 delta 示例必须可 json.loads（硬性）：dumps→loads 往返。"""
    for section_name, section in DELTA_GOLDEN_CORPUS.items():
        assert json.loads(json.dumps(section)) == section, section_name


def test_registry_expansion_golden_schema(client: TestClient) -> None:
    """契约 §21.9/§21.3.1 结构不变量：kgsl_missing/not_landed 门禁分支、四项勾选组
    不含三新指标、设备级 available.mb 不挂门禁。"""
    gpu_freq = REGISTRY_EXPANSION_GOLDEN["gpu_freq_kgsl_missing"]
    assert gpu_freq["capabilities"]["reason"] == "kgsl_missing"
    assert gpu_freq["available"] is False
    assert gpu_freq["evidenceLevel"] == "L2"  # kgsl OEM 差异口径（同 gpu.busy.pct）
    for key in ("thermal_cpu", "thermal_gpu", "thermal_btemp"):
        entry = REGISTRY_EXPANSION_GOLDEN[key]
        assert entry["direction"] == "lower_better" and entry["unit"] == "°C"
        assert "reason" not in entry["capabilities"]  # 分量不挂 kgsl 门禁
    for key in ("memory_swap_not_landed", "memory_vss_not_landed"):
        entry = REGISTRY_EXPANSION_GOLDEN[key]
        assert entry["capabilities"]["reason"] == "not_landed"
    assert "reason" not in REGISTRY_EXPANSION_GOLDEN["memory_available"]["capabilities"]
    # §21.3 四项勾选组不变（三新指标不在组内）
    assert CAPABILITY_GOLDEN["memory_detail_group"] == [
        "memory.detail.native_mb", "memory.detail.dalvik_mb",
        "memory.detail.gfx_mb", "memory.detail.gl_mb",
    ]


def test_contract_registry_expansion_live(client: TestClient) -> None:
    """§21.9/§21.3.1 注册表实装落点对拍：注册表静态字段、real/synthetic 可用性、
    gpu.freq.mhz 的 gpu_source_required、swap/vss 的 meminfo_detail_required。

    语料中的 kgsl_missing/not_landed 门禁分支为带 profile 的交集判定形态
    （§20.3）；mode 级注册表（无设备上下文）不出 reason——静态字段按
    「语料剥掉 available/capabilities」对拍，门禁分支按 profile 判定落点校验。
    """
    from app.registry import get_metric, is_registered, registry_body
    from app.services.capability_matrix import device_profile

    body = client.get("/api/metrics").json()  # 夹具 provider 模式（synthetic）
    by_id = {m["metricId"]: m for m in body["metrics"]}
    static_fields = {"metricId", "unit", "direction", "evidenceLevel", "defaultIntervalMs"}
    for metric_id in (
        "gpu.freq.mhz", "thermal.cpu.c", "thermal.gpu.c", "thermal.btemp.c",
        "memory.swap.mb", "memory.vss.mb", "memory.available.mb",
    ):
        assert is_registered(metric_id)
        _assert_subset(
            {k: v for k, v in REGISTRY_EXPANSION_GOLDEN_KEY_BY_ID[metric_id].items()
             if k in static_fields},
            by_id[metric_id],
        )
    # synthetic 可用性（§21.9：real-only 组禁勾不写 0；分量序列注册表可勾）
    assert by_id["gpu.freq.mhz"]["available"] is False
    assert by_id["memory.swap.mb"]["available"] is False
    assert by_id["memory.vss.mb"]["available"] is False
    assert by_id["memory.available.mb"]["available"] is False
    assert by_id["thermal.cpu.c"]["available"] is True
    # real 模式级（无设备上下文）：七项全 available=True
    real_by_id = {m["metricId"]: m for m in registry_body("real")["metrics"]}
    for metric_id in (
        "gpu.freq.mhz", "thermal.cpu.c", "thermal.gpu.c", "thermal.btemp.c",
        "memory.swap.mb", "memory.vss.mb", "memory.available.mb",
    ):
        assert real_by_id[metric_id]["available"] is True
    # 门禁分支（语料的 reason 形态）：无 kgsl → kgsl_missing；明细段缺失 → not_landed
    no_kgsl_profile = device_profile(
        usb=True, wifi=False, gpu_source=None, cpu_cores=8, refresh_rate_hz=120,
    )
    no_detail_profile = device_profile(
        usb=True, wifi=False, gpu_source="kgsl",
        cpu_cores=8, refresh_rate_hz=120, meminfo_detail=False,
    )
    profiled = registry_body("real", no_kgsl_profile)
    by_id = {m["metricId"]: m for m in profiled["metrics"]}
    _assert_subset(REGISTRY_EXPANSION_GOLDEN["gpu_freq_kgsl_missing"], by_id["gpu.freq.mhz"])
    profiled = registry_body("real", no_detail_profile)
    by_id = {m["metricId"]: m for m in profiled["metrics"]}
    _assert_subset(REGISTRY_EXPANSION_GOLDEN["memory_swap_not_landed"], by_id["memory.swap.mb"])
    _assert_subset(REGISTRY_EXPANSION_GOLDEN["memory_vss_not_landed"], by_id["memory.vss.mb"])
    _assert_subset(REGISTRY_EXPANSION_GOLDEN["memory_available"], by_id["memory.available.mb"])
    spec = get_metric("gpu.freq.mhz")
    assert spec.gpu_source_required is True
    assert get_metric("memory.swap.mb").meminfo_detail_required is True
    assert get_metric("memory.available.mb").meminfo_detail_required is False


def test_power_group_golden_schema(client: TestClient) -> None:
    """契约 §12 power 组结构不变量：note 原文与 statistics_service 常量同源
    （单一事实源）、null 形态不填假零值、mAh 数值为示意值按类型对拍。"""
    from app.services.statistics_service import (
        POWER_FRAME_NOTE,
        POWER_FRAME_NOTE_NONE,
        POWER_SUM_NOTE,
        POWER_SUM_NOTE_NONE,
    )

    computed = POWER_GROUP_GOLDEN["computed"]["power"]
    assert computed["sum"]["note"] == POWER_SUM_NOTE
    assert computed["frame"]["note"] == POWER_FRAME_NOTE
    assert isinstance(computed["sum"]["windowCount"], int) and computed["sum"]["windowCount"] > 0
    for key in ("meanMah", "p95Mah", "totalMah"):
        assert isinstance(computed["frame"][key], (int, float))
    none_form = POWER_GROUP_GOLDEN["none"]["power"]
    assert none_form["sum"]["note"] == POWER_SUM_NOTE_NONE
    assert none_form["frame"]["note"] == POWER_FRAME_NOTE_NONE
    for key in ("totalMah", "windowCount"):
        assert none_form["sum"][key] in (None, 0)
    assert none_form["frame"]["frameCount"] == 0


# ---- 2026-11-04 体检修复回归（power-3：power.level.pct 注册表口径与实现一致）----


def test_contract_power_level_pct_rate_semantics(client: TestClient) -> None:
    """power.level.pct 登记口径 = 实际输出口径：real 通道该指标仅在降级路径产出，
    值为电量差分速率（%/min，越高掉电越快）——注册表 unit="%/min"+lower_better，
    前端值卡不再渲染成「电量 X% ↑ 越高越好」误导（2026-11-04 体检 power-3）。"""
    from app.registry import get_metric

    spec = get_metric("power.level.pct")
    assert spec.unit == "%/min"
    assert spec.direction == "lower_better"

    body = client.get("/api/metrics?mode=real").json()
    entry = next(m for m in body["metrics"] if m["metricId"] == "power.level.pct")
    assert entry["unit"] == "%/min"
    assert entry["direction"] == "lower_better"


# ---- 契约 §25.3 MCP 工具清单 golden（2026-11-10 S5 delta，工具面 8 → 15）----
#
# 契约 §25.3 表（15 项）与 mcp_server.TOOLS 单一事实源对拍：名单顺序、http 方法+
# 路径（query 参数剥离后）、inputSchema.required（表中加粗列）逐项一致。协议层与
# 透传行为由 test_mcp_server.py 覆盖，本用例只钉契约表 ↔ 工具定义同步。


def _mcp_tools_golden() -> list[dict]:
    """契约 §25.3 golden 表：工具名 → (http 方法, 路径模板, required)。"""
    return [
        ("list_devices", "GET", "/api/devices", []),
        ("start_session", "POST", "/api/sessions", ["deviceId", "packageName"]),
        ("record_start", "POST", "/api/sessions/{sessionId}/record/start", ["sessionId"]),
        ("record_stop", "POST", "/api/sessions/{sessionId}/record/stop", ["sessionId"]),
        ("save_session", "POST", "/api/sessions/{sessionId}/save", ["sessionId"]),
        ("list_runs", "GET", "/api/runs", []),
        ("get_run", "GET", "/api/runs/{runId}", ["runId"]),
        ("get_statistics", "GET", "/api/runs/{runId}/statistics", ["runId"]),
        ("get_metric_series", "GET", "/api/runs/{runId}/replay", ["runId"]),
        ("get_frame_details", "GET", "/api/runs/{runId}/deep-analysis", ["runId"]),
        ("get_deep_analysis", "GET", "/api/runs/{runId}/deep-analysis",
         ["runId", "frameIndex"]),
        ("get_diagnostics", "GET", "/api/runs/{runId}/diagnostics", ["runId"]),
        ("compare_reports", "POST", "/api/reports/compare",
         ["baselineRunIds", "candidateRunId"]),
        ("get_run_markers", "GET", "/api/runs/{runId}/markers", ["runId"]),
        ("list_cases", "GET", "/api/cases", []),
    ]


def test_mcp_tools_golden_matches_contract_25_3() -> None:
    """契约 §25.3 工具清单（15 项，golden）：mcp_server.TOOLS 与契约表逐项对拍。"""
    import sys
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import mcp_server  # noqa: E402

    tools = mcp_server.TOOLS
    golden = _mcp_tools_golden()
    assert len(tools) == len(golden) == 15  # 契约 §25.3（2026-11-10 由 8 扩至 15）
    for tool, (name, method, path, required) in zip(tools, golden):
        assert tool["name"] == name
        http_method, http_path = tool["http"]
        assert (http_method, http_path) == (method, path), name
        assert sorted(tool["inputSchema"]["required"]) == sorted(required), name
    names = [t["name"] for t in tools]
    assert names == [g[0] for g in golden]  # 顺序也对拍（tools/list 输出顺序）


# ---- 契约 §26 R24 真机切片 golden 语料（2026-11-16 契约扩展，契约 §11.5 新增用例）----
#
# 同步原则与 §15-§23 先例一致：§26 示例 JSON 固化为语料常量（值与契约文档逐字对应，
# 改契约示例必须连同本段一起改，契约 §11.4）。语料两条来源线如实分级：
# ①注册表/判定落点为**行为面实装**（registry.py 两行 + capability_matrix 判定链，
# 端点级 registry_body 断言全量可跑）；②设备发现/应用列表/sim-boot 放行为**契约
# 先行**（devicectl 解析与 real 聚合面装配归采集/装配流，落地后补端点级用例，
# §26.6 测试边界）——设备行字段值取自本机 devicectl 实测（xcrun 72/devicectl
# 651.32/jsonVersion 5，2026-11-16），命令面证据（capture screen-record/screenshot、
# xctrace Logging 模板）均 --help/--list 实测。

REAL_IOS_UDID = "00008130-000955B7C2D1E84A"  # 本机实测真机行 UDID（iPhone 15 Pro Max）

REAL_IOS_METRIC_IDS = ("cpu.proc.xctrace.pct", "memory.footprint.mb")

REAL_IOS_GOLDEN: dict = {
    # 契约 §26.1 GET /api/devices（real 聚合面真机 iOS 行；值取自本机 devicectl 实测）
    "device": {
        "id": REAL_IOS_UDID,
        "platform": "ios",
        "model": "iPhone 15 Pro Max",
        "serial": REAL_IOS_UDID,
        "state": "online",
        "connection": "usb",
        "profile": {"usb": True, "wifi": False, "simulator": False, "gpuSource": None,
                    "cpuCores": None, "refreshRateHz": None},
        "info": {
            "os": "iOS 27.2.1",
            "cpu": None,
            "resolution": None,
            "batteryLevel": None,
            "refreshRateHz": None,
        },
    },
    # §26.1 过滤规则拦截样例（devicectl 原文字段片段，本机实测三类行）
    "excluded_rows": [
        {  # watchOS 行：hardwareProperties.platform != "iOS" → 不出
            "hardwareProperties": {"platform": "watchOS", "productType": "Watch7,2"},
            "identifier": "B4E81C92-7A05-5D3F-8E2B-9C41D6F0A735",
        },
        {  # 模拟器行：reality=="simulated"（--filter 实测命中）或
            # transportType=="sameMachine"（jsonVersion 5 原文）任一命中 → 不出
            "hardwareProperties": {"platform": "iOS", "productType": "iPhone19,2"},
            "connectionProperties": {"transportType": "sameMachine",
                                     "pairingState": "paired"},
            "visibilityClass": "simulators",
        },
        {  # 未配对行（仅 ECID 无 UDID）：pairingState=="unsupported" → 不出
            "hardwareProperties": {"platform": "iOS"},
            "connectionProperties": {"pairingState": "unsupported",
                                     "tunnelState": "unavailable"},
        },
    ],
    # §26.1 tunnelState → state 映射（本机实测三值在场）；connectionState 恒不作信号
    "state_mapping": {"connected": "online", "disconnected": "offline",
                      "unavailable": "offline"},
    # §26.1 transportType → connection 映射（localNetwork→wifi 本机实测；
    # 有线形态目标恒 usb，原文值待真机 USB 接入实测回填——不猜测）
    "transport_mapping": {"localNetwork": "wifi"},
    # 契约 §26.2 GET /api/devices/{id}/apps（devicectl 形态；pid 为示意值）
    "apps": {
        "deviceId": REAL_IOS_UDID,
        "apps": [
            {
                "packageName": "com.apple.Preferences",
                "name": "Settings",
                "pid": 22018,
                "running": True,
                "isForeground": None,
                "processes": [{"pid": 22018, "name": "Preferences", "isMain": True}],
            },
            {
                "packageName": "com.example.game",
                "name": "Example Game",
                "pid": None,
                "running": False,
                "isForeground": None,
                "processes": [],
            },
        ],
    },
    # 契约 §26.3 GET /api/metrics?deviceId=<真机 iOS 行>（iOS 条目 golden；
    # description 与 registry.py 实装逐字同源）
    "metrics": {
        "mode": "real",
        "metrics": [
            {
                "metricId": "cpu.proc.xctrace.pct", "unit": "%",
                "direction": "lower_better", "evidenceLevel": "L1",
                "defaultIntervalMs": 1000, "available": True,
                "description": (
                    "真机 Instruments 口径：xctrace（Activity Monitor 模板，--attach 进程名）"
                    "%CPU 序列（export 实测 10ms 粒度，聚合到采样窗出点）；与模拟器宿主口径"
                    "（cpu.proc.host.pct）及 Android 未规范化口径（cpu.proc.pct）均不可直接"
                    "对比（[F§3.6] 平台口径差异）"
                ),
                "capabilities": {"modes": ["usb", "wifi"], "platform": "ios"},
            },
            {
                "metricId": "memory.footprint.mb", "unit": "MB",
                "direction": "lower_better", "evidenceLevel": "L1",
                "defaultIntervalMs": 2000, "available": True,
                "description": (
                    "真机 Instruments 口径：xctrace Activity Monitor 的 physical FootPrint "
                    "序列；与 Android PSS（memory.pss.peak_mb）及模拟器 RSS（memory.rss.mb）"
                    "口径均不同，数值不可直接对比"
                ),
                "capabilities": {"modes": ["usb", "wifi"], "platform": "ios"},
            },
        ],
    },
    # 契约 §23.1（§26 修订）sim-boot 聚合语义：real 聚合面 simulator 连接行放行
    # （200 与 §23.1 simulator 模式 golden 同形）+ 非 simulator 连接设备 400 错误体
    "sim_boot": {
        "ok": {"deviceId": SIMULATOR_UDID, "action": "boot", "state": "online"},
        "non_simulator_error": {
            "error": {
                "code": "validation_error",
                "message": (
                    "sim-boot 仅对 simulator 连接设备有意义"
                    "（usb|wifi 连接的 Android/iOS 真机行无 boot 语义，契约 §23.1/§26.1）"
                ),
                "details": {"deviceId": REAL_IOS_UDID, "connection": "usb"},
            }
        },
    },
    # 契约 §3 新增行（R24 真机切片）：xctrace_unavailable 错误体 golden（契约先行，
    # errors.py 码表与采集执行面随装配流落地）
    "xctrace_unavailable_error": {
        "error": {
            "code": "xctrace_unavailable",
            "message": "真机 iOS 采集通道不可用（xctrace record 执行失败）",
            "details": {"reason": "record_failed", "deviceId": REAL_IOS_UDID},
        }
    },
    # 契约 §26.6 边界分层（如实声明语料；锚定 §23 头注/§23.5 改写的余留边界清单）
    "boundaries": {
        # 第一层：平台无数据源 → available=false + platform_unsupported（不造数）
        "no_data_source": [
            "frame.fps.mean", "net.rx.kbps", "power.current.ma",
            "thermal.max.c", "gpu.busy.pct",
        ],
        # 第二层：本期不接线、命令面已存在（--help 实测）——不写 available=false
        "not_wired_command_face": [
            "xcrun devicectl device capture screen-record",
            "xcrun devicectl device capture screenshot",
        ],
        # 第三层：日志流无实时流式命令证据（xctrace Logging 模板可批录，导出未勘察）
        "log_stream_no_live_evidence": (
            "xctrace list templates 实测第 18 行含 Logging"
            "（批式录制 os_log trace，导出形态未勘察）"
        ),
        # MetricKit 判 infeasible（deferred）：订阅者必须被测 App 进程内注册+日级回吐
        "metrickit_infeasible": (
            "MXMetricManager.h 订阅者必须在被测 App 进程内注册且按天回吐"
        ),
    },
}

R26_GOLDEN_CORPUS: dict = {"real_ios": REAL_IOS_GOLDEN}


def test_r26_golden_corpus_is_valid_json(client: TestClient) -> None:
    """契约 §26 示例必须可 json.loads（硬性要求）：语料 dumps→loads 往返逐值相等；
    错误体键集与 §3 统一错误体一致。"""
    for section_name, section in R26_GOLDEN_CORPUS.items():
        assert json.loads(json.dumps(section)) == section, section_name
    sim_err = REAL_IOS_GOLDEN["sim_boot"]["non_simulator_error"]
    xctrace_err = REAL_IOS_GOLDEN["xctrace_unavailable_error"]
    for err in (sim_err, xctrace_err):
        assert set(err) == {"error"}
        assert set(err["error"]) >= {"code", "message", "details"}


def test_r26_device_golden_schema(client: TestClient) -> None:
    """契约 §26.1 结构不变量：真机行 platform/connection/profile 形态、过滤规则
    三拦截样例、tunnelState→state 与 transportType→connection 映射。"""
    device = REAL_IOS_GOLDEN["device"]
    assert device["platform"] == "ios"
    assert device["connection"] in ("usb", "wifi")  # 真机行只有两种连接（§23.0 枚举）
    assert device["id"] == device["serial"] == REAL_IOS_UDID  # UDID → id/serial
    profile = device["profile"]
    assert profile["simulator"] is False  # 真机行恒 false（§20.2 修订：仅模拟器为 true）
    assert profile["usb"] is not profile["wifi"]  # 连接行按 transport 二选一
    assert profile["gpuSource"] is None and profile["cpuCores"] is None
    assert profile["refreshRateHz"] is None  # devicectl 不暴露 → null（不猜）
    info = device["info"]
    assert info["os"].startswith("iOS ") and "(Simulator)" not in info["os"]
    assert info["cpu"] is None and info["resolution"] is None
    assert info["batteryLevel"] is None and info["refreshRateHz"] is None
    # state 映射：tunnelState 三值 → online/offline；无第三态
    state_mapping = REAL_IOS_GOLDEN["state_mapping"]
    assert state_mapping == {"connected": "online", "disconnected": "offline",
                             "unavailable": "offline"}
    assert set(state_mapping.values()) == {"online", "offline"}
    # transport 映射：localNetwork→wifi 实测在案（wired 原文值待回填，不进语料）
    assert REAL_IOS_GOLDEN["transport_mapping"] == {"localNetwork": "wifi"}
    # 过滤规则三类拦截样例：拦截判据字段与契约规则一一对应
    watch, sim, unpaired = REAL_IOS_GOLDEN["excluded_rows"]
    assert watch["hardwareProperties"]["platform"] == "watchOS"  # 规则 1：非 iOS
    assert (sim["connectionProperties"]["transportType"] == "sameMachine"
            and sim["visibilityClass"] == "simulators")  # 规则 2：模拟器行
    assert unpaired["connectionProperties"]["pairingState"] == "unsupported"  # 规则 3


def test_r26_apps_golden_schema(client: TestClient) -> None:
    """契约 §26.2 结构不变量：§4 apps 形态、running↔pid 一致性、真机 isForeground
    恒 null（不猜）、未运行行 processes 空。"""
    body = REAL_IOS_GOLDEN["apps"]
    assert body["deviceId"] == REAL_IOS_UDID
    for app in body["apps"]:
        assert set(app) >= {"packageName", "name", "pid", "running", "isForeground",
                            "processes"}
        if app["running"]:
            assert app["pid"] is not None and app["processes"]
            assert app["processes"][0]["isMain"] is True
        else:
            assert app["pid"] is None and app["processes"] == []
        assert app["isForeground"] is None  # 真机通道无前台信号源（§26.2 不猜声明）


def test_r26_registry_rows_snapshot(client: TestClient) -> None:
    """§26.3 注册表实装落点快照：真机双行字段与语料同源；**既有 simulator 2 行
    （§23.2）逐字段不变**（防误改——R24 不得触碰 §23 既有行）。"""
    from dataclasses import asdict

    from app.registry import get_metric, is_registered

    # 真机双行：注册表静态字段 + 可用性标志
    for metric_id in REAL_IOS_METRIC_IDS:
        assert is_registered(metric_id)
        spec = asdict(get_metric(metric_id))
        assert spec["modes"] == ("usb", "wifi")
        assert spec["platform"] == "ios"
        assert spec["real_available"] is True
        assert spec["simulator_available"] is False
        assert spec["synthetic_available"] is False
        assert spec["gpu_source_required"] is False
        assert spec["meminfo_detail_required"] is False
        assert spec["direction"] == "lower_better"
        assert spec["evidence_level"] == "L1"
    assert get_metric("cpu.proc.xctrace.pct").default_interval_ms == 1000
    assert get_metric("memory.footprint.mb").default_interval_ms == 2000
    # description 与 §26.3 语料逐字同源（单一事实源）
    golden_by_id = {m["metricId"]: m for m in REAL_IOS_GOLDEN["metrics"]["metrics"]}
    for metric_id in REAL_IOS_METRIC_IDS:
        assert get_metric(metric_id).description == golden_by_id[metric_id]["description"]
    # metricId 唯一性（_BY_ID 不复用）：真机行 ≠ 既有 cpu.proc.pct 行，且全集无重号
    from app.registry import METRICS

    assert get_metric("cpu.proc.pct").platform == "android"
    assert len({s.metric_id for s in METRICS}) == len(METRICS)
    # 既有 simulator 2 行逐字段快照（§23.2 既有 golden 不变——防 R24 误改）
    simulator_snapshot = {
        "cpu.proc.host.pct": {
            "metric_id": "cpu.proc.host.pct", "unit": "%", "direction": "lower_better",
            "evidence_level": "L1", "default_interval_ms": 1000,
            "synthetic_available": False, "real_available": False,
            "modes": ("simulator",), "platform": "ios",
            "gpu_source_required": False, "meminfo_detail_required": False,
            "simulator_available": True, "tags_hint": (),
        },
        "memory.rss.mb": {
            "metric_id": "memory.rss.mb", "unit": "MB", "direction": "lower_better",
            "evidence_level": "L1", "default_interval_ms": 2000,
            "synthetic_available": False, "real_available": False,
            "modes": ("simulator",), "platform": "ios",
            "gpu_source_required": False, "meminfo_detail_required": False,
            "simulator_available": True, "tags_hint": (),
        },
    }
    for metric_id, expected in simulator_snapshot.items():
        spec = asdict(get_metric(metric_id))
        description = spec.pop("description")
        assert spec == expected, metric_id
        # description 锚定关键声明（整串逐字对拍交给 §23.2 语料，此处防语义漂移）
        assert "模拟器宿主口径" in description and "不可直接对比" in description
        if metric_id == "memory.rss.mb":
            assert "FootPrint" in description  # 与真机口径的区分声明仍在


def test_r26_capability_matrix_ios_real(client: TestClient) -> None:
    """§26.1/§26.3 可用性联动（经 registry_body + judge_metric 同源断言，§20.3）：
    ①真机 ios profile（usb/wifi=True、simulator=False）双指标可勾；②Android 全
    指标（含 FPS/网络/功耗/温度/GPU）对 ios 真机 platform_unsupported；③真机双
    指标对模拟器设备（modes 交集空）not_landed；④模拟器双指标在 real 通道
    mode_available=False → not_landed（判定第 0 项分支）。"""
    from app.registry import METRICS, get_metric, mode_available, registry_body
    from app.services.capability_matrix import device_profile, judge_metric

    real_ios = device_profile(
        usb=True, wifi=True, gpu_source=None, cpu_cores=None, refresh_rate_hz=None,
        platform="ios", simulator=False,  # 真机行必须显式 False（§20.2 修订）
    )
    sim_device = device_profile(
        usb=False, wifi=False, gpu_source=None, cpu_cores=14, refresh_rate_hz=None,
        platform="ios", simulator=True,
    )
    # ① registry_body 落点：真机 ios profile 下双指标 available=True 且与语料全等
    body = registry_body("real", real_ios)
    by_id = {m["metricId"]: m for m in body["metrics"]}
    for entry in REAL_IOS_GOLDEN["metrics"]["metrics"]:
        _assert_subset(entry, by_id[entry["metricId"]])
    # ② Android 全指标对 ios 真机 → platform_unsupported（判定第 1 条先行，
    #    power_sysfs_blocked/kgsl_missing/not_landed 均不覆写）
    android_ids = {s.metric_id for s in METRICS if s.platform == "android"}
    assert {"frame.fps.mean", "net.rx.kbps", "net.tx.kbps", "power.current.ma",
            "thermal.max.c", "gpu.busy.pct"} <= android_ids  # 契约点名族在场
    for metric_id in android_ids:
        assert by_id[metric_id]["available"] is False, metric_id
        assert by_id[metric_id]["capabilities"]["reason"] == "platform_unsupported", (
            metric_id
        )
    # ③ 真机双指标对模拟器设备：modes {usb,wifi} ∩ {simulator} = ∅ → not_landed
    for metric_id in REAL_IOS_METRIC_IDS:
        assert judge_metric(get_metric(metric_id), "real", sim_device) == (
            False, "not_landed"
        )
    # ④ 模拟器双指标在 real 通道：modes 交集非空（simulator 行）但通道未落地
    #    （mode_available=False）→ not_landed（判定第 0 项分支）
    for metric_id in IOS_METRIC_IDS:
        assert mode_available(get_metric(metric_id), "real") is False
        assert judge_metric(get_metric(metric_id), "real", sim_device) == (
            False, "not_landed"
        )
    # §20.1 reason 枚举零扩充交叉核对：本流无新 reason 码
    from app.registry import REASON_CODES

    assert set(REASON_CODES) == {
        "kgsl_missing", "power_wifi_only", "not_landed", "platform_unsupported",
        "power_sysfs_blocked",
    }


def test_sim_boot_aggregate_semantics(client: TestClient) -> None:
    """§23.1 sim-boot 聚合语义（2026-11-16 §26 修订）：synthetic 保持 501（模式门
    先行，行为面回归）；real 聚合面的放行 200 与 Android/真机行 400 错误体为契约
    先行语料（行为面随装配流落地，落点后补端点级用例——§26.6 测试边界）。"""
    # 未知设备 → 404（模式门之前；synthetic 聚合面无 simulator 行——simulator
    # UDID 在 synthetic 模式同样 404，模式门 501 只对 provider 枚举的设备触发）
    for device_id in ("NOPE", SIMULATOR_UDID):
        resp = client.post(f"/api/devices/{device_id}/sim-boot")
        assert resp.status_code == 404, device_id
        assert resp.json()["error"]["code"] == "device_not_found"
    # synthetic 模式保持 501（既有行为不变）：枚举内的 Android 行模式门先行
    resp = client.post("/api/devices/synthetic-001/sim-boot")
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "not_implemented"
    # real 聚合面语料：simulator 连接行 200 与 §23.1 golden 同形（同源断言）
    assert REAL_IOS_GOLDEN["sim_boot"]["ok"] == SIMULATOR_GOLDEN["sim_boot"]
    # 非 simulator 连接设备 → 400 validation_error 错误体 golden（§26.1）
    err = REAL_IOS_GOLDEN["sim_boot"]["non_simulator_error"]["error"]
    assert err["code"] == "validation_error"
    assert set(err["details"]) == {"deviceId", "connection"}
    assert err["details"]["connection"] in ("usb", "wifi")


def test_r26_boundary_corpus_schema(client: TestClient) -> None:
    """契约 §26.6 边界分层语料：三层声明与点名的指标族/命令面一一对应——
    平台无数据源（available=false + platform_unsupported，MetricKit infeasible）、
    本期不接线（命令面已存在，不写 available=false）、日志流无实时流式命令证据。"""
    boundaries = REAL_IOS_GOLDEN["boundaries"]
    # 第一层：点名指标族 ⊆ Android 注册行（对 ios 设备随平台判定禁勾）
    from app.registry import METRICS, is_registered

    assert set(boundaries["no_data_source"]) <= {s.metric_id for s in METRICS}
    for metric_id in boundaries["no_data_source"]:
        assert is_registered(metric_id)
    assert "frame.fps.mean" in boundaries["no_data_source"]  # §23.2 不造数口径延续
    # 第二层：命令面已存在的两条 devicectl capture 命令（--help 实测面）
    assert boundaries["not_wired_command_face"] == [
        "xcrun devicectl device capture screen-record",
        "xcrun devicectl device capture screenshot",
    ]
    # 第三层：日志流 + MetricKit infeasible 结论
    assert "Logging" in boundaries["log_stream_no_live_evidence"]
    assert "第 18 行" in boundaries["log_stream_no_live_evidence"]
    assert "MXMetricManager.h" in boundaries["metrickit_infeasible"]


def test_contract_doc_r26_rewrite_anchor() -> None:
    """§23 头注/§23.5 改写锚定 + §26 编号让位核查（契约 §11.4 同源哲学，直接对
    契约/文档文本断言）：①契约不再含「真机未支持」假陈述；②§23 头注双支持范围
    与余留边界清单在位；③§23.1 sim-boot 聚合修订在位；④两处 README bump §1–§26；
    ⑤mcp_server.py 的 §25 引用指向不变（§26 追加于文末，§25 编号不被让位）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    contract_path = root / ".docs/features/perfdog-parity/API_CONTRACT.md"
    if not contract_path.exists():
        pytest.skip("内部契约文档未随公开发布分发（.docs/ 保留于开发仓库）")
    contract = contract_path.read_text(encoding="utf-8")
    # ① 假陈述已删除（§23 头注原文「Simulator 已支持 / 真机未支持」与 §23.5
    #    「真机 iOS 未支持」两处；§26 章内不得回潮）
    assert "真机未支持" not in contract
    assert "真机 iOS 未支持" not in contract
    # ② §23 头注：双支持范围 + 余留边界（录屏/截图不接线/日志流/FPS 等无数据源）
    assert "真机已支持（§26" in contract
    assert "本期不接线" in contract
    assert "无实时流式命令证据" in contract
    assert "MetricKit 通道判 infeasible 不做" in contract
    # ③ §23.1 sim-boot 聚合修订：synthetic 501 保持 + real 聚合面放行语义在位
    assert "模式聚合语义（2026-11-16 §26 修订" in contract
    # ④ §26 章节骨架与注册表双行/错误码在位
    assert "## 26. 真机 iOS 采集（R24 真机切片）" in contract
    for token in ("cpu.proc.xctrace.pct", "memory.footprint.mb",
                  "xctrace_unavailable", "activity-monitor-process-live"):
        assert token in contract, token
    # ⑤ README bump（§1–§25 假引用不得残留）+ mcp_server §25 引用不动
    assert "§1–§26" in (root / "README.md").read_text(encoding="utf-8")
    assert "§1–§25" not in (root / "README.md").read_text(encoding="utf-8")
    parity_readme = (
        root / ".docs/features/perfdog-parity/README.md"
    ).read_text(encoding="utf-8")
    assert "§1–§26" in parity_readme and "§1–§25" not in parity_readme
    mcp_src = (Path(__file__).resolve().parents[1] / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    assert "契约 §25" in mcp_src and "§26" not in mcp_src
    # 本文件 §25.3 golden 用例仍在（编号让位后指向不变）
    assert "def test_mcp_tools_golden_matches_contract_25_3" in Path(__file__).read_text(
        encoding="utf-8"
    )
