from __future__ import annotations

from perfeval.cli.commands.validate import DEFAULT_BUDGET
from perfeval.core.compare import compare_summaries
from perfeval.core.summary import aggregate_summary
from perfeval.fixtures.synthetic import get_case


def _build_summary(
    run_id: str,
    metric_specs: dict[str, tuple[list[float], str, str]],
    *,
    app: dict[str, object],
    device: dict[str, object],
    thermal_state: str = "nominal",
    scenario_id: str = "startup_home",
    evidence_level: str = "L2",
    artifact_path: str = "artifacts/android/perfetto.pftrace",
) -> dict[str, object]:
    metric_inputs: dict[str, list[dict[str, object]]] = {}
    for metric_id, (values, unit, direction) in metric_specs.items():
        metric_inputs[metric_id] = [
            {
                "value": value,
                "unit": unit,
                "direction": direction,
                "sourceEvents": [f"{run_id}:{metric_id}:{index}"],
                "artifactRefs": [artifact_path],
                "evidenceLevel": evidence_level,
            }
            for index, value in enumerate(values, start=1)
        ]

    return aggregate_summary(
        run_id,
        metric_inputs,
        context={
            "scenarioId": scenario_id,
            "app": app,
            "device": device,
            "environment": {"thermalState": thermal_state},
        },
        evidence_level=[evidence_level],
    )


def _build_group(
    run_prefix: str,
    values: list[float],
    *,
    metric_id: str,
    unit: str,
    direction: str,
    app: dict[str, object],
    device: dict[str, object],
    thermal_state: str = "nominal",
    scenario_id: str = "startup_home",
    evidence_level: str = "L2",
    artifact_path: str = "artifacts/android/perfetto.pftrace",
) -> list[dict[str, object]]:
    group: list[dict[str, object]] = []
    for index, value in enumerate(values, start=1):
        group.append(
            _build_summary(
                f"{run_prefix}-{index:03d}",
                {metric_id: ([value], unit, direction)},
                app=app,
                device=device,
                thermal_state=thermal_state,
                scenario_id=scenario_id,
                evidence_level=evidence_level,
                artifact_path=artifact_path,
            )
        )
    return group


def test_compare_same_device_versions_pass() -> None:
    budget = {
        "defaults": {"minRuns": 5, "recommendedRuns": 5, "confidence": 0.95, "outlierPolicy": "mad_3"},
        "metrics": {
            "startup.cold.ms": {
                "direction": "lower_better",
                "unit": "ms",
                "failAbs": 2500,
                "warnRegressionPct": 0.08,
                "failRegressionPct": 0.15,
                "minDeltaAbs": 80,
            }
        },
    }
    device = {
        "serialOrUdid": "R58R",
        "model": "Pixel 6",
        "osVersion": "14",
        "apiLevel": 34,
        "refreshRateHz": 60,
    }
    baseline = _build_group(
        "baseline",
        [1000, 1000, 1000, 1000, 1000],
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"versionName": "1.2.0", "versionCode": "120", "buildId": "git:abc123", "artifactSha256": "aaa"},
        device=device,
    )
    candidate = _build_group(
        "candidate",
        [1010, 1010, 1010, 1010, 1010],
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"versionName": "1.3.0", "versionCode": "130", "buildId": "git:def456", "artifactSha256": "bbb"},
        device=device,
    )

    compare = compare_summaries(baseline, candidate, mode="same_device_versions", budget=budget, compare_id="compare-pass")

    metric = compare["metrics"]["startup.cold.ms"]
    assert compare["status"] == "pass"
    assert metric["judgement"] == "pass"
    assert metric["delta"]["abs"] == 10.0
    assert metric["delta"]["pct"] == 0.01
    assert metric["budget"]["failRegressionPct"] == 0.15
    assert metric["confidence"]["level"] == "high"
    assert metric["confidence"]["bootstrapCi95"] == [10.0, 10.0]
    assert metric["reliability"]["score"] == 1.0
    assert metric["baseline"]["median"] == 1000.0
    assert metric["candidate"]["median"] == 1010.0


def test_compare_same_device_versions_fail_and_unknown() -> None:
    budget = {
        "defaults": {"minRuns": 5, "recommendedRuns": 5, "confidence": 0.95, "outlierPolicy": "mad_3"},
        "metrics": {
            "startup.cold.ms": {
                "direction": "lower_better",
                "unit": "ms",
                "failAbs": 2500,
                "warnRegressionPct": 0.08,
                "failRegressionPct": 0.15,
                "minDeltaAbs": 80,
            }
        },
    }
    device = {
        "serialOrUdid": "R58R",
        "model": "Pixel 6",
        "osVersion": "14",
        "apiLevel": 34,
        "refreshRateHz": 60,
    }
    baseline = _build_group(
        "baseline",
        [1000, 1000, 1000, 1000, 1000],
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"versionName": "1.2.0", "versionCode": "120", "buildId": "git:abc123", "artifactSha256": "aaa"},
        device=device,
    )
    fail_candidate = _build_group(
        "candidate",
        [1200, 1200, 1200, 1200, 1200],
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"versionName": "1.3.0", "versionCode": "130", "buildId": "git:def456", "artifactSha256": "bbb"},
        device=device,
    )
    unknown_candidate = _build_group(
        "candidate",
        [1200, 1210, 1190],
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"versionName": "1.3.1", "versionCode": "131", "buildId": "git:fedcba", "artifactSha256": "ccc"},
        device=device,
    )

    fail_compare = compare_summaries(baseline, fail_candidate, mode="same_device_versions", budget=budget, compare_id="compare-fail")
    unknown_compare = compare_summaries(baseline[:3], unknown_candidate, mode="same_device_versions", budget=budget, compare_id="compare-unknown")

    fail_metric = fail_compare["metrics"]["startup.cold.ms"]
    unknown_metric = unknown_compare["metrics"]["startup.cold.ms"]

    assert fail_compare["status"] == "fail"
    assert fail_metric["judgement"] == "fail"
    assert fail_metric["reason"] == "regression_pct_exceeds_15"
    assert fail_metric["delta"]["abs"] == 200.0
    assert fail_metric["delta"]["pct"] == 0.2
    assert fail_metric["confidence"]["bootstrapCi95"] == [200.0, 200.0]
    assert fail_metric["reliability"]["score"] == 1.0

    assert unknown_compare["status"] == "unknown"
    assert unknown_metric["judgement"] == "unknown"
    assert unknown_metric["reason"] == "sample_size_below_5"
    assert unknown_metric["delta"]["abs"] == 200.0


def test_compare_same_app_devices_warn_and_invalid_device_mismatch() -> None:
    budget = {
        "defaults": {"minRuns": 5, "recommendedRuns": 5, "confidence": 0.95, "outlierPolicy": "mad_3"},
        "metrics": {
            "frame.jank_rate": {
                "direction": "lower_better",
                "unit": "ratio",
                "warnAbs": 0.05,
                "failAbs": 0.10,
                "warnRegressionAbs": 0.02,
                "failRegressionAbs": 0.05,
                "minDeltaAbs": 0.01,
            }
        },
    }
    baseline = _build_group(
        "baseline",
        [0.03, 0.03, 0.03, 0.03, 0.03],
        metric_id="frame.jank_rate",
        unit="ratio",
        direction="lower_better",
        app={"versionName": "2.0.0", "versionCode": "200", "buildId": "git:aaa", "artifactSha256": "same-app"},
        device={
            "serialOrUdid": "DEVICE-A",
            "model": "Pixel 6",
            "osVersion": "14",
            "apiLevel": 34,
            "refreshRateHz": 60,
        },
    )
    candidate = _build_group(
        "candidate",
        [0.055, 0.055, 0.055, 0.055, 0.055],
        metric_id="frame.jank_rate",
        unit="ratio",
        direction="lower_better",
        app={"versionName": "2.0.0", "versionCode": "200", "buildId": "git:aaa", "artifactSha256": "same-app"},
        device={
            "serialOrUdid": "DEVICE-B",
            "model": "Pixel 7",
            "osVersion": "14",
            "apiLevel": 34,
            "refreshRateHz": 120,
        },
    )

    warn_compare = compare_summaries(baseline, candidate, mode="same_app_devices", budget=budget, compare_id="compare-warn")
    assert warn_compare["status"] == "warn"
    warn_metric = warn_compare["metrics"]["frame.jank_rate"]
    assert warn_metric["judgement"] == "warn"
    assert warn_metric["reason"] == "candidate_exceeds_warn_abs"
    assert abs(warn_metric["delta"]["abs"] - 0.025) < 1e-12
    assert warn_metric["confidence"]["level"] == "high"

    invalid_compare = compare_summaries(baseline, candidate, mode="same_device_versions", budget=budget, compare_id="compare-invalid")
    assert invalid_compare["status"] == "invalid"
    assert "device_mismatch" in invalid_compare["errors"]


def test_compare_uses_metric_higher_better_direction_without_budget_override() -> None:
    device = {"serialOrUdid": "R58R"}
    baseline = _build_group(
        "baseline",
        [90, 90, 90, 90, 90],
        metric_id="throughput.items_per_second",
        unit="items/s",
        direction="higher_better",
        app={"artifactSha256": "aaa"},
        device=device,
    )
    candidate = _build_group(
        "candidate",
        [100, 100, 100, 100, 100],
        metric_id="throughput.items_per_second",
        unit="items/s",
        direction="higher_better",
        app={"artifactSha256": "bbb"},
        device=device,
    )

    compare = compare_summaries(baseline, candidate, mode="same_device_versions")

    metric = compare["metrics"]["throughput.items_per_second"]
    assert metric["direction"] == "higher_better"
    assert metric["judgement"] == "pass"


def test_compare_rejects_mixed_device_runs_inside_one_group() -> None:
    baseline = _build_group(
        "baseline",
        [1000, 1000, 1000, 1000, 1000],
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "aaa"},
        device={"serialOrUdid": "DEVICE-A"},
    )
    baseline[-1]["device"] = {"serialOrUdid": "DEVICE-B"}
    candidate = _build_group(
        "candidate",
        [1000, 1000, 1000, 1000, 1000],
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "bbb"},
        device={"serialOrUdid": "DEVICE-A"},
    )

    compare = compare_summaries(baseline, candidate, mode="same_device_versions")

    assert compare["status"] == "invalid"
    assert "baseline_device_inconsistent" in compare["errors"]


def test_compare_honors_configured_min_runs() -> None:
    budget = {
        "defaults": {"minRuns": 10, "recommendedRuns": 10},
        "metrics": {"startup.cold.ms": {"direction": "lower_better"}},
    }
    device = {"serialOrUdid": "R58R"}
    baseline = _build_group(
        "baseline",
        [1000] * 5,
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "aaa"},
        device=device,
    )
    candidate = _build_group(
        "candidate",
        [1010] * 5,
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "bbb"},
        device=device,
    )

    compare = compare_summaries(baseline, candidate, mode="same_device_versions", budget=budget)

    metric = compare["metrics"]["startup.cold.ms"]
    assert metric["judgement"] == "unknown"
    assert metric["reason"] == "sample_size_below_10"


def test_compare_uses_metric_specific_evidence_level() -> None:
    budget = {
        "defaults": {"minRuns": 5, "recommendedRuns": 5},
        "metrics": {"startup.cold.ms": {"requiredEvidenceLevel": "L2"}},
    }
    device = {"serialOrUdid": "R58R"}
    baseline = _build_group(
        "baseline",
        [1000] * 5,
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "aaa"},
        device=device,
        evidence_level="L0",
    )
    candidate = _build_group(
        "candidate",
        [1010] * 5,
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "bbb"},
        device=device,
        evidence_level="L0",
    )
    for summary in baseline + candidate:
        summary["evidenceLevel"] = ["L4"]

    compare = compare_summaries(baseline, candidate, mode="same_device_versions", budget=budget)

    metric = compare["metrics"]["startup.cold.ms"]
    assert metric["evidence"]["level"] == "L0"
    assert "evidence_below_required" in metric["reliability"]["reasons"]


def test_compare_does_not_fail_regression_threshold_when_ci_overlaps_zero() -> None:
    budget = {
        "defaults": {"minRuns": 5, "recommendedRuns": 5},
        "metrics": {
            "frame.duration.p95.ms": {
                "direction": "lower_better",
                "unit": "ms",
                "failRegressionPct": 0.20,
                "minDeltaAbs": 5,
            }
        },
    }
    device = {"serialOrUdid": "R58R"}
    baseline = _build_group(
        "baseline",
        [15, 15, 20, 18, 10],
        metric_id="frame.duration.p95.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "aaa"},
        device=device,
    )
    candidate = _build_group(
        "candidate",
        [24, 18, 26, 20, 17],
        metric_id="frame.duration.p95.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "bbb"},
        device=device,
    )

    compare = compare_summaries(
        baseline,
        candidate,
        mode="same_device_versions",
        budget=budget,
        compare_id="compare-noisy-p95",
    )

    metric = compare["metrics"]["frame.duration.p95.ms"]
    assert metric["delta"]["abs"] == 5.0
    assert metric["delta"]["bootstrapCi95"][0] <= 0 <= metric["delta"]["bootstrapCi95"][1]
    assert metric["judgement"] == "pass"
    assert metric["reason"] == "noise_overlap_zero"


def test_compare_accepts_compose_activity_migration_for_same_page_family() -> None:
    device = {"serialOrUdid": "R58R"}
    baseline = _build_group(
        "baseline",
        [0.03] * 5,
        metric_id="frame.jank_rate",
        unit="ratio",
        direction="lower_better",
        app={"artifactSha256": "aaa"},
        device=device,
    )
    candidate = _build_group(
        "candidate",
        [0.04] * 5,
        metric_id="frame.jank_rate",
        unit="ratio",
        direction="lower_better",
        app={"artifactSha256": "bbb"},
        device=device,
    )
    for summary in baseline:
        summary["focusedWindow"] = "com.example.app/com.example.app.PersonHomeActivity"
    for summary in candidate:
        summary["focusedWindow"] = "com.example.app/com.example.app.PersonHomeComposeActivity"

    compare = compare_summaries(baseline, candidate, mode="same_device_versions")

    assert compare["status"] == "pass"
    assert "errors" not in compare


def test_compare_rejects_different_focused_page_families() -> None:
    device = {"serialOrUdid": "R58R"}
    baseline = _build_group(
        "baseline",
        [0.03] * 5,
        metric_id="frame.jank_rate",
        unit="ratio",
        direction="lower_better",
        app={"artifactSha256": "aaa"},
        device=device,
    )
    candidate = _build_group(
        "candidate",
        [0.65] * 5,
        metric_id="frame.jank_rate",
        unit="ratio",
        direction="lower_better",
        app={"artifactSha256": "bbb"},
        device=device,
    )
    for summary in baseline:
        summary["focusedWindow"] = "com.example.app/com.example.app.PersonHomeActivity"
    for summary in candidate:
        summary["focusedWindow"] = "com.example.app/com.example.app.HomePageActivity"

    compare = compare_summaries(baseline, candidate, mode="same_device_versions")

    assert compare["status"] == "invalid"
    assert compare["errors"] == ["focused_window_mismatch"]
    assert compare["metrics"] == {}
    assert compare["focusedWindows"] == {
        "baseline": ["com.example.app/com.example.app.PersonHomeActivity"],
        "candidate": ["com.example.app/com.example.app.HomePageActivity"],
    }


# --- stream 指标对比适配（CM-TASK-024，CM-AC-007） ---


def _stream_compare(**kwargs) -> dict:
    fixture = get_case("continuous_stream")
    return compare_summaries(
        [run.summary for run in fixture.baseline_runs],
        [run.summary for run in fixture.candidate_runs],
        mode="same_device_versions",
        **kwargs,
    )


def test_compare_stream_metric_exposes_mean_peak_p95_statistics() -> None:
    compare = _stream_compare()
    stream = compare["metrics"]["frame.duration.detail.ms"]["stream"]

    assert stream["isStream"] is True
    assert set(stream["statistics"]) == {"mean", "peak", "p95"}

    # synthetic 定义：baseline 帧序列 8 尖刺 55ms，candidate 尖刺加倍 110ms
    peak = stream["statistics"]["peak"]
    assert peak["baseline"] == 55.0
    assert peak["candidate"] == 110.0
    assert peak["deltaAbs"] == 55.0
    assert peak["deltaPct"] == 1.0

    # candidate 均值（base +30% 且尖刺加倍）显著抬升，差异列与两列一致
    mean = stream["statistics"]["mean"]
    assert mean["candidate"] > mean["baseline"] * 1.25
    assert mean["deltaAbs"] == mean["candidate"] - mean["baseline"]
    assert abs(mean["deltaPct"] - mean["deltaAbs"] / mean["baseline"]) < 1e-12

    p95 = stream["statistics"]["p95"]
    assert p95["baseline"] == 55.0
    assert p95["candidate"] == 110.0

    # sampleCount 取跨 run 最小值（序列完整性保守口径）；置信度为 summary 分离后口径
    assert stream["sampleCount"] == {"baseline": 60, "candidate": 60}
    assert stream["confidence"] == {"baseline": "low", "candidate": "low"}


def test_compare_stream_series_segmented_per_run() -> None:
    compare = _stream_compare()
    series = compare["metrics"]["cpu.proc.pct"]["stream"]["series"]

    baseline_run_ids = [segment["runId"] for segment in series["baseline"]]
    candidate_run_ids = [segment["runId"] for segment in series["candidate"]]
    assert len(baseline_run_ids) == 5
    assert len(candidate_run_ids) == 5
    assert set(baseline_run_ids).isdisjoint(candidate_run_ids)
    for segment in series["baseline"] + series["candidate"]:
        assert set(segment) == {"runId", "points"}
        assert len(segment["points"]) == 60
        # 点仅携带 ts/v（隐私：不携带产物内容）；段内 ts 按 run 起点单调（跨 run 回绕，不合并）
        assert all(set(point) <= {"ts", "v"} for point in segment["points"])
        ts_values = [point["ts"] for point in segment["points"]]
        assert ts_values == sorted(ts_values)


def test_compare_stream_adaptation_keeps_budget_judgement_inputs() -> None:
    fixture = get_case("continuous_stream")
    compare = compare_summaries(
        [run.summary for run in fixture.baseline_runs],
        [run.summary for run in fixture.candidate_runs],
        mode="same_device_versions",
        budget=DEFAULT_BUDGET,
        compare_id="compare-continuous_stream",
    )

    # CM-AC-007：判定主输入仍为预算指标（非 stream）口径，fail==fail，且无 stream 块
    primary = compare["metrics"]["frame.duration.p95.ms"]
    assert compare["status"] == "fail"
    assert primary["judgement"] == "fail"
    assert primary["reason"] == "regression_pct_exceeds_20"
    assert "stream" not in primary

    # stream 行判定仍走既有 median + 阈值引擎，不因三列适配漂移
    for metric_id in ("cpu.proc.pct", "frame.duration.detail.ms"):
        metric = compare["metrics"][metric_id]
        assert metric["judgement"] == "pass"
        assert metric["reason"] == "within_budget"
        assert metric["stream"]["isStream"] is True


def test_compare_stream_confidence_not_inflated_by_sample_count() -> None:
    fixture = get_case("continuous_stream")
    single = compare_summaries(
        [fixture.baseline_runs[0].summary],
        [fixture.candidate_runs[0].summary],
    )
    metric = single["metrics"]["cpu.proc.pct"]

    # 单 repeat（run 数=1）60 采样点：对比置信度按 run 数分档，不得因 sampleCount 虚高
    assert metric["confidence"]["level"] == "low"
    assert metric["stream"]["confidence"] == {"baseline": "low", "candidate": "low"}
    assert metric["stream"]["sampleCount"] == {"baseline": 60, "candidate": 60}

    full = _stream_compare()
    # 对比行置信度按 run 数（5）计算；流内置信度沿用各 run summary 分离口径（单 run 聚合即 low）
    assert full["metrics"]["cpu.proc.pct"]["confidence"]["level"] == "high"
    assert full["metrics"]["cpu.proc.pct"]["stream"]["confidence"] == {
        "baseline": "low",
        "candidate": "low",
    }


def test_compare_non_stream_metric_output_unchanged() -> None:
    budget = {
        "defaults": {"minRuns": 5, "recommendedRuns": 5, "confidence": 0.95, "outlierPolicy": "mad_3"},
        "metrics": {},
    }
    device = {"serialOrUdid": "R58R"}
    baseline = _build_group(
        "baseline",
        [1000] * 5,
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "aaa"},
        device=device,
    )
    candidate = _build_group(
        "candidate",
        [1010] * 5,
        metric_id="startup.cold.ms",
        unit="ms",
        direction="lower_better",
        app={"artifactSha256": "bbb"},
        device=device,
    )

    compare = compare_summaries(baseline, candidate, mode="same_device_versions", budget=budget)

    metric = compare["metrics"]["startup.cold.ms"]
    assert "stream" not in metric
    assert metric["judgement"] == "pass"
    assert metric["delta"]["abs"] == 10.0
    assert metric["delta"]["pct"] == 0.01


def test_compare_legacy_summary_without_stream_fields_still_compatible() -> None:
    def _legacy_summary(run_id: str, value: float) -> dict:
        return {
            "runId": run_id,
            "scenarioId": "legacy_case",
            "status": "completed",
            "metrics": {"startup.cold.ms": {"value": value, "unit": "ms", "confidence": "high"}},
        }

    compare = compare_summaries(
        [_legacy_summary("legacy-b", 1000.0)],
        [_legacy_summary("legacy-c", 1200.0)],
    )

    metric = compare["metrics"]["startup.cold.ms"]
    assert "stream" not in metric
    assert metric["delta"]["abs"] == 200.0
    assert metric["judgement"] == "unknown"
