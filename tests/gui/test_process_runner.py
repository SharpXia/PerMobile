from __future__ import annotations

import json
import sys
from pathlib import Path

from perfeval.gui.models.result import TaskOutcome
from perfeval.gui.models.task import ManualCapability, TaskSpec, TaskState, TaskType
from perfeval.gui.services.process_runner import (
    CliEventStreamParser,
    IncrementalUtf8Decoder,
    ProcessRunner,
)


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def _task(tmp_path: Path, scenario: str, *extra: str, manual: bool = False) -> TaskSpec:
    output = tmp_path / "输出 目录"
    return TaskSpec.create(
        task_type=TaskType.RUN,
        program=sys.executable,
        arguments=(
            str(FAKE_CLI),
            scenario,
            "--output-root",
            str(output),
            *extra,
        ),
        working_directory=tmp_path,
        display_command="fake-cli",
        expected_output_root=output,
        manual_capability=(ManualCapability.EVENT_AND_TEXT if manual else ManualCapability.NONE),
        task_id=f"fake-{scenario}",
    )


def test_process_incremental_utf8_decoder_handles_split_and_invalid_bytes() -> None:
    decoder = IncrementalUtf8Decoder()
    encoded = "中文".encode("utf-8")

    first, count1 = decoder.decode(encoded[:2])
    second, count2 = decoder.decode(encoded[2:] + b"\xff", final=True)

    assert first == ""
    assert second == "中文\ufffd"
    assert count1 == 0
    assert count2 == 1
    assert decoder.replacement_count == 1


def test_process_jsonl_parser_handles_event_split_across_chunks() -> None:
    payload = json.dumps(
        {
            "schemaVersion": "perfeval.cli.event.v1",
            "type": "manual",
            "command": "run",
            "data": {"prompt": "请按 Enter", "timeoutSeconds": 5, "input": "enter"},
        },
        ensure_ascii=False,
    ) + "\n"
    parser = CliEventStreamParser()

    events1, legacy1 = parser.feed(payload[:7])
    events2, legacy2 = parser.feed(payload[7:])

    assert events1 == [] and legacy1 == []
    assert events2[0]["data"]["prompt"] == "请按 Enter"
    assert legacy2 == []


def test_process_runner_streams_structured_logs_unicode_and_result(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    stdout_chunks = []
    stderr_chunks = []
    states = []
    runner.stdout_chunk.connect(stdout_chunks.append)
    runner.stderr_chunk.connect(stderr_chunks.append)
    runner.state_changed.connect(states.append)

    with qtbot.waitSignal(runner.finished, timeout=5000) as blocker:
        runner.start(_task(tmp_path, "unicode", "--status", "warn"))

    result = blocker.args[0]
    assert result.outcome == TaskOutcome.WARN
    assert "中文路径" in "".join(chunk.text for chunk in stdout_chunks)
    assert "设备未授权" in "".join(chunk.text for chunk in stderr_chunks)
    assert [chunk.sequence for chunk in [*stdout_chunks, *stderr_chunks]] != []
    assert TaskState.RUNNING in states
    assert states[-1] == TaskState.COMPLETED
    assert result.stdout_log_path.read_text(encoding="utf-8").startswith("中文路径")


def test_process_runner_emits_structured_phase_events(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    phases = []
    runner.phase_changed.connect(phases.append)

    with qtbot.waitSignal(runner.finished, timeout=5000):
        runner.start(_task(tmp_path, "stream", "--delay", "0"))

    assert [phase.phase_id for phase in phases] == ["device_check", "collection"]
    assert phases[1].repeat_index == 1
    assert phases[1].repeat_count == 1


def test_process_runner_manual_event_writes_enter_and_resumes(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    task = _task(tmp_path, "manual", "--status", "completed", manual=True)

    with qtbot.waitSignal(runner.manual_requested, timeout=5000) as manual_blocker:
        runner.start(task)

    request = manual_blocker.args[0]
    assert request.prompt == "请完成手动步骤，然后按 Enter"
    assert request.source == "event"
    assert runner.state == TaskState.WAITING_FOR_USER

    with qtbot.waitSignal(runner.finished, timeout=5000) as finished_blocker:
        assert runner.continue_manual()

    assert finished_blocker.args[0].outcome == TaskOutcome.COMPLETED
    assert runner.state == TaskState.COMPLETED


def test_process_runner_text_prompt_fallback_and_cancel_mapping(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs", cancel_grace_ms=100)
    with qtbot.waitSignal(runner.manual_requested, timeout=5000) as manual_blocker:
        runner.start(_task(tmp_path, "manual-text", manual=True))
    assert manual_blocker.args[0].source == "text"
    with qtbot.waitSignal(runner.finished, timeout=5000):
        runner.continue_manual()

    with qtbot.waitSignal(runner.started, timeout=5000):
        runner.start(_task(tmp_path, "long"))
    with qtbot.waitSignal(runner.finished, timeout=5000) as finished_blocker:
        assert runner.cancel()

    assert finished_blocker.args[0].outcome == TaskOutcome.CANCELLED
    assert finished_blocker.args[0].user_cancelled


def test_process_runner_launch_failure_finishes_without_crashing_gui(qtbot, tmp_path: Path) -> None:
    runner = ProcessRunner(log_directory=tmp_path / "logs")
    task = TaskSpec.create(
        task_type=TaskType.RUN,
        program=str(tmp_path / "missing-perfeval-cli"),
        arguments=("run",),
        working_directory=tmp_path,
        display_command="missing-perfeval-cli run",
        expected_output_root=tmp_path / "output",
    )

    with qtbot.waitSignal(runner.finished, timeout=5000) as blocker:
        runner.start(task)

    result = blocker.args[0]
    assert result.outcome == TaskOutcome.LAUNCH_ERROR
    assert result.exit_code is None
    assert result.process_error
    assert runner.state == TaskState.COMPLETED
