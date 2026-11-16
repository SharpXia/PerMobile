from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from perfeval.gui.models.preset import PresetTaskKind, TaskLaunchConfig
from perfeval.gui.models.result import (
    ResultDescriptor,
    ResultType,
    ResultViewer,
    TaskOutcome,
    TaskResult,
)
from perfeval.gui.models.task import TaskSpec, TaskType
from perfeval.gui.services.experiment_cache import ExperimentCacheService


def _launch(tmp_path: Path, root: Path, experiment_id: str = "exp-1") -> TaskLaunchConfig:
    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program="perfeval",
        arguments=("run", "scenario.yaml", "--out", str(root / "data")),
        working_directory=tmp_path,
        display_command="perfeval run scenario.yaml",
        expected_output_root=root / "data",
        metadata={
            "experiment_id": experiment_id,
            "experiment_root": str(root),
            "package_name": "com.perfeval.gamebox",
        },
    )
    return TaskLaunchConfig(
        "builtin.smoothness",
        "页面流畅度",
        PresetTaskKind.RUN,
        task,
        package_name="com.perfeval.gamebox",
        repeat_count=3,
    )


def test_experiment_cache_writes_lifecycle_and_relative_report_paths(tmp_path: Path) -> None:
    root = tmp_path / ".perfeval" / "experiments" / "2026" / "08" / "exp-1"
    launch = _launch(tmp_path, root)
    cache = ExperimentCacheService()
    cache.begin(launch)

    running = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    assert running["status"] == "running"
    assert running["packageName"] == "com.perfeval.gamebox"

    report = root / "data" / "quick_smoothness" / "report" / "report.html"
    summary = root / "data" / "quick_smoothness" / "summary.json"
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("{}", encoding="utf-8")
    result = TaskResult(
        task_id=launch.task.task_id,
        task_type=TaskType.RUN,
        outcome=TaskOutcome.PASS,
        results=(
            ResultDescriptor(ResultType.REPORT, "报告", str(report), True, ResultViewer.REPORT),
            ResultDescriptor(ResultType.SUMMARY, "Summary", str(summary), True),
        ),
        started_at=datetime(2026, 8, 12, 7, 59, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
    )

    cache.finish(launch, result)

    completed = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    assert completed["status"] == "pass"
    assert completed["reportPath"] == "data/quick_smoothness/report/report.html"
    assert completed["summaryPath"] == "data/quick_smoothness/summary.json"
    assert completed["durationSeconds"] == 60.0


def test_repeated_quick_launch_gets_a_distinct_cache_directory(tmp_path: Path) -> None:
    original_root = tmp_path / ".perfeval" / "experiments" / "2026" / "08" / "exp-1"
    launch = _launch(tmp_path, original_root)

    repeated = ExperimentCacheService().fresh_launch(launch)

    assert repeated.task.metadata["experiment_id"] != "exp-1"
    assert repeated.task.expected_output_root != launch.task.expected_output_root
    output_index = repeated.task.arguments.index("--out") + 1
    assert repeated.task.arguments[output_index] == str(repeated.task.expected_output_root)
    assert ".perfeval/experiments/" in repeated.task.arguments[output_index]
