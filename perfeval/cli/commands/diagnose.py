from __future__ import annotations

import select
import subprocess
import shlex
import sys
import time
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any, Callable

from perfeval.cli.protocol import add_gui_events_argument, emit_phase, emit_result, gui_events_enabled
from perfeval.collectors.android import collect_android_run, index_android_artifacts, parse_android_artifacts, run_preflight
from perfeval.collectors.android.adb import AdbClient
from perfeval.collectors.android.flow import InteractionCommand
from perfeval.core.config import (
    ensure_directory,
    load_json_document,
    sanitize_slug,
    utc_now_iso,
    write_json_document,
    write_ndjson_document,
    write_text,
)
from perfeval.core.memory_profile import MEMORY_PROFILES, normalize_memory_profile
from perfeval.core.report import write_summary_report_bundle
from perfeval.core.summary import summarize_timeline


CommandRunner = Callable[[list[str], float | None], subprocess.CompletedProcess[str]]


def register_parser(subparsers: Any) -> ArgumentParser:
    parser = subparsers.add_parser(
        "diagnose",
        help="Capture focused performance diagnosis artifacts.",
        description="Capture focused performance diagnosis artifacts.",
    )
    target_parsers = parser.add_subparsers(dest="target")
    android = target_parsers.add_parser(
        "android",
        help="Capture Android frame, CPU, or Perfetto evidence.",
        description="Capture Android frame, CPU, or Perfetto evidence for a focused flow.",
    )
    android.add_argument("--serial", help="Android device serial.")
    android.add_argument("--package-name", required=True, help="Android package name to diagnose.")
    android.add_argument("--out", default="runs/android-diagnose", help="Output root for diagnosis artifacts.")
    android.add_argument("--case", default="android-diagnose", help="Case/run id for the diagnosis directory.")
    android.add_argument(
        "--mode",
        choices=["frame", "cpu", "trace", "full"],
        default="frame",
        help="Evidence to capture: frame=gfxinfo/meminfo/logcat, cpu=simpleperf, trace=Perfetto, full=all.",
    )
    android.add_argument("--duration", type=int, default=30, help="Seconds for simpleperf or Perfetto capture.")
    android.add_argument("--interactive", action="store_true", help="Prompt while you perform the focused flow.")
    android.add_argument(
        "--memory-profile",
        choices=MEMORY_PROFILES,
        default="mainstream",
        help="Memory rating profile for the diagnosis report.",
    )
    android.add_argument(
        "--first-party-regex",
        help="Optional app-owned package/module regex noted in diagnosis metadata for hotspot filtering.",
    )
    add_gui_events_argument(android)
    android.set_defaults(func=handle_android)
    parser.set_defaults(func=_handle_missing_target)
    return parser


def _handle_missing_target(args: Namespace) -> int:
    print("Choose a diagnose target, for example: perfeval diagnose android --package-name com.example.app")
    return 0


def default_command_runner(command: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def handle_android(args: Namespace) -> int:
    try:
        result = collect_android_diagnosis(
            package_name=args.package_name,
            serial=args.serial,
            output_root=Path(args.out),
            case_id=args.case,
            mode=args.mode,
            duration_s=args.duration,
            interactive=args.interactive,
            memory_profile=args.memory_profile,
            first_party_regex=args.first_party_regex,
        )
    except Exception as exc:
        emit_phase("diagnosis", "failed", "采集诊断证据", detail=str(exc))
        raise
    emit_phase("diagnosis", "completed", "采集诊断证据")
    emit_phase("report", "started", "生成报告")
    emit_phase("report", "completed", "生成报告")
    if gui_events_enabled(args):
        status = "completed"
        summary_path = result.get("summaryPath")
        if summary_path and Path(summary_path).exists():
            summary_payload = load_json_document(Path(summary_path))
            if isinstance(summary_payload, dict):
                status = str(summary_payload.get("status") or status)
        emit_result(
            status=status,
            exit_code=0,
            outputRoot=str(Path(args.out)),
            runDir=str(result["runDir"]),
            report=str(result["reportHtml"]) if result.get("reportHtml") else None,
            summary=str(summary_path) if summary_path else None,
            artifacts=[str(path) for path in result.get("artifacts", [])],
            diagnosis=str(result["diagnosisPath"]) if result.get("diagnosisPath") else None,
        )
    else:
        print(f"Android diagnosis written to {result['runDir']}")
        if result.get("summaryPath"):
            print(f"summary: {result['summaryPath']}")
        if result.get("reportHtml"):
            print(f"report: {result['reportHtml']}")
            print(f"open report: perfeval open {result['reportHtml']} --viewer report")
        for artifact in result.get("artifacts", []):
            print(f"artifact: {artifact}")
    return 0


def collect_android_diagnosis(
    *,
    package_name: str,
    serial: str | None,
    output_root: Path,
    case_id: str,
    mode: str,
    duration_s: int,
    interactive: bool,
    memory_profile: str = "mainstream",
    first_party_regex: str | None = None,
    command_runner: CommandRunner = default_command_runner,
) -> dict[str, Any]:
    if duration_s < 1:
        raise ValueError("duration must be a positive integer")
    adb = AdbClient()
    emit_phase("device_check", "started", "检查设备")
    preflight = run_preflight(adb, serial=serial)
    if not preflight.ok:
        emit_phase("device_check", "failed", "检查设备", detail=", ".join(preflight.diagnostics))
        reasons = ", ".join(preflight.diagnostics) if preflight.diagnostics else "unknown"
        raise ValueError(f"Android preflight failed: {reasons}")

    selected_serial = serial or preflight.selected_serial
    if not selected_serial:
        raise ValueError("Android preflight failed: android.device.no_selected_serial")
    emit_phase("device_check", "completed", "检查设备")
    emit_phase("diagnosis", "started", "采集诊断证据")

    slug = sanitize_slug(case_id)
    run_dir = ensure_directory(output_root / slug)
    artifact_dir = ensure_directory(run_dir / "artifacts" / "android")
    modes = _expand_modes(mode)
    normalized_memory_profile, profile_diagnostic = normalize_memory_profile(memory_profile)
    metadata: dict[str, Any] = {
        "schemaVersion": "perfeval.diagnosis.v1",
        "createdAtUtc": utc_now_iso(),
        "caseId": slug,
        "platform": "android",
        "packageName": package_name,
        "serial": selected_serial,
        "mode": mode,
        "modes": modes,
        "durationSeconds": duration_s,
        "memoryProfile": normalized_memory_profile,
        "firstPartyRegex": first_party_regex,
        "diagnostics": [],
        "artifacts": [],
    }
    if profile_diagnostic:
        metadata["diagnostics"].append(profile_diagnostic)

    if mode == "full":
        _capture_full_once(
            run_dir=run_dir,
            artifact_dir=artifact_dir,
            package_name=package_name,
            serial=selected_serial,
            duration_s=duration_s,
            interactive=interactive,
            adb=adb,
            command_runner=command_runner,
            metadata=metadata,
        )
    else:
        if "frame" in modes:
            _capture_frame_snapshot(
                run_dir=run_dir,
                package_name=package_name,
                serial=selected_serial,
                duration_s=duration_s,
                interactive=interactive,
                metadata=metadata,
            )
        if "cpu" in modes:
            _capture_simpleperf(
                artifact_dir=artifact_dir,
                package_name=package_name,
                serial=selected_serial,
                duration_s=duration_s,
                interactive=interactive,
                adb=adb,
                command_runner=command_runner,
                metadata=metadata,
            )
        if "trace" in modes:
            _capture_perfetto(
                artifact_dir=artifact_dir,
                package_name=package_name,
                serial=selected_serial,
                duration_s=duration_s,
                interactive=interactive,
                adb=adb,
                command_runner=command_runner,
                metadata=metadata,
            )

    if not metadata.get("summaryPath"):
        _write_evidence_only_summary(
            run_dir=run_dir,
            package_name=package_name,
            serial=selected_serial,
            metadata=metadata,
        )

    diagnosis_path = run_dir / "diagnosis.json"
    metadata["runDir"] = str(run_dir)
    metadata["diagnosisPath"] = str(diagnosis_path)
    summary_path = Path(metadata["summaryPath"]) if metadata.get("summaryPath") else None
    if summary_path and summary_path.exists():
        summary_payload = load_json_document(summary_path)
        if isinstance(summary_payload, dict):
            report_outputs = write_summary_report_bundle(summary_payload, run_dir / "report", diagnosis=metadata)
            metadata["reportHtml"] = str(report_outputs["report_html"])
    write_json_document(diagnosis_path, metadata)
    return metadata


def _expand_modes(mode: str) -> list[str]:
    if mode == "full":
        return ["frame", "cpu", "trace"]
    return [mode]


def _capture_frame_snapshot(
    *,
    run_dir: Path,
    package_name: str,
    serial: str,
    duration_s: int,
    interactive: bool,
    metadata: dict[str, Any],
) -> None:
    adb = AdbClient()
    interaction_commands: list[InteractionCommand] = []
    if interactive:
        interaction_commands.append(
            InteractionCommand(
                name="manual_prepare",
                is_manual=True,
                prompt="请把设备停在目标页面，然后按 Enter；工具会清空 gfxinfo。",
            )
        )
    interaction_commands.append(
        InteractionCommand(
            name="reset_gfxinfo",
            command=tuple(adb.command("shell", "dumpsys", "gfxinfo", package_name, "reset", serial=serial)),
        )
    )
    if interactive:
        interaction_commands.append(
            InteractionCommand(
                name="manual_flow",
                is_manual=True,
                prompt=f"请执行一次聚焦的滑动/操作流程，结束后按 Enter 采集 frame/memory/logcat（最长 {duration_s}s）。",
                timeout_s=float(max(1, duration_s)),
                timeout_message=f"已达到最大采集时长 {duration_s}s，自动采集 frame/memory/logcat。",
            )
        )
    interaction_commands.append(InteractionCommand(name="settle", wait_s=0.5))

    frame_result = collect_android_run(
        run_id=run_dir.name,
        run_dir=run_dir,
        package_name=package_name,
        activity=None,
        serial=serial,
        clear_data=False,
        reinstall=False,
        perfetto_enabled=False,
        post_launch_wait_s=0,
        interaction_commands=interaction_commands,
        interactive=interactive,
    )
    diagnostics = [_diagnostic_event(run_dir.name, idx, item) for idx, item in enumerate(frame_result.diagnostics, start=1)]
    summary = summarize_timeline(
        run_dir.name,
        frame_result.events,
        status="partial" if diagnostics else "completed",
        evidence_level=["L1"],
        artifacts=frame_result.artifacts,
        diagnostics=diagnostics,
        context={
            "scenarioId": run_dir.name,
            "scenario": {"scenarioId": run_dir.name, "description": "Android focused frame diagnosis"},
            "memoryProfile": metadata.get("memoryProfile", "mainstream"),
            "app": {"platform": "android", "packageName": package_name},
            "device": {"platform": "android", "serialOrUdid": serial},
        },
    )
    write_json_document(run_dir / "summary.json", summary)
    write_ndjson_document(run_dir / "timeline.ndjson", frame_result.events)
    write_ndjson_document(run_dir / "diagnostics.ndjson", diagnostics)
    report_outputs = write_summary_report_bundle(summary, run_dir / "report")
    metadata["summaryPath"] = str(run_dir / "summary.json")
    metadata["reportHtml"] = str(report_outputs["report_html"])
    metadata["diagnostics"].extend(frame_result.diagnostics)
    metadata["artifacts"].extend(artifact["path"] for artifact in frame_result.artifacts)


def _capture_full_once(
    *,
    run_dir: Path,
    artifact_dir: Path,
    package_name: str,
    serial: str,
    duration_s: int,
    interactive: bool,
    adb: AdbClient,
    command_runner: CommandRunner,
    metadata: dict[str, Any],
) -> None:
    remote_perf = "/data/local/tmp/perfeval-diagnose-perf.data"
    remote_perf_stdout = "/data/local/tmp/perfeval-diagnose-simpleperf.out"
    remote_perf_stderr = "/data/local/tmp/perfeval-diagnose-simpleperf.err"
    trace_name = f"perfeval-diagnose-{int(time.time())}.pftrace"
    remote_trace = f"/data/misc/perfetto-traces/{trace_name}"
    local_trace = artifact_dir / trace_name
    perf_data_path = artifact_dir / "perf.data"
    simpleperf_report_path = artifact_dir / "simpleperf-report.txt"
    profileable_path = artifact_dir / "simpleperf-profileable.txt"
    simpleperf_start_path = artifact_dir / "simpleperf-start.txt"
    simpleperf_record_stdout = artifact_dir / "simpleperf-record-stdout.txt"
    simpleperf_record_stderr = artifact_dir / "simpleperf-record-stderr.txt"
    perfetto_start_stdout = artifact_dir / "perfetto-start-stdout.txt"
    perfetto_start_stderr = artifact_dir / "perfetto-start-stderr.txt"
    _unlink_existing(
        perf_data_path,
        simpleperf_report_path,
        simpleperf_record_stdout,
        simpleperf_record_stderr,
        simpleperf_start_path,
        local_trace,
    )

    if interactive:
        input("请把设备停在目标页面，然后按 Enter；工具会清空 gfxinfo/logcat 并启动 simpleperf + Perfetto。")

    profileable = _run(adb.command("shell", "dumpsys", "package", package_name, serial=serial), 30, command_runner)
    write_text(profileable_path, profileable.stdout or profileable.stderr)
    _run(
        adb.command(
            "shell",
            "rm",
            "-f",
            remote_perf,
            remote_perf_stdout,
            remote_perf_stderr,
            remote_trace,
            serial=serial,
        ),
        15,
        command_runner,
    )
    _run(adb.command("logcat", "-c", serial=serial), 10, command_runner)
    _run(adb.command("shell", "dumpsys", "gfxinfo", package_name, "reset", serial=serial), 30, command_runner)

    simpleperf_pid = _start_simpleperf_background(
        adb=adb,
        serial=serial,
        package_name=package_name,
        remote_perf=remote_perf,
        remote_stdout=remote_perf_stdout,
        remote_stderr=remote_perf_stderr,
        duration_s=duration_s,
        command_runner=command_runner,
    )
    write_text(simpleperf_start_path, simpleperf_pid + "\n")

    perfetto_start = _start_perfetto(
        adb=adb,
        serial=serial,
        package_name=package_name,
        remote_trace=remote_trace,
        duration_s=duration_s,
        command_runner=command_runner,
    )
    write_text(perfetto_start_stdout, perfetto_start.stdout)
    write_text(perfetto_start_stderr, perfetto_start.stderr)
    perfetto_pid = _last_nonempty_line(perfetto_start.stdout)

    metadata["diagnostics"].append(f"simpleperf.start.pid:{simpleperf_pid or 'unknown'}")
    metadata["diagnostics"].append(f"perfetto.start.returncode:{perfetto_start.returncode}")
    metadata["diagnostics"].append(f"perfetto.start.pid:{perfetto_pid or 'unknown'}")

    if interactive:
        completed_by_enter = _input_with_timeout(
            f"simpleperf + Perfetto 已同时开始采集。请只执行一次聚焦流程，完成后按 Enter 停止并拉取证据（最长 {duration_s}s）。",
            duration_s,
            timeout_message=f"已达到最大采集时长 {duration_s}s，自动结束并拉取证据。",
        )
        if not completed_by_enter:
            metadata["diagnostics"].append(f"manual_flow.timeout:{duration_s}s")
    else:
        time.sleep(max(1, duration_s))

    if simpleperf_pid and simpleperf_pid.isdigit():
        simpleperf_stop = _run(adb.command("shell", "kill", "-INT", simpleperf_pid, serial=serial), 15, command_runner)
        metadata["diagnostics"].append(f"simpleperf.stop.returncode:{simpleperf_stop.returncode}")
    if perfetto_pid and perfetto_pid.isdigit():
        _run(adb.command("shell", "kill", "-TERM", perfetto_pid, serial=serial), 15, command_runner)

    _wait_for_remote_file(adb, serial, remote_perf, command_runner)
    if perfetto_start.returncode == 0:
        _wait_for_remote_file(adb, serial, remote_trace, command_runner)
    simpleperf_pull = _run(adb.command("pull", remote_perf, str(perf_data_path), serial=serial), 60, command_runner)
    if perfetto_start.returncode == 0:
        perfetto_pull = _run(adb.command("pull", remote_trace, str(local_trace), serial=serial), 60, command_runner)
    else:
        perfetto_pull = subprocess.CompletedProcess([], perfetto_start.returncode, "", "perfetto did not start")
    metadata["diagnostics"].append(f"simpleperf.pull.returncode:{simpleperf_pull.returncode}")
    metadata["diagnostics"].append(f"perfetto.pull.returncode:{perfetto_pull.returncode}")
    if simpleperf_pull.returncode != 0:
        _unlink_existing(perf_data_path)

    report = _run(adb.command("shell", "simpleperf", "report", "-i", remote_perf, serial=serial), 60, command_runner)
    write_text(simpleperf_report_path, report.stdout or report.stderr)
    _pull_optional(adb, serial, remote_perf_stdout, simpleperf_record_stdout, command_runner)
    _pull_optional(adb, serial, remote_perf_stderr, simpleperf_record_stderr, command_runner)
    unavailable_reason = _simpleperf_unavailable_reason(
        "\n".join(
            (
                report.stdout or "",
                report.stderr or "",
                _read_text_if_exists(simpleperf_record_stderr),
            )
        )
    )
    if unavailable_reason:
        metadata["diagnostics"].append(f"simpleperf.unavailable:{unavailable_reason}")

    _collect_frame_artifacts_after_flow(
        run_dir=run_dir,
        artifact_dir=artifact_dir,
        package_name=package_name,
        serial=serial,
        adb=adb,
        command_runner=command_runner,
        metadata=metadata,
    )
    metadata["artifacts"].extend(
        str(path)
        for path in (
            profileable_path,
            simpleperf_start_path,
            simpleperf_report_path,
            simpleperf_record_stdout,
            simpleperf_record_stderr,
            local_trace,
            perfetto_start_stdout,
            perfetto_start_stderr,
        )
        if path.exists()
    )
    if perf_data_path.exists() and perf_data_path.stat().st_size > 0:
        metadata["artifacts"].append(str(perf_data_path))


def _capture_simpleperf(
    *,
    artifact_dir: Path,
    package_name: str,
    serial: str,
    duration_s: int,
    interactive: bool,
    adb: AdbClient,
    command_runner: CommandRunner,
    metadata: dict[str, Any],
) -> None:
    remote_perf = "/data/local/tmp/perfeval-diagnose-perf.data"
    profileable_path = artifact_dir / "simpleperf-profileable.txt"
    record_stdout = artifact_dir / "simpleperf-record-stdout.txt"
    record_stderr = artifact_dir / "simpleperf-record-stderr.txt"
    report_path = artifact_dir / "simpleperf-report.txt"
    perf_data_path = artifact_dir / "perf.data"
    _unlink_existing(perf_data_path, report_path, record_stdout, record_stderr)

    profileable = _run(adb.command("shell", "dumpsys", "package", package_name, serial=serial), 30, command_runner)
    write_text(profileable_path, profileable.stdout or profileable.stderr)
    _run(adb.command("shell", "rm", "-f", remote_perf, serial=serial), 15, command_runner)
    _run(adb.command("logcat", "-c", serial=serial), 10, command_runner)

    if interactive:
        print(f"simpleperf recording for {duration_s}s. 请现在执行一次聚焦流程。")
    record = _run(
        adb.command(
            "shell",
            "simpleperf",
            "record",
            "--app",
            package_name,
            "-o",
            remote_perf,
            "-e",
            "cpu-clock",
            "-f",
            "4000",
            "-g",
            "--duration",
            str(max(1, duration_s)),
            serial=serial,
        ),
        duration_s + 20,
        command_runner,
    )
    write_text(record_stdout, record.stdout)
    write_text(record_stderr, record.stderr)
    metadata["diagnostics"].append(f"simpleperf.record.returncode:{record.returncode}")

    pull = _run(adb.command("pull", remote_perf, str(perf_data_path), serial=serial), 60, command_runner)
    metadata["diagnostics"].append(f"simpleperf.pull.returncode:{pull.returncode}")
    if pull.returncode != 0:
        _unlink_existing(perf_data_path)
    report = _run(
        adb.command("shell", "simpleperf", "report", "-i", remote_perf, serial=serial),
        60,
        command_runner,
    )
    write_text(report_path, report.stdout or report.stderr)
    unavailable_reason = _simpleperf_unavailable_reason(
        "\n".join((record.stdout or "", record.stderr or "", report.stdout or "", report.stderr or ""))
    )
    if unavailable_reason:
        metadata["diagnostics"].append(f"simpleperf.unavailable:{unavailable_reason}")
    metadata["artifacts"].extend(
        str(path)
        for path in (profileable_path, report_path, record_stdout, record_stderr)
        if path.exists()
    )
    if perf_data_path.exists() and perf_data_path.stat().st_size > 0:
        metadata["artifacts"].append(str(perf_data_path))


def _capture_perfetto(
    *,
    artifact_dir: Path,
    package_name: str,
    serial: str,
    duration_s: int,
    interactive: bool,
    adb: AdbClient,
    command_runner: CommandRunner,
    metadata: dict[str, Any],
) -> None:
    trace_name = f"perfeval-diagnose-{int(time.time())}.pftrace"
    remote_trace = f"/data/misc/perfetto-traces/{trace_name}"
    local_trace = artifact_dir / trace_name
    start_stdout = artifact_dir / "perfetto-start-stdout.txt"
    start_stderr = artifact_dir / "perfetto-start-stderr.txt"
    _unlink_existing(local_trace)
    _run(adb.command("shell", "rm", "-f", remote_trace, serial=serial), 15, command_runner)

    start = _run(
        adb.command(
            "shell",
            "perfetto",
            "--background-wait",
            "-o",
            remote_trace,
            "-t",
            f"{max(1, duration_s)}s",
            "--app",
            package_name,
            "sched",
            "freq",
            "idle",
            "am",
            "wm",
            "gfx",
            "view",
            "binder_driver",
            "hal",
            "dalvik",
            serial=serial,
        ),
        20,
        command_runner,
    )
    write_text(start_stdout, start.stdout)
    write_text(start_stderr, start.stderr)
    metadata["diagnostics"].append(f"perfetto.start.returncode:{start.returncode}")
    perfetto_pid = _last_nonempty_line(start.stdout)

    if start.returncode != 0:
        metadata["diagnostics"].append("perfetto.unavailable:start_failed")
        metadata["artifacts"].extend(str(path) for path in (start_stdout, start_stderr) if path.exists())
        return

    if interactive:
        completed_by_enter = _input_with_timeout(
            f"Perfetto is recording. 请执行一次聚焦流程，然后按 Enter 停止并拉取 trace（最长 {duration_s}s）。",
            duration_s,
            timeout_message=f"已达到最大采集时长 {duration_s}s，自动结束并拉取 trace。",
        )
        if not completed_by_enter:
            metadata["diagnostics"].append(f"manual_flow.timeout:{duration_s}s")
    else:
        time.sleep(max(1, duration_s))
    if perfetto_pid and perfetto_pid.isdigit():
        _run(adb.command("shell", "kill", "-TERM", perfetto_pid, serial=serial), 15, command_runner)
    _wait_for_remote_file(adb, serial, remote_trace, command_runner)
    pull = _run(adb.command("pull", remote_trace, str(local_trace), serial=serial), 60, command_runner)
    metadata["diagnostics"].append(f"perfetto.pull.returncode:{pull.returncode}")
    if pull.returncode == 0 and (not local_trace.exists() or local_trace.stat().st_size <= 0):
        metadata["diagnostics"].append("perfetto.pull.missing:local_trace")
    metadata["artifacts"].extend(
        str(path)
        for path in (local_trace, start_stdout, start_stderr)
        if path.exists() and path.is_file()
    )


def _start_simpleperf_background(
    *,
    adb: AdbClient,
    serial: str,
    package_name: str,
    remote_perf: str,
    remote_stdout: str,
    remote_stderr: str,
    duration_s: int,
    command_runner: CommandRunner,
) -> str:
    command = " ".join(
        [
            "simpleperf",
            "record",
            "--app",
            shlex.quote(package_name),
            "-o",
            shlex.quote(remote_perf),
            "-e",
            "cpu-clock",
            "-f",
            "4000",
            "-g",
            "--duration",
            str(max(1, duration_s)),
            ">",
            shlex.quote(remote_stdout),
            "2>",
            shlex.quote(remote_stderr),
            "&",
            "echo",
            "$!",
        ]
    )
    # adb shell re-joins arguments on the device side. Passing the script as a
    # bare argument to `sh -c` makes Android's shell execute only `simpleperf`,
    # which prints usage and never records perf.data. Quote the whole script in
    # the remote shell command so `sh -c` receives it as one command string.
    result = _run(adb.command("shell", f"sh -c {shlex.quote(command)}", serial=serial), 15, command_runner)
    return _last_nonempty_line(result.stdout)


def _start_perfetto(
    *,
    adb: AdbClient,
    serial: str,
    package_name: str,
    remote_trace: str,
    duration_s: int,
    command_runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    return _run(
        adb.command(
            "shell",
            "perfetto",
            "--background-wait",
            "-o",
            remote_trace,
            "-t",
            f"{max(1, duration_s)}s",
            "--app",
            package_name,
            "sched",
            "freq",
            "idle",
            "am",
            "wm",
            "gfx",
            "view",
            "binder_driver",
            "hal",
            "dalvik",
            serial=serial,
        ),
        20,
        command_runner,
    )


def _collect_frame_artifacts_after_flow(
    *,
    run_dir: Path,
    artifact_dir: Path,
    package_name: str,
    serial: str,
    adb: AdbClient,
    command_runner: CommandRunner,
    metadata: dict[str, Any],
) -> None:
    captures = (
        ("window.txt", adb.command("shell", "dumpsys", "window", serial=serial), 30),
        ("gfxinfo.txt", adb.command("shell", "dumpsys", "gfxinfo", package_name, serial=serial), 30),
        ("gfxinfo-framestats.txt", adb.command("shell", "dumpsys", "gfxinfo", package_name, "framestats", serial=serial), 30),
        ("meminfo.txt", adb.command("shell", "dumpsys", "meminfo", package_name, serial=serial), 30),
        ("logcat.txt", adb.command("logcat", "-d", "-v", "epoch", serial=serial), 30),
    )
    frame_diagnostics: list[str] = []
    for filename, command, timeout in captures:
        result = _run(command, timeout, command_runner)
        write_text(artifact_dir / filename, result.stdout)
        if result.returncode != 0:
            frame_diagnostics.append(f"android.command_failed:{filename}:{result.returncode}:{result.stderr}")

    events = parse_android_artifacts(artifact_dir, run_id=run_dir.name, package_name=package_name)
    artifacts = index_android_artifacts(artifact_dir)
    issue_messages = [*frame_diagnostics, *_diagnosis_issue_messages(metadata.get("diagnostics", []))]
    diagnostics = [_diagnostic_event(run_dir.name, idx, item) for idx, item in enumerate(issue_messages, start=1)]
    summary = summarize_timeline(
        run_dir.name,
        events,
        status="partial" if diagnostics else "completed",
        evidence_level=["L1"],
        artifacts=artifacts,
        diagnostics=diagnostics,
        context={
            "scenarioId": run_dir.name,
            "scenario": {"scenarioId": run_dir.name, "description": "Android focused full diagnosis"},
            "memoryProfile": metadata.get("memoryProfile", "mainstream"),
            "app": {"platform": "android", "packageName": package_name},
            "device": {"platform": "android", "serialOrUdid": serial},
        },
    )
    write_json_document(run_dir / "summary.json", summary)
    write_ndjson_document(run_dir / "timeline.ndjson", events)
    write_ndjson_document(run_dir / "diagnostics.ndjson", diagnostics)
    report_outputs = write_summary_report_bundle(summary, run_dir / "report")
    metadata["summaryPath"] = str(run_dir / "summary.json")
    metadata["reportHtml"] = str(report_outputs["report_html"])
    metadata["diagnostics"].extend(frame_diagnostics)
    metadata["artifacts"].extend(artifact["path"] for artifact in artifacts)


def _write_evidence_only_summary(
    *,
    run_dir: Path,
    package_name: str,
    serial: str,
    metadata: dict[str, Any],
) -> None:
    issue_messages = _diagnosis_issue_messages(metadata.get("diagnostics", []))
    diagnostics = [
        _diagnostic_event(run_dir.name, index, message)
        for index, message in enumerate(issue_messages, start=1)
    ]
    artifacts = [
        {"id": Path(path).stem, "path": path}
        for path in metadata.get("artifacts", [])
        if isinstance(path, str) and Path(path).exists() and Path(path).is_file()
    ]
    evidence_levels = []
    modes = metadata.get("modes") if isinstance(metadata.get("modes"), list) else []
    if "cpu" in modes:
        evidence_levels.append("L4")
    if "trace" in modes:
        evidence_levels.append("L2")
    summary = summarize_timeline(
        run_dir.name,
        [],
        status="partial" if issue_messages else "completed",
        evidence_level=evidence_levels or ["L1"],
        artifacts=artifacts,
        diagnostics=diagnostics,
        context={
            "scenarioId": run_dir.name,
            "scenario": {"scenarioId": run_dir.name, "description": "Android focused diagnosis"},
            "memoryProfile": metadata.get("memoryProfile", "mainstream"),
            "app": {"platform": "android", "packageName": package_name},
            "device": {"platform": "android", "serialOrUdid": serial},
        },
    )
    write_json_document(run_dir / "summary.json", summary)
    write_ndjson_document(run_dir / "timeline.ndjson", [])
    write_ndjson_document(run_dir / "diagnostics.ndjson", diagnostics)
    metadata["summaryPath"] = str(run_dir / "summary.json")


def _diagnosis_issue_messages(messages: object) -> list[str]:
    if not isinstance(messages, list):
        return []
    issues: list[str] = []
    for raw in messages:
        message = str(raw)
        if ".returncode:" in message and not message.endswith(":0"):
            issues.append(message)
        elif any(marker in message for marker in (".unavailable:", ".missing:", "manual_flow.timeout:")):
            issues.append(message)
        elif message.endswith("pid:unknown"):
            issues.append(message)
    return list(dict.fromkeys(issues))


def _pull_optional(
    adb: AdbClient,
    serial: str,
    remote_path: str,
    local_path: Path,
    command_runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    return _run(adb.command("pull", remote_path, str(local_path), serial=serial), 30, command_runner)


def _run(command: list[str], timeout: float | None, command_runner: CommandRunner) -> subprocess.CompletedProcess[str]:
    try:
        return command_runner(command, timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            _timeout_text(exc.stdout),
            _timeout_text(exc.stderr) or f"timed out after {exc.timeout} seconds",
        )


def _input_with_timeout(prompt: str, timeout_s: int, *, timeout_message: str) -> bool:
    wait_s = max(1, int(timeout_s))
    if not _stdin_timeout_supported():
        try:
            input(prompt)
            return True
        except EOFError:
            print(timeout_message)
            return False

    print(prompt, end="", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], wait_s)
    except (OSError, ValueError):
        try:
            input()
            return True
        except EOFError:
            print()
            print(timeout_message)
            return False
    if ready:
        sys.stdin.readline()
        return True
    print()
    print(timeout_message)
    return False


def _stdin_timeout_supported() -> bool:
    try:
        sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return bool(sys.stdin.isatty())


def _wait_for_remote_file(adb: AdbClient, serial: str, remote_path: str, command_runner: CommandRunner) -> None:
    last_size = ""
    stable_count = 0
    for _ in range(30):
        result = _run(adb.command("shell", "ls", "-l", remote_path, serial=serial), 5, command_runner)
        parts = result.stdout.split()
        size = parts[4] if len(parts) >= 5 else ""
        if size and size == last_size:
            stable_count += 1
            if stable_count >= 2:
                return
        else:
            stable_count = 0
        last_size = size
        time.sleep(1)


def _last_nonempty_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _unlink_existing(*paths: Path) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except OSError:
        return ""


def _simpleperf_unavailable_reason(text: str) -> str:
    lowered = text.lower()
    if "doesn't exist or isn't debuggable/profileable" in lowered:
        return "package_not_debuggable_or_profileable"
    if "not debuggable/profileable" in lowered or "isn't debuggable/profileable" in lowered:
        return "package_not_debuggable_or_profileable"
    if "failed to open record file" in lowered and "no such file or directory" in lowered:
        return "perf_data_missing"
    return ""


def _diagnostic_event(run_id: str, seq: int, message: str) -> dict[str, Any]:
    code = message.split(":", 1)[0]
    severity = "error" if "failed" in code or "missing" in code else "warning"
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-diagnose-{seq:03d}",
        "type": "diagnostic",
        "name": code,
        "tsMonotonicNs": time.monotonic_ns(),
        "source": {"platform": "host", "collector": "diagnose.android", "evidenceLevel": "L0"},
        "data": {"severity": severity, "code": code, "message": message},
    }
