"""case 库索引与端点测试（契约 §14 / ARCH §4.7；DEVELOPMENT_PLAN R42-R44）。

覆盖：save 自动登记、索引 CRUD（GET/PATCH/DELETE/rebuild）、重建幂等与陈旧行
物理清除、筛选/排序/分页、WAL 模式、损坏自愈、CLI 重建入口、对比数据面透传。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import make_session

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parents[1]

STATS_KEYS = {
    "avgFps",
    "smooth",
    "jankPer10min",
    "peakMemoryMb",
    "avgAppCpuPct",
    "avgPowerMa",
}


# ---- 夹具辅助 ----


def _save_run(client: TestClient, case_name: str, **session_overrides: object) -> dict:
    """走完整会话流 save 一个 run（触发自动登记）。"""
    sid = make_session(client, **session_overrides)["id"]
    time.sleep(0.25)  # 留出采样窗口
    resp = client.post(f"/api/sessions/{sid}/save", json={"caseName": case_name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _write_run_dir(
    runs_dir: Path,
    name: str,
    *,
    manifest: dict | None = None,
    samples: list[dict] | None = None,
    summary: bool = True,
) -> Path:
    """手工伪造 run 目录（事实源），供 rebuild/统计快照消费。"""
    run_dir = runs_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if samples:
        with (run_dir / "samples.ndjson").open("w", encoding="utf-8") as sink:
            for row in samples:
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    if summary:
        (run_dir / "summary.json").write_text(
            json.dumps({"schemaVersion": "perfeval.summary.v1", "runId": f"run-{name}"}) + "\n",
            encoding="utf-8",
        )
    return run_dir


def _fps_samples(start_ms: int, values: list[float], interval_ms: int = 500) -> list[dict]:
    return [
        {"tsMs": start_ms + idx * interval_ms, "metricId": "frame.fps.mean", "value": value, "tags": {}}
        for idx, value in enumerate(values)
    ]


def _case_ids(body: dict) -> list[str]:
    return [case["runId"] for case in body["cases"]]


def _error_code(resp) -> str:
    body = resp.json()
    assert set(body) == {"error"}
    return body["error"]["code"]


# ---- save 自动登记（契约 §14 / §6 注）----


def test_save_auto_registers_case_index(client: TestClient) -> None:
    saved = _save_run(client, "PD_20261031_120001")
    body = client.get("/api/cases").json()
    assert body["total"] == 1
    row = body["cases"][0]
    assert row["runId"] == saved["runId"] == "run-PD_20261031_120001"
    assert row["caseName"] == "PD_20261031_120001"
    assert row["device"] == "synthetic-001"  # 新 run：deviceId（契约 §14）
    assert row["packageName"] == "com.example.synthetic.game"
    assert isinstance(row["createdAt"], int)
    assert row["summaryAvailable"] is True
    assert set(row["stats"]) == STATS_KEYS  # 六个快照键恒在，无数据为 null
    assert all(row["stats"][key] is None or isinstance(row["stats"][key], (int, float)) for key in STATS_KEYS)
    assert row["path"] == saved["runPath"]
    assert Path(row["path"]).is_dir()


def test_save_register_failure_does_not_fail_save(client: TestClient, monkeypatch) -> None:
    """契约 §14：登记失败不使 save 失败（rebuild 兜底）。"""
    index = client.app.state.case_index

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(index, "upsert", boom)
    saved = _save_run(client, "PD_REGISTER_FAIL")
    assert saved["state"] == "saved"
    body = client.get("/api/cases").json()
    assert body["total"] == 0  # 登记失败 → 行缺席
    monkeypatch.undo()
    rebuilt = client.post("/api/cases/rebuild").json()
    assert rebuilt["total"] == 1  # rebuild 兜底补登记
    assert _case_ids(client.get("/api/cases").json()) == [saved["runId"]]


def test_index_uses_wal_journal_mode(client: TestClient) -> None:
    _save_run(client, "PD_WAL_CHECK")
    with sqlite3.connect(client.cases_db_path) as conn:  # type: ignore[attr-defined]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ---- rebuild：幂等、陈旧行、兼容导入、损坏自愈（契约 §14）----


def _fabricate_three_runs(runs_dir: Path) -> None:
    _write_run_dir(
        runs_dir,
        "PD_A",
        manifest={
            "schemaVersion": "perfeval.manifest.v1",
            "runId": "run-PD_A",
            "caseName": "PD_A",
            "device": "synthetic-001",
            "packageName": "com.example.a",
            "createdAt": 1000,
        },
        samples=_fps_samples(5000, [30.0, 30.0, 30.0, 30.0])
        + [
            {"tsMs": 5000, "metricId": "memory.pss.peak_mb", "value": 100.0, "tags": {}},
            {"tsMs": 5500, "metricId": "memory.pss.peak_mb", "value": 200.0, "tags": {}},
        ],
    )
    _write_run_dir(
        runs_dir,
        "PD_B",
        manifest={
            "schemaVersion": "perfeval.manifest.v1",
            "runId": "run-PD_B",
            "caseName": "回归基线",
            "device": "device-B",
            "packageName": "com.example.b",
            "createdAt": 2000,
        },
        samples=_fps_samples(9000, [60.0] * 8),
    )
    # PD_C：无 samples（快照全 null）；无 manifest 的旧目录在 rebuild 测试里单独造
    _write_run_dir(
        runs_dir,
        "PD_C",
        manifest={
            "schemaVersion": "perfeval.manifest.v1",
            "runId": "run-PD_C",
            "caseName": "PD_C",
            "device": "synthetic-001",
            "packageName": "com.example.c",
            "createdAt": 3000,
        },
        samples=[],
    )


def test_rebuild_idempotent_and_purges_stale(client: TestClient) -> None:
    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    _fabricate_three_runs(runs_dir)

    first = client.post("/api/cases/rebuild").json()
    assert first["total"] == 3
    assert isinstance(first["rebuiltAt"], int)
    rows_before = client.get("/api/cases", params={"limit": 200}).json()
    second = client.post("/api/cases/rebuild").json()
    assert second["total"] == 3
    rows_after = client.get("/api/cases", params={"limit": 200}).json()
    assert rows_before == rows_after  # 幂等：同事实源重建结果一致

    # 事实源消失：行不展示；rebuild 物理清除
    shutil.rmtree(runs_dir / "PD_B")
    body = client.get("/api/cases").json()
    assert body["total"] == 2 and "run-PD_B" not in _case_ids(body)
    cleared = client.post("/api/cases/rebuild").json()
    assert cleared["total"] == 2
    assert "run-PD_B" not in _case_ids(client.get("/api/cases").json())


def test_rebuild_includes_legacy_run_dirs(client: TestClient) -> None:
    """[R§2.12] 兼容导入：无 manifest 的旧 run 目录按目录名登记，createdAt 取 mtime。"""
    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    _write_run_dir(
        runs_dir,
        "legacy-run-01",
        manifest=None,
        samples=_fps_samples(1000, [55.0, 58.0]),
    )
    body = client.post("/api/cases/rebuild").json()
    assert body["total"] == 1
    row = client.get("/api/cases").json()["cases"][0]
    assert row["runId"] == "run-legacy-run-01"
    assert row["caseName"] == "legacy-run-01"  # 无 manifest → 目录名回填
    assert row["device"] is None
    assert row["createdAt"] > 0  # mtime（epoch ms）
    assert row["stats"]["avgFps"] is not None  # 快照来自 samples 事实源


def test_rebuild_skips_run_group_directories(client: TestClient) -> None:
    """runs 根下的 run-group 容器目录（perfeval 多轮布局，只含子目录）不是 run，
    不登记（实测 runs/synthetic-validation 即此形态，否则产生空壳行）。"""
    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    _fabricate_three_runs(runs_dir)
    group = runs_dir / "synthetic-validation"
    (group / "crash_fail").mkdir(parents=True)  # 只含子目录，无事实源文件
    (group / "crash_fail" / "summary.json").write_text("{}", encoding="utf-8")

    body = client.post("/api/cases/rebuild").json()
    assert body["total"] == 3  # 三个 run 目录，group 容器不算
    assert "run-synthetic-validation" not in _case_ids(client.get("/api/cases").json())


def test_rebuild_normalizes_legacy_perfeval_manifest(client: TestClient) -> None:
    """[R§2.12] 兼容导入：旧 perfeval manifest（device/app 为 Mapping、
    createdAtUtc ISO、无 caseName）归一为标量行。"""
    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    _write_run_dir(
        runs_dir,
        "legacy-emu",
        manifest={
            "schemaVersion": "perfeval.manifest.v1",
            "runId": "run-legacy-emu",
            "createdAtUtc": "2026-01-15T08:30:00.000000+00:00",
            "device": {
                "platform": "android",
                "deviceId": "emu-5554",
                "serialOrUdid": "emu-5554",
                "model": "Pixel 8",
            },
            "app": {
                "platform": "android",
                "appId": "com.legacy.game",
                "packageName": "com.legacy.game",
                "versionName": "1.2.3",
            },
        },
    )
    client.post("/api/cases/rebuild")
    row = client.get("/api/cases").json()["cases"][0]
    assert row["runId"] == "run-legacy-emu"
    assert row["caseName"] == "legacy-emu"  # 无 caseName → 目录名
    assert row["device"] == "emu-5554"  # device.deviceId
    assert row["packageName"] == "com.legacy.game"  # app.packageName
    assert row["createdAt"] == 1768465800000  # createdAtUtc → epoch ms


def test_rebuild_recovers_from_corrupt_db(client: TestClient) -> None:
    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    _fabricate_three_runs(runs_dir)
    client.post("/api/cases/rebuild")
    db_path = client.cases_db_path  # type: ignore[attr-defined]
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    db_path.write_bytes(b"garbage not a sqlite db" * 64)

    body = client.post("/api/cases/rebuild").json()
    assert body["total"] == 3  # 损坏 → 新建空库恢复
    assert client.get("/api/cases").json()["total"] == 3


# ---- GET /api/cases：筛选 / 排序 / 分页 / 参数校验（契约 §14）----


def test_get_cases_query_device_sort_and_pagination(client: TestClient) -> None:
    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    _fabricate_three_runs(runs_dir)
    client.post("/api/cases/rebuild")

    # 缺省排序 createdAt desc
    assert _case_ids(client.get("/api/cases").json()) == ["run-PD_C", "run-PD_B", "run-PD_A"]

    # query：caseName 子串不区分大小写
    body = client.get("/api/cases", params={"query": "回归"}).json()
    assert body["total"] == 1 and body["cases"][0]["caseName"] == "回归基线"
    body = client.get("/api/cases", params={"query": "pd_a"}).json()
    assert _case_ids(body) == ["run-PD_A"]

    # device：精确匹配
    body = client.get("/api/cases", params={"device": "device-B"}).json()
    assert _case_ids(body) == ["run-PD_B"]
    body = client.get("/api/cases", params={"device": "synthetic-001"}).json()
    assert set(_case_ids(body)) == {"run-PD_A", "run-PD_C"}

    # sort=avgFps：null 恒排末尾（asc/desc 一致）
    body = client.get("/api/cases", params={"sort": "avgFps", "order": "asc"}).json()
    assert _case_ids(body) == ["run-PD_A", "run-PD_B", "run-PD_C"]
    values = [case["stats"]["avgFps"] for case in body["cases"]]
    assert values[0] == 30.0 and values[1] == 60.0 and values[2] is None
    body = client.get("/api/cases", params={"sort": "avgFps", "order": "desc"}).json()
    assert _case_ids(body) == ["run-PD_B", "run-PD_A", "run-PD_C"]

    # 其余统计键可排序（峰值取 samples max）
    body = client.get("/api/cases", params={"sort": "peakMemoryMb", "order": "desc"}).json()
    assert body["cases"][0]["stats"]["peakMemoryMb"] == 200.0

    # 分页：limit/offset 切片，total 仍为筛选后总数
    body = client.get("/api/cases", params={"limit": 1, "offset": 1}).json()
    assert body["total"] == 3 and _case_ids(body) == ["run-PD_B"]

    # 参数校验：400 validation_error
    for params in (
        {"sort": "bogus"},
        {"order": "side"},
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"limit": "abc"},
    ):
        resp = client.get("/api/cases", params=params)
        assert resp.status_code == 400, params
        assert _error_code(resp) == "validation_error"


# ---- PATCH /api/cases/{runId}：改名（契约 §14）----


def test_rename_case_updates_index_and_manifest(client: TestClient) -> None:
    saved = _save_run(client, "PD_REN")
    resp = client.patch(f"/api/cases/{saved['runId']}", json={"caseName": "一轮回归基线"})
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["runId"] == "run-PD_REN"  # runId 不变（对比历史引用稳定）
    assert row["caseName"] == "一轮回归基线"
    assert row["device"] == "synthetic-001" and row["packageName"] == "com.example.synthetic.game"
    assert Path(row["path"]) == Path(saved["runPath"])  # run 目录不移动

    # 写回事实源：manifest.json caseName 同步；目录名不变
    manifest = json.loads((Path(saved["runPath"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["caseName"] == "一轮回归基线"
    assert (client.runs_dir / "PD_REN").is_dir()  # type: ignore[attr-defined]

    # 列表展示新名；rebuild 不丢（显示名持久化在事实源内）
    body = client.get("/api/cases", params={"query": "一轮回归基线"}).json()
    assert body["total"] == 1
    client.post("/api/cases/rebuild")
    assert client.get("/api/cases").json()["cases"][0]["caseName"] == "一轮回归基线"


def test_rename_case_errors(client: TestClient) -> None:
    saved = _save_run(client, "PD_REN_ERR")
    base = f"/api/cases/{saved['runId']}"
    for payload in ({}, {"caseName": ""}, {"caseName": "   "}, {"caseName": 42}, {"caseName": None}):
        resp = client.patch(base, json=payload)
        assert resp.status_code == 400, payload
        assert _error_code(resp) == "validation_error"
    missing = client.patch("/api/cases/run-NOPE", json={"caseName": "x"})
    assert missing.status_code == 404
    assert _error_code(missing) == "report_not_found"


# ---- DELETE /api/cases/{runId}（契约 §14：删事实源 + 索引行 + 同名 .xlsx，一次删净）----


def test_delete_case_full_delete(client: TestClient) -> None:
    """契约 §14 DELETE 唯一口径：run 目录 + 同名 .xlsx + 索引行一起删，
    rebuild 不复活（事实源已删）；已删除再删 → 404 report_not_found。"""
    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    saved = _save_run(client, "PD_DEL")
    run_path = Path(saved["runPath"])

    sid = make_session(client)["id"]
    time.sleep(0.25)
    saved2 = client.post(
        f"/api/sessions/{sid}/save", json={"caseName": "PD_DEL2", "exportExcel": True}
    ).json()
    xlsx = runs_dir / "PD_DEL2.xlsx"
    assert xlsx.is_file()

    # 默认 DELETE：连事实源一起删（与契约 §14 / 前端删除确认框承诺同口径）
    resp = client.delete(f"/api/cases/{saved2['runId']}")
    assert resp.status_code == 204
    assert not (runs_dir / "PD_DEL2").exists()
    assert not xlsx.exists()
    assert _case_ids(client.get("/api/cases").json()) == [saved["runId"]]
    # rebuild 不复活：事实源已删（「只删索引会被 rebuild 复活」——契约 §14 删除动机）
    assert client.post("/api/cases/rebuild").json()["total"] == 1

    resp = client.delete(f"/api/cases/{saved['runId']}")
    assert resp.status_code == 204
    assert not run_path.exists()
    assert client.get("/api/cases").json()["total"] == 0

    # 已删除再删 → 404
    resp = client.delete(f"/api/cases/{saved['runId']}")
    assert resp.status_code == 404
    assert _error_code(resp) == "report_not_found"


# ---- POST /api/reports/compare：saved run 数据面（契约 §9 + M3 扩展）----


def test_compare_saved_runs_passthrough(client: TestClient) -> None:
    run_a = _save_run(client, "PD_CMP_A")["runId"]
    run_b = _save_run(client, "PD_CMP_B")["runId"]
    resp = client.post(
        "/api/reports/compare", json={"baselineRunIds": [run_a], "candidateRunId": run_b}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # perfeval.compare.v1 透传体不加壳（契约 §9）
    assert body["schemaVersion"] == "perfeval.compare.v1"
    for key in ("compareId", "mode", "baselineGroup", "candidateGroup", "status", "metrics", "reliability"):
        assert key in body, key
    assert body["mode"] == "same_app_devices"  # 缺省：契约 §9 明示的错误码所属模式
    # M3 扩展可选 meta（§11.1）：各 runId 的 caseName/device/时长 + 可比前提提示
    meta = body["meta"]
    assert [entry["runId"] for entry in meta["baseline"]] == [run_a]
    assert meta["candidate"][0]["runId"] == run_b
    assert meta["baseline"][0]["caseName"] == "PD_CMP_A"
    assert meta["baseline"][0]["device"] == "synthetic-001"
    assert isinstance(meta["baseline"][0]["durationMs"], int)
    assert "可比前提" in meta["preconditionNote"]


def test_compare_validation_and_not_found(client: TestClient) -> None:
    run_a = _save_run(client, "PD_CMP_ERR")["runId"]
    # <1 条 baseline → 400
    resp = client.post("/api/reports/compare", json={"baselineRunIds": [], "candidateRunId": run_a})
    assert resp.status_code == 400
    assert _error_code(resp) == "validation_error"
    # candidateRunId 非法形态 → 400
    resp = client.post(
        "/api/reports/compare", json={"baselineRunIds": [run_a], "candidateRunId": [run_a]}
    )
    assert resp.status_code == 400
    # run 不存在 → 404 report_not_found
    resp = client.post(
        "/api/reports/compare", json={"baselineRunIds": ["run-NOPE"], "candidateRunId": run_a}
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "report_not_found"
    # run 目录存在但无 summary.json → 404（不可对比）
    no_summary = client.runs_dir / "PD_NO_SUMMARY"  # type: ignore[attr-defined]
    no_summary.mkdir(parents=True)
    resp = client.post(
        "/api/reports/compare", json={"baselineRunIds": ["run-PD_NO_SUMMARY"], "candidateRunId": run_a}
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "report_not_found"


def test_compare_mode_passthrough_and_validation(client: TestClient) -> None:
    """mode 随请求透传（响应 mode 回显）；非法/非字符串 mode → 400 validation_error。
    评审页模式选择器（same_app_devices / same_device_versions）依赖本行为。"""
    run_a = _save_run(client, "PD_CMP_MODE_A")["runId"]
    run_b = _save_run(client, "PD_CMP_MODE_B")["runId"]
    for mode in ("same_app_devices", "same_device_versions"):
        resp = client.post(
            "/api/reports/compare",
            json={"baselineRunIds": [run_a], "candidateRunId": run_b, "mode": mode},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == mode
    for bad_mode in ("bogus", 42, None):
        resp = client.post(
            "/api/reports/compare",
            json={"baselineRunIds": [run_a], "candidateRunId": run_b, "mode": bad_mode},
        )
        assert resp.status_code == 400, bad_mode
        assert _error_code(resp) == "validation_error"


# ---- python -m services.backend.rebuild_index（CLI 重建入口）----


def test_rebuild_cli_entry(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run_dir(
        runs_dir,
        "PD_CLI_A",
        manifest={"runId": "run-PD_CLI_A", "caseName": "PD_CLI_A", "device": "d1",
                  "packageName": "p1", "createdAt": 111},
    )
    _write_run_dir(
        runs_dir,
        "PD_CLI_B",
        manifest={"runId": "run-PD_CLI_B", "caseName": "PD_CLI_B", "device": "d1",
                  "packageName": "p1", "createdAt": 222},
    )
    db = tmp_path / "data" / "cases.db"
    cmd = [
        sys.executable, "-m", "services.backend.rebuild_index",
        "--runs-dir", str(runs_dir), "--db", str(db),
    ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["total"] == 2
    assert Path(payload["db"]) == db.resolve()

    # 幂等 + 陈旧行清除：删掉一个目录后重跑
    shutil.rmtree(runs_dir / "PD_CLI_A")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["total"] == 1


def test_rebuild_cli_entry_matches_endpoint(client: TestClient) -> None:
    """CLI 与 POST /api/cases/rebuild 共用同一 rebuild_case_index 实现。"""
    from app.services.case_index import CaseIndex, rebuild_case_index

    runs_dir = client.runs_dir  # type: ignore[attr-defined]
    _fabricate_three_runs(runs_dir)
    standalone = CaseIndex(client.cases_db_path.parent / "cli-mirror.db")  # type: ignore[attr-defined]
    assert rebuild_case_index(runs_dir, standalone) == 3
    endpoint = client.post("/api/cases/rebuild").json()
    assert endpoint["total"] == 3
    names = {row["caseName"] for row in client.get("/api/cases", params={"limit": 200}).json()["cases"]}
    assert names == {"PD_A", "回归基线", "PD_C"}
