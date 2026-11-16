"""M7 diagnostics 端点测试（契约 §22.2，R51）：合成序列走通根因诊断。

构造已知相邻点下降（60→45，25% ≥ 默认阈值 15%）与三类方向明确的伴随指标：
- cpu.total.pct 与 FPS **完全反相线性**（fps 跌 → cpu 升）→ 皮尔逊 = -1（方向对拍）；
- memory.pss.peak_mb 同相强相关（构造关系，r ≈ 0.92）；
- net.rx.kbps 交替噪声 → |r| 小；
- power.current.ma 反相 r=-1 但样本对仅 5 → 可用性降权 0.8（评分公式对拍）。
皮尔逊为纯 Python 等价实现（perfeval ``core/_stats.py`` 无相关基元，模块
docstring 已声明），perfect-linear 的 ±1 是数学事实，非实现自证。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from m7_fixtures import (  # noqa: E402
    EXPECTED_INTERVAL,
    TS_BASE,
    drop_series_rows,
    flat_series_rows,
    write_run_samples,
)

RUN_WITH_DROP = "run-PD_M7_DROP"


@pytest.fixture()
def drop_run(client) -> str:
    return write_run_samples(client, "PD_M7_DROP", drop_series_rows())


# ---- 默认口径：下降区间 + 皮尔逊方向 + 候选排序 ----


def test_diagnostics_default_params_detect_drop_and_direction(client, drop_run) -> None:
    resp = client.get(f"/api/runs/{drop_run}/diagnostics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runId"] == drop_run
    assert body["anchor"] == "frame.fps.mean"
    assert body["params"] == {"dropThresholdPct": 15, "minDurationMs": 1000}

    # 下降区间（golden 不变量：区间自洽 + 命中阈值才检出）
    assert len(body["dropIntervals"]) == 1
    interval = body["dropIntervals"][0]
    assert interval["metricId"] == "frame.fps.mean"
    for key in ("fromMs", "toMs", "durationMs", "dropPct", "baseline", "trough"):
        assert interval[key] == EXPECTED_INTERVAL[key], key
    assert interval["durationMs"] == interval["toMs"] - interval["fromMs"]
    implied = (interval["baseline"] - interval["trough"]) / interval["baseline"] * 100
    assert abs(interval["dropPct"] - implied) < 0.1
    assert interval["dropPct"] >= body["params"]["dropThresholdPct"]

    # 皮尔逊方向（构造关系 → 数学事实）：
    by_metric = {row["metricId"]: row for row in body["correlations"]}
    assert by_metric["cpu.total.pct"]["pearson"] == pytest.approx(-1.0, abs=1e-6)
    assert by_metric["cpu.total.pct"]["sampleCount"] == 10
    assert by_metric["memory.pss.peak_mb"]["pearson"] > 0.9  # 同相强相关
    assert abs(by_metric["net.rx.kbps"]["pearson"]) < 0.5  # 噪声弱相关
    assert by_metric["power.current.ma"]["sampleCount"] == 5
    assert body["note"] is None

    # 候选排序：|score| 降序；score = pearson × weight（样本充足 weight=1.0 →
    # score==pearson；5 对 → 0.8 降权，perfect 相关也排在样本充足强相关之后）
    candidates = body["candidates"]
    assert [c["metricId"] for c in candidates] == [
        "cpu.total.pct",
        "memory.pss.peak_mb",
        "power.current.ma",
        "net.rx.kbps",
    ]
    assert [c["rank"] for c in candidates] == [1, 2, 3, 4]
    assert candidates[0]["score"] == by_metric["cpu.total.pct"]["pearson"]
    assert candidates[1]["score"] == by_metric["memory.pss.peak_mb"]["pearson"]
    assert candidates[2]["score"] == pytest.approx(
        round(by_metric["power.current.ma"]["pearson"] * 0.8, 3)
    )
    scores = [abs(float(c["score"])) for c in candidates]
    assert scores == sorted(scores, reverse=True)
    for cand in candidates:
        assert isinstance(cand["evidence"], str) and cand["evidence"]
    assert "负相关" in candidates[0]["evidence"]  # 方向信息保留在 evidence


# ---- 参数可调：阈值 / 最小持续 / 分析窗口 ----


def test_diagnostics_threshold_param_filters_small_drops(client, drop_run) -> None:
    """dropThresholdPct=50：25% 相邻点跌幅低于阈值 → 不检出。"""
    resp = client.get(f"/api/runs/{drop_run}/diagnostics?dropThresholdPct=50")
    assert resp.status_code == 200
    body = resp.json()
    assert body["params"]["dropThresholdPct"] == 50
    assert body["dropIntervals"] == []
    assert body["correlations"] == [] and body["candidates"] == []
    assert body["note"] is None  # 有数据无下降：计算结果为空，非缺数据


def test_diagnostics_window_params_narrow_analysis(client, drop_run) -> None:
    """fromMs/toMs（M7 加参，§18 同款防护）：截窗后窗口内无下降 → 空区间。"""
    resp = client.get(f"/api/runs/{drop_run}/diagnostics?fromMs={TS_BASE + 10000}")
    assert resp.status_code == 200
    assert resp.json()["dropIntervals"] == []
    resp = client.get(f"/api/runs/{drop_run}/diagnostics?toMs={TS_BASE + 4500}")
    assert resp.status_code == 200
    assert resp.json()["dropIntervals"] == []
    resp = client.get(f"/api/runs/{drop_run}/diagnostics?fromMs=5&toMs=1")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"


# ---- 空结构与错误路径 ----


def test_diagnostics_flat_series_no_drop(client) -> None:
    run_id = write_run_samples(client, "PD_M7_FLAT", flat_series_rows())
    resp = client.get(f"/api/runs/{run_id}/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dropIntervals"] == []
    assert body["correlations"] == [] and body["candidates"] == []
    assert body["note"] is None


def test_diagnostics_anchor_without_data_empty_note(client) -> None:
    """锚指标无数据点 → 200 空结构 + note（契约 golden 形态，不填假零值）。"""
    run_id = write_run_samples(client, "PD_M7_NOANCHOR", [])
    resp = client.get(f"/api/runs/{run_id}/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dropIntervals"] == [] and body["correlations"] == []
    assert body["candidates"] == []
    assert body["note"] == (
        "run 无锚指标 frame.fps.mean 数据点，根因诊断不可计算；不填假零值（[R§8.6]）"
    )


def test_diagnostics_unregistered_anchor_400(client) -> None:
    (client.runs_dir / "PD_M7_ANY").mkdir(parents=True)
    resp = client.get("/api/runs/run-PD_M7_ANY/diagnostics?anchor=not.a.metric")
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "anchor"


@pytest.mark.parametrize(
    "query,field",
    [
        ("dropThresholdPct=0", "dropThresholdPct"),
        ("dropThresholdPct=-3", "dropThresholdPct"),
        ("dropThresholdPct=abc", "dropThresholdPct"),
        ("minDurationMs=0", "minDurationMs"),
        ("minDurationMs=-5", "minDurationMs"),
        ("minDurationMs=1.5", "minDurationMs"),
        ("fromMs=-1", "fromMs"),
    ],
)
def test_diagnostics_param_validation(client, query, field) -> None:
    (client.runs_dir / "PD_M7_ANY").mkdir(parents=True)
    resp = client.get(f"/api/runs/run-PD_M7_ANY/diagnostics?{query}")
    assert resp.status_code == 400, query
    assert resp.json()["error"]["code"] == "validation_error"
    assert resp.json()["error"]["details"]["field"] == field


def test_diagnostics_run_not_found(client) -> None:
    resp = client.get("/api/runs/run-PD_NOWHERE/diagnostics")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "report_not_found"


# ---- 纯函数单元（pearson 边界与区间检测口径）----


def test_pearson_edges() -> None:
    from app.services.diagnostics import pearson

    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
    assert pearson([1, 1, 1], [1, 2, 3]) is None  # 零方差未定义（不出行，不填 0）
    assert pearson([1], [1]) is None  # 样本 <2
    assert pearson([], []) is None


def test_detect_drop_intervals_short_drop_filtered_by_min_duration() -> None:
    from app.services.diagnostics import detect_drop_intervals

    # 60→40 触发但仅持续 500ms（< 默认 1000ms）→ 不产出
    points = [(0, 60.0), (500, 60.0), (1000, 40.0), (1500, 41.0), (2000, 60.0)]
    assert detect_drop_intervals(
        points, metric_id="m", threshold_pct=15, min_duration_ms=1000
    ) == []
    # minDurationMs=500 → 同一序列检出（参数可调）
    intervals = detect_drop_intervals(
        points, metric_id="m", threshold_pct=15, min_duration_ms=500
    )
    assert len(intervals) == 1
    assert intervals[0]["fromMs"] == 1000 and intervals[0]["toMs"] == 1500
    assert intervals[0]["durationMs"] == 500


def test_detect_drop_intervals_non_positive_baseline_skipped() -> None:
    from app.services.diagnostics import detect_drop_intervals

    # 基线非正（0→负）不判跌幅百分比（跳过，不猜）
    points = [(0, 0.0), (1000, -5.0), (2000, -6.0)]
    assert (
        detect_drop_intervals(points, metric_id="m", threshold_pct=15, min_duration_ms=100)
        == []
    )
