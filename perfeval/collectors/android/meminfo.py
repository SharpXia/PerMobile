from __future__ import annotations

import re
from typing import Any

from ._events import metric_event


_KB_RE = re.compile(r"([A-Za-z ]+):\s*([0-9,]+)\s*$")


def _kb_to_mb(value: str) -> float:
    return round(float(value.replace(",", "")) / 1024.0, 3)


def parse_meminfo(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    in_app_summary = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "App Summary":
            in_app_summary = True
            continue
        if in_app_summary:
            match = _KB_RE.match(line)
            if not match:
                continue
            label = " ".join(match.group(1).lower().split())
            value_mb = _kb_to_mb(match.group(2))
            if label == "java heap":
                metrics["java_heap_mb"] = value_mb
            elif label == "native heap":
                metrics["native_heap_mb"] = value_mb
            elif label == "graphics":
                metrics["graphics_mb"] = value_mb
            elif label == "total":
                metrics["pss_mb"] = value_mb
            continue

        if line.startswith("TOTAL "):
            parts = line.split()
            if len(parts) >= 2 and parts[1].replace(",", "").isdigit():
                metrics.setdefault("pss_mb", _kb_to_mb(parts[1]))
    return metrics


def parse_meminfo_events(
    text: str,
    *,
    run_id: str,
    start_seq: int = 1,
    include_pss: bool = True,
    pss_semantic: str | None = None,
) -> list[dict[str, Any]]:
    parsed = parse_meminfo(text)
    events: list[dict[str, Any]] = []
    seq = start_seq
    mapping = [
        ("memory.pss.peak_mb", "pss_mb"),
        ("memory.java_heap.mb", "java_heap_mb"),
        ("memory.native_heap.mb", "native_heap_mb"),
        ("memory.graphics.mb", "graphics_mb"),
    ]
    for metric_name, parsed_key in mapping:
        if parsed_key not in parsed:
            continue
        # 周期采样已覆盖 PSS 序列时跳过结束快照，避免同一指标 ID 混入两种口径（CM-001 §5）
        if metric_name == "memory.pss.peak_mb" and not include_pss:
            continue
        extra = {"semantic": pss_semantic} if pss_semantic and metric_name == "memory.pss.peak_mb" else None
        events.append(
            metric_event(
                seq=seq,
                run_id=run_id,
                name=metric_name,
                value=parsed[parsed_key],
                unit="MB",
                collector="dumpsys.meminfo",
                data=extra,
            )
        )
        seq += 1
    return events

