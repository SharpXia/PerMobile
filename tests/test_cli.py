from __future__ import annotations

import json
import subprocess
from pathlib import Path

from perfeval.cli.commands import diagnose as diagnose_command
from perfeval.cli.commands.compare import _load_summary_group
from perfeval.cli.commands import run as run_command
from perfeval.cli.main import main
from perfeval.collectors.android import (
    AndroidCollectionResult,
    AndroidPreflightResult,
    index_android_artifacts,
    parse_android_artifacts,
)
from perfeval.core.schema import Manifest


FIXTURE_ROOT = Path("perfeval/fixtures/android/golden")


def _seed_android_artifacts(target: Path) -> Path:
    artifact_dir = target / "artifacts" / "android"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "gfxinfo.txt").write_text((FIXTURE_ROOT / "gfxinfo.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (artifact_dir / "meminfo.txt").write_text((FIXTURE_ROOT / "meminfo.txt").read_text(encoding="utf-8"), encoding="utf-8")
    logcat = (
        (FIXTURE_ROOT / "logcat-startup.txt").read_text(encoding="utf-8")
        + "\n"
        + (FIXTURE_ROOT / "logcat-crash.txt").read_text(encoding="utf-8")
        + "\n"
        + (FIXTURE_ROOT / "logcat-anr.txt").read_text(encoding="utf-8")
    )
    (artifact_dir / "logcat.txt").write_text(logcat, encoding="utf-8")
    (artifact_dir / "perfetto.pftrace").write_text("trace", encoding="utf-8")
    return artifact_dir


def _collection_result(run_dir: Path, run_id: str, package_name: str) -> AndroidCollectionResult:
    artifact_dir = _seed_android_artifacts(run_dir)
    events = parse_android_artifacts(artifact_dir, run_id=run_id, package_name=package_name)
    artifacts = index_android_artifacts(artifact_dir)
    return AndroidCollectionResult(
        run_id=run_id,
        artifact_dir=artifact_dir,
        events=events,
        artifacts=artifacts,
        commands=(),
        diagnostics=(),
    )


def test_cli_help_dispatches() -> None:
    assert main(["--help"]) == 0


def test_validate_synthetic_single_case_generates_report(tmp_path: Path) -> None:
    code = main(
        [
            "validate",
            "synthetic",
            "--case",
            "startup_regression",
            "--out",
            str(tmp_path / "runs"),
            "--reports",
            str(tmp_path / "reports"),
        ]
    )

    assert code == 0
    report = tmp_path / "reports" / "startup_regression" / "report.html"
    compare = tmp_path / "reports" / "startup_regression" / "compare.json"
    assert report.exists()
    assert compare.exists()
    assert json.loads(compare.read_text(encoding="utf-8"))["status"] == "fail"


def test_validate_android_parsers() -> None:
    assert main(["validate", "android-parsers"]) == 0


def test_run_android_artifacts_populates_run_tree(tmp_path: Path) -> None:
    artifact_source = _seed_android_artifacts(tmp_path / "android-artifacts")
    code = main(
        [
            "run",
            "scenarios/android-fixture-startup.yaml",
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-artifacts",
            str(artifact_source),
            "--package-name",
            "com.perfeval.fixture",
        ]
    )

    assert code == 0
    run_dir = tmp_path / "runs" / "android_fixture_startup"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    diagnostics = [json.loads(line) for line in (run_dir / "diagnostics.ndjson").read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert summary["status"] == "completed"
    Manifest.from_dict(manifest)
    assert "frame.jank_rate" in summary["metrics"]
    assert "startup.cold.ms" in summary["metrics"]
    assert summary["metrics"]["startup.cold.ms"]["value"] == 1234
    assert summary["diagnostics"][0]["name"] == "android.artifacts.parsed"
    assert diagnostics[0]["type"] == "diagnostic"
    assert diagnostics[0]["name"] == "android.artifacts.parsed"
    assert (run_dir / "artifacts" / "android" / "perfetto.pftrace").exists()
    # run command should auto-generate the Chinese HTML report alongside summary.json
    report_html = run_dir / "report" / "report.html"
    assert report_html.exists()
    report_text = report_html.read_text(encoding="utf-8")
    assert "PerfEval Lite Android 单次采集报告" in report_text
    assert "startup.cold.ms" in report_text


def test_compare_summary_group_prefers_saved_repeat_summaries(tmp_path: Path) -> None:
    group = tmp_path / "baseline"
    (group / "repeats" / "r001").mkdir(parents=True)
    (group / "repeats" / "r002").mkdir(parents=True)
    (group / "summary.json").write_text('{"runId":"aggregate","metrics":{}}', encoding="utf-8")
    (group / "repeats" / "r001" / "summary.json").write_text(
        '{"runId":"r001","metrics":{}}', encoding="utf-8"
    )
    (group / "repeats" / "r002" / "summary.json").write_text(
        '{"runId":"r002","metrics":{}}', encoding="utf-8"
    )

    summaries = _load_summary_group(str(group))

    assert [summary["runId"] for summary in summaries] == ["r001", "r002"]


def test_run_android_artifacts_preserves_memory_profile_and_budgets(tmp_path: Path) -> None:
    artifact_source = _seed_android_artifacts(tmp_path / "android-artifacts")
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        """
        schemaVersion: perfeval.scenario.v1
        project: PerMobile
        scenarioId: android_memory_profile
        description: Android memory profile report check.
        memoryProfile: heavy
        apps:
          - id: target
            platform: android
            packageName: com.perfeval.fixture
        run:
          repeats: 1
          mode: same_device_versions
        budgets:
          memory.pss.peak_mb:
            direction: lower_better
            ratingAbs: [700, 1200, 1800]
            warnAbs: 1200
            failAbs: 1800
        """,
        encoding="utf-8",
    )

    code = main(
        [
            "run",
            str(scenario),
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-artifacts",
            str(artifact_source),
            "--package-name",
            "com.perfeval.fixture",
        ]
    )

    assert code == 0
    run_dir = tmp_path / "runs" / "android_memory_profile"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    report_text = (run_dir / "report" / "report.html").read_text(encoding="utf-8")

    assert summary["memoryProfile"] == "heavy"
    assert summary["budgets"]["memory.pss.peak_mb"]["ratingAbs"] == [700, 1200, 1800]
    assert "heavy 内存档位" in report_text
    assert "优&lt;700 / 良&lt;1200 / 中&lt;1800" in report_text


def test_run_android_device_mode_uses_collector(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_collect_android_run(**kwargs):
        calls.update(kwargs)
        return _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], kwargs["package_name"])

    monkeypatch.setattr(
        run_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(run_command, "collect_android_run", fake_collect_android_run)

    code = main(
        [
            "run",
            "scenarios/android-fixture-startup.yaml",
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-device-run",
            "--package-name",
            "com.perfeval.fixture",
            "--serial",
            "emulator-5554",
            "--apk",
            "fixture.apk",
            "--activity",
            ".SplashActivity",
            "--no-clear-data",
            "--no-reinstall",
            "--perfetto",
        ]
    )

    assert code == 0
    assert calls["package_name"] == "com.perfeval.fixture"
    assert calls["serial"] == "emulator-5554"
    assert calls["apk_path"] == "fixture.apk"
    assert calls["activity"] == ".SplashActivity"
    assert calls["auto_resolve_activity"] is False
    assert calls["clear_data"] is False
    assert calls["reinstall"] is False
    assert calls["perfetto_enabled"] is True

    run_dir = tmp_path / "runs" / "android_fixture_startup"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    diagnostics = [json.loads(line) for line in (run_dir / "diagnostics.ndjson").read_text(encoding="utf-8").splitlines()]
    repeat_dir = run_dir / "repeats" / "r001"
    repeat_summary = json.loads((repeat_dir / "summary.json").read_text(encoding="utf-8"))
    repeat_manifest = json.loads((repeat_dir / "manifest.json").read_text(encoding="utf-8"))

    assert summary["status"] == "completed"
    assert summary["device"]["serialOrUdid"] == "emulator-5554"
    assert repeat_summary["device"]["serialOrUdid"] == "emulator-5554"
    assert (repeat_dir / "timeline.ndjson").exists()
    assert (repeat_dir / "diagnostics.ndjson").exists()
    Manifest.from_dict(repeat_manifest)
    Manifest.from_dict(json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")))
    assert summary["diagnostics"][0]["name"] == "android.device.run.completed"
    assert summary["metrics"]["frame.jank_rate"]["value"] == 0.12
    assert diagnostics[0]["type"] == "diagnostic"
    assert diagnostics[0]["name"] == "android.device.run.completed"


def test_run_android_device_mode_auto_resolves_launcher_activity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        run_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(
        run_command,
        "collect_android_run",
        lambda **kwargs: (
            calls.update(kwargs)
            or _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], kwargs["package_name"])
        ),
    )

    code = main(
        [
            "run",
            "perfeval/gui/resources/presets/startup.yaml",
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-device-run",
            "--package-name",
            "com.example.app",
            "--serial",
            "emulator-5554",
            "--no-reinstall",
        ]
    )

    assert code == 0
    assert calls["activity"] is None
    assert calls["auto_resolve_activity"] is True


def test_run_android_device_mode_without_launch_step_does_not_auto_resolve(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        run_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(
        run_command,
        "collect_android_run",
        lambda **kwargs: (
            calls.update(kwargs)
            or _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], kwargs["package_name"])
        ),
    )

    code = main(
        [
            "run",
            "scenarios/android-current-page-manual-scroll.yaml",
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-device-run",
            "--package-name",
            "com.example.app",
            "--serial",
            "emulator-5554",
            "--no-reinstall",
        ]
    )

    assert code == 0
    assert calls["activity"] is None
    assert calls["auto_resolve_activity"] is False


def test_run_android_device_mode_fails_fast_when_serial_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        run_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=False,
            adb_available=True,
            diagnostics=(f"android.device.not_found:{serial}",),
        ),
    )
    monkeypatch.setattr(run_command, "collect_android_run", lambda **kwargs: calls.update(kwargs))

    code = main(
        [
            "run",
            "scenarios/android-current-page-manual-scroll.yaml",
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-device-run",
            "--package-name",
            "com.perfeval.gamebox",
            "--serial",
            "9806c67b",
            "--no-clear-data",
            "--no-reinstall",
            "--interactive",
        ]
    )

    captured = capsys.readouterr()
    assert code == 10
    assert "Android preflight failed: android.device.not_found:9806c67b" in captured.err
    assert calls == {}


def test_diagnose_android_frame_generates_unified_artifacts(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_collect_android_run(**kwargs):
        calls.update(kwargs)
        return _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], kwargs["package_name"])

    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(diagnose_command, "collect_android_run", fake_collect_android_run)

    code = main(
        [
            "diagnose",
            "android",
            "--package-name",
            "com.perfeval.fixture",
            "--serial",
            "emulator-5554",
            "--out",
            str(tmp_path / "diagnose"),
            "--case",
            "scroll-hotspot",
            "--mode",
            "frame",
            "--memory-profile",
            "heavy",
            "--interactive",
        ]
    )

    assert code == 0
    run_dir = tmp_path / "diagnose" / "scroll-hotspot"
    diagnosis = json.loads((run_dir / "diagnosis.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    assert diagnosis["packageName"] == "com.perfeval.fixture"
    assert diagnosis["serial"] == "emulator-5554"
    assert diagnosis["modes"] == ["frame"]
    assert diagnosis["memoryProfile"] == "heavy"
    assert summary["status"] == "completed"
    assert summary["memoryProfile"] == "heavy"
    assert "frame.jank_rate" in summary["metrics"]
    assert (run_dir / "report" / "report.html").exists()
    assert calls["activity"] is None
    assert calls["clear_data"] is False
    assert calls["reinstall"] is False
    manual_flow = next(command for command in calls["interaction_commands"] if command.name == "manual_flow")
    assert manual_flow.timeout_s == 30.0


def test_diagnose_android_frame_mode_consumes_frame_timeline(
    tmp_path: Path, monkeypatch
) -> None:
    """--mode frame 经优先级链消费帧时间轴：tp 可用时 fpsSource=tp + FPS 派生进 summary。"""
    from perfeval.collectors.android import collector as collector_module
    from perfeval.collectors.android.perfetto_trace import (
        FrameRecord,
        FrameTimelineResult,
    )

    def fake_collect_android_run(**kwargs):
        return _collection_result(Path(kwargs["run_dir"]), kwargs["run_id"], kwargs["package_name"])

    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(diagnose_command, "collect_android_run", fake_collect_android_run)
    frames = [
        (8.0, False),
        (9.0, False),
        (40.0, True),
        (41.0, True),
        (42.0, True),
    ]
    monkeypatch.setattr(
        collector_module,
        "query_frame_timeline",
        lambda trace_path: FrameTimelineResult(
            frames=tuple(
                FrameRecord(
                    frame_index=index,
                    ts_boottime_ns=1_000_000_000 + index * 16_666_667,
                    ts_epoch_ms=1_700_000_000_000 + index * 16,
                    duration_ms=duration_ms,
                    jank=jank,
                    process_name="com.perfeval.fixture",
                    pid=4321,
                )
                for index, (duration_ms, jank) in enumerate(frames)
            ),
            vsync_period_ms=16.667,
            epoch_offset_ns=None,
        ),
    )

    code = main(
        [
            "diagnose",
            "android",
            "--package-name",
            "com.perfeval.fixture",
            "--serial",
            "emulator-5554",
            "--out",
            str(tmp_path / "diagnose"),
            "--case",
            "frame-timeline",
            "--mode",
            "frame",
        ]
    )

    assert code == 0
    summary = json.loads(
        (tmp_path / "diagnose" / "frame-timeline" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["fpsSource"] == "tp"
    assert summary["metrics"]["frame.fps.mean"]["n"] == 5
    assert summary["metrics"]["frame.fps.mean"]["unit"] == "fps"
    # 连续 >= 3 个 jank 帧（frameIndex 2..4）定位为掉帧区间
    assert summary["jankSegments"] == [{"startIndex": 2, "endIndex": 4, "frameCount": 3}]


def test_diagnose_android_fails_fast_when_serial_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=False,
            adb_available=True,
            diagnostics=(f"android.device.not_found:{serial}",),
        ),
    )

    code = main(
        [
            "diagnose",
            "android",
            "--package-name",
            "com.perfeval.gamebox",
            "--serial",
            "missing-device",
            "--out",
            str(tmp_path / "diagnose"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 10
    assert "Android preflight failed: android.device.not_found:missing-device" in captured.err


def test_diagnose_android_full_captures_one_interactive_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_calls: list[str] = []
    commands: list[list[str]] = []

    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": input_calls.append(prompt) or "")
    monkeypatch.setattr(diagnose_command.time, "sleep", lambda seconds: None)

    def fake_runner(command, timeout):
        commands.append(command)
        joined = " ".join(command)
        if " pull " in f" {joined} ":
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_text("artifact", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "pulled\n", "")
        if "simpleperf record" in joined:
            return subprocess.CompletedProcess(command, 0, "111\n", "")
        if "perfetto --background-wait" in joined:
            return subprocess.CompletedProcess(command, 0, "222\n", "")
        if "ls -l /data/misc/perfetto-traces/" in joined:
            return subprocess.CompletedProcess(command, 0, "-rw------- 1 shell shell 12 trace.pftrace\n", "")
        if "simpleperf report" in joined:
            return subprocess.CompletedProcess(command, 0, "Children  Self  Command\n", "")
        if "dumpsys package" in joined:
            return subprocess.CompletedProcess(command, 0, "pkgFlags=[ DEBUGGABLE ]\n", "")
        if "dumpsys gfxinfo" in joined and "reset" not in command and "framestats" not in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n",
                "",
            )
        if "dumpsys meminfo" in joined:
            return subprocess.CompletedProcess(command, 0, "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n", "")
        if "logcat -d" in joined:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = diagnose_command.collect_android_diagnosis(
        package_name="com.perfeval.fixture",
        serial="emulator-5554",
        output_root=tmp_path / "diagnose",
        case_id="full-once",
        mode="full",
        duration_s=30,
        interactive=True,
        command_runner=fake_runner,
    )

    run_dir = tmp_path / "diagnose" / "full-once"
    diagnosis = json.loads((run_dir / "diagnosis.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    assert len(input_calls) == 2
    assert diagnosis["mode"] == "full"
    assert diagnosis["modes"] == ["frame", "cpu", "trace"]
    assert "frame.jank_rate" in summary["metrics"]
    assert (run_dir / "artifacts" / "android" / "perf.data").exists()
    simpleperf_start = next(command for command in commands if "simpleperf record" in " ".join(command))
    assert "sh -c 'simpleperf record" in " ".join(simpleperf_start)
    assert not any(part == "-c" for part in simpleperf_start)
    report_html = (run_dir / "report" / "report.html").read_text(encoding="utf-8")
    assert "CPU 采样" in report_html
    assert "Perfetto Trace" in report_html
    assert "simpleperf-report.txt 摘要" in report_html
    assert any(path.endswith(".pftrace") for path in result["artifacts"])


def test_diagnose_android_full_auto_finishes_when_enter_times_out(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepare_prompts: list[str] = []
    timed_prompts: list[tuple[str, int]] = []

    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": prepare_prompts.append(prompt) or "")
    monkeypatch.setattr(diagnose_command.time, "sleep", lambda seconds: None)

    def fake_timed_input(prompt: str, timeout_s: int, *, timeout_message: str) -> bool:
        timed_prompts.append((prompt, timeout_s))
        return False

    monkeypatch.setattr(diagnose_command, "_input_with_timeout", fake_timed_input)

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if " pull " in f" {joined} ":
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_text("artifact", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "pulled\n", "")
        if "simpleperf record" in joined:
            return subprocess.CompletedProcess(command, 0, "111\n", "")
        if "perfetto --background-wait" in joined:
            return subprocess.CompletedProcess(command, 0, "222\n", "")
        if "ls -l /data/misc/perfetto-traces/" in joined or "ls -l /data/local/tmp/perfeval-diagnose-perf.data" in joined:
            return subprocess.CompletedProcess(command, 0, "-rw------- 1 shell shell 12 artifact\n", "")
        if "simpleperf report" in joined:
            return subprocess.CompletedProcess(command, 0, "Children  Self  Command\n", "")
        if "dumpsys package" in joined:
            return subprocess.CompletedProcess(command, 0, "pkgFlags=[ DEBUGGABLE ]\n", "")
        if "dumpsys gfxinfo" in joined and "reset" not in command and "framestats" not in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n",
                "",
            )
        if "dumpsys meminfo" in joined:
            return subprocess.CompletedProcess(command, 0, "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n", "")
        if "logcat -d" in joined:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    diagnose_command.collect_android_diagnosis(
        package_name="com.perfeval.fixture",
        serial="emulator-5554",
        output_root=tmp_path / "diagnose",
        case_id="full-timeout",
        mode="full",
        duration_s=7,
        interactive=True,
        command_runner=fake_runner,
    )

    run_dir = tmp_path / "diagnose" / "full-timeout"
    diagnosis = json.loads((run_dir / "diagnosis.json").read_text(encoding="utf-8"))

    assert len(prepare_prompts) == 1
    assert timed_prompts and timed_prompts[0][1] == 7
    assert "manual_flow.timeout:7s" in diagnosis["diagnostics"]
    assert (run_dir / "artifacts" / "android" / "perf.data").exists()


def test_diagnose_android_full_drops_stale_perf_data_when_simpleperf_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(diagnose_command.time, "sleep", lambda seconds: None)

    run_dir = tmp_path / "diagnose" / "full-simpleperf-fail"
    stale_perf_data = run_dir / "artifacts" / "android" / "perf.data"
    stale_perf_data.parent.mkdir(parents=True)
    stale_perf_data.write_text("stale", encoding="utf-8")

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "simpleperf record" in joined:
            return subprocess.CompletedProcess(command, 0, "111\n", "")
        if "perfetto --background-wait" in joined:
            return subprocess.CompletedProcess(command, 0, "222\n", "")
        if "ls -l /data/misc/perfetto-traces/" in joined:
            return subprocess.CompletedProcess(command, 0, "-rw------- 1 shell shell 12 trace.pftrace\n", "")
        if "ls -l /data/local/tmp/perfeval-diagnose-perf.data" in joined:
            return subprocess.CompletedProcess(command, 1, "", "No such file or directory")
        if " pull " in f" {joined} ":
            remote_path = command[-2]
            local_path = Path(command[-1])
            if remote_path.endswith(".pftrace"):
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text("trace", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "pulled\n", "")
            if remote_path.endswith("perfeval-diagnose-simpleperf.err"):
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(
                    "simpleperf E environment.cpp:833] Package com.perfeval.gamebox doesn't exist or isn't debuggable/profileable.\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "pulled\n", "")
            if remote_path.endswith("perfeval-diagnose-simpleperf.out"):
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text("", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "pulled\n", "")
            return subprocess.CompletedProcess(command, 1, "", "No such file or directory")
        if "simpleperf report" in joined:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "simpleperf E record_file_reader.cpp:88] failed to open record file '/data/local/tmp/perfeval-diagnose-perf.data': No such file or directory\n",
            )
        if "dumpsys package" in joined:
            return subprocess.CompletedProcess(command, 0, "pkgFlags=[ HAS_CODE ALLOW_CLEAR_USER_DATA LARGE_HEAP ]\n", "")
        if "dumpsys gfxinfo" in joined and "reset" not in command and "framestats" not in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n",
                "",
            )
        if "dumpsys meminfo" in joined:
            return subprocess.CompletedProcess(command, 0, "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n", "")
        if "logcat -d" in joined:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = diagnose_command.collect_android_diagnosis(
        package_name="com.perfeval.gamebox",
        serial="emulator-5554",
        output_root=tmp_path / "diagnose",
        case_id="full-simpleperf-fail",
        mode="full",
        duration_s=10,
        interactive=False,
        command_runner=fake_runner,
    )

    diagnosis = json.loads((run_dir / "diagnosis.json").read_text(encoding="utf-8"))

    assert not stale_perf_data.exists()
    assert not any(path.endswith("/perf.data") for path in diagnosis["artifacts"])
    assert not any(path.endswith("/perf.data") for path in result["artifacts"])
    assert "simpleperf.unavailable:package_not_debuggable_or_profileable" in diagnosis["diagnostics"]
    assert any(path.endswith("simpleperf-record-stderr.txt") for path in diagnosis["artifacts"])


def test_diagnose_android_trace_auto_finishes_when_enter_times_out(
    tmp_path: Path,
    monkeypatch,
) -> None:
    timed_prompts: list[tuple[str, int]] = []

    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )
    monkeypatch.setattr(diagnose_command.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        diagnose_command,
        "_input_with_timeout",
        lambda prompt, timeout_s, *, timeout_message: timed_prompts.append((prompt, timeout_s)) or False,
    )

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "perfetto --background-wait" in joined:
            return subprocess.CompletedProcess(command, 0, "222\n", "")
        if "ls -l /data/misc/perfetto-traces/" in joined:
            return subprocess.CompletedProcess(command, 0, "-rw------- 1 shell shell 12 trace.pftrace\n", "")
        if " pull " in f" {joined} ":
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_text("trace", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "pulled\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = diagnose_command.collect_android_diagnosis(
        package_name="com.perfeval.fixture",
        serial="emulator-5554",
        output_root=tmp_path / "diagnose",
        case_id="trace-timeout",
        mode="trace",
        duration_s=9,
        interactive=True,
        command_runner=fake_runner,
    )

    run_dir = tmp_path / "diagnose" / "trace-timeout"
    diagnosis = json.loads((run_dir / "diagnosis.json").read_text(encoding="utf-8"))

    assert timed_prompts and timed_prompts[0][1] == 9
    assert "manual_flow.timeout:9s" in diagnosis["diagnostics"]
    assert any(path.endswith(".pftrace") for path in result["artifacts"])
    assert any(path.endswith(".pftrace") for path in diagnosis["artifacts"])
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "report" / "report.html").exists()


def test_diagnose_android_trace_start_failure_does_not_reuse_stale_trace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        diagnose_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial="emulator-5554",
        ),
    )
    run_dir = tmp_path / "diagnose" / "trace-fail"
    artifact_dir = run_dir / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    stale = artifact_dir / "perfeval-diagnose-stale.pftrace"
    stale.write_text("stale", encoding="utf-8")
    pull_commands: list[list[str]] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "perfetto --background-wait" in joined:
            return subprocess.CompletedProcess(command, 1, "", "perfetto failed")
        if " pull " in f" {joined} ":
            pull_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = diagnose_command.collect_android_diagnosis(
        package_name="com.perfeval.fixture",
        serial="emulator-5554",
        output_root=tmp_path / "diagnose",
        case_id="trace-fail",
        mode="trace",
        duration_s=5,
        interactive=False,
        command_runner=fake_runner,
    )

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    report_html = (run_dir / "report" / "report.html").read_text(encoding="utf-8")
    assert pull_commands == []
    assert summary["status"] == "partial"
    assert not any(path.endswith(".pftrace") for path in result["artifacts"])
    assert "Perfetto Trace</span><span class=\"value tone-poor\">失败" in report_html


def test_open_command_dry_run_supports_trace_viewers(tmp_path: Path, capsys) -> None:
    trace = tmp_path / "perfeval-diagnose.pftrace"
    trace.write_text("trace", encoding="utf-8")

    studio_code = main(["open", str(trace), "--viewer", "android-studio", "--dry-run"])
    studio_output = capsys.readouterr().out
    assert studio_code == 0
    assert str(trace) in studio_output
    assert "Android Studio" in studio_output or "android-studio" in studio_output or "studio" in studio_output

    perfetto_code = main(["open", str(trace), "--viewer", "perfetto-ui", "--dry-run"])
    perfetto_output = capsys.readouterr().out
    assert perfetto_code == 0
    assert "https://ui.perfetto.dev/" in perfetto_output
    assert "127.0.0.1%3A%3Cport%3E" in perfetto_output

    report_dir = tmp_path / "run" / "report"
    report_dir.mkdir(parents=True)
    report = report_dir / "report.html"
    report.write_text("<!doctype html><title>report</title>", encoding="utf-8")
    report_code = main(["open", str(report), "--viewer", "report", "--dry-run"])
    report_output = capsys.readouterr().out
    assert report_code == 0
    assert f"Would serve {tmp_path / 'run'}" in report_output
    assert "http://127.0.0.1:<port>/report/report.html" in report_output


def test_report_command_renders_summary_html(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.summary.v1",
                "runId": "android_fixture_startup-r001",
                "scenarioId": "android_fixture_startup",
                "status": "completed",
                "app": {"platform": "android", "packageName": "com.perfeval.fixture"},
                "device": {"platform": "android"},
                "metrics": {
                    "frame.jank_rate": {"value": 1.0, "unit": "ratio", "confidence": "low", "evidenceLevel": "L1", "n": 1},
                },
                "diagnostics": [{"name": "android.device.run.completed"}],
                "artifacts": [{"id": "gfxinfo", "path": "artifacts/android/gfxinfo.txt"}],
            }
        ),
        encoding="utf-8",
    )

    code = main(["report", str(summary_path), "--out", str(tmp_path / "report")])

    assert code == 0
    html_text = (tmp_path / "report" / "report.html").read_text(encoding="utf-8")
    assert "PerfEval Lite Android 单次采集报告" in html_text
    assert "frame.jank_rate" in html_text
    assert "没有看到 crash、ANR 或 StrictMode 异常" in html_text
