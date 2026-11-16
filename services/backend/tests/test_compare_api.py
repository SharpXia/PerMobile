"""§9 compare S3 扩展（compare-budget / compare-no-interval-window / compare-two-groups-only）。

POST /api/reports/compare 新增可选请求字段（§11.1 不升版本）：
- budget（逐指标预算门禁，口径随引擎 _budget_for_metric）；
- fromMs/toMs（时间区间窗口化——窗口过滤单一实现，statistics_service 同一批
  事实源 + perfeval 统计内核）；
- candidateRunIds（N 路 2–10 组；旧单数 candidateRunId 保留兼容）。
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

_BUDGET_MIN = {"defaults": {"minRuns": 1, "recommendedRuns": 1}}


def _write_run(
    runs_dir: Path,
    case_name: str,
    *,
    device: str,
    cpu_value: float,
    samples: list[tuple[int, float]] | None = None,
    extra_metrics: dict[str, dict] | None = None,
) -> str:
    """最小 perfeval.summary.v1 run（summary.json 为主；samples.ndjson 供窗口化）。"""
    run_dir = runs_dir / case_name
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"run-{case_name}"
    metrics: dict[str, dict] = {
        "cpu.total.pct": {
            "value": cpu_value,
            "median": cpu_value,
            "mean": cpu_value,
            "n": 1,
            "unit": "%",
            "direction": "lower_better",
        }
    }
    if extra_metrics:
        metrics.update(extra_metrics)
    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": run_id,
        "status": "completed",
        "metrics": metrics,
        "device": {"serialOrUdid": device},
        "app": {"packageName": "com.example.app"},
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8"
    )
    manifest = {
        "schemaVersion": "perfeval.manifest.v1",
        "runId": run_id,
        "caseName": case_name,
        "device": device,
        "packageName": "com.example.app",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    if samples:
        with (run_dir / "samples.ndjson").open("w", encoding="utf-8") as sink:
            for ts, value in samples:
                sink.write(
                    json.dumps({"metricId": "cpu.total.pct", "tsMs": ts, "value": value})
                    + "\n"
                )
    return run_id


# ---- 缺口1：budget 可配置 ----


def test_compare_budget_changes_verdict(client: TestClient) -> None:
    """同一 fixture：不带 budget → unknown（缺省 minRuns=5 样本不足）；带 budget
    （minRuns=1 + failRegressionPct=0.1）→ fail。判定结论随预算改变。"""
    base = _write_run(client.runs_dir, "PD_BUDGET_BASE", device="D1", cpu_value=10.0)
    cand = _write_run(client.runs_dir, "PD_BUDGET_CAND", device="D2", cpu_value=12.0)
    body = {"baselineRunIds": [base], "candidateRunId": cand, "mode": "same_app_devices"}

    plain = client.post("/api/reports/compare", json=body)
    assert plain.status_code == 200, plain.text
    plain_metric = plain.json()["metrics"]["cpu.total.pct"]
    assert plain_metric["judgement"] == "unknown"
    assert plain_metric["reason"] == "sample_size_below_5"

    budget = {
        "defaults": {"minRuns": 1, "recommendedRuns": 1},
        "metrics": {"cpu.total.pct": {"failRegressionPct": 0.1}},
    }
    gated = client.post("/api/reports/compare", json={**body, "budget": budget})
    assert gated.status_code == 200, gated.text
    gated_metric = gated.json()["metrics"]["cpu.total.pct"]
    assert gated_metric["judgement"] == "fail"
    assert gated_metric["reason"] == "regression_pct_exceeds_10"
    # 预算回显（引擎 _budget_for_metric 合并产物）
    assert gated_metric["budget"]["failRegressionPct"] == 0.1


def test_compare_budget_abs_gate(client: TestClient) -> None:
    """warnRegressionAbs/failRegressionAbs 绝对阈值同样生效（预算编辑入口口径）。"""
    base = _write_run(client.runs_dir, "PD_BUDGET_ABS_B", device="D1", cpu_value=10.0)
    cand = _write_run(client.runs_dir, "PD_BUDGET_ABS_C", device="D2", cpu_value=10.5)
    budget = {
        "defaults": {"minRuns": 1, "recommendedRuns": 1},
        "metrics": {"cpu.total.pct": {"warnRegressionAbs": 0.4}},
    }
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [base],
            "candidateRunId": cand,
            "mode": "same_app_devices",
            "budget": budget,
        },
    )
    assert resp.status_code == 200
    metric = resp.json()["metrics"]["cpu.total.pct"]
    assert metric["judgement"] == "warn"
    assert metric["reason"] == "regression_abs_exceeds_warn"


def test_compare_budget_invalid_400(client: TestClient) -> None:
    """budget 结构非法 → 400 validation_error（非对象/指标项非对象/叶值非标量）。"""
    base = _write_run(client.runs_dir, "PD_BUDGET_INV_B", device="D1", cpu_value=10.0)
    cand = _write_run(client.runs_dir, "PD_BUDGET_INV_C", device="D2", cpu_value=11.0)
    body = {"baselineRunIds": [base], "candidateRunId": cand, "mode": "same_app_devices"}
    invalid_budgets: list[dict] = [
        {"metrics": {"cpu.total.pct": 5}},  # 指标项非对象
        {"defaults": {"failAbs": [1.0]}},  # 叶值非标量
        {"defaults": "x"},  # section 非对象
        {"metrics": {"m": {"nested": {"deep": 1}}}},  # 嵌套容器
    ]
    for budget in invalid_budgets:
        resp = client.post("/api/reports/compare", json={**body, "budget": budget})
        assert resp.status_code == 400, (budget, resp.text)
        assert resp.json()["error"]["code"] == "validation_error"


# ---- 缺口2：时间区间窗口化 ----


def _window_run(runs_dir: Path, case_name: str, device: str) -> tuple[str, list[tuple[int, float]]]:
    """样本点列：ts=1000..9000（9 点，每 1000ms 一点），value=10+idx。"""
    samples = [(1000 * (idx + 1), 10.0 + idx) for idx in range(9)]
    values = [value for _ts, value in samples]
    run_id = _write_run(
        runs_dir,
        case_name,
        device=device,
        cpu_value=statistics.median(values),  # 整段 summary value = 全量中位数
        samples=samples,
    )
    return run_id, samples


def test_compare_window_equals_manual_recut(client: TestClient) -> None:
    """窗口对比结果 == 整段数据手工截窗重算（同 fixture 双路径一致）。

    - 无窗口：基线中位数 == 全量样本中位数（整段 summary 路径）；
    - fromMs=4000,toMs=6000：基线中位数 == 手工截窗 [40,50,60] 中位数 50
      （窗口化路径，samples 闭区间过滤后同一统计内核现算）。
    """
    base, base_samples = _window_run(client.runs_dir, "PD_WIN_BASE", device="D1")
    cand, cand_samples = _window_run(client.runs_dir, "PD_WIN_CAND", device="D2")
    body = {"baselineRunIds": [base], "candidateRunId": cand, "mode": "same_app_devices"}

    whole = client.post("/api/reports/compare", json=body)
    assert whole.status_code == 200
    whole_metric = whole.json()["metrics"]["cpu.total.pct"]
    assert whole_metric["baseline"]["median"] == statistics.median(
        [v for _t, v in base_samples]
    )

    windowed = client.post(
        "/api/reports/compare",
        json={**body, "fromMs": 4000, "toMs": 6000},
    )
    assert windowed.status_code == 200, windowed.text
    win_metric = windowed.json()["metrics"]["cpu.total.pct"]
    # 手工截窗重算（闭区间含端点，§18 同语义）：ts 4000/5000/6000 → 值 13/14/15
    base_cut = [v for t, v in base_samples if 4000 <= t <= 6000]
    cand_cut = [v for t, v in cand_samples if 4000 <= t <= 6000]
    assert base_cut == [13.0, 14.0, 15.0]
    assert win_metric["baseline"]["median"] == statistics.median(base_cut) == 14.0
    assert win_metric["candidate"]["median"] == statistics.median(cand_cut)
    # 区间回显于响应 meta（时戳域 §18：epoch ms）
    meta = windowed.json()["meta"]
    assert meta["fromMs"] == 4000 and meta["toMs"] == 6000
    # 无窗口响应 meta 不回显（§12 形态不变）
    assert "fromMs" not in whole.json()["meta"]


def test_compare_window_invalid_params_400(client: TestClient) -> None:
    """fromMs/toMs 校验同 §18：非整数/负值/from>to → 400 validation_error。"""
    base = _write_run(client.runs_dir, "PD_WIN_INV_B", device="D1", cpu_value=10.0)
    cand = _write_run(client.runs_dir, "PD_WIN_INV_C", device="D2", cpu_value=11.0)
    body = {"baselineRunIds": [base], "candidateRunId": cand, "mode": "same_app_devices"}
    for patch in ({"fromMs": "abc"}, {"fromMs": -1}, {"fromMs": 9000, "toMs": 1000}):
        resp = client.post("/api/reports/compare", json={**body, **patch})
        assert resp.status_code == 400, (patch, resp.text)
        assert resp.json()["error"]["code"] == "validation_error"


def test_compare_window_without_points_is_unknown_not_fabricated(client: TestClient) -> None:
    """窗口内无任何样本点 → 引擎 metrics 空 → status unknown（不伪造结论）。"""
    base, _samples = _window_run(client.runs_dir, "PD_WIN_EMPTY_B", device="D1")
    cand, _samples2 = _window_run(client.runs_dir, "PD_WIN_EMPTY_C", device="D2")
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [base],
            "candidateRunId": cand,
            "mode": "same_app_devices",
            "fromMs": 99000,
            "toMs": 100000,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unknown"
    assert body["metrics"] == {}


def test_compare_window_missing_run_404(client: TestClient) -> None:
    """run 不存在 → 404 report_not_found（§9 既有错误约定，窗口参数不变更）。"""
    base = _write_run(client.runs_dir, "PD_WIN_404_B", device="D1", cpu_value=10.0)
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [base],
            "candidateRunId": "run-NOPE",
            "mode": "same_app_devices",
            "fromMs": 1000,
            "toMs": 2000,
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "report_not_found"


# ---- 缺口3：N 路（2–10 组）----


def _nway_fixture(client: TestClient) -> tuple[list[str], dict]:
    """5 条 run：cpu 值 10/11/12/20/30（+10%/+20%/+100%/+200% 相对基线）。"""
    values = [10.0, 11.0, 12.0, 20.0, 30.0]
    run_ids = [
        _write_run(client.runs_dir, f"PD_NWAY_{idx}", device=f"D{idx}", cpu_value=value)
        for idx, value in enumerate(values)
    ]
    budget = {
        "defaults": {"minRuns": 1, "recommendedRuns": 1},
        "metrics": {"cpu.total.pct": {"warnRegressionPct": 0.05, "failRegressionPct": 0.5}},
    }
    return run_ids, budget


def test_compare_nway_5_groups_matrix(client: TestClient) -> None:
    """5 路判定矩阵：首组=全局基线，pairs 4 对逐对判定，worst=fail 传导至顶层。"""
    run_ids, budget = _nway_fixture(client)
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [run_ids[0]],
            "candidateRunIds": run_ids[1:],
            "mode": "same_app_devices",
            "budget": budget,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schemaVersion"] == "perfeval.compare.v1"  # 顶层兼容，schemaVersion 不变
    assert body["status"] == "fail"  # worst（+200% fail）传导
    assert len(body["groups"]) == 5
    assert [g["index"] for g in body["groups"]] == [0, 1, 2, 3, 4]
    assert body["summary"]["groupCounts"] == [1, 1, 1, 1, 1]
    assert body["summary"]["baselineRuns"] == 1 and body["summary"]["candidateRuns"] == 4

    metric = body["metrics"]["cpu.total.pct"]
    assert metric["judgement"] == "fail"
    assert len(metric["pairs"]) == 4
    pair_verdicts = [(p["groupIndex"], p["judgement"]) for p in metric["pairs"]]
    assert pair_verdicts == [(1, "warn"), (2, "warn"), (3, "fail"), (4, "fail")]
    # headline = 最差对（同分取先出现 → groupIndex=3，+100% fail）
    assert metric["candidate"]["median"] == 20.0
    assert metric["delta"]["pct"] == 1.0
    # perGroup：5 组中位数呈现面（N 路 UI 消费）
    assert [g["groupIndex"] for g in metric["perGroup"]] == [0, 1, 2, 3, 4]
    assert [g["median"] for g in metric["perGroup"]] == [10.0, 11.0, 12.0, 20.0, 30.0]
    # reliability 取各对最小分（保守）
    pair_scores = [p["reliability"]["score"] for p in metric["pairs"]]
    assert metric["reliability"]["score"] == min(pair_scores)
    # meta：候选按序回显
    assert [m["runId"] for m in body["meta"]["candidate"]] == run_ids[1:]


def test_compare_nway_3_groups(client: TestClient) -> None:
    """3 路：groups=3、pairs=2、逐对判定矩阵成立。"""
    run_ids, budget = _nway_fixture(client)
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [run_ids[0]],
            "candidateRunIds": run_ids[1:3],
            "mode": "same_app_devices",
            "budget": budget,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["groups"]) == 3
    metric = body["metrics"]["cpu.total.pct"]
    assert [(p["groupIndex"], p["judgement"]) for p in metric["pairs"]] == [
        (1, "warn"),
        (2, "warn"),
    ]
    assert metric["judgement"] == "warn"
    assert body["meta"]["nWayNote"]


def test_compare_two_way_legacy_request_unchanged(client: TestClient) -> None:
    """2 路旧请求回归不变：旧单数 candidateRunId 路径响应无 groups 字段（逐字段形态
    与历史一致），judgement 语义不变。"""
    run_ids, budget = _nway_fixture(client)
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [run_ids[0]],
            "candidateRunId": run_ids[1],
            "mode": "same_app_devices",
            "budget": budget,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "groups" not in body
    assert body["baselineGroup"] == "baseline" and body["candidateGroup"] == "candidate"
    metric = body["metrics"]["cpu.total.pct"]
    assert metric["judgement"] == "warn"
    assert metric["baseline"]["median"] == 10.0 and metric["candidate"]["median"] == 11.0
    assert "pairs" not in metric and "perGroup" not in metric


def test_compare_candidate_run_ids_precedence_and_limit(client: TestClient) -> None:
    """candidateRunIds 与旧 candidateRunId 同给时以新字段为准；超 9 条候选 → 400。"""
    run_ids, _budget = _nway_fixture(client)
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [run_ids[0]],
            "candidateRunId": run_ids[1],
            "candidateRunIds": [run_ids[1], run_ids[2]],
            "mode": "same_app_devices",
        },
    )
    assert resp.status_code == 200
    assert [m["runId"] for m in resp.json()["meta"]["candidate"]] == [run_ids[1], run_ids[2]]

    too_many = [f"run-NOPE-{i}" for i in range(10)]
    resp2 = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [run_ids[0]],
            "candidateRunIds": too_many,
            "mode": "same_app_devices",
        },
    )
    assert resp2.status_code == 400
    assert resp2.json()["error"]["code"] == "validation_error"


def test_compare_nway_engine_full_metric_intersection(client: TestClient) -> None:
    """引擎全指标交集呈现：memory/cpu/thermal/net 指标同时进判定矩阵
    （compare-web-metrics-limited 呈现面顺带覆盖——对比页按响应 metrics 全量渲染）。"""
    base = _write_run(
        client.runs_dir,
        "PD_NWAY_MX_B",
        device="D1",
        cpu_value=10.0,
        extra_metrics={
            "memory.pss.peak_mb": {"value": 300.0, "median": 300.0, "unit": "MB", "direction": "lower_better"},
            "thermal.max.c": {"value": 38.0, "median": 38.0, "unit": "°C", "direction": "lower_better"},
            "net.rx.kbps": {"value": 1200.0, "median": 1200.0, "unit": "kbps", "direction": "none"},
        },
    )
    cand = _write_run(
        client.runs_dir,
        "PD_NWAY_MX_C",
        device="D2",
        cpu_value=11.0,
        extra_metrics={
            "memory.pss.peak_mb": {"value": 330.0, "median": 330.0, "unit": "MB", "direction": "lower_better"},
            "net.tx.kbps": {"value": 900.0, "median": 900.0, "unit": "kbps", "direction": "none"},
        },
    )
    resp = client.post(
        "/api/reports/compare",
        json={
            "baselineRunIds": [base],
            "candidateRunIds": [cand],
            "mode": "same_app_devices",
            "budget": _BUDGET_MIN,
        },
    )
    assert resp.status_code == 200
    metrics = resp.json()["metrics"]
    # 引擎全指标交集（并集）：memory/cpu/net/thermal 全在矩阵中
    assert set(metrics) == {
        "cpu.total.pct",
        "memory.pss.peak_mb",
        "thermal.max.c",
        "net.rx.kbps",
        "net.tx.kbps",
    }
    assert metrics["net.tx.kbps"]["judgement"] == "unknown"  # 单侧缺失 → missing_metric
    # 预算未设 memory 阈值 → 回归未越界 → within_budget（方向 lower_better，+10%）
    assert metrics["memory.pss.peak_mb"]["judgement"] == "pass"
    assert metrics["memory.pss.peak_mb"]["reason"] == "within_budget"
