from __future__ import annotations

from pathlib import Path

from perfeval.core.report import write_report_bundle


SAMPLE_COMPARE = {
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


def main() -> None:
    out_dir = Path("reports/sample")
    outputs = write_report_bundle(SAMPLE_COMPARE, out_dir)
    print(f"Sample compare report written to: {out_dir}")
    for key, path in outputs.items():
        print(f"  {key}: {path}")


if __name__ == "__main__":
    main()
