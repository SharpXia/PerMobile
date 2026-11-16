from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from perfeval.cli.protocol import add_gui_events_argument, emit_phase, emit_result, gui_events_enabled
from perfeval.collectors.android import AdbClient, build_interaction_commands, collect_android_run, find_launch_activity, parse_flow_steps, run_preflight
from perfeval.core.compare import compare_summaries
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
from perfeval.core.report import write_multi_run_report_bundle, write_report_bundle, write_summary_report_bundle

from . import run as run_command

CommandRunner = Callable[[list[str], float | None], subprocess.CompletedProcess[str]]


def default_command_runner(command: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def register_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "ab",
        help="Run Android baseline/multi-candidate collection and compare saved metrics.",
        description=(
            "Sequentially install and collect one baseline plus one or more candidate "
            "Android APKs, then compare every candidate against the baseline."
        ),
    )
    parser.add_argument("scenario", help="Path to a scenario config file.")
    parser.add_argument("--out", help="Output directory for this A/B run. Defaults to runs/<scenarioId>-ab.")
    parser.add_argument("--repeat", type=int, default=None, help="Override scenario run.repeats.")
    parser.add_argument("--android-device-run", action="store_true", help="Execute Android adb collection for both app groups.")
    parser.add_argument("--serial", help="Android device serial.")
    parser.add_argument("--package-name", help="Override Android package name for both app groups.")
    parser.add_argument("--activity", help="Android launch activity, for example .MainActivity.")
    parser.add_argument("--baseline-apk", help="Override the baseline apps[].artifact APK path.")
    parser.add_argument(
        "--candidate-apk",
        action="append",
        dest="candidate_apks",
        help=(
            "Candidate APK path; repeat for multi-APK tests. Use ID=PATH to name a "
            "candidate, for example --candidate-apk v4_3_2=/path/app.apk."
        ),
    )
    parser.add_argument("--baseline-id", default="baseline", help="apps[].id to use as baseline.")
    parser.add_argument(
        "--candidate-id",
        action="append",
        dest="candidate_ids",
        help="Candidate apps[].id; repeat to select or name multiple candidates.",
    )
    parser.add_argument(
        "--clear-data",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override run.install.clearData for every repeat.",
    )
    parser.add_argument("--allow-uninstall", action="store_true", help="Uninstall the package and retry once when install -r -d fails.")
    parser.add_argument("--perfetto", action="store_true", help="Collect a Perfetto artifact during each repeat.")
    parser.add_argument("--post-launch-wait", type=float, default=2.0, help="Seconds to wait after launch before collection.")
    parser.add_argument("--interactive", action="store_true", help="Pause at manual steps for user navigation before collection.")
    add_gui_events_argument(parser)
    parser.set_defaults(func=handle)
    return parser


def handle(args: argparse.Namespace) -> int:
    result = collect_android_ab(
        scenario_path=Path(args.scenario),
        output_root=Path(args.out) if args.out else None,
        repeat_override=args.repeat,
        android_device_run=args.android_device_run,
        serial=args.serial,
        package_name_override=args.package_name,
        activity_override=args.activity,
        baseline_apk_override=args.baseline_apk,
        candidate_apk_overrides=args.candidate_apks,
        baseline_id=args.baseline_id,
        candidate_ids=args.candidate_ids,
        clear_data_override=args.clear_data,
        allow_uninstall=args.allow_uninstall,
        perfetto_enabled=args.perfetto,
        post_launch_wait_s=args.post_launch_wait,
        interactive=args.interactive,
    )
    output = {
        "status": result["status"],
        "root": str(result["outputRoot"]),
        "compare": str(result["comparePath"]),
        "report": str(result["reportHtml"]),
    }
    if len(result["comparisons"]) == 1 and result["compare"].get("errors"):
        output["errors"] = result["compare"]["errors"]
    focused_windows = result["compare"].get("focusedWindows") if len(result["comparisons"]) == 1 else None
    if isinstance(focused_windows, Mapping) and any(focused_windows.values()):
        output["focusedWindows"] = focused_windows
    if len(result["comparisons"]) > 1:
        output["comparisons"] = {
            candidate_id: {
                "status": compare.get("status"),
                "compare": str(result["comparisonPaths"][candidate_id]),
                "report": str(result["reportPaths"][candidate_id]),
            }
            for candidate_id, compare in result["comparisons"].items()
        }
    status = str(result["status"])
    exit_code = 1 if status == "fail" else 4 if status == "invalid" else 0
    if gui_events_enabled(args):
        emit_result(
            status=status,
            exit_code=exit_code,
            outputRoot=str(result["outputRoot"]),
            runDir=str(result["outputRoot"]),
            report=str(result["reportHtml"]),
            summary=None,
            artifacts=[],
            compare=str(result["comparePath"]),
            comparisons=output.get("comparisons", {}),
            errors=output.get("errors", []),
        )
    else:
        print(json.dumps(output, sort_keys=True))
    return exit_code


def collect_android_ab(
    *,
    scenario_path: Path,
    output_root: Path | None = None,
    repeat_override: int | None = None,
    android_device_run: bool = False,
    serial: str | None = None,
    package_name_override: str | None = None,
    activity_override: str | None = None,
    baseline_apk_override: str | None = None,
    candidate_apk_override: str | None = None,
    candidate_apk_overrides: Sequence[str] | None = None,
    baseline_id: str = "baseline",
    candidate_id: str = "candidate",
    candidate_ids: Sequence[str] | None = None,
    clear_data_override: bool | None = None,
    allow_uninstall: bool = False,
    perfetto_enabled: bool = False,
    post_launch_wait_s: float = 2.0,
    interactive: bool = False,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    if not android_device_run:
        raise ValueError("perfeval ab currently requires --android-device-run")
    command_runner = command_runner or default_command_runner

    config = load_scenario_config(scenario_path)
    config_issues = validate_scenario_config(config)
    if config_issues:
        raise ValueError("; ".join(config_issues))
    mode, scenario_repeats = read_scenario_defaults(config)
    repeats = max(1, repeat_override if repeat_override is not None else scenario_repeats)
    scenario_id = sanitize_slug(str(config.get("scenarioId", "scenario")))
    root = output_root or Path("runs") / f"{scenario_id}-ab"
    ensure_directory(root)

    baseline_app = _select_android_app(config, baseline_id)
    baseline_dir_name = _group_directory_name(baseline_id)
    baseline_apk = _require_apk_path(baseline_app, baseline_id, override=baseline_apk_override)
    candidate_specs = _resolve_candidate_specs(
        config,
        baseline_id=baseline_id,
        legacy_candidate_id=candidate_id,
        candidate_ids=candidate_ids,
        candidate_apk_override=candidate_apk_override,
        candidate_apk_overrides=candidate_apk_overrides,
    )
    _validate_variant_directories(
        baseline_id=baseline_id,
        baseline_dir_name=baseline_dir_name,
        candidate_specs=candidate_specs,
    )
    package_name = _resolve_package_name_for_apps(
        [baseline_app, *(spec.app_config for spec in candidate_specs)],
        override=package_name_override,
    )
    clear_data = _clear_data_enabled(config) if clear_data_override is None else clear_data_override

    flow_steps = parse_flow_steps(config.get("flow") if isinstance(config.get("flow"), dict) else None)
    activity = activity_override or find_launch_activity(flow_steps) or None
    auto_resolve_activity = activity is None and any(
        step.get("type") == "launch" for step in flow_steps
    )
    adb_client = AdbClient()
    emit_phase("device_check", "started", "检查设备")
    preflight = run_preflight(adb_client, serial=serial)
    if not preflight.ok:
        emit_phase("device_check", "failed", "检查设备", detail=", ".join(preflight.diagnostics))
        reasons = ", ".join(preflight.diagnostics) if preflight.diagnostics else "unknown"
        raise ValueError(f"Android preflight failed: {reasons}")
    selected_serial = serial or preflight.selected_serial
    if not selected_serial:
        raise ValueError("Android preflight did not select a device")
    emit_phase("device_check", "completed", "检查设备")

    _clear_previous_ab_outputs(
        root,
        group_dir_names=(baseline_dir_name, *(spec.directory_name for spec in candidate_specs)),
    )

    root_diagnostics: list[dict[str, Any]] = []
    baseline = _collect_group(
        group_id=baseline_id,
        group_dir_name=baseline_dir_name,
        config=config,
        root=root,
        app_config=baseline_app,
        apk_path=baseline_apk,
        package_name=package_name,
        selected_serial=selected_serial,
        activity=activity,
        auto_resolve_activity=auto_resolve_activity,
        flow_steps=flow_steps,
        repeats=repeats,
        clear_data=clear_data,
        allow_uninstall=allow_uninstall,
        perfetto_enabled=perfetto_enabled,
        post_launch_wait_s=post_launch_wait_s,
        interactive=interactive,
        adb_client=adb_client,
        command_runner=command_runner,
        root_diagnostics=root_diagnostics,
    )
    candidates: dict[str, _GroupResult] = {}
    for spec in candidate_specs:
        candidates[spec.group_id] = _collect_group(
            group_id=spec.group_id,
            group_dir_name=spec.directory_name,
            config=config,
            root=root,
            app_config=spec.app_config,
            apk_path=spec.apk_path,
            package_name=package_name,
            selected_serial=selected_serial,
            activity=activity,
            auto_resolve_activity=auto_resolve_activity,
            flow_steps=flow_steps,
            repeats=repeats,
            clear_data=clear_data,
            allow_uninstall=allow_uninstall,
            perfetto_enabled=perfetto_enabled,
            post_launch_wait_s=post_launch_wait_s,
            interactive=interactive,
            adb_client=adb_client,
            command_runner=command_runner,
            root_diagnostics=root_diagnostics,
        )

    budget = {"metrics": dict(config.get("budgets"))} if isinstance(config.get("budgets"), dict) else None
    compare_dir = root / "compare"
    report_dir = root / "report"
    multi_candidate = len(candidate_specs) > 1
    comparisons: dict[str, dict[str, Any]] = {}
    comparison_paths: dict[str, Path] = {}
    report_paths: dict[str, Path] = {}

    emit_phase("comparison", "started", "比较版本结果")
    for spec in candidate_specs:
        candidate = candidates[spec.group_id]
        compare = compare_summaries(
            baseline.repeat_summaries,
            candidate.repeat_summaries,
            mode=mode,
            budget=budget,
            compare_id=f"compare-{scenario_id}-{baseline_id}-{spec.group_id}",
            baseline_group=baseline_id,
            candidate_group=spec.group_id,
        )
        compare["scenarioId"] = config.get("scenarioId")
        compare["device"] = selected_serial
        compare["runCount"] = f"{repeats} x 2"
        compare["apps"] = {
            baseline_id: baseline.summary.get("app"),
            spec.group_id: candidate.summary.get("app"),
        }
        _apply_metric_contract(compare, config, baseline.repeat_summaries, candidate.repeat_summaries)
        comparisons[spec.group_id] = compare
    emit_phase("comparison", "completed", "比较版本结果")

    emit_phase("report", "started", "生成报告")
    for spec in candidate_specs:
        compare = comparisons[spec.group_id]
        if multi_candidate:
            candidate_compare_dir = compare_dir / spec.directory_name
            candidate_report_dir = report_dir / spec.directory_name
        else:
            candidate_compare_dir = compare_dir
            candidate_report_dir = report_dir
        comparison_path = write_json_document(candidate_compare_dir / "compare.json", compare)
        report_outputs = write_report_bundle(compare, candidate_report_dir)
        comparison_paths[spec.group_id] = comparison_path
        report_paths[spec.group_id] = report_outputs["report_html"]

    if multi_candidate:
        compare = _build_multi_compare_manifest(
            scenario_id=scenario_id,
            baseline_id=baseline_id,
            baseline=baseline,
            candidate_specs=candidate_specs,
            candidates=candidates,
            comparisons=comparisons,
            comparison_paths=comparison_paths,
            report_paths=report_paths,
            selected_serial=selected_serial,
            repeats=repeats,
            mode=mode,
        )
        compare_path = write_json_document(compare_dir / "compare.json", compare)
        report_html = _write_multi_ab_report(compare, report_dir / "report.html")
    else:
        only_candidate_id = candidate_specs[0].group_id
        compare = comparisons[only_candidate_id]
        compare_path = comparison_paths[only_candidate_id]
        report_html = report_paths[only_candidate_id]

    emit_phase("report", "completed", "生成报告")

    write_ndjson_document(root / "diagnostics.ndjson", root_diagnostics)
    status = str(compare.get("status") or "unknown")
    candidate_directories = {
        spec.group_id: str(candidates[spec.group_id].directory)
        for spec in candidate_specs
    }
    comparison_metadata = {
        spec.group_id: {
            "status": comparisons[spec.group_id].get("status"),
            "compare": str(comparison_paths[spec.group_id]),
            "report": str(report_paths[spec.group_id]),
        }
        for spec in candidate_specs
    }
    ab_metadata: dict[str, Any] = {
        "schemaVersion": "perfeval.ab.v2" if multi_candidate else "perfeval.ab.v1",
        "scenarioId": config.get("scenarioId"),
        "mode": mode,
        "status": status,
        "baseline": str(baseline.directory),
        "candidates": candidate_directories,
        "comparisons": comparison_metadata,
        "compare": str(compare_path),
        "report": str(report_html),
        "repeats": repeats,
        "variantCount": 1 + len(candidate_specs),
        "clearData": clear_data,
        "allowUninstall": allow_uninstall,
    }
    if len(candidate_specs) == 1:
        ab_metadata["candidate"] = candidate_directories[candidate_specs[0].group_id]
    write_json_document(
        root / "ab.json",
        ab_metadata,
    )
    variant_lines = [f"baseline={baseline_id}:{baseline_apk}"] + [
        f"candidate={spec.group_id}:{spec.apk_path}" for spec in candidate_specs
    ]
    write_text(
        root / "tool.log",
        "\n".join(
            [
                f"[{utc_now_iso()}] PerfEval Lite A/B run",
                f"scenario={scenario_path}",
                *variant_lines,
                f"repeats={repeats}",
                f"clear_data={clear_data}",
                f"status={status}",
                f"compare={compare_path}",
                f"report={report_html}",
            ]
        )
        + "\n",
    )

    return {
        "outputRoot": root,
        "status": status,
        "baseline": baseline.summary,
        "candidate": candidates[candidate_specs[0].group_id].summary,
        "candidates": {candidate_key: group.summary for candidate_key, group in candidates.items()},
        "compare": compare,
        "comparisons": comparisons,
        "comparePath": compare_path,
        "comparisonPaths": comparison_paths,
        "reportHtml": report_html,
        "reportPaths": report_paths,
    }


@dataclass(frozen=True)
class _VariantSpec:
    group_id: str
    directory_name: str
    app_config: dict[str, Any]
    apk_path: str


def _resolve_candidate_specs(
    config: Mapping[str, Any],
    *,
    baseline_id: str,
    legacy_candidate_id: str,
    candidate_ids: Sequence[str] | None,
    candidate_apk_override: str | None,
    candidate_apk_overrides: Sequence[str] | None,
) -> list[_VariantSpec]:
    apk_tokens = [token for token in candidate_apk_overrides or [] if token]
    if candidate_apk_override:
        apk_tokens.insert(0, candidate_apk_override)
    requested_ids = [candidate_id for candidate_id in candidate_ids or [] if candidate_id]
    android_apps = _android_apps(config)
    apps_by_id = {
        str(app["id"]): dict(app)
        for app in android_apps
        if isinstance(app.get("id"), str) and app.get("id")
    }
    template = apps_by_id.get(legacy_candidate_id)
    if template is None:
        template = next((dict(app) for app in android_apps if app.get("id") != baseline_id), None)
    if template is None:
        raise ValueError("perfeval ab requires at least one Android candidate app template")

    resolved: list[tuple[str, str, dict[str, Any]]] = []
    if apk_tokens:
        inline_ids = [_split_candidate_apk_token(token)[0] for token in apk_tokens]
        if requested_ids and any(candidate_key is not None for candidate_key in inline_ids):
            raise ValueError("do not combine --candidate-id with ID=PATH --candidate-apk values")
        if requested_ids and len(requested_ids) != len(apk_tokens):
            raise ValueError("the number of --candidate-id values must match --candidate-apk values")
        for index, token in enumerate(apk_tokens):
            inline_id, apk_path = _split_candidate_apk_token(token)
            if inline_id is not None:
                resolved_id = inline_id
            elif requested_ids:
                resolved_id = requested_ids[index]
            elif index == 0:
                resolved_id = legacy_candidate_id
            else:
                resolved_id = f"{legacy_candidate_id}-{index + 1}"
            app_config = dict(apps_by_id.get(resolved_id, template))
            app_config["id"] = resolved_id
            resolved.append((resolved_id, apk_path, app_config))
    else:
        if requested_ids:
            selected_ids = requested_ids
        elif legacy_candidate_id != "candidate":
            selected_ids = [legacy_candidate_id]
        else:
            selected_ids = [str(app["id"]) for app in android_apps if app.get("id") != baseline_id]
        for selected_id in selected_ids:
            app_config = apps_by_id.get(selected_id)
            if app_config is None:
                raise ValueError(f"Android app with id {selected_id!r} was not found in apps[]")
            resolved.append(
                (
                    selected_id,
                    _require_apk_path(app_config, selected_id),
                    dict(app_config),
                )
            )

    if not resolved:
        raise ValueError("perfeval ab requires at least one candidate APK")
    return [
        _VariantSpec(
            group_id=group_id,
            directory_name=_group_directory_name(group_id),
            app_config=app_config,
            apk_path=apk_path,
        )
        for group_id, apk_path, app_config in resolved
    ]


def _split_candidate_apk_token(value: str) -> tuple[str | None, str]:
    candidate_id, separator, apk_path = value.partition("=")
    if separator and candidate_id and apk_path and "/" not in candidate_id and "\\" not in candidate_id:
        return candidate_id, apk_path
    return None, value


def _android_apps(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    apps = config.get("apps")
    if not isinstance(apps, Sequence) or isinstance(apps, (str, bytes, bytearray)):
        raise ValueError("perfeval ab requires apps[] with Android variants")
    return [dict(app) for app in apps if isinstance(app, Mapping) and app.get("platform") == "android"]


def _validate_variant_directories(
    *,
    baseline_id: str,
    baseline_dir_name: str,
    candidate_specs: Sequence[_VariantSpec],
) -> None:
    ids = [baseline_id, *(spec.group_id for spec in candidate_specs)]
    if len(ids) != len(set(ids)):
        raise ValueError("baseline and candidate ids must be unique")
    directory_names = [baseline_dir_name, *(spec.directory_name for spec in candidate_specs)]
    if len(directory_names) != len(set(directory_names)):
        raise ValueError("variant ids resolve to the same output directory")


def _resolve_package_name_for_apps(
    apps: Sequence[Mapping[str, Any]],
    *,
    override: str | None,
) -> str:
    if override:
        return override
    packages = {
        str(app.get("packageName"))
        for app in apps
        if isinstance(app.get("packageName"), str) and app.get("packageName")
    }
    if len(packages) > 1:
        raise ValueError("all Android variants must use the same packageName")
    if not packages:
        raise ValueError("perfeval ab requires apps[].packageName or --package-name")
    return next(iter(packages))


def _build_multi_compare_manifest(
    *,
    scenario_id: str,
    baseline_id: str,
    baseline: _GroupResult,
    candidate_specs: Sequence[_VariantSpec],
    candidates: Mapping[str, _GroupResult],
    comparisons: Mapping[str, Mapping[str, Any]],
    comparison_paths: Mapping[str, Path],
    report_paths: Mapping[str, Path],
    selected_serial: str,
    repeats: int,
    mode: str,
) -> dict[str, Any]:
    statuses = [str(comparisons[spec.group_id].get("status") or "unknown") for spec in candidate_specs]
    return {
        "schemaVersion": "perfeval.multi_compare.v1",
        "compareId": f"compare-{scenario_id}-{baseline_id}-multi",
        "scenarioId": scenario_id,
        "mode": mode,
        "status": _aggregate_status(statuses),
        "baselineGroup": baseline_id,
        "candidateGroups": [spec.group_id for spec in candidate_specs],
        "variantCount": 1 + len(candidate_specs),
        "device": selected_serial,
        "runCount": f"{repeats} x {1 + len(candidate_specs)}",
        "apps": {
            baseline_id: baseline.summary.get("app"),
            **{
                spec.group_id: candidates[spec.group_id].summary.get("app")
                for spec in candidate_specs
            },
        },
        "statusCounts": {
            status: statuses.count(status)
            for status in ("pass", "warn", "fail", "unknown", "invalid")
            if status in statuses
        },
        "comparisons": {
            spec.group_id: {
                "status": comparisons[spec.group_id].get("status"),
                "compare": str(comparison_paths[spec.group_id]),
                "report": str(report_paths[spec.group_id]),
                "errors": comparisons[spec.group_id].get("errors", []),
                "summary": comparisons[spec.group_id].get("summary", {}),
                "focusedWindows": comparisons[spec.group_id].get("focusedWindows", {}),
            }
            for spec in candidate_specs
        },
    }


def _aggregate_status(statuses: Sequence[str]) -> str:
    for status in ("fail", "invalid", "warn", "unknown", "pass"):
        if status in statuses:
            return status
    return "unknown"


def _write_multi_ab_report(compare: Mapping[str, Any], path: Path) -> Path:
    ensure_directory(path.parent)
    comparisons = compare.get("comparisons")
    rows: list[str] = []
    if isinstance(comparisons, Mapping):
        for candidate_id, item in comparisons.items():
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or "unknown")
            compare_path = Path(str(item.get("compare") or ""))
            report_path = Path(str(item.get("report") or ""))
            compare_href = _relative_link(path.parent, compare_path)
            report_href = _relative_link(path.parent, report_path)
            errors = ", ".join(str(error) for error in item.get("errors", []) or []) or "-"
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(candidate_id))}</td>"
                f'<td><span class="status {html.escape(status)}">{html.escape(status)}</span></td>'
                f"<td>{html.escape(errors)}</td>"
                f'<td><a href="{html.escape(report_href)}">查看报告</a></td>'
                f'<td><a href="{html.escape(compare_href)}">compare.json</a></td>'
                "</tr>"
            )
    status = str(compare.get("status") or "unknown")
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PerfEval 多 APK A/B 总览</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; padding: 28px; background: #f5f7fb; color: #172033; }}
    main {{ max-width: 1100px; margin: 0 auto; }}
    .panel {{ background: white; border: 1px solid #dbe2ea; border-radius: 14px; padding: 20px; margin-bottom: 18px; }}
    h1 {{ margin: 0 0 10px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 12px 10px; border-bottom: 1px solid #e7ebf0; }}
    th {{ color: #596579; font-size: 13px; }}
    a {{ color: #1c6dd0; }}
    .status {{ display: inline-block; padding: 4px 9px; border-radius: 999px; font-weight: 700; }}
    .pass {{ background: #e7f7ee; color: #1f7a45; }}
    .warn {{ background: #fff5d6; color: #9a6500; }}
    .fail {{ background: #fde9e7; color: #b42318; }}
    .unknown, .invalid {{ background: #edf2f7; color: #56657a; }}
  </style>
</head>
<body>
<main>
  <section class="panel">
    <h1>PerfEval 多 APK A/B 总览</h1>
    <p>总体状态：<span class="status {html.escape(status)}">{html.escape(status)}</span></p>
    <p>Baseline：{html.escape(str(compare.get('baselineGroup') or '-'))}；候选数量：{len(rows)}；运行次数：{html.escape(str(compare.get('runCount') or '-'))}</p>
  </section>
  <section class="panel">
    <table>
      <thead><tr><th>Candidate</th><th>状态</th><th>错误</th><th>报告</th><th>数据</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </section>
</main>
</body>
</html>
"""
    write_text(path, document)
    return path


def _relative_link(base: Path, target: Path) -> str:
    return os.path.relpath(target, base)


def _clear_previous_ab_outputs(root: Path, *, group_dir_names: Sequence[str]) -> None:
    managed_group_names = set(group_dir_names)
    metadata_path = root / "ab.json"
    if metadata_path.exists() and metadata_path.is_file():
        try:
            previous = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            previous = {}
        for value in (previous.get("baseline"), previous.get("candidate")):
            if isinstance(value, str) and value:
                managed_group_names.add(Path(value).name)
        candidates = previous.get("candidates")
        if isinstance(candidates, Mapping):
            for value in candidates.values():
                if isinstance(value, str) and value:
                    managed_group_names.add(Path(value).name)
    for directory_name in (*managed_group_names, "compare", "report"):
        path = root / directory_name
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
    for filename in ("ab.json", "diagnostics.ndjson", "tool.log"):
        path = root / filename
        if path.exists() and path.is_file():
            path.unlink()


class _GroupResult:
    def __init__(self, *, directory: Path, summary: dict[str, Any], repeat_summaries: list[dict[str, Any]]) -> None:
        self.directory = directory
        self.summary = summary
        self.repeat_summaries = repeat_summaries


def _collect_group(
    *,
    group_id: str,
    group_dir_name: str,
    config: dict[str, Any],
    root: Path,
    app_config: dict[str, Any],
    apk_path: str,
    package_name: str,
    selected_serial: str,
    activity: str | None,
    auto_resolve_activity: bool,
    flow_steps: list[dict[str, Any]],
    repeats: int,
    clear_data: bool,
    allow_uninstall: bool,
    perfetto_enabled: bool,
    post_launch_wait_s: float,
    interactive: bool,
    adb_client: AdbClient,
    command_runner: CommandRunner,
    root_diagnostics: list[dict[str, Any]],
) -> _GroupResult:
    group_dir = root / group_dir_name
    ensure_directory(group_dir)
    emit_phase("variant_prepare", "started", "准备测试版本", variant_id=group_id)
    installed = _install_group_app(
        group_id=group_id,
        package_name=package_name,
        apk_path=apk_path,
        serial=selected_serial,
        allow_uninstall=allow_uninstall,
        adb_client=adb_client,
        command_runner=command_runner,
        diagnostics=root_diagnostics,
    )
    if not installed:
        emit_phase("variant_prepare", "failed", "准备测试版本", variant_id=group_id)
        write_ndjson_document(root / "diagnostics.ndjson", root_diagnostics)
        raise ValueError(f"Android install failed for {group_id}; see {root / 'diagnostics.ndjson'}")
    emit_phase("variant_prepare", "completed", "准备测试版本", variant_id=group_id)

    interaction_cmds = build_interaction_commands(
        flow_steps,
        adb=adb_client,
        package_name=package_name,
        serial=selected_serial,
    )
    all_events: list[dict[str, Any]] = []
    all_diagnostics: list[dict[str, Any]] = []
    all_artifacts: list[dict[str, Any]] = []
    repeat_summaries: list[dict[str, Any]] = []

    for repeat_index in range(1, repeats + 1):
        repeat_run_id = f"{sanitize_slug(str(config.get('scenarioId', 'scenario')))}-{group_id}-r{repeat_index:03d}"
        repeat_dir = group_dir / "repeats" / f"r{repeat_index:03d}"
        ensure_directory(repeat_dir)
        result = collect_android_run(
            run_id=repeat_run_id,
            run_dir=repeat_dir,
            package_name=package_name,
            activity=activity,
            apk_path=None,
            serial=selected_serial,
            clear_data=clear_data,
            reinstall=False,
            auto_resolve_activity=auto_resolve_activity,
            perfetto_enabled=perfetto_enabled,
            post_launch_wait_s=post_launch_wait_s,
            interaction_commands=interaction_cmds,
            interactive=interactive,
            phase_callback=lambda phase_id, state, label, detail, current=repeat_index: emit_phase(
                phase_id,
                state,
                label,
                repeat_index=current,
                repeat_count=repeats,
                variant_id=group_id,
                detail=detail,
            ),
        )
        diagnostics = run_command._android_device_diagnostics(repeat_run_id, list(result.diagnostics))
        status = "partial" if _has_error_diagnostic(diagnostics) else "completed"
        summary = run_command._summary_from_events(
            config,
            repeat_run_id,
            result.events,
            package_name=package_name,
            app_config=app_config,
            artifact_path=apk_path,
            artifacts=result.artifacts,
            diagnostics=diagnostics,
            status=status,
        )
        _set_device_serial(summary, selected_serial)
        summary["group"] = group_id
        summary["repeatIndex"] = repeat_index
        write_json_document(repeat_dir / "summary.json", summary)
        write_ndjson_document(repeat_dir / "timeline.ndjson", result.events)
        write_ndjson_document(repeat_dir / "diagnostics.ndjson", diagnostics)
        repeat_summaries.append(summary)
        all_events.extend(result.events)
        all_diagnostics.extend(diagnostics)
        all_artifacts.extend(dict(artifact, repeat=repeat_index, group=group_id) for artifact in result.artifacts)

    group_status = "partial" if _has_error_diagnostic(all_diagnostics) else "completed"
    group_summary = run_command._summary_from_events(
        config,
        f"{sanitize_slug(str(config.get('scenarioId', 'scenario')))}-{group_id}",
        all_events,
        package_name=package_name,
        app_config=app_config,
        artifact_path=apk_path,
        artifacts=all_artifacts,
        diagnostics=all_diagnostics,
        status=group_status,
    )
    _set_device_serial(group_summary, selected_serial)
    group_summary["group"] = group_id
    group_summary["repeats"] = repeats
    write_json_document(group_dir / "summary.json", group_summary)
    write_ndjson_document(group_dir / "timeline.ndjson", all_events)
    write_ndjson_document(group_dir / "diagnostics.ndjson", all_diagnostics)
    group_report_dir = group_dir / "report"
    if repeats > 1:
        write_multi_run_report_bundle(group_summary, group_report_dir)
    else:
        write_summary_report_bundle(group_summary, group_report_dir)
    return _GroupResult(directory=group_dir, summary=group_summary, repeat_summaries=repeat_summaries)


def _install_group_app(
    *,
    group_id: str,
    package_name: str,
    apk_path: str,
    serial: str,
    allow_uninstall: bool,
    adb_client: AdbClient,
    command_runner: CommandRunner,
    diagnostics: list[dict[str, Any]],
) -> bool:
    install_command = adb_client.command("install", "-r", "-d", apk_path, serial=serial)
    install = _run_adb(install_command, command_runner, timeout=120)
    diagnostics.append(_command_diagnostic(len(diagnostics) + 1, group_id, "install", install))
    if install.returncode == 0:
        return True

    if not allow_uninstall:
        return False

    uninstall_command = adb_client.command("uninstall", package_name, serial=serial)
    uninstall = _run_adb(uninstall_command, command_runner, timeout=60)
    diagnostics.append(_command_diagnostic(len(diagnostics) + 1, group_id, "uninstall", uninstall))
    if uninstall.returncode != 0:
        return False

    retry = _run_adb(install_command, command_runner, timeout=120)
    diagnostics.append(_command_diagnostic(len(diagnostics) + 1, group_id, "install_retry", retry))
    return retry.returncode == 0


def _run_adb(
    command: list[str],
    command_runner: CommandRunner,
    *,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    try:
        return command_runner(command, timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_output_to_text(exc.stdout)
        stderr = _timeout_output_to_text(exc.stderr) or f"timed out after {exc.timeout} seconds"
        return subprocess.CompletedProcess(command, 124, stdout, stderr)


def _command_diagnostic(seq: int, group_id: str, command_name: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    name = "android.ab.command"
    code = f"android.ab.{command_name}.{result.returncode}"
    message = f"{group_id}: {' '.join(result.args) if isinstance(result.args, list) else result.args}"
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    if stderr:
        message = f"{message}: {stderr}"
    elif stdout:
        message = f"{message}: {stdout}"
    return run_command._diagnostic_entry(
        "ab",
        seq,
        name=name,
        severity="info" if result.returncode == 0 else "error",
        code=code,
        message=message,
        collector="cli.ab",
    )


def _select_android_app(config: Mapping[str, Any], app_id: str) -> dict[str, Any]:
    apps = config.get("apps")
    if not isinstance(apps, Sequence) or isinstance(apps, (str, bytes, bytearray)):
        raise ValueError("perfeval ab requires apps[] with baseline and candidate entries")
    for app in apps:
        if not isinstance(app, Mapping):
            continue
        if app.get("id") == app_id and app.get("platform") == "android":
            return dict(app)
    raise ValueError(f"Android app with id {app_id!r} was not found in apps[]")


def _group_directory_name(app_id: str) -> str:
    if not app_id.strip():
        raise ValueError("app id must be a non-empty string")
    return sanitize_slug(app_id)


def _resolve_package_name(
    baseline_app: Mapping[str, Any],
    candidate_app: Mapping[str, Any],
    *,
    override: str | None,
) -> str:
    if override:
        return override
    baseline_package = baseline_app.get("packageName")
    candidate_package = candidate_app.get("packageName")
    if isinstance(baseline_package, str) and isinstance(candidate_package, str) and baseline_package != candidate_package:
        raise ValueError("baseline and candidate packageName must match for Android same-app A/B")
    package_name = baseline_package or candidate_package
    if not isinstance(package_name, str) or not package_name:
        raise ValueError("perfeval ab requires apps[].packageName or --package-name")
    return package_name


def _require_apk_path(
    app_config: Mapping[str, Any],
    app_id: str,
    *,
    override: str | None = None,
) -> str:
    artifact = override or app_config.get("artifact") or app_config.get("artifactPath")
    if not isinstance(artifact, str) or not artifact:
        raise ValueError(
            f"Android app {app_id!r} requires an artifact APK path in apps[] "
            "or the matching --baseline-apk/--candidate-apk option"
        )
    return artifact


def _clear_data_enabled(config: Mapping[str, Any]) -> bool:
    run = config.get("run")
    if isinstance(run, Mapping):
        install = run.get("install")
        if isinstance(install, Mapping) and isinstance(install.get("clearData"), bool):
            return bool(install["clearData"])
    return True


def _apply_metric_contract(
    compare: dict[str, Any],
    config: Mapping[str, Any],
    baseline_summaries: Sequence[Mapping[str, Any]],
    candidate_summaries: Sequence[Mapping[str, Any]],
) -> None:
    required = _required_metrics(config)
    if not required or compare.get("status") == "invalid":
        return
    metrics = compare.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        return
    baseline_metric_ids = _summary_metric_ids(baseline_summaries)
    candidate_metric_ids = _summary_metric_ids(candidate_summaries)
    missing_required: list[str] = []
    for metric_id in required:
        missing_sides = []
        if metric_id not in baseline_metric_ids:
            missing_sides.append("baseline")
        if metric_id not in candidate_metric_ids:
            missing_sides.append("candidate")
        if not missing_sides:
            continue
        missing_required.append(metric_id)
        metrics[metric_id] = _missing_required_metric(metric_id, missing_sides)
    if missing_required:
        summary = compare.setdefault("summary", {})
        if isinstance(summary, dict):
            unknown = list(summary.get("unknownMetrics") or [])
            summary["unknownMetrics"] = sorted(set(str(item) for item in [*unknown, *missing_required]))
            summary["missingRequiredMetrics"] = sorted(missing_required)
            summary["metricCount"] = len(metrics)
        reliability = compare.setdefault("reliability", {})
        if isinstance(reliability, dict):
            reasons = list(reliability.get("reasons") or [])
            reliability["reasons"] = sorted(set([*map(str, reasons), "required_metric_missing"]))
            reliability["score"] = min(float(reliability.get("score") or 0.0), 0.0)
            reliability["level"] = "low"
        if compare.get("status") != "fail":
            compare["status"] = "unknown"


def _required_metrics(config: Mapping[str, Any]) -> list[str]:
    contract = config.get("metricContract")
    if not isinstance(contract, Mapping):
        return []
    required = contract.get("required")
    if not isinstance(required, Sequence) or isinstance(required, (str, bytes, bytearray)):
        return []
    return [str(metric_id) for metric_id in required if isinstance(metric_id, str) and metric_id]


def _summary_metric_ids(summaries: Sequence[Mapping[str, Any]]) -> set[str]:
    metric_ids: set[str] = set()
    for summary in summaries:
        metrics = summary.get("metrics")
        if isinstance(metrics, Mapping):
            metric_ids.update(str(metric_id) for metric_id in metrics)
    return metric_ids


def _missing_required_metric(metric_id: str, missing_sides: Sequence[str]) -> dict[str, Any]:
    return {
        "priority": "P0" if metric_id.startswith(("startup.", "frame.", "stability.")) else "P1",
        "unit": None,
        "direction": "lower_better",
        "baseline": {"n": 0 if "baseline" in missing_sides else None, "median": None},
        "candidate": {"n": 0 if "candidate" in missing_sides else None, "median": None},
        "delta": {"abs": None, "pct": None, "bootstrapCi95": None},
        "budget": {},
        "judgement": "unknown",
        "reason": "required_metric_missing:" + ",".join(missing_sides),
        "confidence": {"level": "low", "bootstrapCi95": None, "noise": "unknown"},
        "reliability": {"score": 0.0, "level": "low", "reasons": ["required_metric_missing"]},
        "evidence": {"level": None, "artifacts": [], "missingRuns": []},
        "sourceRunIds": [],
    }


def _has_error_diagnostic(diagnostics: Sequence[Mapping[str, Any]]) -> bool:
    for diagnostic in diagnostics:
        data = diagnostic.get("data")
        if isinstance(data, Mapping) and data.get("severity") == "error":
            return True
        if diagnostic.get("severity") == "error":
            return True
    return False


def _set_device_serial(summary: dict[str, Any], serial: str) -> None:
    device = summary.setdefault("device", {})
    if isinstance(device, dict):
        device.setdefault("platform", "android")
        device["serialOrUdid"] = serial


def _timeout_output_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)
