from __future__ import annotations

from perfeval.cli.commands.validate import DEFAULT_BUDGET
from perfeval.core.compare import compare_summaries
from perfeval.fixtures.synthetic import available_cases, get_case, load_golden_compare


EXPECTED_CASES = (
    "no_regression",
    "startup_regression",
    "jank_regression",
    "memory_regression",
    "noisy_unknown",
    "crash_fail",
    "continuous_stream",
)


def test_synthetic_cases_are_reproducible() -> None:
    assert available_cases() == EXPECTED_CASES

    for case_id in EXPECTED_CASES:
        first = get_case(case_id)
        second = get_case(case_id)

        assert first.to_dict() == second.to_dict()
        assert first.expected_outcome == load_golden_compare(case_id)["expectedOutcome"]
        assert first.compare_expected == second.compare_expected


def test_expected_compare_contract() -> None:
    expectations = {
        "no_regression": ("pass", "startup.cold.ms"),
        "startup_regression": ("fail", "startup.cold.ms"),
        "jank_regression": ("fail", "frame.jank_rate"),
        "memory_regression": ("fail", "memory.pss.peak_mb"),
        "noisy_unknown": ("unknown", "startup.cold.ms"),
        "crash_fail": ("fail", "stability.crash.count"),
        "continuous_stream": ("fail", "frame.duration.p95.ms"),
    }

    for case_id, (expected_outcome, primary_metric) in expectations.items():
        fixture = get_case(case_id)
        compare = fixture.compare_expected

        assert compare["status"] == expected_outcome
        assert compare["expectedOutcome"] == expected_outcome
        assert primary_metric in compare["metrics"]
        assert load_golden_compare(case_id) == compare


def test_fixture_object_contains_all_runs() -> None:
    fixture = get_case("crash_fail")

    assert fixture.repeat_count == 5
    assert len(fixture.baseline_runs) == 5
    assert len(fixture.candidate_runs) == 5
    assert fixture.candidate_runs[2].status == "failed"
    assert fixture.candidate_runs[2].summary["metrics"]["stability.crash.count"]["value"] == 1


def test_continuous_stream_fixture_contains_stream_series() -> None:
    fixture = get_case("continuous_stream")
    baseline = fixture.baseline_runs[0]

    # timeline 含 CPU 60 点 + 帧耗时 60 点的 counter 事件
    counter_events = [event for event in baseline.timeline if event["type"] == "counter"]
    assert len(counter_events) == 120
    assert {event["name"] for event in counter_events} == {"cpu.proc.pct", "frame.duration.detail.ms"}

    frame_detail = baseline.summary["metrics"]["frame.duration.detail.ms"]
    assert frame_detail["isStream"] is True
    assert frame_detail["sampleCount"] == 60
    assert frame_detail["seriesDownsampled"] is False
    assert len(frame_detail["series"]) == 60
    assert frame_detail["series"][0]["v"] == 55.0

    cpu = baseline.summary["metrics"]["cpu.proc.pct"]
    assert cpu["isStream"] is True
    assert cpu["sampleCount"] == 60
    assert cpu["mean"] == cpu["value"]

    candidate = fixture.candidate_runs[0]
    candidate_cpu = candidate.summary["metrics"]["cpu.proc.pct"]
    assert candidate_cpu["mean"] > cpu["mean"] * 1.25
    candidate_frame_detail = candidate.summary["metrics"]["frame.duration.detail.ms"]
    assert candidate_frame_detail["max"] == 2 * frame_detail["max"]


def test_continuous_stream_confidence_not_inflated_by_sample_count() -> None:
    fixture = get_case("continuous_stream")
    frame_detail = fixture.baseline_runs[0].summary["metrics"]["frame.duration.detail.ms"]

    # 单 run 60 点：置信度以 repeatCount=1 为基数，不得因 sampleCount 高于 medium
    assert frame_detail["confidence"] in ("low", "medium")
    assert frame_detail["confidence"] == "low"


def test_continuous_stream_compare_reports_regression_fail() -> None:
    fixture = get_case("continuous_stream")

    compare = compare_summaries(
        [run.summary for run in fixture.baseline_runs],
        [run.summary for run in fixture.candidate_runs],
        budget=DEFAULT_BUDGET,
    )

    assert compare["status"] == "fail"
    assert compare["metrics"]["frame.duration.p95.ms"]["judgement"] == "fail"
    assert compare["metrics"]["frame.duration.p95.ms"]["reason"] == "regression_pct_exceeds_20"

