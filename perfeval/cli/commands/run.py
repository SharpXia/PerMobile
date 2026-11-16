from __future__ import annotations

import json
import platform
import shutil
import sys
import time
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from perfeval import __version__
from perfeval.cli.protocol import add_gui_events_argument, emit_phase, emit_result, gui_events_enabled
from perfeval.collectors.android import collect_android_run, index_android_artifacts, parse_android_artifacts, run_preflight
from perfeval.collectors.android.adb import AdbClient
from perfeval.collectors.android.flow import build_interaction_commands, find_launch_activity, parse_flow_steps
from perfeval.collectors.android.plan import (
    SCREENRECORD_MIN_VALID_BYTES,
    resolve_sampling_config,
    resolve_screen_record_config,
)
from perfeval.core.artifacts import sha256_of_path
from perfeval.core.config import (
    ensure_directory,
    load_scenario_config,
    read_scenario_defaults,
    sanitize_slug,
    utc_now_iso,
    validate_scenario_config,
    write_json_document,
    write_ndjson_document,
    write_text,
)
from perfeval.core.memory_profile import normalize_memory_profile
from perfeval.core.report import write_multi_run_report_bundle, write_summary_report_bundle
from perfeval.core.summary import summarize_timeline


def register_parser(subparsers: Any) -> ArgumentParser:
    parser = subparsers.add_parser(
        "run",
        help="Create run directories for a scenario.",
        description="Create run artifacts from a scenario, Android artifacts, or an Android device collection.",
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default="scenarios/startup.synthetic.yaml",
        help="Path to a scenario config file.",
    )
    parser.add_argument(
        "--out",
        default="runs",
        help="Output directory for run artifacts.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="Override scenario run.repeats.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report what would be created without writing files.",
    )
    android_mode = parser.add_mutually_exclusive_group()
    android_mode.add_argument(
        "--android-artifacts",
        help="Parse an existing Android artifact directory containing gfxinfo.txt, meminfo.txt, and/or logcat.txt.",
    )
    android_mode.add_argument(
        "--android-device-run",
        action="store_true",
        help="Execute Android adb install/launch/collect flow for the selected app.",
    )
    parser.add_argument(
        "--package-name",
        help="Android package name for artifact parsing or device collection.",
    )
    parser.add_argument("--serial", help="Android device serial for --android-device-run.")
    parser.add_argument("--apk", help="APK path override for --android-device-run.")
    parser.add_argument("--activity", help="Android launch activity, for example .MainActivity.")
    parser.add_argument("--no-clear-data", action="store_true", help="Do not run pm clear before launch.")
    parser.add_argument("--no-reinstall", action="store_true", help="Do not install the APK before launch.")
    parser.add_argument("--perfetto", action="store_true", help="Collect a Perfetto artifact during Android device run.")
    parser.add_argument(
        "--post-launch-wait",
        type=float,
        default=2.0,
        help="Seconds to wait after launch before collecting dumpsys/logcat artifacts.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pause at manual steps for user navigation before collection.",
    )
    parser.add_argument(
        "--sampling-off",
        action="store_true",
        help="Disable periodic samplers even when collectors.android.samplers is configured.",
    )
    parser.add_argument(
        "--sampling-interval-ms",
        type=int,
        default=None,
        help="Override intervalMs for every configured Android sampler.",
    )
    screen_record_mode = parser.add_mutually_exclusive_group()
    screen_record_mode.add_argument(
        "--screen-record",
        action="store_true",
        help="Enable Android screen recording even when collectors.android.screenRecord is not configured.",
    )
    screen_record_mode.add_argument(
        "--screen-record-off",
        action="store_true",
        help="Disable Android screen recording even when collectors.android.screenRecord is configured.",
    )
    add_gui_events_argument(parser)
    parser.set_defaults(func=handle)
    return parser


def _placeholder_manifest(
    config: dict[str, Any],
    scenario_path: Path,
    run_id: str,
    repeat_index: int,
    *,
    app_context: dict[str, Any] | None = None,
    device_context: dict[str, Any] | None = None,
    status: str = "partial",
    started_at_utc: str | None = None,
    ended_at_utc: str | None = None,
) -> dict[str, Any]:
    mode, scenario_repeats = read_scenario_defaults(config)
    app_context = dict(app_context or {})
    device_context = dict(device_context or {})
    scenario_id = str(config.get("scenarioId") or "scenario")
    description = str(config.get("description") or scenario_id)
    app_platform = str(app_context.get("platform") or "unknown")
    package_name = app_context.get("packageName")
    bundle_id = app_context.get("bundleId")
    app_id = app_context.get("appId") or package_name or bundle_id or "unknown-app"
    device_platform = str(device_context.get("platform") or app_platform)
    serial_or_udid = device_context.get("serialOrUdid") or device_context.get("serial") or "unknown-device"
    device_id = device_context.get("deviceId") or serial_or_udid
    started_at_utc = started_at_utc or utc_now_iso()
    ended_at_utc = ended_at_utc or utc_now_iso()
    scenario = {
        "scenarioId": scenario_id,
        "description": description,
        "flowHash": None,
        "sourcePath": str(scenario_path),
    }
    return {
        "schemaVersion": "perfeval.manifest.v1",
        "runId": run_id,
        "createdAtUtc": utc_now_iso(),
        "mode": mode,
        "scenario": scenario,
        "app": {
            "platform": app_platform,
            "appId": str(app_id),
            "packageName": package_name,
            "bundleId": bundle_id,
            "versionName": app_context.get("versionName"),
            "versionCode": app_context.get("versionCode"),
            "buildId": app_context.get("buildId"),
            "artifactPath": app_context.get("artifactPath"),
            "artifactSha256": app_context.get("artifactSha256"),
        },
        "device": {
            "platform": device_platform,
            "deviceId": str(device_id),
            "serialOrUdid": str(serial_or_udid),
            "model": device_context.get("model"),
            "osVersion": device_context.get("osVersion"),
            "apiLevel": device_context.get("apiLevel"),
            "refreshRateHz": device_context.get("refreshRateHz"),
            "cpuAbi": device_context.get("cpuAbi"),
            "memoryTotalMb": device_context.get("memoryTotalMb"),
        },
        "environment": {
            "hostOs": platform.system(),
            "hostArch": platform.machine(),
            "toolVersion": __version__,
            "adbVersion": None,
            "xcodeVersion": None,
            "networkProfile": None,
            "batteryLevel": None,
            "charging": None,
            "thermalState": None,
        },
        "run": {
            "repeatIndex": repeat_index,
            "warmupIndex": 0,
            "scenarioRepeatCount": scenario_repeats,
            "startedAtUtc": started_at_utc,
            "endedAtUtc": ended_at_utc,
            "status": status,
            "evidenceLevel": ["L0"],
        },
    }


def _placeholder_summary(config: dict[str, Any], run_id: str, repeat_index: int) -> dict[str, Any]:
    return {
        "schemaVersion": "perfeval.summary.v1",
        "runId": run_id,
        "scenarioId": config.get("scenarioId"),
        "createdAtUtc": utc_now_iso(),
        "repeatIndex": repeat_index,
        "status": "partial",
        "metrics": {},
        "notes": [
            "Run scaffold generated successfully.",
            "No external collector was executed for this scaffold run.",
        ],
    }


def _placeholder_events(run_id: str) -> list[dict[str, Any]]:
    now = time.monotonic_ns()
    return [
        {
            "schemaVersion": "perfeval.timeline.v1",
            "seq": 1,
            "runId": run_id,
            "eventId": f"{run_id}-evt-1",
            "type": "diagnostic",
            "name": "cli.run.placeholder",
            "tsMonotonicNs": now,
            "source": {
                "platform": "host",
                "collector": "cli",
                "evidenceLevel": "L0",
            },
            "data": {
                "message": "Run orchestration scaffold created without external collectors.",
            },
        }
    ]


def _diagnostic_entry(
    run_id: str,
    seq: int,
    *,
    name: str,
    severity: str,
    code: str,
    message: str,
    collector: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": run_id,
        "eventId": f"{run_id}-diag-{seq:03d}",
        "type": "diagnostic",
        "name": name,
        "tsMonotonicNs": time.monotonic_ns(),
        "source": {
            "platform": "host",
            "collector": collector,
            "evidenceLevel": "L0",
        },
        "data": {
            "severity": severity,
            "code": code,
            "message": message,
        },
    }


def _placeholder_diagnostics(run_id: str) -> list[dict[str, Any]]:
    return [
        _diagnostic_entry(
            run_id,
            1,
            name="cli.run.placeholder",
            severity="info",
            code="cli.run.placeholder",
            message="Run scaffold generated without external collector execution.",
            collector="cli",
        )
    ]


def _android_artifact_diagnostics(run_id: str, *, artifact_dir: Path, event_count: int) -> list[dict[str, Any]]:
    return [
        _diagnostic_entry(
            run_id,
            1,
            name="android.artifacts.parsed",
            severity="info",
            code="android.artifacts.parsed",
            message=f"Parsed {event_count} timeline event(s) from {artifact_dir}",
            collector="cli.android",
        )
    ]


def _android_device_diagnostics(run_id: str, diagnostics: list[str]) -> list[dict[str, Any]]:
    entries = [
        _diagnostic_entry(
            run_id,
            1,
            name="android.device.run.completed",
            severity="info",
            code="android.device.run.completed",
            message="Executed Android device collector flow.",
            collector="cli.android",
        )
    ]
    for seq, item in enumerate(diagnostics, start=2):
        code = item.split(":", 1)[0]
        severity = "error" if "failed" in code or "missing" in code else "warning"
        entries.append(
            _diagnostic_entry(
                run_id,
                seq,
                name=code,
                severity=severity,
                code=code,
                message=item,
                collector="cli.android",
            )
        )
    return entries


def _summary_from_events(
    config: dict[str, Any],
    run_id: str,
    events: list[dict[str, Any]],
    *,
    package_name: str | None = None,
    app_config: dict[str, Any] | None = None,
    artifact_path: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    status: str = "completed",
    clock_anchors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    memory_profile, profile_diagnostic = normalize_memory_profile(config.get("memoryProfile"))
    summary_diagnostics = list(diagnostics or [])
    if profile_diagnostic:
        summary_diagnostics.append(
            _diagnostic_entry(
                run_id,
                len(summary_diagnostics) + 1,
                name="memoryProfile.unknown",
                severity="warning",
                code="memoryProfile.unknown",
                message=profile_diagnostic,
                collector="cli.config",
            )
        )
    budgets = config.get("budgets") if isinstance(config.get("budgets"), dict) else {}
    context: dict[str, Any] = {
        "scenarioId": config.get("scenarioId"),
        "scenario": {
            "scenarioId": config.get("scenarioId"),
            "description": config.get("description", ""),
        },
        "memoryProfile": memory_profile,
        "budgets": dict(budgets),
        "app": _android_app_context(
            app_config or {},
            package_name=package_name,
            artifact_path=artifact_path,
        ),
        "device": {
            "platform": "android",
        },
    }
    # CM-TASK-021：三方时钟锚点随 summary 落盘（报告回放跳转数据源）
    if clock_anchors:
        context["clockAnchors"] = clock_anchors
    return summarize_timeline(
        run_id,
        events,
        status=status,
        evidence_level=["L1"],
        artifacts=artifacts,
        diagnostics=summary_diagnostics,
        context=context,
    )


def _android_app_context(
    app_config: dict[str, Any],
    *,
    package_name: str | None = None,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    resolved_artifact = artifact_path or app_config.get("artifact") or app_config.get("artifactPath")
    app: dict[str, Any] = {
        "platform": "android",
        "appId": app_config.get("id") or app_config.get("appId"),
        "packageName": package_name or app_config.get("packageName"),
        "versionName": app_config.get("versionName"),
        "versionCode": app_config.get("versionCode"),
        "buildId": app_config.get("buildId") or app_config.get("implementationId"),
        "artifactPath": str(resolved_artifact) if resolved_artifact else None,
        "artifactSha256": _sha256_if_file(resolved_artifact),
    }
    return {key: value for key, value in app.items() if value is not None}


def _sha256_if_file(path: object | None) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    resolved = Path(path)
    if not resolved.exists() or not resolved.is_file():
        return None
    return sha256_of_path(resolved)


def _set_device_serial(summary: dict[str, Any], serial: str) -> None:
    device = summary.setdefault("device", {})
    if isinstance(device, dict):
        device.setdefault("platform", "android")
        device["serialOrUdid"] = serial


def _has_error_diagnostic(diagnostics: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(diagnostic.get("data"), dict)
        and diagnostic["data"].get("severity") == "error"
        for diagnostic in diagnostics
    )


def _load_clock_anchors_from_manifest(artifact_dir: Path) -> dict[str, Any] | None:
    """离线 --android-artifacts 模式读回 run manifest 的 context.clockAnchors。

    F-fix（R1-N4）：SCREEN_RECORD_SYNC §2"离线可复算"——源 run 目录（artifact
    目录的上一级/上两级）的 manifest.json 携带 clockAnchors 时注入 summary，
    离线报告可精确跳转；找不到或格式不符返回 None（不阻断）。
    """
    candidates = (artifact_dir.parent / "manifest.json", artifact_dir.parent.parent / "manifest.json")
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        context = manifest.get("context")
        anchors = context.get("clockAnchors") if isinstance(context, dict) else None
        if isinstance(anchors, dict):
            return anchors
    return None


def _first_android_app(config: dict[str, Any]) -> dict[str, Any]:
    apps = config.get("apps")
    if isinstance(apps, list):
        for app in apps:
            if isinstance(app, dict) and app.get("platform") == "android":
                return app
    return {}


def _launch_activity(config: dict[str, Any], args: Namespace) -> str:
    if args.activity:
        return args.activity
    flow = config.get("flow")
    if isinstance(flow, dict):
        steps = flow.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict) and step.get("type") == "launch" and isinstance(step.get("activity"), str):
                    return step["activity"]
    return ".MainActivity"


def _copy_android_artifacts(source: Path, target: Path) -> tuple[Path, list[str]]:
    """拷贝离线 artifact 目录；返回 (目标目录, 被跳过的空录屏文件名列表)。

    smoke-fix：0 字节/过小（<1KB）的 screen*.mp4 判定无有效帧数据，不拷贝
    （在线路径同样处理，保持一致），由调用方记 diagnostic。
    """
    target.mkdir(parents=True, exist_ok=True)
    for name in (
        "gfxinfo.txt",
        "gfxinfo-framestats.txt",
        "window.txt",
        "meminfo.txt",
        "logcat.txt",
        "perfetto.pftrace",
        "samples.ndjson",
    ):
        source_path = source / name
        if source_path.exists() and source_path.is_file():
            shutil.copy2(source_path, target / name)
    # 录屏文件（含分段）随离线 artifact 目录整体拷贝（报告以相对路径引用）
    empty_screen_files: list[str] = []
    for source_path in sorted(source.glob("screen*.mp4")):
        if not source_path.is_file():
            continue
        if source_path.stat().st_size < SCREENRECORD_MIN_VALID_BYTES:
            empty_screen_files.append(source_path.name)
            continue
        shutil.copy2(source_path, target / source_path.name)
    return target, empty_screen_files


def handle(args: Namespace) -> int:
    scenario_path = Path(args.scenario)
    config = load_scenario_config(scenario_path)
    config_issues = validate_scenario_config(config)
    if config_issues:
        raise ValueError("; ".join(config_issues))
    repeats = args.repeat if args.repeat is not None else read_scenario_defaults(config)[1]
    repeats = max(1, repeats)

    output_root = Path(args.out)
    base_run_id = sanitize_slug(str(config.get("scenarioId", "scenario")))
    run_dir = output_root / base_run_id

    if not args.dry_run:
        ensure_directory(output_root)
        ensure_directory(run_dir)
        ensure_directory(run_dir / "artifacts")

    all_events: list[dict[str, Any]] = []
    all_diagnostics: list[dict[str, Any]] = []
    all_artifacts: list[dict[str, Any]] = []

    if args.dry_run:
        if args.android_device_run:
            all_events = _placeholder_events(base_run_id)
            all_diagnostics = [
                _diagnostic_entry(
                    base_run_id,
                    1,
                    name="android.device.run.preview",
                    severity="info",
                    code="android.device.run.preview",
                    message=f"Would execute Android device collector flow x{repeats}.",
                    collector="cli.android",
                )
            ]
            summary = _placeholder_summary(config, base_run_id, repeats)
            summary["diagnostics"] = all_diagnostics
        elif args.android_artifacts:
            android_artifact_dir = Path(args.android_artifacts)
            all_events = parse_android_artifacts(
                android_artifact_dir,
                run_id=base_run_id,
                package_name=args.package_name,
            )
            all_diagnostics = _android_artifact_diagnostics(
                base_run_id,
                artifact_dir=android_artifact_dir,
                event_count=len(all_events),
            )
            summary = _summary_from_events(
                config,
                base_run_id,
                all_events,
                package_name=args.package_name,
                app_config=_first_android_app(config),
                artifact_path=args.apk,
                artifacts=index_android_artifacts(android_artifact_dir),
                diagnostics=all_diagnostics,
            )
        else:
            summary = _placeholder_summary(config, base_run_id, repeats)
            all_events = _placeholder_events(base_run_id)
            all_diagnostics = _placeholder_diagnostics(base_run_id)
            summary["diagnostics"] = all_diagnostics
    else:
        if args.android_device_run:
            app = _first_android_app(config)
            package_name = args.package_name or app.get("packageName")
            apk_path = args.apk or app.get("artifact")
            if not isinstance(package_name, str) or not package_name:
                raise ValueError("--android-device-run requires --package-name or apps[].packageName")
            flow_config = config.get("flow")
            flow_steps = parse_flow_steps(flow_config) if flow_config else []
            activity = args.activity or find_launch_activity(flow_steps) or None
            auto_resolve_activity = activity is None and any(
                step.get("type") == "launch" for step in flow_steps
            )
            adb_client = AdbClient()
            emit_phase("device_check", "started", "检查设备")
            preflight = run_preflight(adb_client, serial=args.serial)
            if not preflight.ok:
                emit_phase("device_check", "failed", "检查设备", detail=", ".join(preflight.diagnostics))
                reasons = ", ".join(preflight.diagnostics) if preflight.diagnostics else "unknown"
                raise ValueError(f"Android preflight failed: {reasons}")
            selected_serial = args.serial or preflight.selected_serial
            if not selected_serial:
                raise ValueError("Android preflight did not select a device")
            emit_phase("device_check", "completed", "检查设备")
            interaction_cmds = build_interaction_commands(
                flow_steps,
                adb=adb_client,
                package_name=package_name,
                serial=selected_serial,
            )
            samplers_config = resolve_sampling_config(
                config,
                sampling_off=args.sampling_off,
                sampling_interval_ms=args.sampling_interval_ms,
            )
            screen_record_config = resolve_screen_record_config(
                config,
                screen_record=args.screen_record,
                screen_record_off=args.screen_record_off,
            )
            run_level_clock_anchors: dict[str, Any] | None = None
            for repeat_index in range(1, repeats + 1):
                repeat_run_id = f"{base_run_id}-r{repeat_index:03d}"
                repeat_dir = run_dir / "repeats" / f"r{repeat_index:03d}"
                ensure_directory(repeat_dir)
                repeat_started_at = utc_now_iso()
                result = collect_android_run(
                    run_id=repeat_run_id,
                    run_dir=repeat_dir,
                    package_name=package_name,
                    activity=activity,
                    apk_path=str(apk_path) if apk_path else None,
                    serial=selected_serial,
                    clear_data=not args.no_clear_data,
                    reinstall=not args.no_reinstall,
                    auto_resolve_activity=auto_resolve_activity,
                    perfetto_enabled=args.perfetto,
                    post_launch_wait_s=args.post_launch_wait,
                    interaction_commands=interaction_cmds,
                    interactive=args.interactive,
                    samplers_config=samplers_config,
                    screen_record_config=screen_record_config,
                    phase_callback=lambda phase_id, state, label, detail, current=repeat_index: emit_phase(
                        phase_id,
                        state,
                        label,
                        repeat_index=current,
                        repeat_count=repeats,
                        detail=detail,
                    ),
                )
                repeat_diagnostics = _android_device_diagnostics(
                    repeat_run_id,
                    list(result.diagnostics),
                )
                repeat_status = "partial" if _has_error_diagnostic(repeat_diagnostics) else "completed"
                repeat_summary = _summary_from_events(
                    config,
                    repeat_run_id,
                    result.events,
                    package_name=package_name,
                    app_config=app,
                    artifact_path=str(apk_path) if apk_path else None,
                    artifacts=result.artifacts,
                    diagnostics=repeat_diagnostics,
                    status=repeat_status,
                    clock_anchors=result.clock_anchors,
                )
                _set_device_serial(repeat_summary, selected_serial)
                repeat_summary["repeatIndex"] = repeat_index
                write_json_document(repeat_dir / "summary.json", repeat_summary)
                write_ndjson_document(repeat_dir / "timeline.ndjson", result.events)
                write_ndjson_document(repeat_dir / "diagnostics.ndjson", repeat_diagnostics)
                repeat_app_context = _android_app_context(
                    app,
                    package_name=package_name,
                    artifact_path=str(apk_path) if apk_path else None,
                )
                repeat_manifest = _placeholder_manifest(
                    config,
                    scenario_path,
                    repeat_run_id,
                    repeat_index,
                    app_context=repeat_app_context,
                    device_context={"platform": "android", "serialOrUdid": selected_serial},
                    status=repeat_status,
                    started_at_utc=repeat_started_at,
                    ended_at_utc=utc_now_iso(),
                )
                # CM-TASK-021：clockAnchors 写入 manifest context（离线可复算时间轴）
                if result.clock_anchors:
                    repeat_manifest["context"] = {"clockAnchors": result.clock_anchors}
                write_json_document(repeat_dir / "manifest.json", repeat_manifest)
                all_events.extend(result.events)
                all_diagnostics.extend(repeat_diagnostics)
                all_artifacts.extend(
                    dict(artifact, repeat=repeat_index) for artifact in result.artifacts
                )
                # 单轮 run 的顶层 summary/manifest 直接复用该轮锚点
                if repeats == 1:
                    run_level_clock_anchors = result.clock_anchors
            summary = _summary_from_events(
                config,
                base_run_id,
                all_events,
                package_name=package_name,
                app_config=app,
                artifact_path=str(apk_path) if apk_path else None,
                artifacts=all_artifacts if all_artifacts else None,
                diagnostics=all_diagnostics,
                status="partial" if any(
                    d.get("data", {}).get("severity") == "error" for d in all_diagnostics
                ) else "completed",
                clock_anchors=run_level_clock_anchors if repeats == 1 else None,
            )
            _set_device_serial(summary, selected_serial)
            summary["repeats"] = repeats
        elif args.android_artifacts:
            emit_phase("artifact_analysis", "started", "分析离线数据")
            android_artifact_dir, empty_screen_files = _copy_android_artifacts(
                Path(args.android_artifacts), run_dir / "artifacts" / "android"
            )
            all_events = parse_android_artifacts(
                android_artifact_dir,
                run_id=base_run_id,
                package_name=args.package_name,
            )
            all_diagnostics = _android_artifact_diagnostics(
                base_run_id, artifact_dir=android_artifact_dir, event_count=len(all_events)
            )
            # smoke-fix：空录屏与在线路径同样降级（*.unavailable diagnostic）
            for seq, screen_name in enumerate(empty_screen_files, start=len(all_diagnostics) + 1):
                all_diagnostics.append(
                    _diagnostic_entry(
                        base_run_id,
                        seq,
                        name="screen.record.unavailable",
                        severity="warning",
                        code="screen.record.unavailable",
                        message=(
                            "screen.record.unavailable:empty recording "
                            "(ROM 限制等未知原因，建议前台运行 screenrecord 核实)"
                            f":{screen_name}"
                        ),
                        collector="cli.android",
                    )
                )
            summary = _summary_from_events(
                config,
                base_run_id,
                all_events,
                package_name=args.package_name,
                app_config=_first_android_app(config),
                artifacts=index_android_artifacts(android_artifact_dir),
                diagnostics=all_diagnostics,
                clock_anchors=_load_clock_anchors_from_manifest(Path(args.android_artifacts)),
            )
            emit_phase("artifact_analysis", "completed", "分析离线数据")
        else:
            summary = _placeholder_summary(config, base_run_id, repeats)
            all_events = _placeholder_events(base_run_id)
            all_diagnostics = _placeholder_diagnostics(base_run_id)
            summary["diagnostics"] = all_diagnostics

        app_context = summary.get("app") if isinstance(summary.get("app"), dict) else {}
        device_context = summary.get("device") if isinstance(summary.get("device"), dict) else {}
        manifest = _placeholder_manifest(
            config,
            scenario_path,
            base_run_id,
            0 if repeats > 1 and args.android_device_run else 1,
            app_context=app_context,
            device_context=device_context,
            status=str(summary.get("status") or "partial"),
        )
        # CM-TASK-021：单轮真机 run 的 clockAnchors 同步写入 run 级 manifest context
        summary_anchors = summary.get("clockAnchors")
        if isinstance(summary_anchors, dict):
            manifest["context"] = {"clockAnchors": summary_anchors}
        write_json_document(run_dir / "manifest.json", manifest)
        write_json_document(run_dir / "summary.json", summary)
        write_ndjson_document(run_dir / "timeline.ndjson", all_events)
        write_ndjson_document(run_dir / "diagnostics.ndjson", all_diagnostics)
        # 多轮采集时使用多轮对比报告，否则使用单次报告
        emit_phase("report", "started", "生成报告")
        report_dir = run_dir / "report"
        if repeats > 1 and args.android_device_run:
            report_outputs = write_multi_run_report_bundle(summary, report_dir)
        else:
            report_outputs = write_summary_report_bundle(summary, report_dir)
        emit_phase("report", "completed", "生成报告")
        write_text(
            run_dir / "tool.log",
            "\n".join(
                [
                    f"[{utc_now_iso()}] Perfeval Lite run",
                    f"scenario={scenario_path}",
                    f"repeats={repeats}",
                    f"status={summary.get('status', 'partial')}",
                    "note=executed Android device collector" if args.android_device_run else "note=parsed Android artifacts" if args.android_artifacts else "note=scaffold only; no external collector executed",
                    f"report={report_outputs['report_html']}",
                ]
            )
            + "\n",
        )

    created = [run_dir]
    if gui_events_enabled(args):
        report_html = run_dir / "report" / "report.html"
        summary_path = run_dir / "summary.json"
        emit_result(
            status=str(summary.get("status") or "partial"),
            exit_code=0,
            outputRoot=str(output_root),
            runDir=str(run_dir),
            report=str(report_html) if not args.dry_run and report_html.exists() else None,
            summary=str(summary_path) if not args.dry_run and summary_path.exists() else None,
            artifacts=[
                str(artifact.get("path"))
                for artifact in (summary.get("artifacts") or [])
                if isinstance(artifact, dict) and artifact.get("path")
            ],
            dryRun=bool(args.dry_run),
            runDirectories=[str(path) for path in created],
        )
    elif args.dry_run:
        print(f"Would create {len(created)} run directory under {output_root}")
    else:
        print(f"Created {len(created)} run directory under {output_root}")
        for run_dir in created:
            print(f"- {run_dir}")
            report_html = run_dir / "report" / "report.html"
            if report_html.exists():
                print(f"  report: {report_html}")

    if not config.get("collectors") and not args.android_artifacts:
        print(
            "Notice: collectors are not configured yet; these outputs are scaffolding only.",
            file=sys.stderr,
        )

    return 0
