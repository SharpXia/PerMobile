from __future__ import annotations

import json

from perfeval.fixtures.synthetic import get_case, write_case


CASE_IDS = (
    "no_regression",
    "startup_regression",
    "jank_regression",
    "memory_regression",
    "noisy_unknown",
    "crash_fail",
)


def test_write_case_emits_expected_run_tree(tmp_path) -> None:
    for case_id in CASE_IDS:
        fixture = write_case(case_id, tmp_path / case_id)

        baseline_dirs = sorted((tmp_path / case_id / "baseline").glob("run-*"))
        candidate_dirs = sorted((tmp_path / case_id / "candidate").glob("run-*"))

        assert len(baseline_dirs) == fixture.repeat_count
        assert len(candidate_dirs) == fixture.repeat_count

        for run_dir in baseline_dirs + candidate_dirs:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            timeline_lines = (run_dir / "timeline.ndjson").read_text(encoding="utf-8").splitlines()
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            diagnostics = (run_dir / "diagnostics.ndjson").read_text(encoding="utf-8").splitlines()

            assert manifest["schemaVersion"] == "perfeval.manifest.v1"
            assert timeline_lines, "timeline.ndjson should contain synthetic events"
            assert summary["schemaVersion"] == "perfeval.summary.v1"
            assert diagnostics, "diagnostics.ndjson should contain at least one diagnostic"
            assert (run_dir / "artifacts").is_dir()

        expected_compare = json.loads(
            (tmp_path / case_id / "expected" / "compare.json").read_text(encoding="utf-8")
        )
        assert expected_compare == fixture.compare_expected


def test_case_files_match_documented_contract(tmp_path) -> None:
    fixture = get_case("startup_regression")
    fixture.write(tmp_path)

    baseline_run = tmp_path / "baseline" / "run-001"
    manifest = json.loads((baseline_run / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((baseline_run / "summary.json").read_text(encoding="utf-8"))
    timeline = [
        json.loads(line)
        for line in (baseline_run / "timeline.ndjson").read_text(encoding="utf-8").splitlines()
    ]

    assert manifest["scenario"]["scenarioId"] == "startup_regression"
    assert manifest["run"]["status"] == "completed"
    assert summary["metrics"]["startup.cold.ms"]["value"] == 995
    assert timeline[0]["type"] == "instant"
    assert timeline[1]["name"] == "startup.cold.ms"

