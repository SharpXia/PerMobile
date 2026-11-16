from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from perfeval.core.compare import compare_summaries
from perfeval.core.report import write_report_bundle
from perfeval.collectors.android import (
    parse_framestats_detail,
    parse_gfxinfo,
    parse_logcat,
    parse_meminfo,
)
from perfeval.fixtures.synthetic import available_cases, get_case, write_case


DEFAULT_BUDGET = {
    "defaults": {"minRuns": 5, "recommendedRuns": 5, "confidence": 0.95, "outlierPolicy": "mad_3"},
    "metrics": {
        "startup.cold.ms": {
            "direction": "lower_better",
            "unit": "ms",
            "failAbs": 2500,
            "warnRegressionPct": 0.08,
            "failRegressionPct": 0.15,
            "minDeltaAbs": 80,
        },
        "frame.jank_rate": {
            "direction": "lower_better",
            "unit": "ratio",
            "warnAbs": 0.05,
            "failAbs": 0.10,
            "warnRegressionAbs": 0.02,
            "failRegressionAbs": 0.05,
            "minDeltaAbs": 0.01,
        },
        "frame.duration.p95.ms": {
            "direction": "lower_better",
            "unit": "ms",
            "warnRegressionPct": 0.10,
            "failRegressionPct": 0.20,
            "minDeltaAbs": 5,
        },
        "memory.pss.peak_mb": {
            "direction": "lower_better",
            "unit": "MB",
            "warnRegressionPct": 0.10,
            "failRegressionPct": 0.20,
            "minDeltaAbs": 30,
        },
        "stability.crash.count": {
            "direction": "lower_better",
            "unit": "count",
            "failAbs": 0.5,
        },
    },
}


def register_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("validate", help="Run self-validation fixtures.")
    parser.add_argument("suite", nargs="?", default="synthetic", choices=["synthetic", "android-parsers"])
    parser.add_argument("--case", choices=available_cases(), help="Run a single synthetic case.")
    parser.add_argument("--out", default="runs/synthetic-validation", help="Output directory.")
    parser.add_argument("--reports", default="reports/synthetic-validation", help="Report output directory.")
    parser.set_defaults(func=handle)
    return parser


def _case_ids(selected: str | None) -> tuple[str, ...]:
    if selected:
        return (selected,)
    return available_cases()


def handle(args: argparse.Namespace) -> int:
    if args.suite == "android-parsers":
        return _handle_android_parsers()
    return _handle_synthetic(args)


def _handle_synthetic(args: argparse.Namespace) -> int:
    output_root = Path(args.out)
    reports_root = Path(args.reports)
    results: list[dict[str, Any]] = []

    for case_id in _case_ids(args.case):
        fixture = write_case(case_id, output_root / case_id)
        compare = compare_summaries(
            [run.summary for run in fixture.baseline_runs],
            [run.summary for run in fixture.candidate_runs],
            mode="same_device_versions",
            budget=DEFAULT_BUDGET,
            compare_id=f"compare-{case_id}",
        )
        case_report_dir = reports_root / case_id
        write_report_bundle(compare, case_report_dir)
        passed = compare["status"] == fixture.expected_outcome
        results.append(
            {
                "case": case_id,
                "expected": fixture.expected_outcome,
                "actual": compare["status"],
                "passed": passed,
                "report": str(case_report_dir / "report.html"),
            }
        )

    failures = [result for result in results if not result["passed"]]
    print(json.dumps({"suite": args.suite, "results": results}, indent=2, sort_keys=True))
    return 1 if failures else 0


def _handle_android_parsers() -> int:
    fixture_root = Path(__file__).parents[2] / "fixtures" / "android" / "golden"
    gfxinfo = parse_gfxinfo((fixture_root / "gfxinfo.txt").read_text(encoding="utf-8"))
    meminfo = parse_meminfo((fixture_root / "meminfo.txt").read_text(encoding="utf-8"))
    crash = parse_logcat(
        (fixture_root / "logcat-crash.txt").read_text(encoding="utf-8"),
        package_name="com.perfeval.fixture",
    )
    anr = parse_logcat(
        (fixture_root / "logcat-anr.txt").read_text(encoding="utf-8"),
        package_name="com.perfeval.fixture",
    )
    # CM-TASK-010：framestats 逐帧解析 check（TASK-008 golden，帧数/jank 集/换算断言）
    framestats = parse_framestats_detail(
        (fixture_root / "gfxinfo-framestats-detail.txt").read_text(encoding="utf-8")
    )
    framestats_jank_indices = {
        frame["frame_index"] for frame in framestats["frames"] if frame["jank"]
    }
    checks = [
        ("gfxinfo.jank_rate", gfxinfo.get("jank_rate") == 0.12),
        ("gfxinfo.p95", gfxinfo.get("p95_ms") == 24),
        ("framestats.frame_count", framestats["frame_count"] == 21),
        (
            "framestats.jank_indices",
            framestats_jank_indices == {3, 4, 5, 6, 10, 11, 12, 13, 17},
        ),
        (
            "framestats.ns_to_ms_conversion",
            round(framestats["frame_duration_ms"][0], 6) == 1.763908
            and round(framestats["frame_duration_ms"][10], 6) == 460.79349
            and round(framestats["frame_duration_ms"][17], 6) == 40.0,
        ),
        ("meminfo.pss", meminfo.get("pss_mb") == 88.0),
        ("meminfo.java_heap", meminfo.get("java_heap_mb") == 50.0),
        ("logcat.crash", crash.get("crash_count") == 1),
        ("logcat.anr", anr.get("anr_count") == 1),
    ]
    results = [{"check": name, "passed": passed} for name, passed in checks]
    print(json.dumps({"suite": "android-parsers", "results": results}, indent=2, sort_keys=True))
    return 0 if all(passed for _, passed in checks) else 1
