from __future__ import annotations

import csv
import json
from pathlib import Path

from perfeval.core.report import build_compare_report, build_summary_report, write_multi_run_report_bundle, write_report_bundle, write_summary_report_bundle
from perfeval.core.report.summary_report import (
    MultiRunMetricRow,
    _build_metric_trend_groups,
    _sample_value_map,
    parse_simpleperf_report_text,
)


def _sample_compare() -> dict:
    return {
        "schemaVersion": "perfeval.compare.v1",
        "compareId": "compare-startup-baseline-candidate",
        "mode": "same_device_versions",
        "baselineGroup": "baseline",
        "candidateGroup": "candidate",
        "status": "fail",
        "scenarioId": "startup_home",
        "device": "Pixel 6 / Android 14",
        "runCount": {"baseline": 10, "candidate": 10},
        "environmentRisks": [
            "thermal state elevated on one candidate run",
            "artifact missing for one warmup run",
        ],
        "metrics": {
            "startup.cold.ms": {
                "priority": "P0",
                "unit": "ms",
                "direction": "lower_better",
                "baseline": {"median": 1450, "p90": 1520, "n": 10},
                "candidate": {"median": 1680, "p90": 1810, "n": 10},
                "deltaAbs": 230,
                "deltaPct": 0.1586,
                "budget": {
                    "failAbs": 2500,
                    "failRegressionPct": 0.15,
                    "minDeltaAbs": 80,
                },
                "judgement": "fail",
                "reason": "regression_pct_exceeds_15",
                "confidence": {
                    "level": "high",
                    "bootstrapCi95": [120, 340],
                },
                "evidence": {
                    "level": "L2",
                    "artifacts": [
                        "artifacts/android/perfetto.pftrace",
                        "artifacts/android/gfxinfo.txt",
                    ],
                },
                "sourceRunIds": ["run-baseline-001", "run-candidate-001"],
            },
            "frame.jank_rate": {
                "priority": "P0",
                "unit": "ratio",
                "direction": "lower_better",
                "baseline": {"median": 0.038, "n": 10},
                "candidate": {"median": 0.059, "n": 10},
                "deltaAbs": 0.021,
                "deltaPct": 0.5526,
                "budget": {"warnAbs": 0.05, "failAbs": 0.1},
                "judgement": "warn",
                "confidence": {"level": "medium"},
                "evidence": {"level": "L1"},
                "sourceRunIds": ["run-baseline-001", "run-candidate-001"],
            },
            "memory.pss.peak_mb": {
                "priority": "P1",
                "unit": "MB",
                "direction": "lower_better",
                "baseline": {"median": 331, "n": 10},
                "candidate": {"median": 343, "n": 10},
                "deltaAbs": 12,
                "deltaPct": 0.0363,
                "judgement": "pass",
                "confidence": {"level": "high"},
                "evidence": {"level": "L1"},
            },
            "cpu.main_thread_busy.ms": {
                "priority": "P1",
                "unit": "ms",
                "direction": "lower_better",
                "baseline": {"median": 210, "n": 3},
                "candidate": {"median": 225, "n": 3},
                "deltaAbs": 15,
                "deltaPct": 0.0714,
                "judgement": "unknown",
                "reason": "sample_size_below_threshold",
                "confidence": {"level": "low"},
                "evidence": {"level": "L0"},
            },
        },
        "artifacts": [
            {
                "id": "timeline.trace.json",
                "path": "artifacts/timeline.trace.json",
                "collector": "host",
                "metric": "startup.cold.ms",
            }
        ],
        "runs": [
            {
                "runId": "run-baseline-001",
                "group": "baseline",
                "appVersion": "1.2.0",
                "device": "Pixel 6",
                "scenario": "startup_home",
                "status": "completed",
                "measurements": [
                    {"metric": "startup.cold.ms", "value": 1450, "unit": "ms"},
                ],
            },
            {
                "runId": "run-candidate-001",
                "group": "candidate",
                "appVersion": "1.3.0",
                "device": "Pixel 6",
                "scenario": "startup_home",
                "status": "completed",
                "measurements": [
                    {"metric": "startup.cold.ms", "value": 1680, "unit": "ms"},
                ],
            },
        ],
    }


def _sample_summary() -> dict:
    return {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "android_fixture_startup-r001",
        "scenarioId": "android_fixture_startup",
        "status": "completed",
        "app": {"platform": "android", "packageName": "com.perfeval.fixture"},
        "device": {"platform": "android"},
        "metrics": {
            "frame.total_count": {"value": 1, "unit": "count", "confidence": "low", "evidenceLevel": "L1", "n": 1},
            "frame.jank_rate": {"value": 1.0, "unit": "ratio", "confidence": "low", "evidenceLevel": "L1", "n": 1},
            "frame.duration.p95.ms": {"value": 1.0, "unit": "ms", "confidence": "low", "evidenceLevel": "L1", "n": 1},
            "memory.pss.peak_mb": {"value": 54.186, "unit": "MB", "confidence": "low", "evidenceLevel": "L1", "n": 1},
            "stability.crash.count": {"value": 0, "unit": "count", "confidence": "low", "evidenceLevel": "L1", "n": 1},
            "stability.anr.count": {"value": 0, "unit": "count", "confidence": "low", "evidenceLevel": "L1", "n": 1},
            "stability.strict_mode.count": {"value": 0, "unit": "count", "confidence": "low", "evidenceLevel": "L1", "n": 1},
        },
        "diagnostics": [{"name": "android.device.run.completed"}],
        "artifacts": [{"id": "gfxinfo", "path": "artifacts/android/gfxinfo.txt"}],
    }


def _memory_summary(value: float, *, memory_profile: str | None = None, budgets: dict | None = None) -> dict:
    summary = _sample_summary()
    summary["metrics"]["frame.jank_rate"]["value"] = 0.01
    summary["metrics"]["frame.duration.p95.ms"]["value"] = 10
    summary["metrics"]["memory.pss.peak_mb"]["value"] = value
    if memory_profile is not None:
        summary["memoryProfile"] = memory_profile
    if budgets is not None:
        summary["budgets"] = budgets
    return summary


def test_report_bundle_is_offline_and_contains_key_data(tmp_path: Path) -> None:
    compare = _sample_compare()
    outputs = write_report_bundle(compare, tmp_path)

    html_text = outputs["report_html"].read_text(encoding="utf-8")
    assert "总体状态：失败" in html_text
    assert "PerfEval Lite 性能对比报告" in html_text
    assert "startup.cold.ms" in html_text
    assert "frame.jank_rate" in html_text
    assert "artifact missing for one warmup run" in html_text
    assert "artifacts/android/perfetto.pftrace" in html_text
    assert "href=\"artifacts/android/perfetto.pftrace\"" in html_text
    assert "https://" not in html_text
    assert "http://" not in html_text
    assert html_text.index("startup.cold.ms") < html_text.index("memory.pss.peak_mb")
    # ECharts vendored library is shipped alongside the HTML for offline use.
    assert "echarts.min.js" in html_text
    assert outputs["echarts_js"].exists()
    assert outputs["echarts_js"].stat().st_size > 100_000  # full echarts library, not a stub
    # Chart containers and Chinese chart titles are present.
    assert "chart-delta" in html_text
    assert "chart-compare" in html_text
    assert "相对差异" in html_text
    assert "基线 vs 对照中位数" in html_text
    assert "柱形统一从最小数量级基线起绘制" in html_text
    assert "var logFloor" in html_text
    assert "var baselineLogValues" in html_text
    assert "type: 'value'" in html_text
    assert "min: 0" in html_text
    assert "data: baselineLogValues" in html_text
    assert "data: candidateLogValues" in html_text
    assert outputs["compare_json"].exists()
    assert outputs["metrics_csv"].exists()
    assert outputs["runs_csv"].exists()

    compare_json = json.loads(outputs["compare_json"].read_text(encoding="utf-8"))
    assert compare_json["compareId"] == compare["compareId"]
    assert compare_json["status"] == "fail"
    assert compare_json["metrics"]["startup.cold.ms"]["judgement"] == "fail"

    metrics_csv = outputs["metrics_csv"].read_text(encoding="utf-8").splitlines()
    assert metrics_csv[0] == (
        "compare_id,mode,scenario,metric,priority,unit,direction,"
        "baseline_median,candidate_median,delta_abs,delta_pct,judgement,confidence"
    )
    assert any("startup.cold.ms" in line for line in metrics_csv[1:])
    assert any("frame.jank_rate" in line for line in metrics_csv[1:])

    with outputs["runs_csv"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["run_id"] == "run-baseline-001"
    assert rows[0]["metric"] == "startup.cold.ms"
    assert rows[1]["run_id"] == "run-candidate-001"
    assert rows[1]["metric"] == "startup.cold.ms"


def test_build_compare_report_contains_sorted_fail_warn_rows() -> None:
    html_text = build_compare_report(_sample_compare())
    fail_pos = html_text.index("startup.cold.ms")
    warn_pos = html_text.index("frame.jank_rate")
    pass_pos = html_text.index("memory.pss.peak_mb")
    unknown_pos = html_text.index("cpu.main_thread_busy.ms")
    assert fail_pos < warn_pos < pass_pos < unknown_pos
    assert "sample_size_below_threshold" in html_text


def test_invalid_compare_report_explains_focused_window_mismatch() -> None:
    html_text = build_compare_report(
        {
            "compareId": "compare-window-mismatch",
            "mode": "same_device_versions",
            "status": "invalid",
            "errors": ["focused_window_mismatch"],
            "focusedWindows": {
                "baseline": ["com.example.app/com.example.app.PersonHomeActivity"],
                "candidate": ["com.example.app/com.example.app.HomePageActivity"],
            },
            "metrics": {},
        }
    )

    assert "输入校验失败：focused_window_mismatch" in html_text
    assert "前台页面不一致" in html_text
    assert "PersonHomeActivity" in html_text
    assert "HomePageActivity" in html_text
    assert "输入无效，未执行指标判定" in html_text
    assert "未检测到退化" not in html_text


def test_summary_report_bundle_renders_chinese_html_and_summary_copy(tmp_path: Path) -> None:
    summary = _sample_summary()
    outputs = write_summary_report_bundle(summary, tmp_path)

    html_text = outputs["report_html"].read_text(encoding="utf-8")
    assert "PerfEval Lite Android 单次采集报告" in html_text
    assert "本次是一次单次 smoke 采集" in html_text
    assert "帧时序（ms）" in html_text
    assert "frame.jank_rate" in html_text
    assert "评级" in html_text
    assert "参考范围" in html_text
    assert outputs["summary_json"].exists()

    copied = json.loads(outputs["summary_json"].read_text(encoding="utf-8"))
    assert copied["runId"] == summary["runId"]
    assert copied["metrics"]["memory.pss.peak_mb"]["value"] == 54.186


def test_summary_report_bundle_integrates_diagnosis_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "android-diagnose" / "full"
    artifact_dir = run_dir / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    gfxinfo = artifact_dir / "gfxinfo.txt"
    perf_data = artifact_dir / "perf.data"
    simpleperf_report = artifact_dir / "simpleperf-report.txt"
    trace = artifact_dir / "perfeval-diagnose.pftrace"
    gfxinfo.write_text("Total frames rendered: 100\n", encoding="utf-8")
    perf_data.write_text("perf data", encoding="utf-8")
    simpleperf_report.write_text("Children  Self  Command\n  40.00%  app  main\n", encoding="utf-8")
    trace.write_text("trace", encoding="utf-8")

    summary = _sample_summary()
    summary["runId"] = "full"
    summary["artifacts"] = [
        {
            "artifactId": "gfxinfo",
            "path": str(gfxinfo),
            "collector": "dumpsys.gfxinfo",
            "sizeBytes": gfxinfo.stat().st_size,
            "metricData": {"relatedMetrics": ["frame.jank_rate"]},
        }
    ]
    diagnosis = {
        "schemaVersion": "perfeval.diagnosis.v1",
        "caseId": "full",
        "mode": "full",
        "modes": ["frame", "cpu", "trace"],
        "serial": "emulator-5554",
        "durationSeconds": 30,
        "createdAtUtc": "2026-07-01T00:00:00Z",
        "diagnostics": ["simpleperf.stop.returncode:1", "simpleperf.pull.returncode:0", "perfetto.pull.returncode:0"],
        "artifacts": [str(perf_data), str(simpleperf_report), str(trace)],
    }

    outputs = write_summary_report_bundle(summary, run_dir / "report", diagnosis=diagnosis)
    html_text = outputs["report_html"].read_text(encoding="utf-8")

    assert "综合评估" in html_text
    assert "分类摘要" in html_text
    assert "CPU 采样" in html_text
    assert "CPU 采样</span><span class=\"value tone-good\">可用" in html_text
    assert "Perfetto Trace" in html_text
    assert "simpleperf-report.txt 摘要" in html_text
    assert "Children  Self  Command" in html_text
    assert "CPU 证据" in html_text
    assert "../artifacts/android/simpleperf-report.txt" in html_text
    assert "perfeval open" in html_text
    assert "Perfetto UI 直接打开" in html_text
    assert "data-trace-href=\"../artifacts/android/perfeval-diagnose.pftrace\"" in html_text
    assert "复制 Android Studio 打开命令" in html_text
    assert "--viewer android-studio" in html_text
    assert "--viewer perfetto-ui" in html_text
    assert "https://ui.perfetto.dev/" in html_text
    assert "<details" in html_text


def test_summary_report_uses_mainstream_memory_profile_by_default() -> None:
    html_text = build_summary_report(_memory_summary(705))

    assert "mainstream 内存档位" in html_text
    assert "PSS 峰值达到 705MB" not in html_text
    assert "需重点优化" not in html_text
    assert "优&lt;400 / 良&lt;800 / 中&lt;1200" in html_text
    assert "PSS峰值内存" in html_text
    assert '<span class="rating good">良</span>' in html_text


def test_summary_report_uses_heavy_memory_profile() -> None:
    html_text = build_summary_report(_memory_summary(705, memory_profile="heavy"))

    assert "heavy 内存档位" in html_text
    assert "优&lt;700 / 良&lt;1200 / 中&lt;1800" in html_text
    assert '<span class="rating good">良</span>' in html_text


def test_summary_report_uses_lightweight_memory_profile_as_risk() -> None:
    html_text = build_summary_report(_memory_summary(705, memory_profile="lightweight"))

    assert "lightweight 内存档位" in html_text
    assert "优&lt;300 / 良&lt;600 / 中&lt;900" in html_text
    assert '<span class="rating fair">中</span>' in html_text
    assert "综合结论</span><span class=\"value hero-value tone-fair\">存在风险" in html_text


def test_summary_report_budget_rating_abs_overrides_memory_profile() -> None:
    html_text = build_summary_report(
        _memory_summary(
            705,
            memory_profile="lightweight",
            budgets={
                "memory.pss.peak_mb": {
                    "direction": "lower_better",
                    "ratingAbs": [1000, 1500, 2000],
                    "failAbs": 2000,
                }
            },
        )
    )

    assert "lightweight 内存档位" in html_text
    assert "优&lt;1000 / 良&lt;1500 / 中&lt;2000" in html_text
    assert '<span class="rating excellent">优</span>' in html_text


def test_multi_run_report_uses_per_run_median_and_custom_budget(tmp_path: Path) -> None:
    row = MultiRunMetricRow(
        metric_id="memory.pss.peak_mb",
        unit="MB",
        direction="lower_better",
        confidence="high",
        evidence="L1",
        n=3,
        min_val=10,
        max_val=30,
        mean=20,
        median=705,
        p75=25,
        p90=28,
        p95=29,
        mad=10,
        samples=[
            {"runId": "r001", "value": 10},
            {"runId": "r001", "value": 30},
            {"runId": "r002", "value": 20},
        ],
    )
    assert _sample_value_map(row) == {"r001": 20.0, "r002": 20.0}

    summary = _memory_summary(
        705,
        memory_profile="lightweight",
        budgets={
            "memory.pss.peak_mb": {
                "direction": "lower_better",
                "ratingAbs": [1000, 1500, 2000],
            }
        },
    )
    summary["repeats"] = 2
    summary["metrics"]["memory.pss.peak_mb"]["samples"] = row.samples
    outputs = write_multi_run_report_bundle(summary, tmp_path / "report")
    html_text = outputs["report_html"].read_text(encoding="utf-8")

    assert "优&lt;1000 / 良&lt;1500 / 中&lt;2000" in html_text


def test_multi_run_report_groups_trends_by_semantics_and_unit(tmp_path: Path) -> None:
    def row(metric_id: str, unit: str, values: list[float]) -> MultiRunMetricRow:
        return MultiRunMetricRow(
            metric_id=metric_id,
            unit=unit,
            direction="lower_better",
            confidence="high",
            evidence="L1",
            n=len(values),
            min_val=min(values),
            max_val=max(values),
            mean=sum(values) / len(values),
            median=values[len(values) // 2],
            p75=max(values),
            p90=max(values),
            p95=max(values),
            mad=0,
            samples=[{"runId": f"r{index + 1:03d}", "value": value} for index, value in enumerate(values)],
        )

    metrics = [
        row("startup.cold.ms", "ms", [1000, 900]),
        row("frame.duration.p95.ms", "ms", [18, 16]),
        row("frame.jank_rate", "ratio", [0.05, 0.03]),
        row("frame.janky_count", "count", [5, 3]),
        row("frame.total_count", "count", [500, 520]),
        row("memory.pss.peak_mb", "MB", [600, 620]),
        row("stability.crash.count", "count", [0, 0]),
    ]

    groups = _build_metric_trend_groups(metrics, ["r001", "r002"])

    assert [group["title"] for group in groups] == [
        "启动耗时",
        "帧耗时分位",
        "卡顿与慢帧比例",
        "渲染帧规模",
        "帧异常事件",
        "内存占用",
        "稳定性事件",
    ]
    rate_group = next(group for group in groups if group["title"] == "卡顿与慢帧比例")
    frame_volume_group = next(group for group in groups if group["title"] == "渲染帧规模")
    assert rate_group["unit"] == "%"
    assert rate_group["series"][0]["values"] == [5.0, 3.0]
    assert "2.00 个百分点" in rate_group["summary"]
    assert rate_group["summary_tone"] == "good"
    assert frame_volume_group["summary_tone"] == "neutral"

    summary = _memory_summary(620)
    summary["repeats"] = 2
    summary["metrics"] = {}
    for metric in metrics:
        summary["metrics"][metric.metric_id] = {
            "value": metric.median,
            "unit": metric.unit,
            "direction": metric.direction,
            "confidence": metric.confidence,
            "evidenceLevel": metric.evidence,
            "n": metric.n,
            "min": metric.min_val,
            "max": metric.max_val,
            "mean": metric.mean,
            "median": metric.median,
            "p75": metric.p75,
            "p90": metric.p90,
            "p95": metric.p95,
            "mad": metric.mad,
            "samples": metric.samples,
        }

    outputs = write_multi_run_report_bundle(summary, tmp_path / "report")
    html_text = outputs["report_html"].read_text(encoding="utf-8")

    assert "指标趋势矩阵" in html_text
    assert "同类指标同轴" in html_text
    assert "独立 Y 轴 · %" in html_text
    assert "type: 'log'" not in html_text
    assert "数值（对数轴）" not in html_text
    assert "trend-chart-6" in html_text
    assert html_text.count('<div class="table-scroll">') == 3


def test_multi_run_report_splits_oversized_trend_groups() -> None:
    metrics = []
    for index in range(7):
        values = [10.0 + index, 11.0 + index]
        metrics.append(
            MultiRunMetricRow(
                metric_id=f"frame.duration.custom_{index}.ms",
                unit="ms",
                direction="lower_better",
                confidence="high",
                evidence="L1",
                n=2,
                min_val=min(values),
                max_val=max(values),
                mean=sum(values) / 2,
                median=sum(values) / 2,
                p75=max(values),
                p90=max(values),
                p95=max(values),
                mad=0,
                samples=[{"runId": "r001", "value": values[0]}, {"runId": "r002", "value": values[1]}],
            )
        )

    groups = _build_metric_trend_groups(metrics, ["r001", "r002"])

    assert [len(group["series"]) for group in groups] == [5, 2]
    assert [group["title"] for group in groups] == ["帧耗时分位（1/2）", "帧耗时分位（2/2）"]


def test_compare_report_blocks_active_artifact_url_schemes() -> None:
    compare = _sample_compare()
    compare["metrics"]["startup.cold.ms"]["evidence"]["artifacts"] = [
        "javascript:alert(document.domain)"
    ]

    html_text = build_compare_report(compare)

    assert 'href="#"' in html_text
    assert 'href="javascript:' not in html_text


def test_summary_report_marks_stale_perf_data_unavailable_when_pull_failed(tmp_path: Path) -> None:
    run_dir = tmp_path / "android-diagnose" / "full-failed"
    artifact_dir = run_dir / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    perf_data = artifact_dir / "perf.data"
    simpleperf_report = artifact_dir / "simpleperf-report.txt"
    simpleperf_stderr = artifact_dir / "simpleperf-record-stderr.txt"
    perf_data.write_text("stale perf data", encoding="utf-8")
    simpleperf_report.write_text(
        "simpleperf E record_file_reader.cpp:88] failed to open record file '/data/local/tmp/perfeval-diagnose-perf.data': No such file or directory\n",
        encoding="utf-8",
    )
    simpleperf_stderr.write_text(
        "simpleperf E environment.cpp:833] Package com.perfeval.gamebox doesn't exist or isn't debuggable/profileable.\n",
        encoding="utf-8",
    )

    summary = _sample_summary()
    summary["runId"] = "full-failed"
    diagnosis = {
        "schemaVersion": "perfeval.diagnosis.v1",
        "caseId": "full-failed",
        "mode": "full",
        "modes": ["frame", "cpu", "trace"],
        "serial": "emulator-5554",
        "durationSeconds": 10,
        "createdAtUtc": "2026-07-01T00:00:00Z",
        "diagnostics": ["simpleperf.pull.returncode:1"],
        "artifacts": [str(perf_data), str(simpleperf_report)],
    }

    outputs = write_summary_report_bundle(summary, run_dir / "report", diagnosis=diagnosis)
    html_text = outputs["report_html"].read_text(encoding="utf-8")

    assert "CPU 采样</span><span class=\"value tone-poor\">失败" in html_text
    assert "本次采集失败，忽略可能残留的旧文件" in html_text
    assert "doesn&#x27;t exist or isn&#x27;t debuggable/profileable" in html_text


def test_build_summary_report_contains_data_quality_note() -> None:
    html_text = build_summary_report(_sample_summary())
    assert "样本数只有 1" in html_text
    assert "没有看到 crash、ANR 或 StrictMode 异常" in html_text


# --- CM-TASK-010：帧耗时分布图初版 ---


def _summary_with_frame_distribution() -> dict:
    from pathlib import Path as _Path

    from perfeval.collectors.android.gfxinfo import parse_framestats_detail_events
    from perfeval.core.summary import summarize_timeline

    text = (_Path("perfeval/fixtures/android/golden/gfxinfo-framestats-detail.txt")).read_text(
        encoding="utf-8"
    )
    events = parse_framestats_detail_events(text, run_id="run-dist")
    return summarize_timeline("run-dist", events)


def test_summary_frame_distribution_data_derived_from_frame_events() -> None:
    summary = _summary_with_frame_distribution()

    distribution = summary["frameDurationDistribution"]
    assert 1 < len(distribution) <= 20
    assert sum(item["count"] for item in distribution) == 21
    assert all({"upToMs", "count"} <= set(item) for item in distribution)
    # 桶上界单调不减
    upper_bounds = [item["upToMs"] for item in distribution]
    assert upper_bounds == sorted(upper_bounds)


def test_summary_report_renders_frame_distribution_section(tmp_path: Path) -> None:
    summary = _summary_with_frame_distribution()
    outputs = write_summary_report_bundle(summary, tmp_path / "report")

    html_text = outputs["report_html"].read_text(encoding="utf-8")
    assert "帧耗时分布" in html_text
    assert 'id="frame-duration-dist"' in html_text
    assert "__frameDistData" in html_text
    assert "FPS 来源：gfxinfo framestats" in html_text
    assert "平均帧率 15.757 fps" in html_text
    assert "掉帧区间（连续 ≥3 个 jank 帧" in html_text
    assert "#3–#6" in html_text
    # 有分布数据时随包分发离线 ECharts
    assert (tmp_path / "report" / "echarts.min.js").exists()


def test_summary_report_omits_frame_distribution_for_aggregate_fallback(tmp_path: Path) -> None:
    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-agg",
        "status": "completed",
        "fpsSource": "aggregate",
        "metrics": {
            "frame.jank_rate": {
                "value": 0.12,
                "unit": "ratio",
                "confidence": "high",
                "evidenceLevel": "L1",
                "n": 1,
            }
        },
        "diagnostics": [],
    }

    outputs = write_summary_report_bundle(summary, tmp_path / "report")

    html_text = outputs["report_html"].read_text(encoding="utf-8")
    assert "帧耗时分布" not in html_text
    assert not (tmp_path / "report" / "echarts.min.js").exists()


# --- CM-TASK-015：统一折线组件 + CPU 曲线 + FPS/CPU 双轴图 ---


def _fake_series(values: list[float], *, ts_step_ms: int = 1000) -> list[dict]:
    return [{"ts": index * ts_step_ms, "v": value} for index, value in enumerate(values)]


def _series_summary(metrics: dict) -> dict:
    return {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-series",
        "status": "completed",
        "metrics": metrics,
        "diagnostics": [],
    }


def _extract_series_chart_payload(html_text: str) -> dict:
    marker = "var __seriesCharts = "
    start = html_text.index(marker) + len(marker)
    payload, _end = json.JSONDecoder().raw_decode(html_text[start:])
    return payload


def _chart_by_id(payload: dict, chart_id: str) -> dict:
    return next(chart for chart in payload["charts"] if chart["id"] == chart_id)


def test_summary_report_renders_cpu_series_chart() -> None:
    summary = _series_summary(
        {
            "cpu.proc.pct": {
                "value": 12.5,
                "unit": "%",
                "confidence": "high",
                "evidenceLevel": "L1",
                "n": 20,
                "series": _fake_series([12.5 + index * 0.5 for index in range(20)]),
            },
            "cpu.total.pct": {
                "value": 40.0,
                "unit": "%",
                "confidence": "high",
                "evidenceLevel": "L1",
                "n": 20,
                "series": _fake_series([40.0 + index for index in range(20)]),
            },
        }
    )

    html_text = build_summary_report(summary)

    assert "连续指标曲线" in html_text
    assert 'id="series-chart-cpu"' in html_text
    chart = _chart_by_id(_extract_series_chart_payload(html_text), "series-chart-cpu")
    assert chart["axisUnits"] == ["%"]
    assert [item["name"] for item in chart["series"]] == ["进程 CPU 占用率", "整机 CPU 占用率"]
    assert chart["series"][0]["data"][0] == [0.0, 12.5]
    # 离线 ECharts 本地引用随脚本内嵌（图表脚本无外链 CDN）
    assert '<script src="echarts.min.js"></script>' in html_text
    assert "<script src=\"http" not in html_text


def test_summary_report_renders_fps_cpu_dual_axis_chart() -> None:
    summary = _series_summary(
        {
            "frame.duration.detail.ms": {
                "value": 20.0,
                "unit": "ms",
                "confidence": "high",
                "evidenceLevel": "L1",
                "n": 30,
                "series": _fake_series([16.6 if index % 2 == 0 else 33.3 for index in range(30)]),
            },
            "frame.fps.mean": {"value": 45.0, "unit": "fps", "direction": "higher_better", "n": 30},
            "frame.fps.median": {"value": 48.0, "unit": "fps", "direction": "higher_better", "n": 30},
            "cpu.proc.pct": {
                "value": 22.0,
                "unit": "%",
                "confidence": "high",
                "evidenceLevel": "L1",
                "n": 30,
                "series": _fake_series([20.0 + index * 0.1 for index in range(30)]),
            },
        }
    )
    summary["fpsSource"] = "framestats"

    html_text = build_summary_report(summary)

    chart = _chart_by_id(_extract_series_chart_payload(html_text), "series-chart-fps-cpu")
    assert chart["axisUnits"] == ["fps", "%"]
    assert [item["name"] for item in chart["series"]] == ["帧率（逐帧耗时反推）", "进程 CPU 占用率"]
    assert [item["yAxisIndex"] for item in chart["series"]] == [0, 1]
    assert chart["series"][0]["data"][0] == [0.0, 1000.0 / 16.6]
    assert chart["series"][1]["yAxisIndex"] == 1
    assert "FPS 来源：gfxinfo framestats" in html_text
    assert "平均帧率 45 fps" in html_text
    assert "中位帧率 48 fps" in html_text


def test_summary_report_series_section_graceful_default_without_series() -> None:
    html_text = build_summary_report(_sample_summary())

    assert "未启用连续采样" in html_text
    assert 'id="series-chart-cpu"' not in html_text
    assert "__seriesCharts" not in html_text


def test_summary_report_short_series_degrades_to_stat_summary() -> None:
    summary = _series_summary(
        {
            "cpu.proc.pct": {
                "value": 13.5,
                "unit": "%",
                "confidence": "low",
                "evidenceLevel": "L1",
                "n": 5,
                "series": _fake_series([12.5, 13.0, 13.5, 14.0, 14.5]),
            }
        }
    )

    html_text = build_summary_report(summary)

    assert "采样点过少（5 点）" in html_text
    assert "min 12.5 / max 14.5 / mean 13.5" in html_text
    assert 'id="series-chart-cpu"' not in html_text
    assert "__seriesCharts" not in html_text


def test_summary_report_bundle_ships_echarts_for_series_charts(tmp_path: Path) -> None:
    summary = _series_summary(
        {
            "cpu.proc.pct": {
                "value": 12.5,
                "unit": "%",
                "confidence": "high",
                "evidenceLevel": "L1",
                "n": 20,
                "series": _fake_series([12.5 + index for index in range(20)]),
            }
        }
    )

    outputs = write_summary_report_bundle(summary, tmp_path / "report")

    echarts_js = tmp_path / "report" / "echarts.min.js"
    assert echarts_js.exists()
    assert echarts_js.stat().st_size > 100_000  # full echarts library, not a stub
    assert 'id="series-chart-cpu"' in outputs["report_html"].read_text(encoding="utf-8")


# --- CM-TASK-019：net/power/gpu/thermal 曲线 + unavailable 降级 chip ---


def _resource_series_summary() -> dict:
    return _series_summary(
        {
            "net.rx.kbps": {
                "value": 120.0,
                "unit": "kbps",
                "n": 20,
                "series": _fake_series([100.0 + index for index in range(20)]),
            },
            "net.tx.kbps": {
                "value": 30.0,
                "unit": "kbps",
                "n": 20,
                "series": _fake_series([20.0 + index for index in range(20)]),
            },
            "power.current.ma": {
                "value": -300.0,
                "unit": "mA",
                "n": 20,
                "series": _fake_series([-300.0 - index for index in range(20)]),
            },
            "gpu.busy.pct": {
                "value": 55.0,
                "unit": "%",
                "n": 20,
                "series": _fake_series([50.0 + index for index in range(20)]),
            },
            "thermal.max.c": {
                "value": 46.0,
                "unit": "°C",
                "n": 24,
                "series": [
                    {"ts": index * 1000, "v": 40.0 + index, "tags": {"zone": "cpu0"}}
                    for index in range(12)
                ]
                + [
                    {"ts": index * 1000, "v": 50.0 - index, "tags": {"zone": "gpu"}}
                    for index in range(12)
                ],
            },
        }
    )


def test_summary_report_renders_resource_metric_charts_with_units() -> None:
    html_text = build_summary_report(_resource_series_summary())

    assert 'id="series-chart-net"' in html_text
    assert 'id="series-chart-power"' in html_text
    assert 'id="series-chart-gpu"' in html_text
    assert 'id="series-chart-thermal"' in html_text
    payload = _extract_series_chart_payload(html_text)
    net = _chart_by_id(payload, "series-chart-net")
    assert net["axisUnits"] == ["kbps"]
    assert [item["name"] for item in net["series"]] == ["下行速率", "上行速率"]
    power = _chart_by_id(payload, "series-chart-power")
    assert power["axisUnits"] == ["mA"]
    assert [item["name"] for item in power["series"]] == ["电池电流"]
    gpu = _chart_by_id(payload, "series-chart-gpu")
    assert gpu["axisUnits"] == ["%"]
    thermal = _chart_by_id(payload, "series-chart-thermal")
    assert thermal["axisUnits"] == ["°C"]
    # thermal series 点带 tags.zone 时按温区拆分为多条线
    assert {item["name"] for item in thermal["series"]} == {"温区 cpu0", "温区 gpu"}
    assert "未启用连续采样" not in html_text


def test_summary_report_renders_unavailable_chips_when_series_missing() -> None:
    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-chips",
        "status": "completed",
        "metrics": {},
        "diagnostics": [
            "sampler.net.unavailable:/proc/net/xt_qtaguid read failed",
            "sampler.gpu_thermal.failed:dumpsys thermalservice exited 1",
        ],
    }

    html_text = build_summary_report(summary)

    # 全 chip 状态：区块仍渲染，缺数据指标显示原因 chip（诊断原文摘要），无图无脚本
    assert "连续指标曲线" in html_text
    assert 'class="badge"' in html_text
    assert "net.rx.kbps：sampler.net.unavailable:/proc/net/xt_qtaguid read failed" in html_text
    assert "net.tx.kbps：sampler.net.unavailable" in html_text
    assert "gpu.busy.pct：sampler.gpu_thermal.failed" in html_text
    assert "thermal.max.c：sampler.gpu_thermal.failed" in html_text
    assert "power.current.ma：无采样数据" in html_text
    assert "未启用连续采样" in html_text
    assert "__seriesCharts" not in html_text


def test_summary_report_power_degraded_source_renders_level_series() -> None:
    summary = _series_summary(
        {
            "power.level.pct": {
                "value": -0.2,
                "unit": "%/min",
                "n": 20,
                "series": _fake_series([-0.1 - index * 0.05 for index in range(20)]),
            },
            "power.energy.mah": {"value": 12.5, "unit": "mAh", "n": 20},
        }
    )
    summary["diagnostics"] = ["sampler.power.unavailable:current_now missing"]

    html_text = build_summary_report(summary)

    chart = _chart_by_id(_extract_series_chart_payload(html_text), "series-chart-power")
    assert chart["axisUnits"] == ["%/min"]
    assert [item["name"] for item in chart["series"]] == ["电量变化速率"]
    assert "估算能耗 12.5 mAh" in html_text
    assert "power.current.ma：sampler.power.unavailable" in html_text


def test_summary_report_metric_without_series_but_with_n_shows_insufficient_chip() -> None:
    summary = _series_summary(
        {
            # gfxinfo golden 只有 21 帧（< 32 阈值），有指标无 series
            "net.rx.kbps": {"value": 88.0, "unit": "kbps", "n": 21},
        }
    )

    html_text = build_summary_report(summary)

    assert "net.rx.kbps：样本数 21，未形成连续序列" in html_text


# --- CM-TASK-022：报告录屏回放区（video + 时间轴游标 + 标记跳转） ---


def _screen_summary(**overrides) -> dict:
    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-screen-r001",
        "scenarioId": "screen_playback",
        "status": "completed",
        "app": {"platform": "android", "packageName": "com.perfeval.fixture"},
        "metrics": {
            "frame.jank_rate": {"value": 0.02, "unit": "ratio", "confidence": "medium", "evidenceLevel": "L1", "n": 40}
        },
        "jankSegments": [
            {"startIndex": 2, "endIndex": 5, "frameCount": 4, "startTsEpochMs": 1757300000032, "endTsEpochMs": 1757300000080},
            {"startIndex": 8, "endIndex": 10, "frameCount": 3},
        ],
        "clockAnchors": {
            "t0EpochMs": 1757300000000,
            "t0Source": "logcat.epoch.first_line",
            "clockDomain": "device_epoch",
            # B1（TASK-023-fix R1）：SamplingScheduler.start() 同刻设备时钟——
            # 采样序列 ts 原点晚于 t0 约 2s setup 间隙（真实 run 的系统性偏移）
            "samplers": {"startEpochMs": 1757300002000, "source": "device_date"},
            "video": {
                "startEpochMs": 1757300001100,
                "firstFrameEpochMs": 1757300002100,
                "offsetMs": 2100,
                "method": "videoStart+1000ms encoding delay (SCREEN_RECORD_SYNC §2)",
            },
            "markers": [{"name": "flow.step.login", "epochMs": 1757300003000}],
        },
        "diagnostics": [],
        "artifacts": [
            {
                "artifactId": "screen",
                "path": "artifacts/android/screen.mp4",
                "collector": "screenrecord",
                "metricData": {"relatedMetrics": []},
            }
        ],
    }
    summary.update(overrides)
    return summary


def _write_video(summary: dict, base: Path) -> None:
    video_path = base / "artifacts" / "android" / "screen.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")


def test_playback_section_renders_video_and_jump_markers(tmp_path: Path) -> None:
    base = tmp_path / "run"
    summary = _screen_summary()
    _write_video(summary, base)

    html_text = build_summary_report(summary, base_dir=base, report_dir=base / "report")

    assert "录屏回放" in html_text
    assert "<video" in html_text
    # 视频为相对路径引用（不内嵌内容）
    assert 'src="../artifacts/android/screen.mp4"' in html_text
    assert "video.currentTime" in html_text
    assert "时间轴游标" in html_text
    assert 'id="screen-playback-cursor"' in html_text
    # 跳转秒数 = (eventEpoch - videoFirstFrameEpoch)/1000：
    # jank 段起点 1757300000032 → (32-2100)/1000 → 0（钳到 0）；marker 1757300003000 → 0.9s
    assert 'data-video-jump="0.000"' in html_text
    assert 'data-video-jump="0.900"' in html_text
    assert "掉帧 #2–#5" in html_text
    assert "flow.step.login" in html_text
    # framestats 段（无 epoch）标记为不可跳转 + tp 对齐提示
    assert "掉帧 #8–#10（不可跳转）" in html_text
    assert "fpsSource=tp" in html_text
    # 偏移口径声明
    assert "2100ms" in html_text


def test_playback_section_hidden_when_screen_record_disabled() -> None:
    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-no-screen",
        "scenarioId": "screen_off",
        "status": "completed",
        "metrics": {},
        "diagnostics": [],
        "artifacts": [{"artifactId": "gfxinfo", "path": "artifacts/android/gfxinfo.txt"}],
    }

    html_text = build_summary_report(summary, base_dir=None, report_dir=None)

    assert "录屏回放" not in html_text


def test_playback_section_shows_unavailable_reason_keeps_markers(tmp_path: Path) -> None:
    summary = _screen_summary(
        artifacts=[],
        diagnostics=[
            {
                "name": "screen.record.unavailable",
                "data": {
                    "severity": "warning",
                    "code": "screen.record.unavailable",
                    "message": "screen.record.unavailable:screenrecord_not_found:/system/bin/sh: not found",
                },
            }
        ],
    )

    html_text = build_summary_report(summary, base_dir=tmp_path, report_dir=tmp_path / "report")

    assert "录屏回放" in html_text
    assert "<video" not in html_text
    assert "screen.record.unavailable:screenrecord_not_found" in html_text
    assert "保留时间轴标记" in html_text
    # 指标时间轴标记保留（无 epoch 时不可跳转）
    assert "掉帧 #2–#5（不可跳转）" in html_text


def test_playback_jumps_disabled_without_clock_anchors(tmp_path: Path) -> None:
    base = tmp_path / "run-offline"
    summary = _screen_summary()
    # 离线 --android-artifacts 模式：有视频但无 clockAnchors / 无 epoch 段
    summary["clockAnchors"] = {
        "t0EpochMs": 1757300000000,
        "t0Source": "logcat.epoch.first_line",
        "clockDomain": "device_epoch",
    }
    summary["jankSegments"] = [
        {"startIndex": 2, "endIndex": 5, "frameCount": 4, "startTsEpochMs": 1757300000032, "endTsEpochMs": 1757300000080},
    ]
    _write_video(summary, base)

    html_text = build_summary_report(summary, base_dir=base, report_dir=base / "report")

    assert "<video" in html_text
    assert 'src="../artifacts/android/screen.mp4"' in html_text
    # 有 epoch 标记但缺视频锚点 → 无法换算 videoSeconds → 禁用跳转
    assert 'data-video-jump="0.' not in html_text
    assert "无法精确跳转" in html_text


def test_playback_video_without_markers_still_renders(tmp_path: Path) -> None:
    base = tmp_path / "run-plain"
    summary = _screen_summary(jankSegments=[], clockAnchors={
        "t0EpochMs": 1757300000000,
        "clockDomain": "device_epoch",
        "video": {"startEpochMs": 1757300001100, "firstFrameEpochMs": 1757300002100, "offsetMs": 2100},
    })
    _write_video(summary, base)

    html_text = build_summary_report(summary, base_dir=base, report_dir=base / "report")

    assert "录屏回放" in html_text
    assert "<video" in html_text
    assert "无跳转标记" in html_text


def test_playback_segmented_run_selects_first_segment(tmp_path: Path) -> None:
    """F-fix（R1-B1）：分段 run 首段 screen.mp4 排首——字典序会让
    screen-seg002.mp4（'-' < '.'）冒充首段，画面错位、跳转整体偏移。"""
    base = tmp_path / "run-seg"
    summary = _screen_summary(
        artifacts=[
            {
                # 故意让 seg002 排在 artifacts 列表首位（字典序陷阱复现）
                "artifactId": "screen_seg002",
                "path": "artifacts/android/screen-seg002.mp4",
                "collector": "screenrecord",
                "metricData": {"relatedMetrics": []},
            },
            {
                "artifactId": "screen",
                "path": "artifacts/android/screen.mp4",
                "collector": "screenrecord",
                "metricData": {"relatedMetrics": []},
            },
        ]
    )
    for name in ("screen.mp4", "screen-seg002.mp4"):
        video = base / "artifacts" / "android" / name
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-" + name.encode())

    html_text = build_summary_report(summary, base_dir=base, report_dir=base / "report")

    # <video> src 必须指向首段 screen.mp4
    assert "<video" in html_text
    assert 'src="../artifacts/android/screen.mp4"' in html_text
    assert "screen-seg002.mp4" not in html_text.split("录屏回放", 1)[1].split("</section>", 1)[0]
    # 排序函数契约：exact screen.mp4 → seg 按段号数值序
    from perfeval.core.report.summary_report import SummaryArtifact, _playback_video_artifacts

    def _artifact(path_name: str) -> SummaryArtifact:
        return SummaryArtifact(
            artifact_id=path_name,
            path=f"artifacts/android/{path_name}",
            href=path_name,
            collector="screenrecord",
            size_bytes=1,
            related_metrics=[],
            category="other",
            present=True,
            note="可用",
        )

    ordered = _playback_video_artifacts(
        type(
            "R",
            (),
            {"artifacts": [_artifact(n) for n in ("screen-seg010.mp4", "screen-seg002.mp4", "screen.mp4", "gfxinfo.txt")]},
        )()
    )
    assert [Path(a.path).name for a in ordered] == [
        "screen.mp4",
        "screen-seg002.mp4",
        "screen-seg010.mp4",
    ]


# --- 对比报告 stream 适配（CM-TASK-024，CM-AC-007）：三统计量卡片 + series 叠加图 ---


def _continuous_stream_compare() -> dict:
    from perfeval.cli.commands.validate import DEFAULT_BUDGET
    from perfeval.core.compare import compare_summaries
    from perfeval.fixtures.synthetic import get_case

    fixture = get_case("continuous_stream")
    return compare_summaries(
        [run.summary for run in fixture.baseline_runs],
        [run.summary for run in fixture.candidate_runs],
        mode="same_device_versions",
        budget=DEFAULT_BUDGET,
        compare_id="compare-continuous_stream",
    )


def test_compare_report_renders_stream_statistics_and_overlay_chart() -> None:
    html_text = build_compare_report(_continuous_stream_compare())

    # 流式聚合三列对比卡片（均值/峰值/P95 × 基线/对照/差异）与置信度说明
    assert "流式聚合对比" in html_text
    assert "均值" in html_text
    assert "峰值" in html_text
    assert "P95" in html_text
    assert "采样点数/run：基线 60，对照 60" in html_text
    assert "不随采样点数虚高" in html_text

    # series 叠加图：每个 stream 指标一图，容器与渲染数据齐备
    assert "连续指标叠加" in html_text
    assert 'id="chart-stream-0"' in html_text
    assert 'id="chart-stream-1"' in html_text
    assert "var streamCharts = data.stream_charts" in html_text
    assert "type: 'line'" in html_text

    # X 轴口径（相对 run 秒）与多 repeat 按 run 分段说明（series ts 跨 run 回绕）
    assert "相对 run 时间" in html_text
    assert "按 run 分段" in html_text
    assert "不跨 run 连线" in html_text

    # 每 run 分段数据进入图表 JSON（runId 可回溯到具体 repeat）
    assert "continuous_stream-baseline-r001" in html_text
    assert "continuous_stream-candidate-r005" in html_text


def test_compare_report_stream_chart_escapes_untrusted_labels() -> None:
    compare = {
        "compareId": "compare-stream-xss",
        "mode": "same_device_versions",
        "status": "fail",
        "metrics": {
            "cpu.<b>bold</b>.pct": {
                "priority": "P1",
                "unit": "%",
                "direction": "lower_better",
                "baseline": {"median": 1.0, "n": 5},
                "candidate": {"median": 2.0, "n": 5},
                "deltaAbs": 1.0,
                "deltaPct": 1.0,
                "judgement": "fail",
                "confidence": {"level": "low"},
                "stream": {
                    "isStream": True,
                    "sampleCount": {"baseline": 2, "candidate": 2},
                    "confidence": {"baseline": "low", "candidate": "low"},
                    "statistics": {
                        "mean": {"baseline": 1.0, "candidate": 2.0, "deltaAbs": 1.0, "deltaPct": 1.0},
                        "peak": {"baseline": 1.5, "candidate": 2.5, "deltaAbs": 1.0, "deltaPct": 2 / 3},
                        "p95": {"baseline": 1.9, "candidate": 2.9, "deltaAbs": 1.0, "deltaPct": 0.5},
                    },
                    "series": {
                        "baseline": [
                            {
                                "runId": "run<img src=x onerror=alert(1)>",
                                "points": [{"ts": 0, "v": 1.0}, {"ts": 1000, "v": 1.5}],
                            }
                        ],
                        "candidate": [
                            {
                                "runId": "run</script><script>alert(1)</script>",
                                "points": [{"ts": 0, "v": 2.0}],
                            }
                        ],
                    },
                },
            }
        },
    }

    html_text = build_compare_report(compare)

    # 可见区域（指标名/图题/统计表）转义
    assert "cpu.&lt;b&gt;bold&lt;/b&gt;.pct" in html_text
    # runId 服务端转义 + 图表 JSON `</` 防注入：原始载荷不得出现在报告任何位置
    assert "<img src=x onerror=alert(1)>" not in html_text
    assert "</script><script>alert(1)" not in html_text
    assert "run&lt;img src=x" in html_text
    # tooltip 侧兜底转义函数存在（echarts tooltip 以 HTML 内插渲染）
    assert "function escHtml" in html_text


def test_compare_report_without_stream_metrics_has_no_stream_section() -> None:
    html_text = build_compare_report(_sample_compare())

    assert "连续指标叠加" not in html_text
    assert "流式聚合对比" not in html_text
    assert '"stream_charts": []' in html_text  # 键恒存在，无 stream 指标时为空


def test_report_bundle_with_stream_metric_keeps_offline_and_csv_contract(tmp_path: Path) -> None:
    outputs = write_report_bundle(_continuous_stream_compare(), tmp_path)

    html_text = outputs["report_html"].read_text(encoding="utf-8")
    # 离线合规：echarts vendor 随包分发，报告无外链
    assert "http://" not in html_text
    assert "https://" not in html_text
    assert outputs["echarts_js"].stat().st_size > 100_000
    assert 'id="chart-stream-0"' in html_text

    # metrics.csv 列契约不变：stream 行仍以跨 run 中位数列呈现
    metrics_csv = outputs["metrics_csv"].read_text(encoding="utf-8").splitlines()
    assert metrics_csv[0] == (
        "compare_id,mode,scenario,metric,priority,unit,direction,"
        "baseline_median,candidate_median,delta_abs,delta_pct,judgement,confidence"
    )
    assert any("frame.duration.p95.ms" in line for line in metrics_csv[1:])
    assert any("cpu.proc.pct" in line for line in metrics_csv[1:])

    # compare.json 保留 stream 块（统计量 + series 分段）
    compare_json = json.loads(outputs["compare_json"].read_text(encoding="utf-8"))
    assert compare_json["status"] == "fail"
    assert compare_json["metrics"]["cpu.proc.pct"]["stream"]["isStream"] is True
    assert compare_json["metrics"]["frame.duration.p95.ms"]["judgement"] == "fail"
# --- CM-TASK-023：口径声明横幅 + 事件竖线联动 + hover/点击联动收口 + 遗留修 ---


def test_summary_report_renders_scope_banner_as_first_section() -> None:
    summary = _sample_summary()

    html_text = build_summary_report(summary)

    # 关键口径文案（与 CAPABILITY_SCOPE §4 / METRIC_SOURCES §3 / SAMPLING_FRAMEWORK §5 一致）
    assert "口径声明" in html_text
    assert "不可直接对标" in html_text
    assert "逐帧渲染耗时反推" in html_text
    assert "整机容量归一" in html_text
    assert "xt_qtaguid" in html_text
    assert "估算能耗" in html_text
    assert "真实峰值" in html_text
    assert "final_snapshot" in html_text
    assert "数据不出本地" in html_text
    # 横幅为静态 HTML（无脚本、无外链），且位于标题之下、所有业务区块之前
    banner_slice = html_text.split("口径声明", 1)[1].split("分类摘要", 1)[0]
    assert "<script" not in banner_slice
    assert "http://" not in banner_slice and "https://" not in banner_slice
    assert html_text.index("口径声明") > html_text.index("PerfEval Lite Android 单次采集报告")
    assert html_text.index("口径声明") < html_text.index("分类摘要")
    # 无 fpsSource 的旧数据优雅缺省
    assert "未标注（无逐帧来源）" in banner_slice


def test_summary_report_scope_banner_shows_actual_fps_source() -> None:
    summary = _sample_summary()
    summary["fpsSource"] = "tp"

    html_text = build_summary_report(summary)

    assert "本次 run 实际生效：FPS 来源：Perfetto frame timeline" in html_text


def test_summary_report_renders_event_marker_lines_and_playback_linkage(tmp_path: Path) -> None:
    base = tmp_path / "run"
    summary = _screen_summary()
    summary["metrics"]["cpu.proc.pct"] = {
        "value": 12.5,
        "unit": "%",
        "n": 20,
        "series": _fake_series([10.0 + index for index in range(20)]),
    }
    summary["metrics"]["net.rx.kbps"] = {
        "value": 120.0,
        "unit": "kbps",
        "n": 20,
        "series": _fake_series([100.0 + index for index in range(20)]),
    }
    # 帧序列（原点=首帧，非 scheduler.start()）：fps/cpu 双轴图 frame_relative
    summary["metrics"]["frame.duration.detail.ms"] = {
        "value": 16.6,
        "unit": "ms",
        "n": 20,
        "series": _fake_series([16.6 if index % 2 == 0 else 33.3 for index in range(20)]),
    }
    summary["fpsSource"] = "framestats"
    _write_video(summary, base)

    html_text = build_summary_report(summary, base_dir=base, report_dir=base / "report")

    payload = _extract_series_chart_payload(html_text)
    # B1（R1）：sampler 系图竖线以 samplers.startEpochMs 为 epoch 基准（与序列同轴）——
    # marker epoch 1757300003000、start 1757300002000 → x = 1.0s；
    # 误用 t0（1757300000000）会得到焊死的 3.0（系统性偏移 2s setup 间隙）
    for chart_id in ("series-chart-cpu", "series-chart-net"):
        chart = _chart_by_id(payload, chart_id)
        assert chart["timeBase"] == {"domain": "sampler_epoch", "startEpochMs": 1757300002000}
        assert chart["markers"] == [{"name": "flow.step.login", "x": 1.0, "videoSeconds": 0.9}]
    # fps/cpu 双轴图：帧序列相对首帧 → frame_relative，不画竖线并注明原因
    fps_chart = _chart_by_id(payload, "series-chart-fps-cpu")
    assert fps_chart["timeBase"] == {"domain": "frame_relative"}
    assert "markers" not in fps_chart
    assert "本图不叠加事件竖线" in html_text
    # 图区点击 → 回放秒换算：图内 epoch 基准用 timeBase.startEpochMs，视频侧共用 timeAlign
    assert payload["timeAlign"] == {"videoFirstFrameEpochMs": 1757300002100}
    assert "timeBase.domain === 'sampler_epoch'" in html_text
    # 联动回调：回放区定义 + 折线组件引用（点击竖线/图区调 __perfevalSeekPlayback）
    assert html_text.count("__perfevalSeekPlayback") >= 2
    assert "getZr" in html_text and "convertFromPixel" in html_text
    # hover 最近事件与点击命中共用一个阈值常量
    assert "MARKER_NEAR_S" in html_text
    # hover tooltip：各序列值 + 最近 marker 名
    assert "事件：" in html_text


def test_summary_report_series_markers_without_video_disable_jump(tmp_path: Path) -> None:
    summary = _screen_summary(artifacts=[])
    summary["metrics"]["cpu.proc.pct"] = {
        "value": 12.5,
        "unit": "%",
        "n": 20,
        "series": _fake_series([10.0 + index for index in range(20)]),
    }

    html_text = build_summary_report(summary, base_dir=None, report_dir=None)

    payload = _extract_series_chart_payload(html_text)
    chart = _chart_by_id(payload, "series-chart-cpu")
    # 有 samplers 锚点 → 竖线可画（x 以 samplers.startEpochMs 为基准）；
    # 无视频 → 无 videoSeconds（跳转优雅禁用）
    assert chart["timeBase"] == {"domain": "sampler_epoch", "startEpochMs": 1757300002000}
    assert chart["markers"] == [{"name": "flow.step.login", "x": 1.0}]
    # 无录屏 → 回放区不渲染（回调无定义处）；折线组件以 typeof 守卫优雅禁用点击
    assert "window.__screenPlayback" not in html_text
    assert "typeof seek === 'function'" in html_text


def test_summary_report_series_charts_render_without_markers_for_legacy_data() -> None:
    summary = _series_summary(
        {
            "cpu.proc.pct": {
                "value": 12.5,
                "unit": "%",
                "n": 20,
                "series": _fake_series([10.0 + index for index in range(20)]),
            }
        }
    )
    # 完全无 clockAnchors（旧数据）：无竖线无 timeBase，图正常渲染
    html_text = build_summary_report(summary)
    payload = _extract_series_chart_payload(html_text)
    chart = _chart_by_id(payload, "series-chart-cpu")
    assert "markers" not in chart
    assert "timeBase" not in chart
    assert "timeAlign" not in payload
    assert 'id="series-chart-cpu"' in html_text

    # TASK-021 旧数据：有 t0 + marker epoch 但无 samplers 锚点——X 轴基准未知，
    # 竖线优雅不渲染（不冒错位竖线）
    summary["clockAnchors"] = {
        "t0EpochMs": 1757300000000,
        "clockDomain": "device_epoch",
        "markers": [{"name": "flow.step.login", "epochMs": 1757300003000}],
    }
    html_text = build_summary_report(summary)
    chart = _chart_by_id(_extract_series_chart_payload(html_text), "series-chart-cpu")
    assert "markers" not in chart
    assert "timeBase" not in chart

    # markers 全部缺 epochMs（有 samplers 锚点）→ 同样优雅跳过
    summary["clockAnchors"] = {
        "t0EpochMs": 1000,
        "samplers": {"startEpochMs": 1000, "source": "device_date"},
        "markers": [{"name": "flow.no-epoch"}],
    }
    html_text = build_summary_report(summary)
    chart = _chart_by_id(_extract_series_chart_payload(html_text), "series-chart-cpu")
    assert "markers" not in chart


def test_summary_report_series_marker_names_are_script_safe() -> None:
    summary = _series_summary(
        {
            "cpu.proc.pct": {
                "value": 12.5,
                "unit": "%",
                "n": 20,
                "series": _fake_series([10.0 + index for index in range(20)]),
            }
        }
    )
    # marker 名来自设备 logcat（不可信外部数据）
    summary["clockAnchors"] = {
        "t0EpochMs": 1000,
        "samplers": {"startEpochMs": 1000, "source": "device_date"},
        "markers": [{"name": "flow.</script><img src=x onerror=alert(1)>", "epochMs": 4000}],
    }

    html_text = build_summary_report(summary)

    # JSON payload 中 </ 被转义，不发生 </script> 逃逸
    assert "</script><img" not in html_text
    assert "flow.<\\/script><img" in html_text
    # tooltip（HTML 渲染）侧对 marker 名做 HTML 转义
    assert "replace(/&/g, '&amp;')" in html_text


def test_summary_report_all_short_series_keeps_stat_summary_without_hint() -> None:
    """遗留修：全短序列（各 series 非空但 < 10 点）时统计摘要与
    "未启用连续采样"提示不再并存自相矛盾。"""
    summary = _series_summary(
        {
            "cpu.proc.pct": {
                "value": 13.5,
                "unit": "%",
                "n": 5,
                "series": _fake_series([12.5, 13.0, 13.5, 14.0, 14.5]),
            }
        }
    )

    html_text = build_summary_report(summary)

    assert "采样点过少（5 点）" in html_text
    assert "min 12.5 / max 14.5 / mean 13.5" in html_text
    assert "未启用连续采样" not in html_text


def test_summary_report_chips_match_mapping_diagnostics_by_code_and_message() -> None:
    """遗留修：Mapping 形态诊断此前只匹配一级文本（message 优先），
    message 不含采样器 code 时失配；现在同时匹配 code 与 message 字段。"""
    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-mapping-chip",
        "status": "completed",
        "metrics": {},
        "diagnostics": [
            {
                "name": "sampler.gpu_thermal.unavailable",
                "data": {
                    "severity": "warning",
                    "code": "sampler.gpu_thermal.unavailable",
                    "message": "dumpsys thermalservice exited 1",
                },
            },
            {
                "name": "power.read",
                "data": {"message": "sampler.power.unavailable:current_now missing"},
            },
        ],
    }

    html_text = build_summary_report(summary)

    # code 命中（message 不含 code 也匹配）→ 展示 code:message
    assert "gpu.busy.pct：sampler.gpu_thermal.unavailable:dumpsys thermalservice exited 1" in html_text
    # message 命中（code 不含 hint 也匹配）
    assert "power.current.ma：power.read:sampler.power.unavailable:current_now missing" in html_text
    # 未命中 hint 的指标仍走兜底
    assert "net.rx.kbps：无采样数据" in html_text


def test_playback_jump_highlight_uses_timeline_interval(tmp_path: Path) -> None:
    """N8 裁决：跳转高亮实现为时间轴 3s 区间高亮（SCREEN_RECORD_SYNC §4 区间语义），
    游标轨道渐变区间 + 保留 3s 外框作即时反馈。"""
    base = tmp_path / "run"
    summary = _screen_summary()
    _write_video(summary, base)

    html_text = build_summary_report(summary, base_dir=base, report_dir=base / "report")

    script_slice = html_text.split("window.__screenPlayback", 1)[1]
    assert "linear-gradient(to right" in script_slice
    assert "highlightTrack" in script_slice
    assert "screen-playback-cursor" in html_text


# --- smoke-fix：锚点残留时的回放区防御（缺失/空 artifact 不渲染 video 标签） ---

def test_playback_video_anchor_without_artifact_renders_no_video(tmp_path: Path) -> None:
    """防御断言：clockAnchors.video 存在但 screen artifact 缺失（索引已移除）→
    回放区整体不渲染，绝不出现 404 video 标签。"""
    summary = _screen_summary(artifacts=[])

    html_text = build_summary_report(summary, base_dir=tmp_path, report_dir=tmp_path / "report")

    assert "<video" not in html_text
    # 无 screen artifact 且无 unavailable 诊断 → 整块不渲染（锚点残留也不渲染）
    assert "录屏回放" not in html_text


def test_playback_missing_video_file_renders_no_video(tmp_path: Path) -> None:
    """防御断言：artifact 条目存在但文件缺失（present=False）→ 不渲染 video 标签。"""
    summary = _screen_summary()  # 锚点/标记齐备，但不落盘任何 mp4 文件

    html_text = build_summary_report(summary, base_dir=tmp_path, report_dir=tmp_path / "report")

    # 文件缺失（present=False）→ 无可播视频且无降级诊断 → 区块整体不渲染
    assert "录屏回放" not in html_text
    assert "<video" not in html_text


def test_playback_zero_size_artifact_renders_no_video(tmp_path: Path) -> None:
    """防御断言：artifact 条目在索引中但 size_bytes=0 → 不渲染 video 标签。"""
    summary = _screen_summary(
        artifacts=[
            {
                "artifactId": "screen",
                "path": "artifacts/android/screen.mp4",
                "collector": "screenrecord",
                "sizeBytes": 0,
                "metricData": {"relatedMetrics": []},
            }
        ]
    )

    html_text = build_summary_report(summary, base_dir=tmp_path, report_dir=tmp_path / "report")

    assert "<video" not in html_text


# ---- 2026-11-04 体检修复回归（gpu-2：逐轮跳点原因上浮到降级 chip）----


def test_summary_report_gpu_chip_shows_round_skip_reason() -> None:
    """gpu-2 修复：gpu_thermal 逐轮诊断带 sampler.gpu_thermal. 前缀后，报告降级
    chip 能匹配到真实跳点原因（kgsl 门控 no increment）——此前 46 条逐轮诊断
    沉底，chip 只显示「样本数 1，未形成连续序列」，真实原因用户不可见。"""
    summary = _series_summary(
        {
            "gpu.busy.pct": {
                "value": 13.106,
                "unit": "%",
                "n": 1,
                "confidence": "low",
            },
        }
    )
    summary["diagnostics"] = [
        "sampler.gpu_thermal.gpu.no_increment:gpu total counter no increment; skipped this round",
        "sampler.gpu_thermal.gpu.counter_reset:gpu busy counter reset detected; re-baselined",
    ]

    html_text = build_summary_report(summary)

    assert (
        "gpu.busy.pct：sampler.gpu_thermal.gpu.no_increment:gpu total counter "
        "no increment; skipped this round"
    ) in html_text


# ---- simpleperf report 解析 → stat 卡 + 火焰图树（flamegraph 缺口，2026-11-10）----
# fixture 按真机 simpleperf 实测输出格式构造（emulator-5554 record/report 实测，
# 2026-11-10 抓取 flat 表与 -g 简报调用树两种真实格式；数值脱敏合成，手算见断言）。


def test_parse_simpleperf_report_flat_table_builds_distribution_tree() -> None:
    """flat 表（缺省 report）→ Command→SharedObject→Symbol overhead 分布树 + stat 卡。"""
    text = (
        "Cmdline: simpleperf record -e cpu-clock -f 4000 -g --duration 5 --app com.example.game\n"
        "Arch: arm64\n"
        "Event: cpu-clock (type 1, config 0)\n"
        "Samples: 4\n"
        "Event count: 8000000\n"
        "\n"
        "Overhead  Command  Pid    Tid    Shared Object               Symbol\n"
        "50.00%    com.game  1234  1234  libgame.so                  GameThread::Update\n"
        "30.00%    com.game  1234  1234  libgame.so                  RenderScene\n"
        "20.00%    com.game  1234  1234  [kernel.kallsyms]           freeze_slab\n"
    )

    face = parse_simpleperf_report_text(text)

    assert face["arch"] == "arm64"
    assert face["events"] == [{"event": "cpu-clock", "samples": 4, "eventCount": 8000000}]
    assert face["mode"] == "flat" and face["entryCount"] == 3 and face["unparsedLines"] == 0
    assert face["basis"].startswith("flat:")
    tree = face["tree"]
    assert tree["name"] == "ALL" and tree["value"] == 100.0
    (command,) = tree["children"]
    assert command["name"] == "com.game" and command["value"] == 100.0
    dso = {child["name"]: child for child in command["children"]}
    assert dso["libgame.so"]["value"] == 80.0
    assert {c["name"]: c["value"] for c in dso["libgame.so"]["children"]} == {
        "GameThread::Update": 50.0,
        "RenderScene": 30.0,
    }
    assert dso["[kernel.kallsyms]"]["children"][0] == {
        "name": "freeze_slab", "value": 20.0, "children": [],
    }
    assert face["maxDepth"] == 4


def test_parse_simpleperf_report_callgraph_indent_tree_with_zero_weight_frames() -> None:
    """-g 简报调用树：--pct%-- 分支按缩进成树（兄弟同列、子层更深），链上延续帧挂
    value=0 叶；首个含调用树块的 entry 成树（--children 首行 = 累计占比最高链）。"""
    text = (
        "Arch: arm64\n"
        "Event: cpu-clock (type 1, config 0)\n"
        "Samples: 4\n"
        "Event count: 8000000\n"
        "\n"
        "Children  Self    Command  Pid    Tid    Shared Object        Symbol\n"
        "100.00%   0.00%   com.game  1234  1234  libgame.so           GameThread::Update\n"
        "       |\n"
        "       -- GameThread::Update\n"
        "          |\n"
        "           -- RenderScene\n"
        "               |\n"
        "               |--60.00%-- BuildDrawCalls\n"
        "               |    |\n"
        "               |     --40.00%-- SubmitCmds\n"
        "               |\n"
        "               |--30.00%-- SortDrawCalls\n"
        "               |           SortQueue\n"
        "100.00%   0.00%   com.game  1234  1234  libgame.so           RenderScene\n"
        "       [skipped in brief callgraph mode]\n"
    )

    face = parse_simpleperf_report_text(text)

    assert face["mode"] == "callgraph" and face["entryCount"] == 2
    assert face["basis"].startswith("callgraph:")
    tree = face["tree"]
    assert tree["name"] == "GameThread::Update" and tree["value"] == 100.0
    branches = {c["name"]: c for c in tree["children"]}
    assert branches["RenderScene"]["value"] == 0.0  # 链上延续帧
    assert branches["BuildDrawCalls"]["value"] == 60.0
    assert branches["BuildDrawCalls"]["children"] == [
        {"name": "SubmitCmds", "value": 40.0, "children": []}
    ]
    assert branches["SortDrawCalls"]["children"] == [
        {"name": "SortQueue", "value": 0.0, "children": []}
    ]
    assert face["maxDepth"] == 3


def test_parse_simpleperf_report_unparseable_placeholder_has_no_tree() -> None:
    """失败占位 report（无样本表行）→ tree=None、events 空，不伪造火焰图数据。"""
    face = parse_simpleperf_report_text(
        "simpleperf E record_file_reader.cpp:88] failed to open record file: No such file\n"
    )

    assert face["tree"] is None
    assert face["events"] == [] and face["entryCount"] == 0
    assert face["maxDepth"] == 0


def test_summary_report_bundle_renders_flamegraph_block_and_stat_card(tmp_path: Path) -> None:
    """离线 HTML：simpleperf face → stat 卡 + 火焰图区块（数据内嵌 payload + 渲染脚本，
    echarts 缺失时 DOM 兜底）；echarts.min.js 随包分发条件覆盖火焰图。"""
    run_dir = tmp_path / "android-diagnose" / "cpu"
    artifact_dir = run_dir / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    simpleperf_report = artifact_dir / "simpleperf-report.txt"
    perf_data = artifact_dir / "perf.data"
    perf_data.write_text("perf data", encoding="utf-8")
    simpleperf_report.write_text(
        "Arch: arm64\n"
        "Event: cpu-clock (type 1, config 0)\n"
        "Samples: 4\n"
        "Event count: 8000000\n"
        "\n"
        "Overhead  Command  Pid    Tid    Shared Object               Symbol\n"
        "50.00%    com.game  1234  1234  libgame.so                  GameThread::Update\n"
        "50.00%    com.game  1234  1234  libgame.so                  RenderScene\n",
        encoding="utf-8",
    )
    summary = _sample_summary()
    summary["runId"] = "cpu"
    diagnosis = {
        "schemaVersion": "perfeval.diagnosis.v1",
        "caseId": "cpu",
        "mode": "cpu",
        "modes": ["cpu"],
        "serial": "emulator-5554",
        "durationSeconds": 5,
        "createdAtUtc": "2026-11-10T00:00:00Z",
        "diagnostics": ["simpleperf.record.returncode:0", "simpleperf.pull.returncode:0"],
        "artifacts": [str(perf_data), str(simpleperf_report)],
    }

    outputs = write_summary_report_bundle(summary, run_dir / "report", diagnosis=diagnosis)
    html_text = outputs["report_html"].read_text(encoding="utf-8")

    # stat 卡：事件样本数 + 解析模式 + 条目数
    assert "CPU 热点（simpleperf stat）" in html_text
    assert "4 样本" in html_text
    assert "cpu-clock" in html_text
    assert "flat" in html_text
    # 火焰图区块：内嵌数据 payload + 渲染脚本（离线，无外链 CDN）
    assert "__simpleperfFlame" in html_text
    assert "simpleperf-flame-chart" in html_text
    assert "type: 'treemap'" in html_text  # echarts 路径
    assert "GameThread::Update" in html_text  # 树数据内嵌（ensure_ascii=False）
    assert "<script src=\"echarts.min.js\"></script>" in html_text  # vendor 分发（本例无曲线区块）
    assert (run_dir / "report" / "echarts.min.js").exists()  # 随包分发条件覆盖火焰图


def test_summary_report_bundle_without_flamegraph_face_keeps_excerpt_only(tmp_path: Path) -> None:
    """report 不可解析（如既有最小 fixture 形态）→ 无火焰图区块，原 12 行摘录保留。"""
    run_dir = tmp_path / "android-diagnose" / "full"
    artifact_dir = run_dir / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    simpleperf_report = artifact_dir / "simpleperf-report.txt"
    simpleperf_report.write_text("Children  Self  Command\n  40.00%  app  main\n", encoding="utf-8")
    summary = _sample_summary()
    summary["runId"] = "full"
    diagnosis = {
        "schemaVersion": "perfeval.diagnosis.v1",
        "caseId": "full",
        "mode": "full",
        "modes": ["full"],
        "serial": "emulator-5554",
        "durationSeconds": 30,
        "createdAtUtc": "2026-11-10T00:00:00Z",
        "diagnostics": ["simpleperf.pull.returncode:0"],
        "artifacts": [str(simpleperf_report)],
    }

    outputs = write_summary_report_bundle(summary, run_dir / "report", diagnosis=diagnosis)
    html_text = outputs["report_html"].read_text(encoding="utf-8")

    assert "CPU 热点（simpleperf stat）" not in html_text
    assert "__simpleperfFlame" not in html_text
    assert "simpleperf-report.txt 摘要" in html_text  # 原摘录路径不变
    assert "Children  Self  Command" in html_text
