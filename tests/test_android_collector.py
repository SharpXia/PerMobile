from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from perfeval.collectors.android import (
    AdbClient,
    AndroidDevice,
    AndroidRunExecutor,
    build_android_run_plan,
    parse_launcher_activity_component,
    preflight_from_devices,
    query_launcher_activities_command,
    select_device,
)
from perfeval.collectors.android.flow import InteractionCommand
from perfeval.collectors.android.runner import default_command_runner
from perfeval.core.timeline import TimelineEvent


def test_android_preflight_selects_single_online_device() -> None:
    devices = [AndroidDevice(serial="emulator-5554", state="device", model="Pixel_6")]
    selected, diagnostics = select_device(devices)

    assert selected == devices[0]
    assert diagnostics == []

    result = preflight_from_devices(devices)
    assert result.ok is True
    assert result.selected_serial == "emulator-5554"


def test_android_preflight_reports_ambiguous_or_offline_devices() -> None:
    ambiguous = [
        AndroidDevice(serial="a", state="device"),
        AndroidDevice(serial="b", state="device"),
    ]
    result = preflight_from_devices(ambiguous)
    assert result.ok is False
    assert result.diagnostics == ("android.device.ambiguous",)

    offline = preflight_from_devices([AndroidDevice(serial="a", state="offline")], serial="a")
    assert offline.ok is False
    assert offline.diagnostics == ("android.device.not_online:a:offline",)


def test_default_android_command_runner_replaces_malformed_utf8() -> None:
    completed = default_command_runner(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'before\\xe6after\\n'); os.write(2, b'err\\xff\\n')",
        ],
        5,
    )

    assert completed.returncode == 0
    assert completed.stdout == "before�after\n"
    assert completed.stderr == "err�\n"


def test_adb_client_configures_tolerant_utf8_decoding(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("perfeval.collectors.android.adb.subprocess.run", fake_run)

    result = AdbClient("/adb").run("devices", "-l")

    assert result.stdout == "ok"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_android_run_plan_builds_documented_adb_commands() -> None:
    adb = AdbClient("/opt/android-sdk/platform-tools/adb")
    plan = build_android_run_plan(
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        apk_path="fixture.apk",
        serial="emulator-5554",
        perfetto_enabled=True,
        perfetto_local_path="artifacts/android/perfetto.pftrace",
        adb=adb,
    )

    assert plan.install == [
        "/opt/android-sdk/platform-tools/adb",
        "-s",
        "emulator-5554",
        "install",
        "-r",
        "-d",
        "fixture.apk",
    ]
    assert plan.force_stop[-3:] == ["am", "force-stop", "com.perfeval.fixture"]
    assert plan.reset_gfxinfo[-2:] == ["com.perfeval.fixture", "reset"]
    assert plan.launch[-2:] == ["-n", "com.perfeval.fixture/.MainActivity"]
    assert "-W" in plan.launch
    assert plan.collect_window[-2:] == ["dumpsys", "window"]
    assert plan.collect_meminfo[-2:] == ["meminfo", "com.perfeval.fixture"]
    assert plan.perfetto is not None
    assert "--app" in plan.perfetto
    assert plan.perfetto_pull is not None
    assert plan.perfetto_pull[-1] == "artifacts/android/perfetto.pftrace"


def test_launcher_activity_resolver_builds_command_and_parses_component() -> None:
    adb = AdbClient("/opt/android-sdk/platform-tools/adb")

    command = query_launcher_activities_command(
        adb,
        "com.example.app",
        serial="emulator-5554",
    )

    assert command == [
        "/opt/android-sdk/platform-tools/adb",
        "-s",
        "emulator-5554",
        "shell",
        "cmd",
        "package",
        "query-activities",
        "--brief",
        "-a",
        "android.intent.action.MAIN",
        "-c",
        "android.intent.category.LAUNCHER",
        "com.example.app",
    ]
    assert (
        parse_launcher_activity_component(
            "com.example.app/.MainActivity\n",
            package_name="com.example.app",
        )
        == "com.example.app/.MainActivity"
    )
    assert (
        parse_launcher_activity_component(
            "priority=0 preferredOrder=0 match=0x108000\n"
            "com.example.app/com.example.app.HomeActivity\n",
            package_name="com.example.app",
        )
        == "com.example.app/com.example.app.HomeActivity"
    )
    assert (
        parse_launcher_activity_component(
            "priority=0 preferredOrder=0 match=0x0\n"
            "android/com.android.internal.app.ResolverActivity\n",
            package_name="com.example.app",
        )
        is None
    )
    assert (
        parse_launcher_activity_component(
            "2 activities found:\n"
            "  Activity #0:\n"
            "    com.example.app/.HomeActivity\n"
            "  Activity #1:\n"
            "    com.example.app/.DebugActivity\n",
            package_name="com.example.app",
        )
        == "com.example.app/.HomeActivity"
    )


def test_android_run_plan_auto_resolves_activity_when_requested() -> None:
    plan = build_android_run_plan(
        package_name="com.example.app",
        activity=None,
        auto_resolve_activity=True,
        adb=AdbClient("/adb"),
    )

    assert plan.resolve_activity is not None
    assert "query-activities" in plan.resolve_activity
    assert plan.force_stop is not None
    assert plan.clear_data is not None
    assert plan.launch is None
    assert "launch.activity.auto_resolve" in plan.notes
    assert "launch.skipped.no_activity" not in plan.notes


def test_android_run_executor_collects_artifacts_with_injected_runner(tmp_path) -> None:
    perfetto_path = tmp_path / "run" / "artifacts" / "android" / "perfetto.pftrace"

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = (
                "Total frames rendered: 100\n"
                "Janky frames: 12 (12.00%)\n"
                "95th percentile: 24ms\n"
            )
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = (
                "Starting: Intent { flg=0x10000000 cmp=com.perfeval.fixture/.MainActivity }\n"
                "Status: ok\n"
                "LaunchState: COLD\n"
                "Activity: com.perfeval.fixture/.MainActivity\n"
                "TotalTime: 1450\n"
                "WaitTime: 1462\n"
                "Complete\n"
            )
        elif "logcat -d" in joined:
            completed.stdout = "E AndroidRuntime: FATAL EXCEPTION: main\n"
        elif "pull" in command:
            perfetto_path.parent.mkdir(parents=True, exist_ok=True)
            perfetto_path.write_text("perfetto-trace", encoding="utf-8")
        return completed

    executor = AndroidRunExecutor(
        adb=AdbClient("/adb"),
        command_runner=fake_runner,
    )
    result = executor.collect(
        run_id="run-android",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        perfetto_enabled=True,
        perfetto_duration_ms=0,
        post_launch_wait_s=0,
    )

    events_by_name = {event["name"]: event for event in result.events}
    assert "frame.jank_rate" in events_by_name
    assert "memory.pss.peak_mb" in events_by_name
    assert "stability.crash.count" in events_by_name
    assert "startup.cold.ms" in events_by_name
    assert events_by_name["startup.cold.ms"]["data"]["value"] == 1450
    assert events_by_name["startup.cold.ms"]["data"]["launchState"] == "cold"
    assert events_by_name["startup.cold.ms"]["source"]["evidenceLevel"] == "L0"
    assert events_by_name["startup.cold.ms"]["seq"] == 1
    assert all(event["seq"] >= 1 for event in result.events)
    for event in result.events:
        TimelineEvent.from_dict(event)
    assert (tmp_path / "run" / "artifacts" / "android" / "gfxinfo.txt").exists()
    assert (tmp_path / "run" / "artifacts" / "android" / "perfetto.pftrace").exists()
    assert any(artifact["path"].endswith("perfetto.pftrace") for artifact in result.artifacts)
    assert result.diagnostics == ()


def test_android_run_executor_reports_missing_perfetto_pull(tmp_path) -> None:
    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1450\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = "E AndroidRuntime: FATAL EXCEPTION: main\n"
        return completed

    executor = AndroidRunExecutor(
        adb=AdbClient("/adb"),
        command_runner=fake_runner,
    )
    result = executor.collect(
        run_id="run-android",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        perfetto_enabled=True,
        perfetto_duration_ms=0,
        post_launch_wait_s=0,
    )

    assert any(diagnostic.startswith("android.perfetto_pull_missing:") for diagnostic in result.diagnostics)
    assert not (tmp_path / "run" / "artifacts" / "android" / "perfetto.pftrace").exists()


def test_android_run_executor_removes_stale_artifacts_before_early_failure(tmp_path) -> None:
    artifact_dir = tmp_path / "run" / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "gfxinfo.txt").write_text(
        "Total frames rendered: 100\nJanky frames: 99 (99.00%)\n",
        encoding="utf-8",
    )

    def fake_runner(command, timeout):
        if "install" in command:
            return subprocess.CompletedProcess(command, 1, "", "install failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    result = executor.collect(
        run_id="run-stale",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        apk_path="fixture.apk",
        post_launch_wait_s=0,
    )

    assert result.events == []
    assert result.artifacts == []
    assert not (artifact_dir / "gfxinfo.txt").exists()


def test_android_run_executor_keeps_interactions_inside_perfetto_window(tmp_path, monkeypatch) -> None:
    order: list[str] = []
    monotonic_values = iter([10.0, 10.0, 10.0, 11.0])
    monkeypatch.setattr("perfeval.collectors.android.runner.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("perfeval.collectors.android.runner.time.sleep", lambda seconds: order.append("sleep"))

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "perfetto" in command:
            order.append("perfetto")
        elif "am start" in joined:
            order.append("launch")
        elif "input swipe" in joined:
            order.append("swipe")
        elif "pull" in command:
            order.append("pull")
            target = Path(command[-1])
            target.write_text("trace", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    adb = AdbClient("/adb")
    executor = AndroidRunExecutor(adb=adb, command_runner=fake_runner)
    executor.collect(
        run_id="run-trace",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        perfetto_enabled=True,
        perfetto_duration_ms=2000,
        post_launch_wait_s=0,
        interaction_commands=[
            InteractionCommand(
                name="swipe",
                command=tuple(adb.command("shell", "input", "swipe", "1", "2", "3", "4", "100")),
            )
        ],
    )

    assert order.index("perfetto") < order.index("launch") < order.index("swipe")
    assert order.index("swipe") < order.index("sleep") < order.index("pull")


def test_android_run_executor_continues_when_clear_logcat_times_out(tmp_path) -> None:
    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "logcat -c" in joined:
            raise subprocess.TimeoutExpired(command, timeout)

        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1450\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    result = executor.collect(
        run_id="run-android",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        perfetto_enabled=False,
        post_launch_wait_s=0,
    )

    events_by_name = {event["name"]: event for event in result.events}
    assert events_by_name["frame.jank_rate"]["data"]["value"] == 0.12
    assert any(diagnostic.startswith("android.command_failed:clear_logcat:124:") for diagnostic in result.diagnostics)


def test_scroll_executor_resets_gfxinfo_before_collect(tmp_path) -> None:
    """reset_gfxinfo interaction command must execute before collect_gfxinfo."""
    executed_names: list[str] = []

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "dumpsys gfxinfo" in joined and "reset" in command:
            executed_names.append("reset_gfxinfo")
        elif "dumpsys gfxinfo" in joined and "framestats" not in joined:
            executed_names.append("collect_gfxinfo")
            completed.stdout = "Total frames rendered: 200\nJanky frames: 5 (2.50%)\n95th percentile: 16ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1200\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    adb = AdbClient("/adb")
    interaction_commands = [
        InteractionCommand(
            name="reset_gfxinfo",
            command=tuple(adb.command("shell", "dumpsys", "gfxinfo", "com.perfeval.fixture", "reset")),
        ),
        InteractionCommand(
            name="swipe_1",
            command=tuple(adb.command("shell", "input", "swipe", "540", "1600", "540", "400", "300")),
            wait_s=0,
        ),
    ]
    executor = AndroidRunExecutor(adb=adb, command_runner=fake_runner)
    result = executor.collect(
        run_id="run-scroll",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".ScrollListActivity",
        perfetto_enabled=False,
        post_launch_wait_s=0,
        interaction_commands=interaction_commands,
    )

    reset_idx = executed_names.index("reset_gfxinfo")
    collect_idx = executed_names.index("collect_gfxinfo")
    assert reset_idx < collect_idx
    assert result.diagnostics == ()


def test_scroll_executor_with_interaction_steps(tmp_path) -> None:
    """Swipe interaction commands are executed during the run."""
    executed_names: list[str] = []

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "input swipe" in joined:
            executed_names.append("swipe")
        elif "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 200\nJanky frames: 5 (2.50%)\n95th percentile: 16ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1200\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    adb = AdbClient("/adb")
    interaction_commands = [
        InteractionCommand(
            name="swipe_1",
            command=tuple(adb.command("shell", "input", "swipe", "540", "1600", "540", "400", "300")),
            wait_s=0,
        ),
        InteractionCommand(
            name="swipe_2",
            command=tuple(adb.command("shell", "input", "swipe", "540", "1600", "540", "400", "300")),
            wait_s=0,
        ),
    ]
    executor = AndroidRunExecutor(adb=adb, command_runner=fake_runner)
    executor.collect(
        run_id="run-scroll",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".ScrollListActivity",
        perfetto_enabled=False,
        post_launch_wait_s=0,
        interaction_commands=interaction_commands,
    )

    assert executed_names.count("swipe") == 2


def test_interaction_command_waits_after_command(tmp_path, monkeypatch) -> None:
    """Interaction commands must honor their post-command wait interval."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("perfeval.collectors.android.runner.time.sleep", lambda seconds: sleep_calls.append(seconds))

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 100\nJanky frames: 5 (5.00%)\n95th percentile: 20ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1000\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    adb = AdbClient("/adb")
    executor = AndroidRunExecutor(adb=adb, command_runner=fake_runner)
    executor.collect(
        run_id="run-scroll",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".ScrollListActivity",
        perfetto_enabled=False,
        post_launch_wait_s=0,
        interaction_commands=[
            InteractionCommand(
                name="swipe_1",
                command=tuple(adb.command("shell", "input", "swipe", "540", "1600", "540", "400", "300")),
                wait_s=0.5,
            )
        ],
    )

    assert 0.5 in sleep_calls


def test_non_interactive_mode_manual_step_skipped(tmp_path) -> None:
    """In non-interactive mode, manual steps are skipped with a diagnostic."""
    manual_cmd = InteractionCommand(name="manual", is_manual=True, prompt="press enter")

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 100\nJanky frames: 5 (5.00%)\n95th percentile: 20ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1000\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    result = executor.collect(
        run_id="run-scroll",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".ScrollListActivity",
        perfetto_enabled=False,
        post_launch_wait_s=0,
        interaction_commands=[manual_cmd],
        interactive=False,
    )

    assert "flow.manual.skipped" in result.diagnostics


def test_interactive_mode_manual_step_pauses(tmp_path, monkeypatch) -> None:
    """In interactive mode, manual steps call input() and then continue."""
    manual_cmd = InteractionCommand(name="manual", is_manual=True, prompt="press enter")
    input_calls: list[str] = []

    monkeypatch.setattr("builtins.input", lambda *a: input_calls.append("enter") or "")

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 100\nJanky frames: 5 (5.00%)\n95th percentile: 20ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1000\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    result = executor.collect(
        run_id="run-scroll",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".ScrollListActivity",
        perfetto_enabled=False,
        post_launch_wait_s=0,
        interaction_commands=[manual_cmd],
        interactive=True,
    )

    assert len(input_calls) == 1
    assert "flow.manual.skipped" not in result.diagnostics


def test_interactive_mode_manual_step_timeout_continues(tmp_path, monkeypatch) -> None:
    """Timed manual steps continue collection when Enter is not pressed."""
    manual_cmd = InteractionCommand(name="manual_flow", is_manual=True, prompt="scroll", timeout_s=2)

    monkeypatch.setattr("perfeval.collectors.android.runner._input_with_timeout", lambda *a, **kw: False)

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 100\nJanky frames: 5 (5.00%)\n95th percentile: 20ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "am start" in joined and "-W" in command:
            completed.stdout = "Status: ok\nLaunchState: COLD\nTotalTime: 1000\nComplete\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    result = executor.collect(
        run_id="run-scroll",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".ScrollListActivity",
        perfetto_enabled=False,
        post_launch_wait_s=0,
        interaction_commands=[manual_cmd],
        interactive=True,
    )

    assert "flow.manual.timeout:manual_flow:2s" in result.diagnostics
    assert result.artifacts


def test_no_launch_activity_skips_am_start(tmp_path) -> None:
    """When activity is None, am start is not executed and no startup event."""
    launch_commands: list[str] = []

    def fake_runner(command, timeout):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        joined = " ".join(command)
        if "am start" in joined:
            launch_commands.append(joined)
        elif "dumpsys gfxinfo" in joined and "framestats" not in joined:
            completed.stdout = "Total frames rendered: 100\nJanky frames: 5 (5.00%)\n95th percentile: 20ms\n"
        elif "dumpsys meminfo" in joined:
            completed.stdout = "App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n"
        elif "logcat -d" in joined:
            completed.stdout = ""
        return completed

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    result = executor.collect(
        run_id="run-no-launch",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=None,
        perfetto_enabled=False,
        post_launch_wait_s=0,
    )

    assert len(launch_commands) == 0
    events_by_name = {event["name"]: event for event in result.events}
    assert "startup.cold.ms" not in events_by_name


def test_auto_resolved_activity_runs_after_install_and_launches_app(tmp_path) -> None:
    command_order: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if " install " in f" {joined} ":
            command_order.append("install")
        elif "query-activities" in command:
            command_order.append("resolve_activity")
            return subprocess.CompletedProcess(
                command,
                0,
                "com.example.app/.HomeActivity\n",
                "",
            )
        elif "am force-stop" in joined:
            command_order.append("force_stop")
        elif "pm clear" in joined:
            command_order.append("clear_data")
        elif "am start" in joined:
            command_order.append("launch")
            return subprocess.CompletedProcess(
                command,
                0,
                "Status: ok\nLaunchState: COLD\nTotalTime: 842\nComplete\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    result = executor.collect(
        run_id="run-auto-launch",
        run_dir=tmp_path / "run",
        package_name="com.example.app",
        activity=None,
        apk_path="example.apk",
        auto_resolve_activity=True,
        perfetto_enabled=False,
        post_launch_wait_s=0,
    )

    assert command_order.index("install") < command_order.index("resolve_activity")
    assert command_order.index("resolve_activity") < command_order.index("force_stop")
    assert command_order.index("force_stop") < command_order.index("clear_data")
    assert command_order.index("clear_data") < command_order.index("launch")
    launch_result = next(command for command in result.commands if command.name == "launch")
    assert launch_result.command[-2:] == ("-n", "com.example.app/.HomeActivity")
    startup = next(event for event in result.events if event["name"] == "startup.cold.ms")
    assert startup["data"]["value"] == 842


def test_auto_resolved_activity_failure_stops_collection_and_reports_error(tmp_path) -> None:
    executed: list[str] = []
    phases: list[tuple[str, str, str, str | None]] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        executed.append(joined)
        if "query-activities" in command:
            return subprocess.CompletedProcess(command, 0, "No activity found\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    executor = AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner)
    with pytest.raises(ValueError) as exc_info:
        executor.collect(
            run_id="run-unresolved-launch",
            run_dir=tmp_path / "run",
            package_name="com.example.no_launcher",
            activity=None,
            auto_resolve_activity=True,
            perfetto_enabled=False,
            post_launch_wait_s=0,
            phase_callback=lambda phase_id, state, label, detail: phases.append(
                (phase_id, state, label, detail)
            ),
        )

    message = str(exc_info.value)
    assert "com.example.no_launcher" in message
    assert "Launcher Activity" in message
    assert "填写 Activity" in message
    assert not any("am start" in command for command in executed)
    assert not any("dumpsys meminfo" in command for command in executed)
    assert phases[-1][0:3] == ("app_prepare", "failed", "启动应用")
    assert phases[-1][3] == message
