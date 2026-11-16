from __future__ import annotations

import json
import math
import re
from typing import Any

from ._events import diagnostic_event, metric_event


_PERFEVAL_EVENT_RE = re.compile(r"PERFEVAL_EVENT\s+(\{.*\})")

# `logcat -v epoch` 行首时间戳：<epoch 秒>.<毫秒>（SCREEN_RECORD_SYNC §2 时间对齐）
_LOGCAT_EPOCH_PREFIX_RE = re.compile(r"^(\d{9,12})\.(\d{1,6})\s")


def _line_epoch_ms(line: str) -> int | None:
    """解析 logcat -v epoch 行首时间戳为 epoch 毫秒；非时间前缀行返回 None。"""
    match = _LOGCAT_EPOCH_PREFIX_RE.match(line)
    if match is None:
        return None
    seconds = int(match.group(1))
    fraction = match.group(2)[:3].ljust(3, "0")
    return seconds * 1000 + int(fraction)


def parse_logcat(text: str, *, package_name: str | None = None) -> dict[str, Any]:
    crash_count = 0
    anr_count = 0
    strict_mode_count = 0
    custom_events: list[dict[str, Any]] = []
    first_epoch_ms: int | None = None

    pending_fatal = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # t0（时间对齐原点）= logcat 首条 epoch 时间戳（SCREEN_RECORD_SYNC §2）
        if first_epoch_ms is None:
            first_epoch_ms = _line_epoch_ms(line)
        if "FATAL EXCEPTION" in line:
            if package_name is None:
                crash_count += 1
            else:
                pending_fatal = True
            continue
        if pending_fatal and "Process:" in line:
            process_match = re.search(r"Process:\s*([^,\s]+)", line)
            if process_match and process_match.group(1) == package_name:
                crash_count += 1
            pending_fatal = False
        if "ANR in " in line and (package_name is None or f"ANR in {package_name}" in line):
            anr_count += 1
        if "StrictMode" in line:
            strict_mode_count += 1
        match = _PERFEVAL_EVENT_RE.search(line)
        if match:
            try:
                event = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                # flow marker 的 epoch（CM-TASK-021）：来自 logcat 行首时间戳，
                # 事件自带 tsEpochMs 时以事件为准
                line_epoch_ms = _line_epoch_ms(line)
                if line_epoch_ms is not None:
                    event.setdefault("tsEpochMs", line_epoch_ms)
                custom_events.append(event)

    return {
        "crash_count": crash_count,
        "anr_count": anr_count,
        "strict_mode_count": strict_mode_count,
        "custom_events": custom_events,
        "first_epoch_ms": first_epoch_ms,
    }


def parse_logcat_events(
    text: str,
    *,
    run_id: str,
    package_name: str | None = None,
    start_seq: int = 1,
) -> list[dict[str, Any]]:
    parsed = parse_logcat(text, package_name=package_name)
    events: list[dict[str, Any]] = []
    seq = start_seq
    for metric_name, parsed_key in (
        ("stability.crash.count", "crash_count"),
        ("stability.anr.count", "anr_count"),
        ("stability.strict_mode.count", "strict_mode_count"),
    ):
        events.append(
            metric_event(
                seq=seq,
                run_id=run_id,
                name=metric_name,
                value=parsed[parsed_key],
                unit="count",
                collector="logcat",
            )
        )
        seq += 1

    # 将 app 通过 PERFEVAL_EVENT 上报的 metric 标记转换为 timeline 事件
    for custom in parsed["custom_events"]:
        if not isinstance(custom, dict):
            continue
        if custom.get("type") != "metric":
            continue
        name = custom.get("name")
        value = custom.get("value")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            continue
        events.append(
            metric_event(
                seq=seq,
                run_id=run_id,
                name=name,
                value=value,
                unit=str(custom.get("unit") or ""),
                collector="logcat",
                evidence_level="L1",
                data={k: v for k, v in custom.items() if k not in {"type", "name", "value", "unit"}},
            )
        )
        seq += 1

    if parsed["crash_count"]:
        events.append(
            diagnostic_event(
                seq=seq,
                run_id=run_id,
                name="stability.crash.detected",
                severity="error",
                code="android.crash",
                message="FATAL EXCEPTION found in logcat",
                collector="logcat",
            )
        )
        seq += 1
    if parsed["anr_count"]:
        events.append(
            diagnostic_event(
                seq=seq,
                run_id=run_id,
                name="stability.anr.detected",
                severity="error",
                code="android.anr",
                message="ANR marker found in logcat",
                collector="logcat",
            )
        )
    return events
