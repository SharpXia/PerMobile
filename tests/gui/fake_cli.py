#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "perfeval.cli.event.v1"


def _event(event_type: str, data: dict[str, Any], *, command: str = "run") -> bytes:
    return (
        json.dumps(
            {
                "schemaVersion": SCHEMA_VERSION,
                "type": event_type,
                "command": command,
                "data": data,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _result_data(
    args: argparse.Namespace,
    *,
    status: str | None = None,
    exit_code: int | None = None,
) -> dict[str, Any]:
    root = Path(args.output_root)
    run_dir = root / "测试任务"
    report = run_dir / "report" / "report.html"
    summary = run_dir / "summary.json"
    artifact = run_dir / "artifacts" / "日志.txt"
    if args.report_state == "exists":
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("<!doctype html><title>fake report</title>", encoding="utf-8")
    elif args.report_state == "damaged":
        report.mkdir(parents=True, exist_ok=True)
    return {
        "status": status or args.status,
        "exitCode": args.exit_code if exit_code is None else exit_code,
        "outputRoot": str(root),
        "runDir": str(run_dir),
        "report": str(report) if args.report_state != "missing" else None,
        "summary": str(summary),
        "artifacts": [str(artifact)],
    }


def _write_chunks(fd: int, payload: bytes, cuts: list[int], delay: float) -> None:
    start = 0
    for end in [*cuts, len(payload)]:
        os.write(fd, payload[start:end])
        start = end
        if delay:
            time.sleep(delay)


def _stream(args: argparse.Namespace) -> int:
    os.write(
        sys.stdout.fileno(),
        _event("phase", {"id": "device_check", "state": "completed", "label": "检查设备"}),
    )
    os.write(
        sys.stdout.fileno(),
        _event(
            "phase",
            {
                "id": "collection",
                "state": "started",
                "label": "采集性能数据",
                "repeatIndex": 1,
                "repeatCount": 1,
            },
        ),
    )
    first = _event("log", {"stream": "stdout", "text": "first chunk\n"})
    second = _event("log", {"stream": "stdout", "text": "second chunk\n"})
    _write_chunks(sys.stdout.fileno(), first, [5, 17], args.delay)
    _write_chunks(sys.stderr.fileno(), b"stderr-one\n", [3], args.delay)
    _write_chunks(sys.stdout.fileno(), second, [1, 9, 25], args.delay)
    _write_chunks(sys.stderr.fileno(), "错误流\n".encode("utf-8"), [1, 4], args.delay)
    os.write(sys.stdout.fileno(), _event("result", _result_data(args)))
    return args.exit_code


def _manual(args: argparse.Namespace) -> int:
    payload = _event(
        "manual",
        {
            "prompt": "请完成手动步骤，然后按 Enter",
            "timeoutSeconds": args.manual_timeout,
            "input": "enter",
        },
    )
    _write_chunks(sys.stdout.fileno(), payload, [7, max(8, len(payload) // 2)], args.delay)
    response = sys.stdin.readline()
    status = args.status if response else "tool_error"
    exit_code = args.exit_code if response else 10
    os.write(sys.stdout.fileno(), _event("result", _result_data(args, status=status, exit_code=exit_code)))
    return exit_code


def _manual_text(args: argparse.Namespace) -> int:
    os.write(sys.stdout.fileno(), "Fallback 手动提示：按 Enter".encode("utf-8"))
    response = sys.stdin.readline()
    if response:
        os.write(sys.stdout.fileno(), b"\ncontinued\n")
        return args.exit_code
    return 10


def _unicode(args: argparse.Namespace) -> int:
    payload = _event("log", {"stream": "stdout", "text": "中文路径/测试任务/报告.html 🚀\n"})
    marker = "测".encode("utf-8")
    marker_index = payload.index(marker)
    _write_chunks(sys.stdout.fileno(), payload, [marker_index + 1, marker_index + 2], args.delay)
    stderr = "标准错误：设备未授权\n".encode("utf-8")
    _write_chunks(sys.stderr.fileno(), stderr, [1, 2, 7], args.delay)
    os.write(sys.stdout.fileno(), _event("result", _result_data(args)))
    return args.exit_code


def _long_log(args: argparse.Namespace) -> int:
    for index in range(max(1, args.line_count)):
        os.write(
            sys.stdout.fileno(),
            _event(
                "log",
                {
                    "stream": "stdout" if index % 7 else "stderr",
                    "text": f"长日志 line {index:05d} / 路径 空格 🚀\n",
                },
            ),
        )
        if args.delay:
            time.sleep(args.delay)
    os.write(sys.stdout.fileno(), _event("result", _result_data(args)))
    return args.exit_code


def _devices_event() -> int:
    data = {
        "tools": {
            "adb": {"available": True, "path": sys.argv[0]},
            "xcrun": {"available": False, "path": None},
            "xctrace": {"available": False, "path": None},
        },
        "android": {
            "status": "ready",
            "available": True,
            "adbAvailable": True,
            "selectedSerial": "fake-online-1",
            "devices": [
                {
                    "serial": "fake-online-1",
                    "state": "device",
                    "model": "Fake_Pixel",
                    "product": "fake_product",
                    "device": "fake_device",
                }
            ],
            "diagnostics": [],
        },
    }
    result = {
        "status": "completed",
        "exitCode": 0,
        "outputRoot": None,
        "runDir": None,
        "report": None,
        "summary": None,
        "artifacts": [],
    }
    os.write(sys.stdout.fileno(), _event("devices", data, command="devices"))
    os.write(sys.stdout.fileno(), _event("result", result, command="devices"))
    return 0


def _long(args: argparse.Namespace, *, ignore_terminate: bool) -> int:
    if ignore_terminate:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        def terminate(_signum: int, _frame: object) -> None:
            os.write(
                sys.stdout.fileno(),
                _event("result", _result_data(args, status="cancelled", exit_code=130)),
            )
            raise SystemExit(130)

        signal.signal(signal.SIGTERM, terminate)

    tick = 0
    while True:
        os.write(
            sys.stdout.fileno(),
            _event("log", {"stream": "stdout", "text": f"tick {tick}\n"}),
        )
        tick += 1
        time.sleep(max(args.delay, 0.02))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parametric fake PerfEval CLI used by GUI process tests.")
    parser.add_argument(
        "scenario",
        choices=[
            "stream",
            "manual",
            "manual-text",
            "unicode",
            "long-log",
            "exit",
            "long",
            "ignore-terminate",
        ],
    )
    parser.add_argument("--status", default="completed")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.01)
    parser.add_argument("--manual-timeout", type=float, default=None)
    parser.add_argument("--line-count", type=int, default=2500)
    parser.add_argument("--output-root", default="fake-output")
    parser.add_argument("--report-state", choices=["exists", "missing", "damaged"], default="exists")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args == ["--version"]:
        os.write(sys.stdout.fileno(), b"perfeval 0.1.0-fake\n")
        return 0
    if raw_args[:2] == ["devices", "-l"]:
        os.write(
            sys.stdout.fileno(),
            b"List of devices attached\nfake-online-1\tdevice product:fake_product model:Fake_Pixel device:fake_device\n",
        )
        return 0
    if raw_args and raw_args[0] == "devices" and "--gui-events" in raw_args[1:]:
        return _devices_event()

    args = build_parser().parse_args(raw_args)
    if args.scenario == "stream":
        return _stream(args)
    if args.scenario == "manual":
        return _manual(args)
    if args.scenario == "manual-text":
        return _manual_text(args)
    if args.scenario == "unicode":
        return _unicode(args)
    if args.scenario == "long-log":
        return _long_log(args)
    if args.scenario == "long":
        return _long(args, ignore_terminate=False)
    if args.scenario == "ignore-terminate":
        return _long(args, ignore_terminate=True)
    os.write(sys.stdout.fileno(), _event("result", _result_data(args)))
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
