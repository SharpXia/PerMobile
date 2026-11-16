from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from perfeval.gui.models.result import ResultType, ResultViewer
from perfeval.gui.models.task import TaskSpec, TaskType
from perfeval.gui.services.result_locator import ResultLocator


def _task(workspace: Path, output: Path) -> TaskSpec:
    return TaskSpec.create(
        task_type=TaskType.RUN,
        program="perfeval",
        arguments=("run", "scenario.yaml"),
        working_directory=workspace,
        display_command="perfeval run scenario.yaml",
        expected_output_root=output,
        task_id="locator-task",
    )


def test_process_result_locator_prefers_envelope_and_ignores_stale_or_symlinked_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "runs"
    fresh = output / "new" / "report" / "report.html"
    fresh.parent.mkdir(parents=True)
    fresh.write_text("new", encoding="utf-8")
    stale = output / "old" / "report.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    os.utime(stale, (1, 1))
    external = tmp_path / "outside.html"
    external.write_text("external", encoding="utf-8")
    symlink = output / "linked-report.html"
    symlink.symlink_to(external)
    task = _task(workspace, output)

    located = ResultLocator(max_entries=50).locate(
        task,
        envelope={
            "schemaVersion": "perfeval.cli.event.v1",
            "type": "result",
            "command": "run",
            "data": {"status": "pass", "report": str(fresh), "artifacts": [str(external)]},
        },
        started_at=datetime.now(timezone.utc).timestamp() - 10,
    )

    locations = {item.location for item in located.descriptors}
    assert str(fresh) in locations
    assert str(stale) not in locations
    assert str(external) not in locations
    assert str(symlink) not in locations
    assert not located.result_not_found


def test_process_result_locator_returns_missing_output_directory_descriptor(tmp_path: Path) -> None:
    output = tmp_path / "missing"
    located = ResultLocator().locate(_task(tmp_path, output))

    assert located.result_not_found
    assert located.descriptors[-1].type == ResultType.DIRECTORY
    assert located.descriptors[-1].location == str(output)
    assert not located.descriptors[-1].exists


def test_process_result_locator_does_not_treat_wrong_path_types_as_results(tmp_path: Path) -> None:
    output = tmp_path / "runs"
    damaged_report = output / "report" / "report.html"
    damaged_report.mkdir(parents=True)
    fake_directory = output / "not-a-directory"
    fake_directory.write_text("file", encoding="utf-8")
    task = _task(tmp_path, output)

    located = ResultLocator().locate(
        task,
        envelope={
            "schemaVersion": "perfeval.cli.event.v1",
            "type": "result",
            "command": "run",
            "data": {
                "status": "partial",
                "report": str(damaged_report),
                "runDir": str(fake_directory),
            },
        },
    )

    by_location = {item.location: item for item in located.descriptors}
    assert not by_location[str(damaged_report)].exists
    assert not by_location[str(fake_directory)].exists
    assert located.result_not_found


def test_process_result_locator_only_accepts_controlled_local_viewer_urls(tmp_path: Path) -> None:
    output = tmp_path / "runs"
    task = _task(tmp_path, output)

    located = ResultLocator().locate(
        task,
        envelope={
            "schemaVersion": "perfeval.cli.event.v1",
            "type": "result",
            "command": "open",
            "data": {
                "report": "http://127.0.0.1:8123/report.html",
                "result": "https://127.0.0.1.evil.example/report.html",
            },
        },
    )

    urls = [item.location for item in located.descriptors if item.type == ResultType.URL]
    assert urls == ["http://127.0.0.1:8123/report.html"]


def test_process_result_locator_groups_diagnose_report_and_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "runs" / "focus"
    report = output / "report" / "report.html"
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")
    perfetto = output / "artifacts" / "capture.perfetto-trace"
    perfetto.parent.mkdir(parents=True)
    perfetto.write_bytes(b"trace")
    simpleperf = output / "artifacts" / "simpleperf.data"
    simpleperf.write_bytes(b"profile")
    task = _task(tmp_path, output)

    located = ResultLocator().locate(
        task,
        envelope={
            "schemaVersion": "perfeval.cli.event.v1",
            "type": "result",
            "command": "diagnose",
            "data": {
                "report": str(report),
                "runDir": str(output),
                "artifacts": [str(perfetto), str(simpleperf)],
            },
        },
    )
    by_location = {item.location: item for item in located.descriptors}

    assert by_location[str(report)].viewer == ResultViewer.REPORT
    assert by_location[str(perfetto)].viewer == ResultViewer.PERFETTO
    assert by_location[str(perfetto)].label == "Perfetto trace"
    assert by_location[str(simpleperf)].viewer == ResultViewer.ANDROID_STUDIO
    assert by_location[str(simpleperf)].label == "simpleperf artifact"


def test_process_result_locator_parses_legacy_diagnose_directory_without_colon(tmp_path: Path) -> None:
    output = tmp_path / "runs" / "focus"
    output.mkdir(parents=True)
    located = ResultLocator().locate(
        _task(tmp_path, output),
        stdout_text=f"Android diagnosis written to {output}\n",
    )

    assert any(
        item.location == str(output) and item.type == ResultType.DIRECTORY
        for item in located.descriptors
    )
