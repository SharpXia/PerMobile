from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from perfeval import __version__
from perfeval.cli.commands import ab, compare, diagnose, open as open_command, report, run, validate
from perfeval.cli.protocol import (
    add_gui_events_argument,
    emit_event,
    emit_result,
    gui_event_stream,
    gui_events_enabled,
)
from perfeval.collectors.android import AndroidPreflightResult, run_preflight
from perfeval.core.config import ensure_directory, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perfeval",
        description="PerfEval Lite host CLI.",
    )
    parser.add_argument("--version", action="version", version=f"perfeval {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create local PerfEval directories and examples.")
    init_parser.add_argument("--root", default=".", help="Project root to initialize.")
    init_parser.set_defaults(func=handle_init)

    devices_parser = subparsers.add_parser("devices", help="Show host tool diagnostics for device collectors.")
    add_gui_events_argument(devices_parser)
    devices_parser.set_defaults(func=handle_devices)

    run.register_parser(subparsers)
    ab.register_parser(subparsers)
    diagnose.register_parser(subparsers)
    compare.register_parser(subparsers)
    report.register_parser(subparsers)
    open_command.register_parser(subparsers)
    validate.register_parser(subparsers)
    return parser


def handle_init(args: argparse.Namespace) -> int:
    root = Path(args.root)
    for name in ("scenarios", "runs", "reports"):
        ensure_directory(root / name)

    example = root / "scenarios" / "startup.synthetic.yaml"
    if not example.exists():
        write_text(
            example,
            "\n".join(
                [
                    "schemaVersion: perfeval.scenario.v1",
                    "project: PerMobile",
                    "scenarioId: startup_home",
                    "description: Synthetic cold start regression scenario.",
                    "run:",
                    "  repeats: 5",
                    "  mode: same_device_versions",
                    "budgets:",
                    "  startup.cold.ms:",
                    "    direction: lower_better",
                    "    failAbs: 2500",
                    "    warnRegressionPct: 0.08",
                    "    failRegressionPct: 0.15",
                    "    minDeltaAbs: 80",
                    "",
                ]
            ),
        )

    print(f"Initialized PerfEval Lite workspace at {root.resolve()}")
    print(f"Scenario example: {example}")
    return 0


def handle_devices(args: argparse.Namespace) -> int:
    tools = {
        "adb": shutil.which("adb"),
        "xcrun": shutil.which("xcrun"),
        "xctrace": shutil.which("xctrace"),
    }
    android = run_preflight()
    if gui_events_enabled(args):
        emit_event(
            "devices",
            {
                "tools": {
                    name: {"available": path is not None, "path": path}
                    for name, path in tools.items()
                },
                "android": {
                    "status": _android_device_status(android),
                    "available": android.ok,
                    "adbAvailable": android.adb_available,
                    "selectedSerial": android.selected_serial,
                    "devices": [
                        {
                            "serial": device.serial,
                            "state": device.state,
                            "model": device.model,
                            "product": device.product,
                            "device": device.device,
                        }
                        for device in android.devices
                    ],
                    "diagnostics": list(android.diagnostics),
                },
            },
        )
        emit_result(status="completed", exit_code=0)
        return 0

    print("Host collector tool diagnostics:")
    for tool, path in tools.items():
        status = path if path else "not found"
        print(f"- {tool}: {status}")
    if android.ok:
        print(f"Android: selected {android.selected_serial}")
    else:
        reasons = ", ".join(android.diagnostics) if android.diagnostics else "unknown"
        print(f"Android: unavailable ({reasons})")
    return 0


def _android_device_status(android: AndroidPreflightResult) -> str:
    if android.ok:
        return "ready"
    if not android.adb_available:
        return "adb_missing"
    states = {device.state for device in android.devices}
    if not states:
        return "no_devices"
    if "unauthorized" in states and "device" not in states:
        return "unauthorized"
    if "offline" in states and "device" not in states:
        return "offline"
    if "android.device.ambiguous" in android.diagnostics:
        return "ambiguous"
    return "unavailable"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    with gui_event_stream(str(args.command), enabled=gui_events_enabled(args)):
        try:
            return int(args.func(args))
        except KeyboardInterrupt:
            emit_result(status="cancelled", exit_code=130)
            print("Interrupted", file=sys.stderr)
            return 130
        except Exception as exc:
            emit_result(status="tool_error", exit_code=10, error=str(exc))
            print(f"perfeval: error: {exc}", file=sys.stderr)
            return 10


if __name__ == "__main__":
    raise SystemExit(main())
