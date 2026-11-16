from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from perfeval.cli import main as cli_main_module
from perfeval.cli.commands import ab as ab_command
from perfeval.cli.commands import diagnose as diagnose_command
from perfeval.cli.protocol import (
    SCHEMA_VERSION,
    CliEventError,
    gui_event_stream,
    parse_event_line,
)
from perfeval.collectors.android import AndroidDevice, AndroidPreflightResult


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def _parse_events(output: str) -> list:
    return [parse_event_line(line) for line in output.splitlines() if line]


def _write_scenario(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "schemaVersion: perfeval.scenario.v1",
                "project: PerMobile",
                "scenarioId: gui_契约",
                "run:",
                "  repeats: 1",
                "  mode: same_device_versions",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_event_parser_binds_v1_schema_and_unicode() -> None:
    event = parse_event_line(
        json.dumps(
            {
                "schemaVersion": SCHEMA_VERSION,
                "type": "manual",
                "command": "run",
                "data": {
                    "prompt": "请按 Enter",
                    "timeoutSeconds": None,
                    "input": "enter",
                },
            },
            ensure_ascii=False,
        )
    )

    assert event.schema_version == "perfeval.cli.event.v1"
    assert event.type == "manual"
    assert event.data["prompt"] == "请按 Enter"
    assert event.data["input"] == "enter"


def test_event_parser_accepts_phase_contract() -> None:
    event = parse_event_line(
        json.dumps(
            {
                "schemaVersion": SCHEMA_VERSION,
                "type": "phase",
                "command": "run",
                "data": {
                    "id": "app_prepare",
                    "state": "started",
                    "label": "启动应用",
                    "repeatIndex": 1,
                    "repeatCount": 3,
                    "variantId": "baseline",
                },
            },
            ensure_ascii=False,
        )
    )
    assert event.type == "phase"
    assert event.data["repeatCount"] == 3


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("not-json", "invalid JSONL event"),
        ('{"schemaVersion":"other","type":"result","command":"run","data":{}}', "unsupported CLI event schema"),
        (
            f'{{"schemaVersion":"{SCHEMA_VERSION}","type":"other","command":"run","data":{{}}}}',
            "unsupported CLI event type",
        ),
        (f'{{"schemaVersion":"{SCHEMA_VERSION}","type":"result","command":"","data":{{}}}}', "non-empty string"),
        (
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "type": "manual",
                    "command": "run",
                    "data": {"prompt": "go", "input": "text", "timeoutSeconds": None},
                }
            ),
            "input must be 'enter'",
        ),
        (
            f'{{"schemaVersion":"{SCHEMA_VERSION}","type":"result","command":"run","data":{{"status":"pass"}}}}',
            "missing required fields",
        ),
        (
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "type": "phase",
                    "command": "run",
                    "data": {"id": "x", "state": "unknown", "label": "X"},
                }
            ),
            "phase state",
        ),
    ],
)
def test_event_parser_rejects_invalid_contract(line: str, message: str) -> None:
    with pytest.raises(CliEventError, match=message):
        parse_event_line(line)


def test_devices_default_human_text_is_unchanged(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_main_module.shutil, "which", lambda name: f"/tools/{name}" if name == "adb" else None)
    monkeypatch.setattr(
        cli_main_module,
        "run_preflight",
        lambda: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial="emulator-5554",
            devices=(AndroidDevice(serial="emulator-5554", state="device"),),
        ),
    )

    code = cli_main_module.main(["devices"])

    assert code == 0
    assert capsys.readouterr().out == (
        "Host collector tool diagnostics:\n"
        "- adb: /tools/adb\n"
        "- xcrun: not found\n"
        "- xctrace: not found\n"
        "Android: selected emulator-5554\n"
    )


@pytest.mark.parametrize(
    ("preflight", "expected_status"),
    [
        (AndroidPreflightResult(ok=False, adb_available=False, diagnostics=("android.adb.not_found",)), "adb_missing"),
        (
            AndroidPreflightResult(
                ok=False,
                adb_available=True,
                diagnostics=("android.device.none_online",),
            ),
            "no_devices",
        ),
        (
            AndroidPreflightResult(
                ok=False,
                adb_available=True,
                devices=(
                    AndroidDevice(
                        serial="offline-1",
                        state="offline",
                        model="Pixel",
                        product="sdk",
                        device="emu",
                    ),
                ),
                diagnostics=("android.device.none_online",),
            ),
            "offline",
        ),
        (
            AndroidPreflightResult(
                ok=False,
                adb_available=True,
                devices=(
                    AndroidDevice(
                        serial="unauthorized-1",
                        state="unauthorized",
                        model="Phone",
                        product="p",
                        device="d",
                    ),
                ),
                diagnostics=("android.device.none_online",),
            ),
            "unauthorized",
        ),
    ],
)
def test_devices_gui_events_cover_unavailable_states(monkeypatch, capsys, preflight, expected_status) -> None:
    monkeypatch.setattr(cli_main_module, "run_preflight", lambda: preflight)
    monkeypatch.setattr(cli_main_module.shutil, "which", lambda name: "/tools/adb" if name == "adb" else None)

    code = cli_main_module.main(["devices", "--gui-events"])

    assert code == 0
    captured = capsys.readouterr()
    events = _parse_events(captured.out)
    assert [event.type for event in events] == ["devices", "result"]
    android = events[0].data["android"]
    assert android["status"] == expected_status
    assert android["devices"] == [
        {
            "serial": device.serial,
            "state": device.state,
            "model": device.model,
            "product": device.product,
            "device": device.device,
        }
        for device in preflight.devices
    ]
    assert events[-1].data["exitCode"] == 0


def test_run_gui_result_has_stable_paths_and_default_output_stays_human(tmp_path: Path, capsys) -> None:
    scenario = tmp_path / "场景.yaml"
    _write_scenario(scenario)

    human_code = cli_main_module.main(["run", str(scenario), "--out", str(tmp_path / "human")])
    human = capsys.readouterr()
    assert human_code == 0
    assert human.out.startswith("Created 1 run directory under ")
    assert SCHEMA_VERSION not in human.out

    event_code = cli_main_module.main(
        ["run", str(scenario), "--out", str(tmp_path / "事件输出"), "--gui-events"]
    )
    captured = capsys.readouterr()
    events = _parse_events(captured.out)

    assert event_code == 0
    assert [event.type for event in events] == ["phase", "phase", "result"]
    assert [event.data["state"] for event in events[:-1]] == ["started", "completed"]
    result = events[-1].data
    assert result["status"] == "partial"
    assert result["exitCode"] == 0
    assert result["outputRoot"] == str(tmp_path / "事件输出")
    assert Path(result["runDir"]).parent == tmp_path / "事件输出"
    assert result["report"].endswith("report/report.html")
    assert result["summary"].endswith("summary.json")
    assert result["artifacts"] == []


def test_run_gui_dry_run_does_not_report_stale_outputs(tmp_path: Path, capsys) -> None:
    scenario = tmp_path / "scenario.yaml"
    _write_scenario(scenario)
    stale_run = tmp_path / "runs" / "gui_"
    (stale_run / "report").mkdir(parents=True)
    (stale_run / "report" / "report.html").write_text("stale", encoding="utf-8")
    (stale_run / "summary.json").write_text("{}", encoding="utf-8")

    code = cli_main_module.main(
        ["run", str(scenario), "--out", str(tmp_path / "runs"), "--dry-run", "--gui-events"]
    )

    events = _parse_events(capsys.readouterr().out)
    assert code == 0
    assert events[-1].data["dryRun"] is True
    assert events[-1].data["report"] is None
    assert events[-1].data["summary"] is None


def test_gui_tool_error_still_emits_final_result(tmp_path: Path, capsys) -> None:
    code = cli_main_module.main(["run", str(tmp_path / "missing.yaml"), "--gui-events"])

    captured = capsys.readouterr()
    events = _parse_events(captured.out)
    assert code == 10
    assert len(events) == 1
    assert events[0].type == "result"
    assert events[0].data["status"] == "tool_error"
    assert events[0].data["exitCode"] == 10
    assert events[0].data["report"] is None
    assert events[0].data["artifacts"] == []
    assert "perfeval: error:" in captured.err


def test_ab_gui_result_preserves_business_exit_code(monkeypatch, tmp_path: Path, capsys) -> None:
    root = tmp_path / "ab"
    monkeypatch.setattr(
        ab_command,
        "collect_android_ab",
        lambda **kwargs: {
            "status": "fail",
            "outputRoot": root,
            "comparePath": root / "compare" / "compare.json",
            "reportHtml": root / "report" / "report.html",
            "compare": {"status": "fail"},
            "comparisons": {"candidate": {"status": "fail"}},
            "comparisonPaths": {"candidate": root / "compare" / "compare.json"},
            "reportPaths": {"candidate": root / "report" / "report.html"},
        },
    )

    code = cli_main_module.main(["ab", "scenario.yaml", "--android-device-run", "--gui-events"])

    events = _parse_events(capsys.readouterr().out)
    assert code == 1
    assert len(events) == 1
    assert events[0].data["status"] == "fail"
    assert events[0].data["exitCode"] == 1
    assert events[0].data["outputRoot"] == str(root)
    assert events[0].data["report"].endswith("report.html")


def test_diagnose_gui_result_uses_summary_status(monkeypatch, tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "诊断" / "case"
    run_dir.mkdir(parents=True)
    summary = run_dir / "summary.json"
    summary.write_text(json.dumps({"status": "partial"}), encoding="utf-8")
    monkeypatch.setattr(
        diagnose_command,
        "collect_android_diagnosis",
        lambda **kwargs: {
            "runDir": str(run_dir),
            "diagnosisPath": str(run_dir / "diagnosis.json"),
            "summaryPath": str(summary),
            "reportHtml": str(run_dir / "report" / "report.html"),
            "artifacts": [str(run_dir / "artifacts" / "trace.pftrace")],
        },
    )

    code = cli_main_module.main(
        [
            "diagnose",
            "android",
            "--package-name",
            "com.example.app",
            "--out",
            str(tmp_path / "诊断"),
            "--gui-events",
        ]
    )

    events = _parse_events(capsys.readouterr().out)
    assert code == 0
    assert [event.type for event in events] == ["phase", "phase", "phase", "result"]
    assert events[-1].data["status"] == "partial"
    assert events[-1].data["summary"] == str(summary)
    assert events[-1].data["artifacts"][0].endswith("trace.pftrace")


def test_manual_input_is_emitted_before_enter(monkeypatch, capsys) -> None:
    input_calls: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": input_calls.append(prompt) or "")

    with gui_event_stream("run", enabled=True):
        print("请把设备停在目标页面，然后按 Enter")
        input()

    events = _parse_events(capsys.readouterr().out)
    assert [event.type for event in events] == ["log", "manual"]
    assert events[-1].data == {
        "prompt": "请把设备停在目标页面，然后按 Enter",
        "timeoutSeconds": None,
        "input": "enter",
    }
    assert input_calls == [""]


def _fake_command(tmp_path: Path, scenario: str, *args: str) -> list[str]:
    return [
        sys.executable,
        str(FAKE_CLI),
        scenario,
        "--output-root",
        str(tmp_path / "fake 输出"),
        *args,
    ]


def test_fake_cli_covers_chunked_stdout_stderr_and_unicode(tmp_path: Path) -> None:
    stream = subprocess.run(
        _fake_command(tmp_path, "stream", "--delay", "0"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    unicode_run = subprocess.run(
        _fake_command(tmp_path, "unicode", "--delay", "0"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    stream_events = _parse_events(stream.stdout.decode("utf-8"))
    unicode_events = _parse_events(unicode_run.stdout.decode("utf-8"))
    assert stream.returncode == 0
    assert [event.type for event in stream_events] == ["phase", "phase", "log", "log", "result"]
    assert stream.stderr.decode("utf-8") == "stderr-one\n错误流\n"
    assert unicode_run.returncode == 0
    assert "中文路径/测试任务/报告.html 🚀" in unicode_events[0].data["text"]
    assert unicode_run.stderr.decode("utf-8") == "标准错误：设备未授权\n"


def test_fake_cli_manual_stdin_and_exit_codes(tmp_path: Path) -> None:
    manual = subprocess.run(
        _fake_command(tmp_path, "manual", "--status", "warn", "--manual-timeout", "2.5"),
        input=b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    failed = subprocess.run(
        _fake_command(tmp_path, "exit", "--status", "invalid", "--exit-code", "4", "--report-state", "missing"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    manual_events = _parse_events(manual.stdout.decode("utf-8"))
    failed_events = _parse_events(failed.stdout.decode("utf-8"))
    assert manual.returncode == 0
    assert [event.type for event in manual_events] == ["manual", "result"]
    assert manual_events[0].data["input"] == "enter"
    assert manual_events[0].data["timeoutSeconds"] == 2.5
    assert manual_events[-1].data["status"] == "warn"
    assert failed.returncode == 4
    assert failed_events[-1].data["status"] == "invalid"
    assert failed_events[-1].data["exitCode"] == 4
    assert failed_events[-1].data["report"] is None


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("pass", 0),
        ("warn", 0),
        ("unknown", 0),
        ("completed", 0),
        ("partial", 0),
        ("fail", 1),
        ("invalid", 4),
        ("tool_error", 10),
    ],
)
def test_fake_cli_result_status_and_exit_code_matrix(tmp_path: Path, status: str, exit_code: int) -> None:
    completed = subprocess.run(
        _fake_command(tmp_path, "exit", "--status", status, "--exit-code", str(exit_code)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    events = _parse_events(completed.stdout.decode("utf-8"))
    assert completed.returncode == exit_code
    assert events[-1].data["status"] == status
    assert events[-1].data["exitCode"] == exit_code


def test_fake_cli_long_task_supports_terminate_and_kill(tmp_path: Path) -> None:
    terminating = subprocess.Popen(
        _fake_command(tmp_path, "long", "--delay", "0.02"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert terminating.stdout is not None
    first_line = terminating.stdout.readline()
    terminating.terminate()
    remaining, _ = terminating.communicate(timeout=3)
    events = _parse_events((first_line + remaining).decode("utf-8"))
    assert terminating.returncode == 130
    assert events[-1].type == "result"
    assert events[-1].data["status"] == "cancelled"

    ignoring = subprocess.Popen(
        _fake_command(tmp_path, "ignore-terminate", "--delay", "0.02"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert ignoring.stdout is not None
    ignoring.stdout.readline()
    ignoring.terminate()
    time.sleep(0.08)
    assert ignoring.poll() is None
    ignoring.kill()
    ignoring.communicate(timeout=3)
    assert ignoring.returncode != 0


def test_run_help_documents_sampling_and_screen_record_contract(capsys) -> None:
    """CM-AC-012：run --help 与 GUI 高级开关共用同一 CLI 契约描述。"""
    exit_code = cli_main_module.main(["run", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert exit_code == 0
    assert "--sampling-off" in help_text
    assert (
        "Disable periodic samplers even when collectors.android.samplers is configured."
        in help_text
    )
    assert "--sampling-interval-ms" in help_text
    assert "Override intervalMs for every configured Android sampler." in help_text
    assert "--screen-record" in help_text
    assert "--screen-record-off" in help_text
    assert (
        "Enable Android screen recording even when collectors.android.screenRecord is not configured."
        in help_text
    )
    assert (
        "Disable Android screen recording even when collectors.android.screenRecord is configured."
        in help_text
    )
