from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from perfeval.core.config import write_json_document
from perfeval.core._stats import numeric_value_from_mapping
from perfeval.core.memory_profile import (
    DEFAULT_MEMORY_PROFILE,
    fail_abs_for_metric,
    normalize_budget_metrics,
    normalize_memory_profile,
    rating_thresholds_for_metric,
)

# 中文翻译映射（CSS 类名保持英文，仅翻译显示文本）
_STATUS_CN = {
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "unknown": "未知",
    "pass": "通过",
    "warn": "警告",
    "fail": "失败",
    "invalid": "无效",
}
_CONFIDENCE_CN = {"high": "高", "medium": "中", "low": "低"}
_SEVERITY_CN = {"error": "错误", "warning": "警告", "info": "信息"}


def _status_cn(value: str) -> str:
    return _STATUS_CN.get(str(value).lower().strip(), str(value))


def _confidence_cn(value: str) -> str:
    return _CONFIDENCE_CN.get(str(value).lower().strip(), str(value))


def _severity_cn(value: str) -> str:
    return _SEVERITY_CN.get(str(value).lower().strip(), str(value))


# ---------------------------------------------------------------------------
# 指标评级系统（差 / 中 / 良 / 优 + 参考范围）
# ---------------------------------------------------------------------------

# lower_better 指标的评级阈值: (优上限, 良上限, 中上限), 超过中上限为差
# 对于 count 类稳定性指标，0=优，>0=差
_METRIC_RATINGS: dict[str, tuple[float, float, float]] = {
    "startup.cold.ms": (800, 1500, 2500),
    "startup.warm.ms": (400, 800, 1500),
    "startup.hot.ms": (200, 400, 800),
    "frame.jank_rate": (0.03, 0.05, 0.10),
    "frame.slow_rate": (0.05, 0.10, 0.20),
    "frame.duration.p50.ms": (8, 16, 32),
    "frame.duration.p90.ms": (16, 32, 50),
    "frame.duration.p95.ms": (16, 32, 50),
    "frame.duration.p99.ms": (32, 50, 100),
    "memory.pss.peak_mb": (200, 400, 600),
    "memory.pss.mean_mb": (150, 300, 500),
    "memory.rss.peak_mb": (200, 400, 600),
    "stability.crash.count": (0, 0, 0),
    "stability.anr.count": (0, 0, 0),
    "stability.strict_mode.count": (0, 0, 0),
}

_RATING_LABELS = {
    "excellent": ("优", "#1f7a45"),
    "good": ("良", "#2a9d4f"),
    "fair": ("中", "#9a6500"),
    "poor": ("差", "#b42318"),
    "none": ("-", "#5d6a7a"),
}


def _rate_metric(
    metric_id: str,
    value: float,
    direction: str | None = None,
    *,
    budgets: Mapping[str, Any] | None = None,
    memory_profile: str = DEFAULT_MEMORY_PROFILE,
) -> tuple[str, str, str]:
    """返回 (评级文本, 参考范围文本, CSS类名)。"""
    thresholds = rating_thresholds_for_metric(metric_id, budgets=budgets, memory_profile=memory_profile)
    if thresholds is None:
        thresholds = _METRIC_RATINGS.get(metric_id)
    if thresholds is None:
        # 按前缀匹配
        for prefix, thresh in _METRIC_RATINGS.items():
            if metric_id.startswith(prefix):
                thresholds = thresh
                break
    if thresholds is None:
        return ("-", "无参考范围", "none")

    excellent, good, fair = thresholds
    excellent_text = _format_threshold_value(excellent)
    good_text = _format_threshold_value(good)
    fair_text = _format_threshold_value(fair)
    # 参考范围文本
    unit_hint = ""
    if "ms" in metric_id:
        unit_hint = "ms"
    elif "rate" in metric_id:
        unit_hint = ""
    elif "mb" in metric_id:
        unit_hint = "MB"
    elif "count" in metric_id:
        unit_hint = ""

    if unit_hint:
        range_text = f"优<{excellent_text} / 良<{good_text} / 中<{fair_text} / 差>={fair_text} ({unit_hint})"
    else:
        range_text = f"优<{excellent_text} / 良<{good_text} / 中<{fair_text} / 差>={fair_text}"

    if value <= excellent:
        return ("优", range_text, "excellent")
    if value <= good:
        return ("良", range_text, "good")
    if value <= fair:
        return ("中", range_text, "fair")
    return ("差", range_text, "poor")


def _format_threshold_value(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# 指标中文名称映射
# ---------------------------------------------------------------------------

_METRIC_NAMES_CN: dict[str, str] = {
    # 启动
    "startup.cold.ms": "冷启动耗时",
    "startup.warm.ms": "温启动耗时",
    "startup.hot.ms": "热启动耗时",
    # 帧率
    "frame.total_count": "总帧数",
    "frame.janky_count": "卡顿帧数",
    "frame.jank_rate": "卡顿率",
    "frame.slow_rate": "慢帧率",
    "frame.missed_vsync_count": "丢VSync次数",
    "frame.slow_ui_thread_count": "UI线程慢帧数",
    "frame.duration.detail.ms": "逐帧渲染耗时",
    "frame.fps.mean": "平均帧率",
    "frame.fps.median": "中位帧率",
    "frame.duration.p50.ms": "帧耗时P50",
    "frame.duration.p90.ms": "帧耗时P90",
    "frame.duration.p95.ms": "帧耗时P95",
    "frame.duration.p99.ms": "帧耗时P99",
    "frame.duration.mean.ms": "平均帧耗时",
    "frame.duration.max.ms": "最大帧耗时",
    # 内存
    "memory.pss.peak_mb": "PSS峰值内存",
    "memory.pss.mean_mb": "PSS平均内存",
    "memory.rss.peak_mb": "RSS峰值内存",
    "memory.java_heap.mb": "Java堆内存",
    # 稳定性
    "stability.crash.count": "崩溃次数",
    "stability.anr.count": "ANR次数",
    "stability.strict_mode.count": "StrictMode违规次数",
}


def _metric_name_cn(metric_id: str) -> str:
    """返回指标的中文显示名，未知指标按前缀推断或返回原 ID。"""
    if metric_id in _METRIC_NAMES_CN:
        return _METRIC_NAMES_CN[metric_id]
    # 按前缀推断
    if metric_id.startswith("startup."):
        return "启动指标"
    if metric_id.startswith("frame.duration."):
        return "帧耗时指标"
    if metric_id.startswith("frame."):
        return "帧率指标"
    if metric_id.startswith("memory."):
        return "内存指标"
    if metric_id.startswith("stability."):
        return "稳定性指标"
    if metric_id.startswith("cpu."):
        return "CPU指标"
    return metric_id


# ---------------------------------------------------------------------------
# 场景中文名称映射
# ---------------------------------------------------------------------------

_SCENARIO_NAMES_CN: dict[str, str] = {
    "android_fixture_startup": "Android 启动性能测试",
    "startup_home": "启动性能测试",
    "startup_synthetic": "启动性能测试（合成数据）",
}


def _scenario_name_cn(scenario_id: str) -> str:
    """返回场景的中文名称，未知场景将 ID 转为可读文本。"""
    if scenario_id in _SCENARIO_NAMES_CN:
        return _SCENARIO_NAMES_CN[scenario_id]
    # 将下划线/连字符分隔的 ID 转为可读文本
    readable = scenario_id.replace("_", " ").replace("-", " ").strip().title()
    return readable if readable else scenario_id


@dataclass(frozen=True)
class SummaryMetricRow:
    metric_id: str
    value: float
    unit: str
    confidence: str
    evidence: str
    sample_count: int
    outlier_ratio: float
    source_events: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SummaryArtifact:
    artifact_id: str
    path: str
    href: str
    collector: str
    size_bytes: int | None
    related_metrics: list[str]
    category: str
    present: bool
    note: str


@dataclass(frozen=True)
class DiagnosisReportData:
    mode: str
    modes: list[str]
    duration_seconds: int | None
    serial: str
    created_at_utc: str
    memory_profile: str
    first_party_regex: str
    diagnostics: list[str]
    artifacts: list[str]
    cpu_status: str
    trace_status: str
    frame_status: str
    simpleperf_excerpt: list[str]
    # simpleperf stat 卡 + 火焰图树数据面（parse_simpleperf_report_text 输出原样；
    # 无可用 report → None。报告面与后端 deep_analysis 数据面共用同一解析）
    simpleperf_face: dict[str, Any] | None = None


def build_summary_report(
    summary: Mapping[str, Any],
    diagnosis: Mapping[str, Any] | None = None,
    *,
    base_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
) -> str:
    report = _normalize_summary(summary, diagnosis=diagnosis, base_dir=base_dir, report_dir=report_dir)
    return _render_html(report)


def write_summary_report(
    summary: Mapping[str, Any],
    output_path: str | Path,
    diagnosis: Mapping[str, Any] | None = None,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    discovered = diagnosis or _load_diagnosis_if_present(path.parent)
    path.write_text(
        build_summary_report(summary, discovered, base_dir=path.parent.parent, report_dir=path.parent),
        encoding="utf-8",
    )
    return path


def write_summary_report_bundle(
    summary: Mapping[str, Any],
    output_dir: str | Path,
    diagnosis: Mapping[str, Any] | None = None,
) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "report_html": directory / "report.html",
        "summary_json": directory / "summary.json",
    }
    discovered = diagnosis or _load_diagnosis_if_present(directory.parent)
    report_data = _normalize_summary(
        summary, diagnosis=discovered, base_dir=directory.parent, report_dir=directory
    )
    outputs["report_html"].write_text(_render_html(report_data), encoding="utf-8")
    write_json_document(outputs["summary_json"], summary)
    # 帧耗时分布图初版（CM-TASK-010）、连续指标曲线（CM-TASK-015）与 simpleperf
    # 火焰图（flamegraph 缺口，2026-11-10）：有数据时随包分发离线 ECharts
    diagnosis_data = report_data.diagnosis
    has_flamegraph = diagnosis_data is not None and bool(
        (diagnosis_data.simpleperf_face or {}).get("tree")
    )
    if _has_frame_distribution(summary) or _has_stream_series(summary) or has_flamegraph:
        vendor_src = Path(__file__).parent / "vendor" / "echarts.min.js"
        echarts_dst = directory / "echarts.min.js"
        if vendor_src.exists() and vendor_src.is_file():
            shutil.copyfile(vendor_src, echarts_dst)
        else:
            echarts_dst.write_text("/* echarts.min.js missing */", encoding="utf-8")
    return outputs


@dataclass(frozen=True)
class SummaryReportData:
    run_id: str
    scenario_id: str
    status: str
    package_name: str
    platform: str
    memory_profile: str
    budgets: dict[str, dict[str, Any]]
    metric_count: int
    sample_min: int
    low_confidence_count: int
    crash_count: float
    anr_count: float
    strict_mode_count: float
    artifact_count: int
    diagnostics_count: int
    metrics: list[SummaryMetricRow]
    metric_groups: dict[str, list[SummaryMetricRow]]
    artifacts: list[SummaryArtifact]
    diagnosis: DiagnosisReportData | None
    raw_summary: Mapping[str, Any]


def _normalize_summary(
    summary: Mapping[str, Any],
    *,
    diagnosis: Mapping[str, Any] | None = None,
    base_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
) -> SummaryReportData:
    metrics = _normalize_metrics(summary)
    sample_min = min((row.sample_count for row in metrics), default=0)
    low_confidence_count = sum(1 for row in metrics if row.confidence == "low")
    metric_groups: dict[str, list[SummaryMetricRow]] = defaultdict(list)
    for row in metrics:
        metric_groups[_metric_group(row.metric_id)].append(row)

    run_id = _coerce_text(summary.get("runId") or summary.get("run_id") or "run")
    scenario_id = _coerce_text(summary.get("scenarioId") or _scenario_id_from_summary(summary) or "unknown")
    status = _coerce_text(summary.get("status") or "unknown")
    app = summary.get("app") if isinstance(summary.get("app"), Mapping) else {}
    package_name = _coerce_text(app.get("packageName") if isinstance(app, Mapping) else "unknown")
    platform = _coerce_text(app.get("platform") if isinstance(app, Mapping) else summary.get("platform") or "unknown")
    normalized_diagnosis = _normalize_diagnosis(diagnosis, metrics, base_dir=base_dir)
    raw_memory_profile = summary.get("memoryProfile")
    if (raw_memory_profile is None or raw_memory_profile == "") and normalized_diagnosis is not None:
        raw_memory_profile = normalized_diagnosis.memory_profile
    memory_profile, _profile_diagnostic = normalize_memory_profile(raw_memory_profile)
    budgets = normalize_budget_metrics(summary.get("budgets"))
    artifacts = _collect_artifacts(
        summary,
        metrics,
        diagnosis=normalized_diagnosis,
        base_dir=base_dir,
        report_dir=report_dir,
    )
    diagnostics = summary.get("diagnostics")
    diagnostics_count = len(diagnostics) if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, (str, bytes, bytearray)) else 0

    crash_count = _metric_value(summary, "stability.crash.count")
    anr_count = _metric_value(summary, "stability.anr.count")
    strict_mode_count = _metric_value(summary, "stability.strict_mode.count")

    return SummaryReportData(
        run_id=run_id,
        scenario_id=scenario_id,
        status=status,
        package_name=package_name,
        platform=platform,
        memory_profile=memory_profile,
        budgets=budgets,
        metric_count=len(metrics),
        sample_min=sample_min,
        low_confidence_count=low_confidence_count,
        crash_count=crash_count,
        anr_count=anr_count,
        strict_mode_count=strict_mode_count,
        artifact_count=len(artifacts),
        diagnostics_count=diagnostics_count,
        metrics=metrics,
        metric_groups=dict(metric_groups),
        artifacts=artifacts,
        diagnosis=normalized_diagnosis,
        raw_summary=summary,
    )


def _normalize_metrics(summary: Mapping[str, Any]) -> list[SummaryMetricRow]:
    raw_metrics = summary.get("metrics")
    if not isinstance(raw_metrics, Mapping):
        return []

    rows: list[SummaryMetricRow] = []
    for metric_id, metric_data in raw_metrics.items():
        if not isinstance(metric_data, Mapping):
            continue
        value = numeric_value_from_mapping(metric_data)
        if value is None:
            continue
        source_events = metric_data.get("sourceEvents") or []
        if isinstance(source_events, str):
            source_events = [source_events]
        artifact_refs = metric_data.get("artifactRefs") or []
        if isinstance(artifact_refs, str):
            artifact_refs = [artifact_refs]
        rows.append(
            SummaryMetricRow(
                metric_id=str(metric_id),
                value=float(value),
                unit=_coerce_text(metric_data.get("unit") or "-"),
                confidence=_coerce_text(metric_data.get("confidence") or "low"),
                evidence=_coerce_text(metric_data.get("evidenceLevel") or metric_data.get("evidence") or "-"),
                sample_count=int(metric_data.get("n") or 0),
                outlier_ratio=float(metric_data.get("outlierRatio") or 0.0),
                source_events=[str(item) for item in source_events if isinstance(item, str)],
                artifact_refs=[str(item) for item in artifact_refs if isinstance(item, str)],
            )
        )
    rows.sort(key=lambda row: (_group_order(_metric_group(row.metric_id)), row.metric_id))
    return rows


def _load_diagnosis_if_present(directory: Path) -> Mapping[str, Any] | None:
    candidates = (directory / "diagnosis.json", directory.parent / "diagnosis.json")
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping):
            return payload
    return None


def _normalize_diagnosis(
    diagnosis: Mapping[str, Any] | None,
    metrics: Sequence[SummaryMetricRow],
    *,
    base_dir: str | Path | None = None,
) -> DiagnosisReportData | None:
    if not isinstance(diagnosis, Mapping):
        return None

    raw_modes = diagnosis.get("modes")
    if isinstance(raw_modes, str):
        modes = [raw_modes]
    elif isinstance(raw_modes, Sequence) and not isinstance(raw_modes, (bytes, bytearray)):
        modes = [str(item) for item in raw_modes if isinstance(item, str)]
    else:
        modes = []
    mode = _coerce_text(diagnosis.get("mode") or ("/".join(modes) if modes else "unknown"))

    raw_diagnostics = diagnosis.get("diagnostics")
    if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, (str, bytes, bytearray)):
        diagnostics = [str(item) for item in raw_diagnostics]
    elif raw_diagnostics:
        diagnostics = [str(raw_diagnostics)]
    else:
        diagnostics = []

    raw_artifacts = diagnosis.get("artifacts")
    if isinstance(raw_artifacts, Sequence) and not isinstance(raw_artifacts, (str, bytes, bytearray)):
        artifacts = [str(item) for item in raw_artifacts if isinstance(item, str)]
    else:
        artifacts = []

    duration = diagnosis.get("durationSeconds")
    duration_seconds = int(duration) if isinstance(duration, int) and not isinstance(duration, bool) else None
    simpleperf_excerpt = _read_simpleperf_excerpt(artifacts, base_dir=base_dir)
    simpleperf_face = _simpleperf_face_from_artifacts(artifacts, base_dir=base_dir)
    memory_profile, _profile_diagnostic = normalize_memory_profile(diagnosis.get("memoryProfile"))

    return DiagnosisReportData(
        mode=mode,
        modes=modes,
        duration_seconds=duration_seconds,
        serial=_coerce_text(diagnosis.get("serial") or "unknown"),
        created_at_utc=_coerce_text(diagnosis.get("createdAtUtc") or "unknown"),
        memory_profile=memory_profile,
        first_party_regex=_coerce_text(diagnosis.get("firstPartyRegex") or "-"),
        diagnostics=diagnostics,
        artifacts=artifacts,
        cpu_status=_diagnosis_mode_status("cpu", artifacts, diagnostics, metrics, base_dir=base_dir),
        trace_status=_diagnosis_mode_status("trace", artifacts, diagnostics, metrics, base_dir=base_dir),
        frame_status=_diagnosis_mode_status("frame", artifacts, diagnostics, metrics, base_dir=base_dir),
        simpleperf_excerpt=simpleperf_excerpt,
        simpleperf_face=simpleperf_face,
    )


def _collect_artifacts(
    summary: Mapping[str, Any],
    metrics: Sequence[SummaryMetricRow],
    *,
    diagnosis: DiagnosisReportData | None,
    base_dir: str | Path | None,
    report_dir: str | Path | None,
) -> list[SummaryArtifact]:
    seen: set[str] = set()
    rows: list[SummaryArtifact] = []
    invalid_artifact_names: set[str] = set()
    if diagnosis is not None:
        if any(_returncode_failed(item, "simpleperf.pull.returncode") for item in diagnosis.diagnostics):
            invalid_artifact_names.add("perf.data")

    def add(
        path: str,
        *,
        artifact_id: str = "",
        collector: str = "",
        size_bytes: int | None = None,
        related_metrics: Sequence[str] | None = None,
    ) -> None:
        if not path or path in seen:
            return
        seen.add(path)
        resolved = _resolve_artifact_path(path, base_dir=base_dir)
        stat_size = resolved.stat().st_size if resolved is not None and resolved.exists() and resolved.is_file() else None
        actual_size = size_bytes if size_bytes is not None else stat_size
        invalid = Path(path).name in invalid_artifact_names
        present = bool(resolved is not None and resolved.exists() and resolved.is_file() and not invalid)
        if invalid:
            note = "本次采集失败，忽略可能残留的旧文件"
        else:
            note = "可用" if present and (actual_size is None or actual_size > 0) else "缺失或空文件"
        rows.append(
            SummaryArtifact(
                artifact_id=artifact_id or Path(path).stem.replace("-", "_") or path,
                path=path,
                href=_artifact_href(path, resolved=resolved, report_dir=report_dir),
                collector=collector or _collector_for_artifact_path(path),
                size_bytes=actual_size,
                related_metrics=[str(item) for item in (related_metrics or []) if isinstance(item, str)],
                category=_artifact_category(path, collector),
                present=present,
                note=note,
            )
        )

    artifacts = summary.get("artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes, bytearray)):
        for artifact in artifacts:
            if isinstance(artifact, Mapping):
                path = artifact.get("path")
                related = []
                metric_data = artifact.get("metricData")
                if isinstance(metric_data, Mapping):
                    raw_related = metric_data.get("relatedMetrics")
                    if isinstance(raw_related, Sequence) and not isinstance(raw_related, (str, bytes, bytearray)):
                        related = [str(item) for item in raw_related if isinstance(item, str)]
                if isinstance(path, str):
                    size = artifact.get("sizeBytes")
                    add(
                        path,
                        artifact_id=str(artifact.get("artifactId") or ""),
                        collector=str(artifact.get("collector") or ""),
                        size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
                        related_metrics=related,
                    )
    for row in metrics:
        for ref in row.artifact_refs:
            add(ref, related_metrics=[row.metric_id])
    if diagnosis is not None:
        for path in diagnosis.artifacts:
            add(path)
    rows.sort(key=lambda row: (_artifact_group_order(row.category), row.path))
    return rows


def _resolve_artifact_path(path: str, *, base_dir: str | Path | None) -> Path | None:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if base_dir is not None:
        scoped = Path(base_dir) / candidate
        if scoped.exists():
            return scoped.resolve()
    if candidate.exists():
        return candidate.resolve()
    return candidate


def _artifact_href(path: str, *, resolved: Path | None, report_dir: str | Path | None) -> str:
    if resolved is None or report_dir is None:
        return path
    if not resolved.exists():
        return path
    try:
        return os.path.relpath(str(resolved), start=str(Path(report_dir).resolve()))
    except ValueError:
        return path


def _artifact_category(path: str, collector: str = "") -> str:
    text = f"{path} {collector}".lower()
    if "simpleperf" in text or "perf.data" in text:
        return "cpu"
    if "perfetto" in text or text.endswith(".pftrace") or ".pftrace" in text:
        return "trace"
    if "gfxinfo" in text or "framestats" in text:
        return "frame"
    if "meminfo" in text:
        return "memory"
    if "logcat" in text:
        return "stability"
    return "other"


def _artifact_group_order(group: str) -> int:
    return {"frame": 0, "cpu": 1, "trace": 2, "memory": 3, "stability": 4, "other": 5}.get(group, 99)


def _collector_for_artifact_path(path: str) -> str:
    name = Path(path).name.lower()
    if "simpleperf" in name or name == "perf.data":
        return "simpleperf"
    if "perfetto" in name or name.endswith(".pftrace"):
        return "perfetto"
    if name.startswith("gfxinfo"):
        return "dumpsys.gfxinfo"
    if name.startswith("meminfo"):
        return "dumpsys.meminfo"
    if name.startswith("logcat"):
        return "logcat"
    return "android"


def _diagnosis_mode_status(
    mode: str,
    artifacts: Sequence[str],
    diagnostics: Sequence[str],
    metrics: Sequence[SummaryMetricRow],
    *,
    base_dir: str | Path | None = None,
) -> str:
    if mode == "frame":
        has_frame_metric = any(row.metric_id.startswith("frame.") for row in metrics)
        return "pass" if has_frame_metric else "warn"
    if mode == "cpu":
        if any(_returncode_failed(item, "simpleperf.record.returncode") for item in diagnostics):
            return "fail"
        if any(_returncode_failed(item, "simpleperf.pull.returncode") for item in diagnostics):
            return "fail"
        has_perf_data = _artifact_exists(artifacts, "perf.data", base_dir=base_dir)
        has_report = _simpleperf_report_has_samples(artifacts, base_dir=base_dir)
        return "pass" if has_perf_data and has_report else "warn"
    if mode == "trace":
        if any(_returncode_failed(item, "perfetto.start.returncode") for item in diagnostics):
            return "fail"
        if any(_returncode_failed(item, "perfetto.pull.returncode") for item in diagnostics):
            return "fail"
        has_trace = any(_artifact_exists(artifacts, Path(path).name, base_dir=base_dir) for path in artifacts if path.endswith(".pftrace"))
        return "pass" if has_trace else "warn"
    return "unknown"


def _returncode_failed(message: str, code: str) -> bool:
    return code in message and ".returncode:" in message and not message.endswith(":0")


def _artifact_exists(artifacts: Sequence[str], filename: str, *, base_dir: str | Path | None) -> bool:
    for path in artifacts:
        if Path(path).name != filename:
            continue
        resolved = _resolve_artifact_path(path, base_dir=base_dir)
        if resolved is not None and resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0:
            return True
    return False


def _simpleperf_report_has_samples(artifacts: Sequence[str], *, base_dir: str | Path | None) -> bool:
    for path in artifacts:
        if Path(path).name != "simpleperf-report.txt":
            continue
        resolved = _resolve_artifact_path(path, base_dir=base_dir)
        if resolved is None or not resolved.exists() or not resolved.is_file() or resolved.stat().st_size <= 0:
            continue
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "failed to open record file" in text or "No such file or directory" in text:
            return False
        if "Samples:" in text or "Overhead" in text or "Children" in text:
            return True
    return False


def _read_simpleperf_excerpt(artifacts: Sequence[str], *, base_dir: str | Path | None) -> list[str]:
    report_paths = [path for path in artifacts if Path(path).name == "simpleperf-report.txt"]
    stderr_paths = [path for path in artifacts if Path(path).name == "simpleperf-record-stderr.txt"]
    for path in report_paths:
        resolved = _resolve_artifact_path(path, base_dir=base_dir)
        if resolved is not None:
            sibling = resolved.parent / "simpleperf-record-stderr.txt"
            if sibling.exists():
                stderr_paths.append(str(sibling))

    collected: list[str] = []
    seen: set[str] = set()
    for path in [*report_paths, *stderr_paths]:
        resolved = _resolve_artifact_path(path, base_dir=base_dir)
        if resolved is None or not resolved.exists() or not resolved.is_file():
            continue
        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in (line.rstrip() for line in lines if line.strip()):
            if line in seen:
                continue
            seen.add(line)
            collected.append(line)
            if len(collected) >= 12:
                return collected
    return collected


# ---------------------------------------------------------------------------
# simpleperf report 解析 → stat 卡 + 火焰图树（flamegraph-compare-missing 缺口，
# 2026-11-10）。**单一实现**：报告面（本文件渲染）与后端数据面（services/backend
# deep_analysis.py 经 perfeval 同仓库 import）共用本解析，不复制第二份。
# 格式基准 = simpleperf report 实测输出（emulator-5554 simpleperf record/report，
# 2026-11-10 抓取）：flat 表（缺省）与 -g 简报调用树两种，见 parse 函数 docstring。
# ---------------------------------------------------------------------------

SIMPLEPERF_REPORT_FILE = "simpleperf-report.txt"

_CALLGRAPH_BRANCH_RE = re.compile(r"--\s*(\d+(?:\.\d+)?)%--\s*(.*)$")
_EVENT_NAME_SPLIT_RE = re.compile(r"\s*\(")


def _parse_int_tail(line: str) -> int | None:
    """'Samples: 4' / 'Event count: 8000000' → 尾部整数；非数字返回 None（不猜）。"""
    tail = line.split(":", 1)[-1].strip()
    return int(tail) if tail.isdigit() else None


def parse_simpleperf_report_text(text: str) -> dict[str, Any]:
    """simpleperf report 文本 → stat 卡 + 火焰图树数据。

    两种实测格式：
    - **flat 表**（``simpleperf report`` 缺省）：``Overhead Command Pid Tid Shared
      Object Symbol`` 行 → 分组树 ``Command→SharedObject→Symbol``（落盘 report
      无调用栈，以 overhead 分布树呈现，``basis`` 如实标注）；
    - **callgraph 表**（``simpleperf report -g``）：``Children Self ...`` 行 +
      缩进 ``|--pct%-- symbol`` 简报调用树 → 分支按缩进成树（``--pct%--`` 起始列
      即深度键），链上无百分比的延续帧挂 value=0 叶节点（``basis`` 如实标注
      「简报调用树近似层级」）。-g 报告每个表 entry 各带一棵 entry 内相对百分比的
      简报树（--children 按 Children% 降序排列）——成树取**首个含调用树块的 entry**
      （即累计占比最高的链，覆盖全样本），其余 entry 只进 entryCount 统计不做树
      （避免重叠 entry 重复计入同样本）。

    返回 ``{"arch", "events", "mode", "basis", "entryCount", "maxDepth", "tree",
    "unparsedLines"}``；``tree`` 为 ``{"name","value","children"}`` 嵌套节点
    （value 单位 = 该事件 overhead%），无任何可解析表行时为 None。多事件报告
    取首个产出表行的事件块成树（单事件为 diagnose 采集常态，多事件成树留待按需）。
    """
    arch: str | None = None
    events: list[dict[str, Any]] = []
    flat_rows: list[dict[str, Any]] = []
    cg_roots: list[dict[str, Any]] = []
    mode = "flat"
    header_cols = 0  # 当前表列数（6=flat / 7=children+self）；0=尚未见到表头
    unparsed = 0
    row_collection_open = True  # 多事件报告：树只取首个事件块（events 统计照常收集）
    stack: list[tuple[int, dict[str, Any]]] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            stack.clear()
            continue
        if stripped.startswith("Arch:"):
            arch = stripped.split(":", 1)[1].strip()
            header_cols = 0
            continue
        if stripped.startswith("Event:"):
            name = _EVENT_NAME_SPLIT_RE.split(stripped.split(":", 1)[1].strip(), 1)[0].strip()
            row_collection_open = not events  # 第二个 Event: 起关闭行收集
            events.append({"event": name, "samples": None, "eventCount": None})
            header_cols = 0
            continue
        if stripped.startswith("Samples:"):
            if events:
                events[-1]["samples"] = _parse_int_tail(stripped)
            continue
        if stripped.startswith("Event count:"):
            if events:
                events[-1]["eventCount"] = _parse_int_tail(stripped)
            continue
        if stripped.startswith("Overhead"):
            header_cols = 6  # Overhead + Command/Pid/Tid/SharedObject/Symbol（缺省 sort）
            continue
        if stripped.startswith("Children") and "Self" in stripped.split():
            header_cols = 7  # Children + Self + Command/Pid/Tid/SharedObject/Symbol（-g）
            mode = "callgraph"
            stack.clear()
            continue
        if stripped.startswith("Children"):
            header_cols = 6  # --children 无 Self：列数同 flat（未实测，按 flat 容错）
            mode = "callgraph"
            stack.clear()
            continue

        if not row_collection_open:
            continue

        if header_cols == 6:
            parts = stripped.split(None, 5)
            if (
                len(parts) == 6
                and parts[0].endswith("%")
                and parts[2].isdigit()
                and parts[3].isdigit()
            ):
                try:
                    pct = float(parts[0][:-1])
                except ValueError:
                    pct = None
                if pct is not None:
                    flat_rows.append({
                        "overheadPct": pct,
                        "command": parts[1],
                        "pid": int(parts[2]),
                        "tid": int(parts[3]),
                        "dso": parts[4],
                        "symbol": parts[5],
                    })
                    continue
            unparsed += 1
            continue

        if header_cols == 7 and not (line[:1] in {" ", "|"}):
            parts = stripped.split(None, 6)
            if (
                len(parts) == 7
                and parts[0].endswith("%")
                and parts[3].isdigit()
                and parts[4].isdigit()
            ):
                try:
                    pct = float(parts[0][:-1])
                except ValueError:
                    pct = None
                if pct is not None:
                    node: dict[str, Any] = {"name": parts[6], "value": pct, "children": []}
                    cg_roots.append(node)
                    stack = [(-1, node)]
                    continue
            unparsed += 1
            continue

        # -g 简报调用树块（缩进行）；无 -g 表时跳过（不误吞 flat 无关行）
        if header_cols == 7 and stack:
            branch = _CALLGRAPH_BRANCH_RE.search(line)
            if branch is not None:
                indent = branch.start()
                pct = float(branch.group(1))
                node = {"name": branch.group(2).strip(), "value": pct, "children": []}
                while len(stack) > 1 and stack[-1][0] >= indent:
                    stack.pop()
                stack[-1][1]["children"].append(node)
                stack.append((indent, node))
                continue
            if set(stripped) <= {"|", " "}:
                continue  # 纯连接线
            frame_name = stripped.lstrip("| ").removeprefix("-- ").strip()
            if frame_name == "[skipped in brief callgraph mode]":
                continue  # 简报模式截断标记（实测输出），不成帧
            if frame_name and not frame_name.startswith(("%",)):
                stack[-1][1]["children"].append(
                    {"name": frame_name, "value": 0.0, "children": []}
                )
            continue

    tree: dict[str, Any] | None = None
    if flat_rows:
        tree = _flat_tree(flat_rows)
    elif cg_roots:
        # -g：取首个含调用树块的 entry 成树（--children 首行 = 累计占比最高链）
        tree = next((node for node in cg_roots if node["children"]), None)

    basis = (
        "flat:overhead 分布树（落盘 report 无调用栈，Command→SharedObject→Symbol 分组呈现）"
        if mode == "flat"
        else "callgraph:简报调用树近似层级（--pct%-- 分支按缩进成树，链上延续帧挂 value=0 叶）"
    )
    return {
        "arch": arch,
        "events": events,
        "mode": mode,
        "basis": basis,
        "entryCount": len(flat_rows) + len(cg_roots),
        "maxDepth": _tree_depth(tree) if tree is not None else 0,
        "tree": tree,
        "unparsedLines": unparsed,
    }


def _flat_tree(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """flat 表行 → Command→SharedObject→Symbol 三层分布树（value = overhead% 累加）。"""
    root: dict[str, Any] = {
        "name": "ALL",
        "value": round(sum(row["overheadPct"] for row in rows), 3),
        "children": [],
    }
    by_command: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["command"])
        if key not in by_command:
            by_command[key] = {"name": key, "value": 0.0, "children": []}
            root["children"].append(by_command[key])
        command = by_command[key]
        command["value"] = round(command["value"] + float(row["overheadPct"]), 3)
        by_dso = next(
            (child for child in command["children"] if child["name"] == row["dso"]),
            None,
        )
        if by_dso is None:
            by_dso = {"name": str(row["dso"]), "value": 0.0, "children": []}
            command["children"].append(by_dso)
        by_dso["value"] = round(by_dso["value"] + float(row["overheadPct"]), 3)
        by_dso["children"].append(
            {
                "name": str(row["symbol"]),
                "value": float(row["overheadPct"]),
                "children": [],
            }
        )
    return root


def _tree_depth(node: Mapping[str, Any] | None) -> int:
    if node is None:
        return 0
    children = node.get("children")
    if not isinstance(children, Sequence) or isinstance(children, (str, bytes, bytearray)) or not children:
        return 1
    return 1 + max(_tree_depth(child) for child in children)


def _simpleperf_face_from_artifacts(
    artifacts: Sequence[str],
    *,
    base_dir: str | Path | None,
) -> dict[str, Any] | None:
    """diagnosis artifacts → simpleperf 数据面（stat 卡 + 火焰图树）；无可用 report → None。"""
    for path in artifacts:
        if Path(path).name != SIMPLEPERF_REPORT_FILE:
            continue
        resolved = _resolve_artifact_path(path, base_dir=base_dir)
        if resolved is None or not resolved.exists() or not resolved.is_file():
            continue
        if resolved.stat().st_size <= 0:
            continue
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "failed to open record file" in text or "No such file or directory" in text:
            continue  # 失败占位 report（cpu 状态判定同源），不成 face
        face = parse_simpleperf_report_text(text)
        if face["entryCount"] or face["events"]:
            return face
    return None


def _metric_value(summary: Mapping[str, Any], metric_id: str) -> float:
    raw_metrics = summary.get("metrics")
    if not isinstance(raw_metrics, Mapping):
        return 0.0
    metric = raw_metrics.get(metric_id)
    if not isinstance(metric, Mapping):
        return 0.0
    value = numeric_value_from_mapping(metric)
    return float(value) if value is not None else 0.0


def _metric_group(metric_id: str) -> str:
    if metric_id.startswith("startup."):
        return "startup"
    if metric_id.startswith("frame.duration."):
        return "frame_duration"
    if metric_id.startswith("frame."):
        return "frame_quality"
    if metric_id.startswith("memory."):
        return "memory"
    if metric_id.startswith("stability."):
        return "stability"
    return "other"


def _group_order(group: str) -> int:
    return {
        "startup": 0,
        "frame_duration": 1,
        "frame_quality": 2,
        "memory": 3,
        "stability": 4,
        "other": 5,
    }.get(group, 99)


def _scenario_id_from_summary(summary: Mapping[str, Any]) -> str | None:
    scenario = summary.get("scenario")
    if isinstance(scenario, Mapping):
        value = scenario.get("scenarioId") or scenario.get("id")
        if isinstance(value, str) and value:
            return value
    value = summary.get("scenarioId")
    if isinstance(value, str) and value:
        return value
    return None


def _frame_fps_value(summary: Mapping[str, Any], metric_id: str) -> float | None:
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    entry = metrics.get(metric_id)
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _has_frame_distribution(summary: Mapping[str, Any]) -> bool:
    distribution = summary.get("frameDurationDistribution")
    return isinstance(distribution, Sequence) and not isinstance(distribution, (str, bytes, bytearray)) and bool(distribution)


# ---------------------------------------------------------------------------
# 口径声明横幅（CM-TASK-023，REPORT_PRESENTATION §2.1 / roadmap §1 原则 5）
# 文案事实源：requirements/CAPABILITY_SCOPE.md §4、METRIC_SOURCES §3、
# SAMPLING_FRAMEWORK §5。静态 HTML（无 JS）、无外链，任何 summary（含旧数据）
# 都固定渲染；仅"本次 run 实际生效"一行按 fpsSource 动态标注。
# ---------------------------------------------------------------------------

_FPS_SOURCE_LABELS = {"tp": "Perfetto frame timeline", "framestats": "gfxinfo framestats", "aggregate": "聚合统计"}

_SCOPE_BANNER_ITEMS: tuple[tuple[str, str], ...] = (
    ("FPS", "由逐帧渲染耗时反推（1000 / frame_duration_ms，来源 gfxinfo framestats 或 Perfetto frame timeline），"
     "非合成器帧率，非 PerfDog 独有算法，绝对值不可直接对标。"),
    ("掉帧（jank 帧）", "单帧耗时 > 刷新周期 1.5×（阈值可配），与 PerfDog 卡顿判定链定义不同。"),
    ("CPU", "/proc 两次采样差分并按整机容量归一（含全部线程），采样频率与归一化基数与 PerfDog 不同。"),
    ("网络", "/proc/net/xt_qtaguid 按 uid 差分（kbps，与图中单位一致），降级走 dumpsys netstats 总量差分。"),
    ("功耗", "电池电流（current_now）采样均值 + 估算能耗（mAh，估算值），与 PerfDog 外接功耗仪口径不可比。"),
    ("温度 / GPU", "dumpsys thermalservice 温区温度 / kgsl busy 差分（仅 Qualcomm 设备有效，OEM 差异大）。"),
    ("内存峰值", "memory.pss.peak_mb 周期采样时为序列真实峰值；回退结束快照时以 final_snapshot 事件标注（非全程峰值）。"),
    ("隐私", "录屏与完整 trace 仅保留在本地 artifact 目录，不上传 LLM、不进入对比摘要；"
     "本报告为离线 HTML（ECharts 内联），数据不出本地。"),
)


def _render_scope_banner(report: SummaryReportData) -> str:
    """口径声明横幅：报告顶部（标题下第一区块）固定渲染的静态 HTML。"""
    fps_raw = str(report.raw_summary.get("fpsSource") or "").strip()
    fps_label = _FPS_SOURCE_LABELS.get(fps_raw, fps_raw) if fps_raw else "未标注（无逐帧来源）"
    items_html = "".join(
        f"<li><strong>{html.escape(name)}</strong>：{html.escape(text)}</li>"
        for name, text in _SCOPE_BANNER_ITEMS
    )
    return (
        "<section class=\"section scope-banner\">"
        "<h2>口径声明</h2>"
        "<p class=\"muted\">以下指标为自建采集口径，与 PerfDog 等商业工具口径不可直接对标；"
        "跨工具对比前请先阅读本声明。</p>"
        f"<ul class=\"scope-list\">{items_html}</ul>"
        f"<p class=\"muted\">本次 run 实际生效：FPS 来源：{html.escape(fps_label)}。</p>"
        "</section>"
    )


def _render_frame_distribution(report: SummaryReportData) -> tuple[str, str]:
    """帧耗时分布图初版（CM-TASK-010）：直方图 + FPS / 掉帧区间口径说明。

    数据源为 summary 根字段 frameDurationDistribution / jankSegments / fpsSource
    与 frame.fps.mean/median 指标（engine FPS 派生层产出）；聚合兜底（无逐帧
    来源）时不渲染本区块。返回 (区块 HTML, 图表脚本 HTML)，均为最小新增区块。
    """
    summary = report.raw_summary
    if not _has_frame_distribution(summary):
        return "", ""
    distribution = [
        dict(item)
        for item in summary["frameDurationDistribution"]
        if isinstance(item, Mapping)
    ]
    if not distribution:
        return "", ""

    fps_mean = _frame_fps_value(summary, "frame.fps.mean")
    fps_median = _frame_fps_value(summary, "frame.fps.median")
    fps_source = _coerce_text(summary.get("fpsSource") or "-")
    jank_segments = [
        dict(item) for item in (summary.get("jankSegments") or []) if isinstance(item, Mapping)
    ]

    caption_parts = [f"FPS 来源：{_FPS_SOURCE_LABELS.get(fps_source, fps_source)}"]
    if fps_mean is not None:
        caption_parts.append(f"平均帧率 {_format_number(fps_mean)} fps")
    if fps_median is not None:
        caption_parts.append(f"中位帧率 {_format_number(fps_median)} fps")
    caption = " · ".join(caption_parts)

    segment_text = ""
    if jank_segments:
        ranges = "、".join(
            f"#{int(item.get('startIndex', 0))}–#{int(item.get('endIndex', 0))}"
            for item in jank_segments
        )
        segment_text = (
            f"<p class=\"muted\" style=\"margin-top: 10px;\">掉帧区间（连续 ≥3 个 jank 帧，"
            f"共 {len(jank_segments)} 段）：{html.escape(ranges)}</p>"
        )

    chart_json = json.dumps(
        {
            "categories": [f"≤{item.get('upToMs', 0)}" for item in distribution],
            "counts": [item.get("count", 0) for item in distribution],
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    section = (
        "<section class=\"section\">"
        "<h2>帧耗时分布</h2>"
        f"<p class=\"muted\">逐帧渲染耗时直方图 · {html.escape(caption)}</p>"
        "<div id=\"frame-duration-dist\" class=\"dist-chart\" role=\"img\" aria-label=\"帧耗时分布直方图\"></div>"
        f"{segment_text}"
        "</section>"
    )
    script = (
        "<script src=\"echarts.min.js\"></script>"
        "<script>"
        f"var __frameDistData = {chart_json};"
        "(function () {"
        "  if (typeof echarts === 'undefined') { console.warn('echarts.min.js not loaded'); return; }"
        "  var node = document.getElementById('frame-duration-dist');"
        "  if (!node) return;"
        "  var styles = getComputedStyle(document.documentElement);"
        "  var accent = styles.getPropertyValue('--accent').trim() || '#1c6dd0';"
        "  var ink = styles.getPropertyValue('--ink').trim() || '#162235';"
        "  var muted = styles.getPropertyValue('--muted').trim() || '#5d6a7a';"
        "  var grid = styles.getPropertyValue('--line').trim() || '#d9e1ec';"
        "  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;"
        "  var chart = echarts.init(node, null, { renderer: 'canvas' });"
        "  chart.setOption({"
        "    animationDuration: reduceMotion ? 0 : 260,"
        "    aria: { enabled: true, decal: { show: true } },"
        "    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, textStyle: { color: ink, fontSize: 12 } },"
        "    grid: { left: 48, right: 18, top: 24, bottom: 40, containLabel: true },"
        "    xAxis: { type: 'category', data: __frameDistData.categories, name: '帧耗时 (ms)',"
        "      nameTextStyle: { color: muted, fontSize: 11 },"
        "      axisLine: { lineStyle: { color: grid } }, axisTick: { show: false },"
        "      axisLabel: { color: muted, fontSize: 11, interval: Math.ceil(__frameDistData.categories.length / 10) - 1 } },"
        "    yAxis: { type: 'value', name: '帧数', minInterval: 1,"
        "      nameTextStyle: { color: muted, fontSize: 11 },"
        "      axisLabel: { color: muted, fontSize: 11 },"
        "      splitLine: { lineStyle: { color: grid, type: 'dashed' } } },"
        "    series: [{ type: 'bar', data: __frameDistData.counts, barCategoryGap: '18%',"
        "      itemStyle: { color: accent, borderRadius: [3, 3, 0, 0] } }]"
        "  });"
        "  window.addEventListener('resize', function () { chart.resize(); });"
        "})();"
        "</script>"
    )
    return section, script


# ---------------------------------------------------------------------------
# 连续指标曲线区块（CM-TASK-015）：统一折线组件 + CPU 曲线 + FPS/CPU 双轴图
# 数据源为 summary["metrics"][id]["series"]（SAMPLING_FRAMEWORK §3：ts 为相对
# run 起点的毫秒，engine 已分桶降采样至 ≤600 点；旧 summary 无 series 时优雅缺省）。
# ---------------------------------------------------------------------------

# 序列点数下限（REPORT_PRESENTATION §4）：过短序列退化为统计摘要行，不画折线
_LINE_CHART_MIN_POINTS = 10
# 降级 chip 原因摘要最大长度（CM-TASK-019）
_CHIP_REASON_MAX_CHARS = 160


def _metric_series_points(summary: Mapping[str, Any], metric_id: str) -> list[dict[str, Any]]:
    """读取 summary["metrics"][metric_id]["series"]，清洗为 [{"ts": ms, "v": value}]。

    非 Mapping 指标、缺失 series、非法点均按无数据处理（旧报告数据兼容）；
    点按 ts 升序排序；tags 子字典原样保留，供 thermal 按温区拆分（CM-TASK-019）。
    """
    metrics = summary.get("metrics")
    entry = metrics.get(metric_id) if isinstance(metrics, Mapping) else None
    raw_series = entry.get("series") if isinstance(entry, Mapping) else None
    if not isinstance(raw_series, Sequence) or isinstance(raw_series, (str, bytes, bytearray)):
        return []
    points: list[dict[str, Any]] = []
    for raw_point in raw_series:
        if not isinstance(raw_point, Mapping):
            continue
        ts = raw_point.get("ts")
        value = raw_point.get("v")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        point: dict[str, Any] = {"ts": float(ts), "v": float(value)}
        tags = raw_point.get("tags")
        if isinstance(tags, Mapping) and tags:
            point["tags"] = dict(tags)
        points.append(point)
    points.sort(key=lambda point: point["ts"])
    return points


def _render_series_chart_block(
    *,
    title: str,
    caption: str,
    chart_id: str,
    series: Sequence[Mapping[str, Any]],
    axis_units: Sequence[str],
    specs_out: list[dict[str, Any]],
    extra_html: str = "",
) -> str:
    """统一折线组件（CM-TASK-015）：多序列输入 [{label, points[{ts, v}], axis}]，
    构造离线 ECharts option 数据并渲染单个图区块。

    - axis_units 提供 1 或 2 个 Y 轴单位，2 个即双轴（series 项 axis: 0=左 / 1=右）；
    - 序列总点数 >= _LINE_CHART_MIN_POINTS：注册图表 spec（页面内通用 JS 渲染器消费）；
    - 点数过短：退化为 min/max/mean 统计摘要，不画折线（REPORT_PRESENTATION §4）；
    - 无任何点：仅渲染 extra_html（通常是降级 chip 行，CM-TASK-019），不渲染图。
    """
    total_points = sum(len(item.get("points") or []) for item in series)
    if not series or total_points == 0:
        return extra_html
    if total_points < _LINE_CHART_MIN_POINTS:
        stat_parts = [
            f"{item.get('label', '')} min {_format_number(min(point['v'] for point in item['points']))}"
            f" / max {_format_number(max(point['v'] for point in item['points']))}"
            f" / mean {_format_number(sum(point['v'] for point in item['points']) / len(item['points']))}"
            for item in series
        ]
        body = (
            f"<p class=\"muted\">采样点过少（{total_points} 点），退化为统计摘要："
            f"{html.escape(' · '.join(stat_parts))}</p>"
        )
    else:
        specs_out.append(
            {
                "id": chart_id,
                "axisUnits": [str(unit) for unit in axis_units],
                "series": [
                    {
                        "name": str(item.get("label") or ""),
                        "yAxisIndex": int(item.get("axis") or 0),
                        "data": [[point["ts"] / 1000.0, point["v"]] for point in item["points"]],
                    }
                    for item in series
                ],
            }
        )
        body = (
            f"<div id=\"{html.escape(chart_id)}\" class=\"dist-chart\" role=\"img\" "
            f"aria-label=\"{html.escape(title)}折线图\"></div>"
        )
    head = (
        f"<div class=\"metric-section\">"
        f"<h3>{html.escape(title)}</h3>"
        f"<p class=\"muted\">{html.escape(caption)}</p>"
    )
    return f"{head}{body}{extra_html}</div>"


_SIMPLEPERF_FLAME_RENDERER_JS = """
(function () {
  var data = typeof __simpleperfFlame === 'undefined' ? null : __simpleperfFlame;
  var node = document.getElementById('simpleperf-flame-chart');
  if (!node || !data || !data.tree) return;
  // 有离线 echarts → treemap（火焰图式分层面积）；无 echarts → DOM 缩进列表兜底，
  // 两条路径都只消费内嵌数据（离线报告不外链 CDN）。
  if (typeof echarts !== 'undefined') {
    try {
      var chart = echarts.init(node, null, { renderer: 'canvas' });
      chart.setOption({
        tooltip: { formatter: function (info) { return info.name + ': ' + info.value + '%'; } },
        series: [{
          type: 'treemap', data: [data.tree], roam: false, leafDepth: 3,
          label: { show: true, overflow: 'truncate' }, breadcrumb: { show: true }
        }]
      });
      window.addEventListener('resize', function () { chart.resize(); });
      return;
    } catch (err) { /* echarts 渲染失败 → DOM 兜底 */ }
  }
  node.innerHTML = '';
  node.style.height = 'auto';
  node.className = 'flame-dom';
  function render(el, item, depth) {
    var row = document.createElement('div');
    row.className = 'flame-row';
    row.style.marginLeft = (depth * 14) + 'px';
    row.textContent = item.name + '  ' + item.value + '%';
    el.appendChild(row);
    var kids = item.children || [];
    for (var i = 0; i < kids.length; i++) render(el, kids[i], depth + 1);
  }
  render(node, data.tree, 0);
})();
"""


def _simpleperf_flame_script(face: Mapping[str, Any], *, include_echarts_src: bool) -> str:
    """把火焰图树数据内嵌为离线 payload + 渲染脚本（_series_charts_script 同款：
    `</` 转义防标签截断；禁止外链 CDN）。``include_echarts_src``：页面其余区块
    （帧分布/连续曲线）都没带 echarts 引用时由本区块引入 vendor 脚本。"""
    payload = json.dumps({"tree": face.get("tree")}, ensure_ascii=False).replace("</", "<\\/")
    src = '<script src="echarts.min.js"></script>' if include_echarts_src else ""
    return f"{src}<script>\nvar __simpleperfFlame = {payload};\n{_SIMPLEPERF_FLAME_RENDERER_JS}</script>"


def _simpleperf_stat_signals(face: Mapping[str, Any]) -> list[dict[str, str]]:
    """simpleperf stat 卡 chip：事件（样本数/event count）、模式与成树基准。"""
    signals: list[dict[str, str]] = []
    for event in face.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        name = str(event.get("event") or "?")
        samples = event.get("samples")
        event_count = event.get("eventCount")
        value = samples if samples is not None else "-"
        hint = f"事件 {name}"
        if event_count is not None:
            hint += f" · event count {event_count}"
        signals.append({"label": name, "value": f"{value} 样本", "hint": hint, "tone": "tone-good"})
    mode = str(face.get("mode") or "flat")
    signals.append({
        "label": "解析模式",
        "value": "callgraph" if mode == "callgraph" else "flat",
        "hint": str(face.get("basis") or "-"),
        "tone": "tone-none",
    })
    entry_count = face.get("entryCount")
    signals.append({
        "label": "报告条目",
        "value": f"{entry_count}",
        "hint": f"火焰图最大深度 {face.get('maxDepth')} 层"
        + (f" · {face.get('unparsedLines')} 行未解析" if face.get("unparsedLines") else ""),
        "tone": "tone-none",
    })
    return signals


def _render_simpleperf_block(face: Mapping[str, Any], *, include_echarts_src: bool) -> str:
    """simpleperf stat 卡 + 火焰图区块（离线 HTML 内嵌数据渲染，报告页内自包含）。"""
    stat_html = _render_signal_group(
        "CPU 热点（simpleperf stat）",
        "由 simpleperf-report.txt 解析（flat 表 → overhead 分布树；-g → 简报调用树）。",
        _simpleperf_stat_signals(face),
    )
    tree = face.get("tree")
    if isinstance(tree, Mapping) and tree:
        flame_script = _simpleperf_flame_script(face, include_echarts_src=include_echarts_src)
        flame_html = (
            "<div class=\"detail\" style=\"margin-top: 12px;\">"
            "<div class=\"label\">CPU 热点火焰图（treemap 面积 = overhead%；无 echarts 时以缩进列表渲染）</div>"
            f"{flame_script}"
            "<div id=\"simpleperf-flame-chart\" style=\"width: 100%; height: 320px;\"></div>"
            "</div>"
        )
    else:
        flame_html = (
            "<p class=\"muted\" style=\"margin-top: 12px;\">simpleperf report 未见可解析的样本表行，"
            "火焰图不可用（原始摘录见下方摘要，不回退伪造）。</p>"
        )
    return stat_html + flame_html


_SERIES_CHART_RENDERER_JS = """
(function () {
  if (typeof echarts === 'undefined') { console.warn('echarts.min.js not loaded'); return; }
  var payload = window.__seriesCharts || __seriesCharts || { charts: [] };
  var styles = getComputedStyle(document.documentElement);
  var palette = ['--accent', '--violet', '--good', '--warn', '--bad'].map(function (name) {
    return styles.getPropertyValue(name).trim() || '#1c6dd0';
  });
  var ink = styles.getPropertyValue('--ink').trim() || '#162235';
  var muted = styles.getPropertyValue('--muted').trim() || '#5d6a7a';
  var grid = styles.getPropertyValue('--line').trim() || '#d9e1ec';
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var charts = [];
  // marker 名来自设备 logcat（外部数据）：进入 tooltip（HTML 渲染）前必须转义
  function esc(value) {
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function formatNumber(value) {
    var num = Number(value);
    if (!isFinite(num)) return '-';
    return Math.abs(num) >= 100 ? num.toFixed(1) : num.toFixed(2);
  }
  var timeAlign = payload.timeAlign || null;
  // marker 命中窗（秒）：hover 最近事件与点击命中共用一个常量（TASK-023-fix）
  var MARKER_NEAR_S = 2;
  // hover/点击共用的最近 marker 查找（thresholdS 为秒窗）
  function nearestMarker(markers, x, thresholdS) {
    var best = null;
    var bestDist = Infinity;
    (markers || []).forEach(function (marker) {
      var dist = Math.abs((marker.x || 0) - x);
      if (dist < bestDist) { bestDist = dist; best = marker; }
    });
    return bestDist <= thresholdS ? best : null;
  }
  // 回放区跳转：回调由录屏回放区脚本暴露，无录屏时不存在 → 优雅禁用
  function seekPlayback(seconds) {
    var seek = window.__perfevalSeekPlayback;
    if (typeof seek === 'function' && isFinite(seconds) && seconds >= 0) seek(seconds);
  }
  (payload.charts || []).forEach(function (spec) {
    var node = document.getElementById(spec.id);
    if (!node) return;
    var axisUnits = spec.axisUnits || [];
    var dual = axisUnits.length > 1;
    var markers = spec.markers || [];
    var yAxis = axisUnits.map(function (unit, index) {
      return {
        type: 'value',
        name: unit,
        nameTextStyle: { color: muted, fontSize: 11 },
        scale: true,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11 },
        splitLine: { show: index === 0, lineStyle: { color: grid, type: 'dashed' } }
      };
    });
    var chart = echarts.init(node, null, { renderer: 'canvas' });
    // hover 联动（REPORT_PRESENTATION §2.2）：该时刻各序列值 + 最近 marker 名
    var tooltipFormatter = function (params) {
      var list = Array.isArray(params) ? params : [params];
      if (!list.length) return '';
      var x = Number(list[0].axisValue);
      var rows = ['<div style="font-weight:600">' + (isFinite(x) ? x.toFixed(1) : '-') + ' s</div>'];
      list.forEach(function (item) {
        var value = Array.isArray(item.value) ? item.value[1] : item.value;
        var unit = axisUnits[item.yAxisIndex || 0] || '';
        rows.push('<div>' + (item.marker || '') + esc(item.seriesName) + '：'
          + formatNumber(value) + (unit ? ' ' + esc(unit) : '') + '</div>');
      });
      var marker = nearestMarker(markers, x, MARKER_NEAR_S);
      if (marker) {
        var delta = x - marker.x;
        rows.push('<div style="color:' + ink + '">事件：' + esc(marker.name)
          + '（Δ ' + (delta >= 0 ? '+' : '') + (isFinite(delta) ? delta.toFixed(1) : '-') + 's）</div>');
      }
      return rows.join('');
    };
    chart.setOption({
      animationDuration: reduceMotion ? 0 : 260,
      aria: { enabled: true, decal: { show: true } },
      color: palette,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: muted, type: 'dashed' } },
        textStyle: { color: ink, fontSize: 12 },
        formatter: tooltipFormatter
      },
      legend: {
        data: (spec.series || []).map(function (item) { return item.name; }),
        top: 2, left: 4, itemWidth: 18, itemHeight: 9, itemGap: 12,
        textStyle: { color: ink, fontSize: 11 }
      },
      grid: { left: 52, right: dual ? 52 : 20, top: 42, bottom: 34, containLabel: true },
      xAxis: {
        type: 'value',
        name: '运行时间 (s)',
        nameTextStyle: { color: muted, fontSize: 11 },
        axisLine: { lineStyle: { color: grid } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11 }
      },
      yAxis: yAxis,
      series: (spec.series || []).map(function (item, index) {
        var line = {
          name: item.name,
          type: 'line',
          yAxisIndex: item.yAxisIndex || 0,
          data: item.data || [],
          showSymbol: false,
          smooth: false,
          lineStyle: { width: 2 },
          emphasis: { focus: 'series' }
        };
        // flow 事件竖线（各图共享）：无 markers 时本分支不生效，图正常渲染
        if (index === 0 && markers.length) {
          line.markLine = {
            symbol: 'none',
            silent: false,
            animation: false,
            lineStyle: { color: muted, type: 'dashed', width: 1 },
            label: {
              show: true, position: 'end', distance: 4, fontSize: 10, color: muted,
              width: 90, overflow: 'truncate',
              formatter: function (params) { return esc(params.name || ''); }
            },
            data: markers.map(function (marker) { return { xAxis: marker.x, name: marker.name }; })
          };
        }
        return line;
      })
    });
    // 点击竖线/图区 → 回放区跳转（CM-TASK-023）：优先命中最近 marker 的
    // videoSeconds（纯 epoch 差，不受 X 轴基准影响）；其余图区点击仅在
    // timeBase.domain=sampler_epoch 时按本图 epoch 基准换算（B1 修——图内时刻
    // 反推 epoch 必须用 timeBase.startEpochMs，而非 t0）；frame_relative 图与
    // 无锚点/无视频回调时保持禁用。
    chart.getZr().on('click', function (event) {
      var pixel = [event.offsetX, event.offsetY];
      if (!chart.containPixel('grid', pixel)) return;
      var x = Number(chart.convertFromPixel('grid', pixel)[0]);
      if (!isFinite(x)) return;
      var marker = nearestMarker(markers, x, MARKER_NEAR_S);
      if (marker) {
        if (typeof marker.videoSeconds === 'number') seekPlayback(marker.videoSeconds);
        return;
      }
      var timeBase = spec.timeBase || {};
      if (timeBase.domain === 'sampler_epoch' && timeAlign && typeof timeBase.startEpochMs === 'number') {
        seekPlayback((timeBase.startEpochMs + x * 1000 - timeAlign.videoFirstFrameEpochMs) / 1000);
      }
    });
    charts.push(chart);
  });
  window.addEventListener('resize', function () { charts.forEach(function (chart) { chart.resize(); }); });
})();
"""


def _series_charts_script(
    specs: Sequence[Mapping[str, Any]],
    *,
    include_echarts_src: bool,
    time_align: Mapping[str, int] | None = None,
) -> str:
    """把统一折线组件 specs 内嵌为离线数据 + 通用渲染脚本（禁止外链 CDN）。

    time_align（t0/video 首帧 epoch）为图区点击 → 回放秒的换算锚点，缺失时
    payload 不带该字段（JS 侧优雅禁用图区联动）。
    """
    payload_obj: dict[str, Any] = {"charts": list(specs)}
    if time_align:
        payload_obj["timeAlign"] = dict(time_align)
    payload = json.dumps(payload_obj, ensure_ascii=False).replace("</", "<\\/")
    src = '<script src="echarts.min.js"></script>' if include_echarts_src else ""
    return f"{src}<script>\nvar __seriesCharts = {payload};\n{_SERIES_CHART_RENDERER_JS}</script>"


def _cpu_series_block(summary: Mapping[str, Any], specs_out: list[dict[str, Any]]) -> str:
    """CPU 曲线：cpu.proc.pct + cpu.total.pct 同图（REPORT_PRESENTATION §2.2，左 % 轴）。"""
    series: list[dict[str, Any]] = []
    for metric_id, label in (
        ("cpu.proc.pct", "进程 CPU 占用率"),
        ("cpu.total.pct", "整机 CPU 占用率"),
    ):
        points = _metric_series_points(summary, metric_id)
        if points:
            series.append({"label": label, "points": points, "axis": 0})
    if not series:
        return ""
    return _render_series_chart_block(
        title="CPU",
        caption="cpu.proc.pct / cpu.total.pct · 单位 %",
        chart_id="series-chart-cpu",
        series=series,
        axis_units=["%"],
        specs_out=specs_out,
    )


def _fps_cpu_series_block(summary: Mapping[str, Any], specs_out: list[dict[str, Any]]) -> str:
    """FPS/CPU 双轴图（首个双轴）：左轴 fps——frame.duration.detail.ms 逐点反推
    1000/耗时（聚合口径 frame.fps.mean/median 标注于 caption），右轴 cpu.proc.pct（%）；
    fpsSource（tp/framestats）标注进 caption。
    """
    fps_points = [
        {"ts": point["ts"], "v": 1000.0 / point["v"]}
        for point in _metric_series_points(summary, "frame.duration.detail.ms")
        if point["v"] > 0
    ]
    cpu_points = _metric_series_points(summary, "cpu.proc.pct")
    series: list[dict[str, Any]] = []
    axis_units: list[str] = []
    if fps_points:
        series.append({"label": "帧率（逐帧耗时反推）", "points": fps_points, "axis": len(axis_units)})
        axis_units.append("fps")
    if cpu_points:
        series.append({"label": "进程 CPU 占用率", "points": cpu_points, "axis": len(axis_units)})
        axis_units.append("%")
    if not series:
        return ""
    fps_source = _coerce_text(summary.get("fpsSource") or "-")
    caption_parts = [
        "frame.duration.detail.ms 反推 fps（左轴）· cpu.proc.pct（右轴 %）",
        f"FPS 来源：{_FPS_SOURCE_LABELS.get(fps_source, fps_source)}",
        # B1（TASK-023-fix R1）：帧序列 ts 相对首帧（engine series 点无逐点 epoch），
        # 不与采样时间轴同基准，故本图不叠加 flow 事件竖线
        "帧序列 ts 相对首帧（不与采样轴对齐），本图不叠加事件竖线",
    ]
    fps_mean = _frame_fps_value(summary, "frame.fps.mean")
    fps_median = _frame_fps_value(summary, "frame.fps.median")
    if fps_mean is not None:
        caption_parts.append(f"平均帧率 {_format_number(fps_mean)} fps")
    if fps_median is not None:
        caption_parts.append(f"中位帧率 {_format_number(fps_median)} fps")
    return _render_series_chart_block(
        title="FPS / CPU",
        caption=" · ".join(caption_parts),
        chart_id="series-chart-fps-cpu",
        series=series,
        axis_units=axis_units,
        specs_out=specs_out,
    )


# ---------------------------------------------------------------------------
# 四项指标曲线 + 降级 chip（CM-TASK-019）：net / power / gpu / thermal
# 单位与图例对齐 METRIC_SOURCES §4 总表（kbps、mA、%、°C）。
# ---------------------------------------------------------------------------


def _summary_diagnostic_texts(summary: Mapping[str, Any]) -> list[str]:
    """summary["diagnostics"] 的文本摘要列表（兼容 str 与 Mapping 诊断事件）。"""
    raw = summary.get("diagnostics")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return [text for text in (_diagnostic_text(item) for item in raw) if text]
    if raw:
        return [str(raw)]
    return []


def _mapping_diagnostic_parts(item: Mapping[str, Any]) -> tuple[str, str]:
    """Mapping 形态诊断事件的 (code, message) 文本。

    code 优先取 data.code，其次顶层 name（engine 诊断事件两种落点都存在）；
    两者皆缺时返回空串。str 形态诊断不走本函数（原文即原因）。
    """
    data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
    code = data.get("code") or item.get("name")
    message = data.get("message")
    return (str(code) if code else "", str(message) if message else "")


def _matched_diagnostic_reason(summary: Mapping[str, Any], sampler_hint: str) -> str:
    """summary["diagnostics"] 中第一个匹配 sampler_hint 的诊断原因原文（未命中返回 ""）。

    遗留修（CM-TASK-023）：Mapping 形态诊断此前只经 _diagnostic_text 取一级文本
    （message 优先），当 message 不含采样器 code 时会失配、chip 错误兜底成
    "无采样数据"；现在同时匹配 code 与 message 字段。命中后 str 形态展示原文，
    Mapping 形态展示 code:message（与 str 形态"code:原文"的视觉一致）。
    """
    raw = summary.get("diagnostics")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        items: list[Any] = list(raw)
    elif raw:
        items = [raw]
    else:
        return ""
    for item in items:
        if not sampler_hint:
            break
        if isinstance(item, Mapping):
            code, message = _mapping_diagnostic_parts(item)
            if sampler_hint in code or sampler_hint in message:
                return f"{code}:{message}" if code and message else (code or message)
        elif sampler_hint in str(item):
            return str(item)
    return ""


def _metric_degraded_chip(
    summary: Mapping[str, Any],
    metric_id: str,
    *,
    sampler_hint: str = "",
) -> str:
    """降级 chip（CM-TASK-019）：指标缺 series/无数据时渲染原因 chip；可用时返回 ""。

    原因摘要优先取 summary["diagnostics"] 中匹配采样器的 unavailable/failed 原文
    （如 sampler.<name>.unavailable / sampler.<name>.failed，截断到
    _CHIP_REASON_MAX_CHARS），其次标注样本数不足连续序列，最后兜底"无采样数据"。
    样式复用现有 .badge（与报告其余徽标一致）。
    """
    metrics = summary.get("metrics")
    entry = metrics.get(metric_id) if isinstance(metrics, Mapping) else None
    series = entry.get("series") if isinstance(entry, Mapping) else None
    if isinstance(series, Sequence) and not isinstance(series, (str, bytes, bytearray)) and series:
        return ""
    reason = _matched_diagnostic_reason(summary, sampler_hint)
    if not reason and isinstance(entry, Mapping):
        n = entry.get("n")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            reason = f"样本数 {n}，未形成连续序列"
    if not reason:
        reason = "无采样数据"
    if len(reason) > _CHIP_REASON_MAX_CHARS:
        reason = reason[: _CHIP_REASON_MAX_CHARS - 1] + "…"
    return (
        f"<span class=\"badge\" title=\"{html.escape(reason)}\">"
        f"{html.escape(metric_id)}：{html.escape(reason)}</span> "
    )


def _net_series_block(summary: Mapping[str, Any], specs_out: list[dict[str, Any]]) -> str:
    """网络曲线：net.rx.kbps / net.tx.kbps（左 kbps 轴）。"""
    series: list[dict[str, Any]] = []
    for metric_id, label in (("net.rx.kbps", "下行速率"), ("net.tx.kbps", "上行速率")):
        points = _metric_series_points(summary, metric_id)
        if points:
            series.append({"label": label, "points": points, "axis": 0})
    chips = (
        _metric_degraded_chip(summary, "net.rx.kbps", sampler_hint="sampler.net.")
        + _metric_degraded_chip(summary, "net.tx.kbps", sampler_hint="sampler.net.")
    )
    return _render_series_chart_block(
        title="网络",
        caption="net.rx.kbps / net.tx.kbps · 单位 kbps",
        chart_id="series-chart-net",
        series=series,
        axis_units=["kbps"],
        specs_out=specs_out,
        extra_html=chips,
    )


def _power_series_block(summary: Mapping[str, Any], specs_out: list[dict[str, Any]]) -> str:
    """功耗曲线：power.current.ma（mA）；降级源（无瞬时电流）时渲染
    power.level.pct（%/min，电量差分），power.energy.mah 估算能耗进 caption。"""
    current_points = _metric_series_points(summary, "power.current.ma")
    series: list[dict[str, Any]] = []
    axis_units = ["mA"]
    caption = "power.current.ma · 单位 mA"
    if current_points:
        series.append({"label": "电池电流", "points": current_points, "axis": 0})
    else:
        level_points = _metric_series_points(summary, "power.level.pct")
        if level_points:
            series.append({"label": "电量变化速率", "points": level_points, "axis": 0})
            axis_units = ["%/min"]
            caption = "power.level.pct · 单位 %/min（降级源：电量差分）"
    energy_mah = _frame_fps_value(summary, "power.energy.mah")  # 通用标量读取（value 字段）
    if energy_mah is not None:
        caption += f" · 估算能耗 {_format_number(energy_mah)} mAh"
    chip = _metric_degraded_chip(summary, "power.current.ma", sampler_hint="sampler.power.")
    return _render_series_chart_block(
        title="功耗",
        caption=caption,
        chart_id="series-chart-power",
        series=series,
        axis_units=axis_units,
        specs_out=specs_out,
        extra_html=chip,
    )


def _gpu_series_block(summary: Mapping[str, Any], specs_out: list[dict[str, Any]]) -> str:
    """GPU 曲线：gpu.busy.pct（左 % 轴）。"""
    points = _metric_series_points(summary, "gpu.busy.pct")
    series = [{"label": "GPU 占用率", "points": points, "axis": 0}] if points else []
    chip = _metric_degraded_chip(summary, "gpu.busy.pct", sampler_hint="sampler.gpu_thermal.")
    return _render_series_chart_block(
        title="GPU",
        caption="gpu.busy.pct · 单位 %",
        chart_id="series-chart-gpu",
        series=series,
        axis_units=["%"],
        specs_out=specs_out,
        extra_html=chip,
    )


def _thermal_series_block(summary: Mapping[str, Any], specs_out: list[dict[str, Any]]) -> str:
    """温度曲线：thermal.max.c（°C），series 点带 tags.zone 时按温区拆分多条线。"""
    points = _metric_series_points(summary, "thermal.max.c")
    series: list[dict[str, Any]] = []
    if points:
        groups: dict[str | None, list[dict[str, Any]]] = {}
        for point in points:
            tags = point.get("tags")
            zone = tags.get("zone") if isinstance(tags, Mapping) else None
            key = str(zone) if isinstance(zone, str) and zone else None
            groups.setdefault(key, []).append(point)
        if list(groups) == [None]:
            series.append({"label": "最高温区温度", "points": points, "axis": 0})
        else:
            series.extend(
                {"label": f"温区 {zone}" if zone else "未标注温区", "points": zone_points, "axis": 0}
                for zone, zone_points in groups.items()
            )
    chip = _metric_degraded_chip(summary, "thermal.max.c", sampler_hint="sampler.gpu_thermal.")
    return _render_series_chart_block(
        title="温度",
        caption="thermal.max.c · 单位 °C（带 zone 标注时按温区拆分）",
        chart_id="series-chart-thermal",
        series=series,
        axis_units=["°C"],
        specs_out=specs_out,
        extra_html=chip,
    )


def _series_section_has_any_points(summary: Mapping[str, Any]) -> bool:
    """区块读取的任一指标存在 ≥1 个有效序列点时返回 True。

    遗留修（CM-TASK-023）：全短序列（各 series 非空但 < 10 点）时各图已退化为
    统计摘要行，若再显示"未启用连续采样：没有可绘制的连续序列"即与统计摘要
    自相矛盾——提示只允许在确实没有任何序列数据时出现。
    """
    return any(
        _metric_series_points(summary, metric_id)
        for metric_id in (
            "cpu.proc.pct",
            "cpu.total.pct",
            "frame.duration.detail.ms",
            "net.rx.kbps",
            "net.tx.kbps",
            "power.current.ma",
            "power.level.pct",
            "gpu.busy.pct",
            "thermal.max.c",
        )
    )


def _samplers_clock_anchor(summary: Mapping[str, Any]) -> int | None:
    """clockAnchors.samplers.startEpochMs（B1，TASK-023-fix R1）。

    采样序列 series ts 的原点是 SamplingScheduler.start()（host 单调 elapsed），
    本锚点是 start() 同刻的设备时钟——sampler 系曲线（cpu/net/power/gpu/thermal）
    的 X 轴 epoch 基准。旧数据（TASK-021 及更早）无该锚点 → 返回 None（报告
    竖线优雅不渲染）。
    """
    anchors = summary.get("clockAnchors") if isinstance(summary.get("clockAnchors"), Mapping) else {}
    samplers = anchors.get("samplers") if isinstance(anchors.get("samplers"), Mapping) else {}
    start_epoch_ms = samplers.get("startEpochMs")
    if isinstance(start_epoch_ms, (int, float)) and not isinstance(start_epoch_ms, bool):
        return int(start_epoch_ms)
    return None


def _video_first_frame_epoch_ms(summary: Mapping[str, Any]) -> int | None:
    anchors = summary.get("clockAnchors") if isinstance(summary.get("clockAnchors"), Mapping) else {}
    video = anchors.get("video") if isinstance(anchors.get("video"), Mapping) else {}
    first_frame = video.get("firstFrameEpochMs")
    if isinstance(first_frame, (int, float)) and not isinstance(first_frame, bool):
        return int(first_frame)
    return None


def _chart_event_markers(
    summary: Mapping[str, Any],
    *,
    origin_epoch_ms: int | None,
    has_video: bool,
) -> list[dict[str, Any]]:
    """flow 步骤 marker → 统一折线组件竖线数据（CM-TASK-023，REPORT_PRESENTATION §2.2）。

    数据源为 summary["clockAnchors"]["markers"]（logcat PERFEVAL_EVENT 行的
    name/epochMs）。竖线 X 必须与所在图的序列同基准：sampler 系图传入
    origin_epoch_ms=clockAnchors.samplers.startEpochMs（B1 修——此前误用 t0，
    与 series 原点 scheduler.start() 相差 setup 间隙，竖线系统性偏移）；
    origin_epoch_ms=None（旧数据）时返回 []，图正常渲染无竖线。
    videoSeconds = (markerEpochMs − video.firstFrameEpochMs) / 1000 为纯 epoch
    差，不受 X 轴基准影响；仅在存在录屏 artifact 且视频锚点有效时给出。
    缺 epochMs 的 marker 逐条跳过。
    """
    if origin_epoch_ms is None:
        return []
    anchors = summary.get("clockAnchors") if isinstance(summary.get("clockAnchors"), Mapping) else {}
    raw_markers = anchors.get("markers")
    if not isinstance(raw_markers, Sequence) or isinstance(raw_markers, (str, bytes, bytearray)):
        return []
    first_frame_epoch_ms = _video_first_frame_epoch_ms(summary)
    first_frame_valid = has_video and first_frame_epoch_ms is not None
    markers: list[dict[str, Any]] = []
    for item in raw_markers:
        if not isinstance(item, Mapping):
            continue
        epoch_ms = item.get("epochMs")
        if not isinstance(epoch_ms, (int, float)) or isinstance(epoch_ms, bool):
            continue
        name = item.get("name")
        marker: dict[str, Any] = {
            "name": name if isinstance(name, str) and name else "flow 事件",
            "x": (int(epoch_ms) - origin_epoch_ms) / 1000.0,
        }
        if first_frame_valid:
            marker["videoSeconds"] = max(0.0, (int(epoch_ms) - int(first_frame_epoch_ms)) / 1000.0)
        markers.append(marker)
    return markers


def _chart_time_align(summary: Mapping[str, Any]) -> dict[str, int] | None:
    """图区点击 → 回放时刻换算所需的视频首帧 epoch；缺失返回 None。

    图内时刻 → epoch 的正向换算用各图自己的 timeBase.startEpochMs（sampler 系图），
    这里只提供共用的视频侧锚点。
    """
    first_frame_epoch_ms = _video_first_frame_epoch_ms(summary)
    return {"videoFirstFrameEpochMs": first_frame_epoch_ms} if first_frame_epoch_ms is not None else None


def _render_series_section(report: SummaryReportData, *, include_echarts_src: bool) -> tuple[str, str]:
    """连续指标曲线区块（CM-TASK-015/019/023）。返回 (区块 HTML, 脚本 HTML)。

    无任何序列数据（未开采样/旧 summary）时区块优雅缺省：显示"未启用连续采样"
    提示而非报错（旧报告数据兼容必须保持）；四项资源指标（net/power/gpu/thermal）
    全部缺失时区块仍渲染（全 chip 状态）。
    flow marker 竖线 + hover/点击联动（CM-TASK-023；B1 修——X 轴基准逐图声明）：
    sampler 系图（cpu/net/power/gpu/thermal）序列 ts 原点是
    SamplingScheduler.start()，以 clockAnchors.samplers.startEpochMs 为 epoch 基
    准换算竖线并允许点击联动；FPS/CPU 双轴图的帧序列 ts 相对首帧（engine series
    无逐点 epoch），domain=frame_relative 不与采样轴对齐——不画竖线并在图注注
    明。无 samplers 锚点（旧数据）时竖线优雅不渲染。
    """
    summary = report.raw_summary
    specs: list[dict[str, Any]] = []
    blocks = [
        _cpu_series_block(summary, specs),
        _fps_cpu_series_block(summary, specs),
        _net_series_block(summary, specs),
        _power_series_block(summary, specs),
        _gpu_series_block(summary, specs),
        _thermal_series_block(summary, specs),
    ]
    blocks_html = "".join(block for block in blocks if block)
    samplers_start_epoch_ms = _samplers_clock_anchor(summary)
    # 竖线只画在 epoch 可对齐的 sampler 系图上；x 与该图序列同基准
    sampler_markers = _chart_event_markers(
        summary,
        origin_epoch_ms=samplers_start_epoch_ms,
        has_video=bool(_playback_video_artifacts(report)),
    )
    for spec in specs:
        if spec.get("id") == "series-chart-fps-cpu":
            # 帧序列 ts 相对首帧（engine series 点不带逐点 epoch，tp 亦无法对齐）
            spec["timeBase"] = {"domain": "frame_relative"}
        elif samplers_start_epoch_ms is not None:
            spec["timeBase"] = {"domain": "sampler_epoch", "startEpochMs": samplers_start_epoch_ms}
            if sampler_markers:
                spec["markers"] = [dict(marker) for marker in sampler_markers]
        # 旧数据无 samplers 锚点：sampler 系图无 timeBase/竖线（优雅缺省）
    hint = "" if specs or _series_section_has_any_points(summary) else (
        "<p class=\"muted\">未启用连续采样：本次 summary 没有可绘制的连续序列"
        "（未开启采样或旧数据），不渲染折线图。</p>"
    )
    section = (
        "<section class=\"section\">"
        "<h2>连续指标曲线</h2>"
        "<p class=\"muted\">连续采样序列折线，X 轴为相对 run 起点的秒"
        "（series 点已由 engine 降采样至 ≤600 点）。</p>"
        f"{hint}"
        f"{blocks_html}"
        "</section>"
    )
    script = (
        _series_charts_script(
            specs,
            include_echarts_src=include_echarts_src,
            time_align=_chart_time_align(summary),
        )
        if specs
        else ""
    )
    return section, script


def _has_stream_series(summary: Mapping[str, Any]) -> bool:
    """summary 中任一指标带非空 series 时返回 True（决定是否随包分发离线 ECharts）。"""
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping):
        return False
    for entry in metrics.values():
        if not isinstance(entry, Mapping):
            continue
        series = entry.get("series")
        if isinstance(series, Sequence) and not isinstance(series, (str, bytes, bytearray)) and bool(series):
            return True
    return False


# ---------------------------------------------------------------------------
# 录屏回放区（CM-TASK-022，SCREEN_RECORD_SYNC §4 / REPORT_PRESENTATION §2.3）
# video 相对路径引用（不内嵌内容，敏感 artifact 策略）；时间轴游标 + 标记跳转：
# jankSegments（tp epoch 对齐）/ flow marker（clockAnchors）→ video.currentTime；
# framestats 路径无 wall-clock，掉帧只高亮不跳转；无录屏时区块不渲染。
# ---------------------------------------------------------------------------

_PLAYBACK_JUMP_WINDOW_MS = 3000


def _playback_video_artifacts(report: SummaryReportData) -> list[SummaryArtifact]:
    """录屏 artifact（screen*.mp4），首段（screen.mp4）排首、其余按段号数值序。

    F-fix（R1-B1）：不能用字典序——'screen-seg002.mp4' < 'screen.mp4'
    （'-'=0x2D < '.'=0x2E），分段 run 会错选第 2 段当首段。
    smoke-fix：过滤缺失（present=False）与 0 字节 artifact——锚点残留时不得
    渲染 404 / 空内容的可播放 video 标签。
    """
    videos = [
        artifact
        for artifact in report.artifacts
        if artifact.path.endswith(".mp4")
        and Path(artifact.path).name.startswith("screen")
        and artifact.present
        and artifact.size_bytes != 0
    ]

    def _segment_order(artifact: SummaryArtifact) -> tuple[int, int]:
        name = Path(artifact.path).name
        if name == "screen.mp4":
            return (0, 0)
        match = re.search(r"screen-seg(\d+)\.mp4$", name)
        if match is not None:
            return (1, int(match.group(1)))
        return (2, 0)

    return sorted(videos, key=_segment_order)


def _playback_unavailable_reasons(summary: Mapping[str, Any]) -> list[str]:
    return [text for text in _summary_diagnostic_texts(summary) if text.startswith("screen.record.unavailable")]


def _collect_playback_markers(
    summary: Mapping[str, Any],
    *,
    has_video: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """汇总回放跳转标记：(标记列表, 是否存在已对齐标记)。

    - jankSegments：帧事件携带 epoch（tp + boottime 锚点）时换算视频时刻；
    - flow marker：clockAnchors.markers（logcat 行 epoch）。
    无 clockAnchors 或无视频时 epoch 原样输出、videoSeconds 缺省（报告端禁用跳转）。
    """
    anchors = summary.get("clockAnchors") if isinstance(summary.get("clockAnchors"), Mapping) else {}
    video = anchors.get("video") if isinstance(anchors.get("video"), Mapping) else {}
    first_frame_epoch_ms = video.get("firstFrameEpochMs")
    first_frame_valid = (
        has_video
        and isinstance(first_frame_epoch_ms, (int, float))
        and not isinstance(first_frame_epoch_ms, bool)
    )

    markers: list[dict[str, Any]] = []
    jank_segments = [
        item for item in (summary.get("jankSegments") or []) if isinstance(item, Mapping)
    ]
    aligned = False
    for segment in jank_segments:
        epoch = segment.get("startTsEpochMs")
        marker: dict[str, Any] = {
            "kind": "jank",
            "label": f"掉帧 #{int(segment.get('startIndex', 0))}–#{int(segment.get('endIndex', 0))}",
        }
        if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
            marker["epochMs"] = int(epoch)
            if first_frame_valid:
                aligned = True
                marker["videoSeconds"] = max(0.0, (int(epoch) - int(first_frame_epoch_ms)) / 1000.0)
        markers.append(marker)

    anchors_markers = anchors.get("markers") if isinstance(anchors.get("markers"), Sequence) and not isinstance(anchors.get("markers"), (str, bytes, bytearray)) else []
    for item in anchors_markers:
        if not isinstance(item, Mapping):
            continue
        epoch = item.get("epochMs")
        if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
            continue
        marker = {"kind": "marker", "label": str(item.get("name") or "flow 事件"), "epochMs": int(epoch)}
        if first_frame_valid:
            marker["videoSeconds"] = max(0.0, (int(epoch) - int(first_frame_epoch_ms)) / 1000.0)
        markers.append(marker)
    return markers, aligned


def _render_playback_section(report: SummaryReportData) -> tuple[str, str]:
    """录屏回放区块。返回 (区块 HTML, 脚本 HTML)，无录屏证据时返回 ("", "")。

    渲染条件：存在 screen*.mp4 artifact（可回放），或存在 screen.record.unavailable
    诊断（降级说明 + 保留时间轴标记）；录屏从未启用时完全不渲染。
    """
    summary = report.raw_summary
    video_artifacts = _playback_video_artifacts(report)
    unavailable_reasons = _playback_unavailable_reasons(summary)
    if not video_artifacts and not unavailable_reasons:
        return "", ""

    anchors = summary.get("clockAnchors") if isinstance(summary.get("clockAnchors"), Mapping) else {}
    video_info = anchors.get("video") if isinstance(anchors.get("video"), Mapping) else {}
    markers, _aligned = _collect_playback_markers(summary, has_video=bool(video_artifacts))
    unaligned_jank = any(
        marker["kind"] == "jank" and "videoSeconds" not in marker for marker in markers
    )

    if video_artifacts:
        video_href = _safe_href(video_artifacts[0].href)
        caption_parts = [f"视频以相对路径引用（{html.escape(Path(video_artifacts[0].path).name)}），不内嵌录屏内容"]
        offset_ms = video_info.get("offsetMs")
        # F-fix（R1-N6）：外部数据不做类型假设，非数值时降级不声明偏移
        if isinstance(offset_ms, (int, float)) and not isinstance(offset_ms, bool):
            caption_parts.append(f"视频相对 run 原点偏移 {_format_number(float(offset_ms))}ms（首帧按启动+1s 估算）")
        else:
            caption_parts.append("缺少 clockAnchors，无法精确跳转（仅手动播放）")
    else:
        video_href = None
        caption_parts = ["录屏不可用"]

    # 标记 chips：可跳转的带 data-video-jump，否则禁用态
    chips: list[str] = []
    for marker in markers:
        label = html.escape(marker["label"])
        if "videoSeconds" in marker:
            chips.append(
                f"<button type=\"button\" class=\"badge playback-marker\" data-video-jump=\"{marker['videoSeconds']:.3f}\""
                f" title=\"epoch {marker.get('epochMs', '')} ms，跳转后高亮 3s\">{label}</button>"
            )
        else:
            hint = "无对齐时间（framestats 无 wall-clock）" if marker["kind"] == "jank" else "缺少时钟锚点"
            chips.append(
                f"<span class=\"badge\" title=\"{html.escape(hint)}\">{label}（不可跳转）</span>"
            )
    if not chips:
        chips.append("<span class=\"badge\">无跳转标记（掉帧区间/flow 事件为空）</span>")
    markers_html = " ".join(chips)
    if unaligned_jank and video_artifacts:
        markers_html += (
            "<span class=\"badge\">掉帧跳转需 Perfetto frame timeline（fpsSource=tp）对齐</span>"
        )

    if video_artifacts and unavailable_reasons:
        # 历史降级诊断（如分段部分失败）与可用视频并存：仅附注
        note = f"<p class=\"muted\">采集诊断：{html.escape('；'.join(unavailable_reasons[:3]))}</p>"
    elif unavailable_reasons:
        note = (
            "<p class=\"muted\">本次录屏未生成（保留时间轴标记功能）：</p>"
            f"<p><code>{html.escape('；'.join(unavailable_reasons[:3]))}</code></p>"
        )
    else:
        note = ""

    video_html = (
        f"<video id=\"screen-playback-video\" controls preload=\"metadata\" src=\"{html.escape(video_href)}\""
        " style=\"width: 100%; max-height: 420px; background: #101828; border-radius: 8px;\"></video>"
        if video_href
        else ""
    )
    cursor_html = (
        "<div class=\"playback-cursor\" style=\"display: flex; align-items: center; gap: 10px; margin-top: 10px;\">"
        "<span class=\"muted\" style=\"font-size: 12px;\">时间轴游标</span>"
        "<input id=\"screen-playback-cursor\" type=\"range\" min=\"0\" max=\"100\" step=\"0.05\" value=\"0\""
        " style=\"flex: 1;\" aria-label=\"录屏时间轴游标\">"
        "<code id=\"screen-playback-time\">0.0s</code>"
        "</div>"
        if video_href
        else ""
    )
    section = (
        "<section class=\"section\">"
        "<h2>录屏回放</h2>"
        f"<p class=\"muted\">{' · '.join(caption_parts)}</p>"
        f"{video_html}"
        f"{cursor_html}"
        f"<div class=\"playback-markers\" style=\"margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px;\">{markers_html}</div>"
        f"{note}"
        "</section>"
    )

    data = {
        "hasVideo": video_href is not None,
        "videoHref": video_href or "",
        "markers": [
            {"label": marker["label"], "videoSeconds": marker.get("videoSeconds")}
            for marker in markers
        ],
    }
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    script = (
        "<script>"
        "window.__screenPlayback = "
        + data_json
        + ";"
        + r"""
(function () {
  var data = window.__screenPlayback || {};
  var video = document.getElementById('screen-playback-video');
  var cursor = document.getElementById('screen-playback-cursor');
  var timeLabel = document.getElementById('screen-playback-time');
  var highlightTimer = null;
  var pendingTrackHighlight = null;
  // N8 裁决（CM-TASK-023）：跳转高亮实现为"时间轴 3s 区间高亮"——
  // SCREEN_RECORD_SYNC §4 要求"跳转并高亮 3s 区间"，区间语义落在游标轨道上
  // （渐变区间，复用现有 cursor 输入框，成本低）；保留 3s 视频外框作为即时反馈。
  function clearTrackHighlight() {
    if (cursor) cursor.style.background = '';
  }
  function highlightTrack(seconds) {
    if (!cursor) return;
    if (!video || !isFinite(video.duration) || video.duration <= 0) {
      // TASK-023-fix（N8 挂起高亮丢失）：metadata 未加载时不依赖 seekTo 的
      // 异常分支，无条件注册 once 监听——早点击（currentTime 赋值不抛错的
      // 浏览器）也能在 metadata 就绪后补画轨道渐变。
      pendingTrackHighlight = seconds;
      if (video && video.addEventListener) {
        video.addEventListener('loadedmetadata', function () {
          if (pendingTrackHighlight != null) {
            highlightTrack(pendingTrackHighlight);
            pendingTrackHighlight = null;
          }
        }, { once: true });
      }
      return;
    }
    var startPct = Math.max(0, Math.min(100, (seconds / video.duration) * 100));
    var endPct = Math.max(0, Math.min(100, ((seconds + 3) / video.duration) * 100));
    cursor.style.background = 'linear-gradient(to right, transparent ' + startPct + '%, '
      + 'rgba(28, 109, 208, 0.35) ' + startPct + '%, rgba(28, 109, 208, 0.35) ' + endPct + '%, '
      + 'transparent ' + endPct + '%)';
  }
  function highlight(seconds) {
    if (!video) return;
    video.style.outline = '3px solid #1c6dd0';
    highlightTrack(seconds);
    if (highlightTimer) clearTimeout(highlightTimer);
    highlightTimer = setTimeout(function () {
      video.style.outline = '';
      clearTrackHighlight();
      pendingTrackHighlight = null;
    }, 3000);
  }
  function seekTo(seconds) {
    if (!video) return;
    var target = Math.max(0, seconds);
    try {
      video.currentTime = target;
    } catch (error) {
      video.addEventListener('loadedmetadata', function () { video.currentTime = target; }, { once: true });
    }
    video.play && video.play().catch(function () {});
    highlight(target);
  }
  window.__perfevalSeekPlayback = seekTo;
  if (!video) return;
  document.querySelectorAll('[data-video-jump]').forEach(function (node) {
    node.addEventListener('click', function () { seekTo(parseFloat(node.getAttribute('data-video-jump'))); });
  });
  if (cursor) {
    video.addEventListener('timeupdate', function () {
      if (!isFinite(video.duration) || video.duration <= 0) return;
      cursor.value = String(Math.min(100, (video.currentTime / video.duration) * 100));
      if (timeLabel) timeLabel.textContent = video.currentTime.toFixed(1) + 's';
    });
    cursor.addEventListener('input', function () {
      if (!isFinite(video.duration) || video.duration <= 0) return;
      video.currentTime = (parseFloat(cursor.value) / 100) * video.duration;
    });
  }
})();
</script>"""
    )
    return section, script


def _render_html(report: SummaryReportData) -> str:
    scope_banner = _render_scope_banner(report)
    frame_distribution_section, frame_distribution_script = _render_frame_distribution(report)
    series_section, series_script = _render_series_section(
        report, include_echarts_src=not frame_distribution_script
    )
    # flamegraph 区块只在页面其余区块（帧分布/连续曲线）都没带 echarts 引用时自引 vendor
    diagnosis_html = _render_diagnosis_section(
        report,
        include_echarts_src=not frame_distribution_script and not series_script,
    )
    playback_section, playback_script = _render_playback_section(report)
    sections = []
    for group_key, title, description in [
        ("startup", "启动耗时（ms）", "冷启动/温启动/热启动耗时，是 P0 指标，数值越低越好。"),
        ("frame_duration", "帧时序（ms）", "帧渲染耗时相关指标，数值越低通常越好。"),
        ("frame_quality", "帧质量（count / ratio）", "掉帧、慢 UI 线程和总帧数等链路指标。"),
        ("memory", "内存（MB）", f"峰值内存占用，适合看大致量级；当前使用 {report.memory_profile} 内存档位。"),
        ("stability", "稳定性（count）", "崩溃、ANR、StrictMode 等稳定性信号。"),
        ("other", "其他指标", "未归类的补充指标。"),
    ]:
        rows = report.metric_groups.get(group_key, [])
        if rows:
            sections.append(_render_metric_section(title, description, rows))

    quality_note = _quality_note(report)
    stability_sentence = _stability_sentence(report)
    low_confidence_sentence = f"共有 {report.low_confidence_count}/{report.metric_count} 个指标处于低置信度。"
    artifact_sentence = f"本次落盘了 {report.artifact_count} 个 artifact，便于后续追查原始证据。"
    overall_label, overall_class, primary_risk = _overall_assessment(report)
    diagnostics_html = _render_diagnostics(report)
    raw_rows = "".join(_render_raw_metric_row(report, row) for row in report.metrics)
    artifact_groups_html = _render_artifact_groups(report.artifacts)
    executive_cards = _render_executive_cards(report, overall_label, overall_class, primary_risk)
    frame_summary = _render_signal_group(
        "流畅度",
        "优先看卡顿率、P95/P99 和 slow UI thread，它们最能代表当前页面滚动/操作是否顺滑。",
        [
            _metric_signal(report, "frame.jank_rate", "卡顿率", as_percent=True),
            _metric_signal(report, "frame.duration.p95.ms", "P95 帧耗时"),
            _metric_signal(report, "frame.duration.p99.ms", "P99 帧耗时"),
            _metric_signal(report, "frame.slow_ui_thread_count", "UI 线程慢帧"),
            _metric_signal(report, "frame.missed_vsync_count", "Missed VSync"),
            _metric_signal(report, "frame.total_count", "总帧数"),
        ],
    )
    memory_summary = _render_signal_group(
        "内存",
        f"PSS 更接近系统实际为进程承担的内存压力；当前使用 {report.memory_profile} 内存档位，需结合基线增长、重复操作增长和 Java/Native/Graphics 来源判断。",
        [
            _metric_signal(report, "memory.pss.peak_mb", "PSS 峰值"),
            _metric_signal(report, "memory.pss.mean_mb", "PSS 平均"),
            _metric_signal(report, "memory.java_heap.mb", "Java Heap"),
            _metric_signal(report, "memory.native_heap.mb", "Native Heap"),
            _metric_signal(report, "memory.graphics.mb", "Graphics"),
        ],
    )
    stability_summary = _render_signal_group(
        "稳定性",
        "这里聚合 logcat 解析出的崩溃、ANR 和 StrictMode 信号；非 0 时不建议把本次数据作为性能基线。",
        [
            _metric_signal(report, "stability.crash.count", "Crash"),
            _metric_signal(report, "stability.anr.count", "ANR"),
            _metric_signal(report, "stability.strict_mode.count", "StrictMode"),
        ],
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PerfEval Lite Android 报告 - {html.escape(report.run_id)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --ink: #162235;
      --muted: #5d6a7a;
      --line: #d9e1ec;
      --accent: #1c6dd0;
      --accent-soft: #edf5ff;
      --good: #1f7a45;
      --warn: #9a6500;
      --bad: #b42318;
      --low: #7a8797;
      --violet: #6544b8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .page {{
      max-width: 1320px;
      margin: 0 auto;
      padding: 22px;
      overflow-x: hidden;
    }}
    .report-head {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 14px;
    }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 14px;
    }}
    h1, h2, h3, p {{ margin: 0; }}
    h1 {{ font-size: 26px; line-height: 1.2; margin-bottom: 8px; }}
    h2 {{ font-size: 18px; line-height: 1.3; margin-bottom: 10px; }}
    h3 {{ font-size: 15px; line-height: 1.35; margin-bottom: 8px; }}
    .subtitle, .muted {{ color: var(--muted); }}
    .head-grid {{
      display: grid;
      grid-template-columns: minmax(260px, 1.2fr) repeat(3, minmax(160px, 0.8fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .headline-tile, .signal, .evidence-row {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fcfdff;
      padding: 12px;
      min-width: 0;
    }}
    .label {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .value {{
      display: block;
      font-size: 15px;
      font-weight: 700;
      word-break: break-word;
      overflow-wrap: anywhere;
    }}
    .hero-value {{
      font-size: 24px;
      line-height: 1.2;
      color: var(--accent);
    }}
    .callout {{
      border-left: 4px solid var(--accent);
      padding: 12px 14px;
      background: var(--accent-soft);
      border-radius: 8px;
      margin-top: 14px;
    }}
    .callout p + p {{ margin-top: 8px; }}
    .status-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .signal-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin-top: 12px;
    }}
    .signal .value {{
      font-size: 20px;
      font-variant-numeric: tabular-nums;
    }}
    .signal .hint {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 5px;
      overflow-wrap: anywhere;
    }}
    .metric-section + .metric-section {{ margin-top: 18px; }}
    .metric-rows {{
      display: grid;
      gap: 10px;
    }}
    .metric-row {{
      display: grid;
      grid-template-columns: minmax(200px, 1.2fr) minmax(300px, 2fr) 84px 110px 72px;
      gap: 10px;
      align-items: center;
    }}
    .metric-row .name {{
      font-weight: 700;
      word-break: break-word;
    }}
    .metric-row .meta {{
      color: var(--muted);
      font-size: 12px;
    }}
    .track {{
      height: 12px;
      border-radius: 999px;
      background: #e7edf5;
      overflow: hidden;
    }}
    .fill {{
      height: 100%;
      border-radius: 999px;
      background: var(--accent);
    }}
    .fill.low {{ background: var(--low); }}
    .fill.medium {{ background: var(--warn); }}
    .fill.high {{ background: var(--good); }}
    .metric-value {{
      font-variant-numeric: tabular-nums;
      text-align: right;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 8px;
      padding: 4px 10px;
      font-size: 12px;
      border: 1px solid var(--line);
      color: var(--muted);
      background: #fafbfc;
      font-variant-numeric: tabular-nums;
    }}
    .badge.low {{ color: var(--low); }}
    .badge.medium {{ color: var(--warn); }}
    .badge.high {{ color: var(--good); }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 56px;
      border-radius: 8px;
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
    }}
    .status-pill.pass, .tone-good {{ background: #e7f7ee; color: var(--good); }}
    .status-pill.warn, .tone-fair {{ background: #fff5d6; color: var(--warn); }}
    .status-pill.fail, .tone-poor {{ background: #fde9e7; color: var(--bad); }}
    .status-pill.unknown, .tone-none {{ background: #edf2f7; color: var(--low); }}
    .rating {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 700;
    }}
    .rating.excellent {{ background: #e7f7ee; color: var(--good); }}
    .rating.good {{ background: #eef9f0; color: #2a9d4f; }}
    .rating.fair {{ background: #fff5d6; color: var(--warn); }}
    .rating.poor {{ background: #fde9e7; color: var(--bad); }}
    .rating.none {{ background: #edf2f7; color: var(--low); }}
    .range-text {{ font-size: 11px; color: var(--muted); white-space: normal; max-width: 220px; }}
    .metric-id-hint {{ display: block; font-size: 11px; color: var(--muted); font-weight: normal; }}
    details.detail {{
      border-top: 1px solid var(--line);
      margin-top: 14px;
      padding-top: 12px;
      max-width: 100%;
      overflow-x: auto;
    }}
    details.detail > summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--accent);
      list-style-position: inside;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      text-align: center;
      padding: 10px 8px;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
    }}
    th:first-child, td:first-child {{ text-align: left; }}
    td.range-text {{ text-align: left; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .evidence-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .evidence-row {{
      display: grid;
      grid-template-columns: 118px minmax(160px, 1fr) 110px 90px;
      gap: 10px;
      align-items: center;
    }}
    .evidence-row code {{ word-break: break-all; }}
    .artifact-actions {{
      grid-column: 1 / -1;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      padding-top: 4px;
    }}
    .artifact-actions a, .artifact-actions button, .artifact-actions code {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 4px 8px;
      color: var(--accent);
      font: inherit;
      font-size: 12px;
      cursor: pointer;
    }}
    .artifact-actions button {{
      appearance: none;
    }}
    .prebox {{
      margin: 12px 0 0;
      padding: 12px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #101828;
      color: #f8fafc;
      font-size: 12px;
      line-height: 1.5;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .dist-chart {{ width: 100%; height: 300px; margin-top: 8px; }}
    .scope-banner ul {{ margin: 10px 0 0; padding-left: 20px; }}
    .scope-banner li + li {{ margin-top: 5px; }}
    @media (max-width: 980px) {{
      .head-grid {{
        grid-template-columns: 1fr;
      }}
      .signal-grid {{
        grid-template-columns: 1fr;
      }}
      .metric-row {{
        grid-template-columns: 1fr;
      }}
      .metric-value {{
        text-align: left;
      }}
      .evidence-row {{
        grid-template-columns: 1fr;
      }}
      table {{
        min-width: 720px;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="report-head">
      <h1>PerfEval Lite Android 单次采集报告 · 综合评估</h1>
      <p class="subtitle">面向当前 Android 性能诊断的整合 H5 报告：先看结论和证据覆盖，再进入分类指标与原始明细。</p>
      <div class="head-grid">
        {executive_cards}
      </div>
      <div class="callout">
        <p>{html.escape(quality_note)}</p>
        <p>{html.escape(stability_sentence)}</p>
        <p>{html.escape(low_confidence_sentence)}</p>
        <p>{html.escape(artifact_sentence)}</p>
      </div>
    </section>

    {scope_banner}

    <section class="section">
      <h2>分类摘要</h2>
      <p class="muted">同类型数据只展示最能代表问题的指标；缺失项会显示为“-”，详细数据在下方可展开。</p>
      {frame_summary}
      {diagnosis_html}
      {memory_summary}
      {stability_summary}
    </section>

    <section class="section">
      <h2>指标图表</h2>
      <p class="muted">每一行都显示当前数值和一个按组内最大值缩放的条形图，便于快速扫一遍采集结果。</p>
      {''.join(sections)}
    </section>

    {series_section}

    {playback_section}

    {frame_distribution_section}

    <section class="section">
      <h2>原始指标明细</h2>
      <details class="detail" open>
        <summary>展开指标表格</summary>
        <table>
          <thead>
            <tr>
              <th>指标</th>
              <th>数值</th>
              <th>单位</th>
              <th>样本数</th>
              <th>置信度</th>
              <th>证据</th>
              <th>评级</th>
              <th>参考范围</th>
            </tr>
          </thead>
          <tbody>
            {raw_rows}
          </tbody>
        </table>
      </details>
    </section>

    <section class="section">
      <h2>证据与诊断</h2>
      {artifact_groups_html}
      {diagnostics_html}
    </section>
  </div>
  {frame_distribution_script}
  {playback_script}
  {series_script}
  <script>
    (function () {{
      function openPerfetto(event) {{
        event.preventDefault();
        var target = event.currentTarget;
        var href = target.getAttribute("data-trace-href");
        var command = target.getAttribute("data-cli-command");
        var traceUrl = new URL(href, window.location.href).href;
        if (window.location.protocol === "file:") {{
          window.alert("当前报告以 file:// 打开，Perfetto UI 不能直接读取本地文件。请运行：" + "\\n" + command);
          return;
        }}
        window.open("https://ui.perfetto.dev/#!/?url=" + encodeURIComponent(traceUrl), "_blank", "noopener,noreferrer");
      }}
      function copyCommand(event) {{
        var command = event.currentTarget.getAttribute("data-copy-command");
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(command);
        }}
        event.currentTarget.textContent = "已复制命令";
      }}
      document.querySelectorAll("[data-open-perfetto]").forEach(function (node) {{
        node.addEventListener("click", openPerfetto);
      }});
      document.querySelectorAll("[data-copy-command]").forEach(function (node) {{
        node.addEventListener("click", copyCommand);
      }});
    }})();
  </script>
</body>
</html>
"""


def _quality_note(report: SummaryReportData) -> str:
    if report.sample_min <= 1:
        return "本次是一次单次 smoke 采集，样本数只有 1，适合验证采集链路，不适合做回归结论。"
    if report.sample_min < 5:
        return f"本次样本数最少为 {report.sample_min}，适合定位问题，但还不足以做稳定回归结论。"
    return "本次采样数量足够，可以作为回归判断的基础。"


def _stability_sentence(report: SummaryReportData) -> str:
    if report.crash_count or report.anr_count or report.strict_mode_count:
        return (
            f"稳定性信号里观察到 crash={report.crash_count:g}、"
            f"ANR={report.anr_count:g}、StrictMode={report.strict_mode_count:g}。"
        )
    return "稳定性信号里没有看到 crash、ANR 或 StrictMode 异常。"


def _overall_assessment(report: SummaryReportData) -> tuple[str, str, str]:
    ratings = [
        _rate_metric(row.metric_id, row.value, budgets=report.budgets, memory_profile=report.memory_profile)[2]
        for row in report.metrics
        if not row.metric_id.startswith("memory.")
    ]
    memory_ratings = [
        _rate_metric(row.metric_id, row.value, budgets=report.budgets, memory_profile=report.memory_profile)[2]
        for row in report.metrics
        if row.metric_id.startswith("memory.")
    ]
    if "poor" in ratings:
        label = "需重点优化"
        tone = "poor"
    elif "fair" in ratings:
        label = "存在风险"
        tone = "fair"
    elif "poor" in memory_ratings or "fair" in memory_ratings:
        label = "存在风险"
        tone = "fair"
    elif report.low_confidence_count:
        label = "仅供定位"
        tone = "none"
    else:
        label = "表现正常"
        tone = "good"

    risk = "暂未发现明确异常，建议结合业务体感和多轮采样确认。"
    jank = _find_metric(report, "frame.jank_rate")
    p95 = _find_metric(report, "frame.duration.p95.ms")
    memory = _find_metric(report, "memory.pss.peak_mb")
    if jank and jank.value >= 0.10:
        risk = f"卡顿率达到 {_format_percent(jank.value)}，滑动或连续操作会有明显不连续感。"
    elif p95 and p95.value >= 50:
        risk = f"P95 帧耗时达到 {_format_number(p95.value)}ms，尾部帧耗时偏高。"
    elif memory and _memory_metric_exceeds_risk_threshold(report, memory):
        risk = (
            f"PSS 峰值达到 {_format_number(memory.value)}MB，按 {report.memory_profile} 内存档位属于高风险；"
            "建议结合基线增长、重复操作增长以及 Java/Native/Graphics 来源判断。"
        )
    elif report.diagnosis and report.diagnosis.cpu_status == "fail":
        risk = "simpleperf 采集或拉取失败，CPU 热点证据不可用。"
    elif report.diagnosis and report.diagnosis.trace_status == "fail":
        risk = "Perfetto trace 采集或拉取失败，主线程耗时证据不可用。"
    return label, tone, risk


def _memory_metric_exceeds_risk_threshold(report: SummaryReportData, row: SummaryMetricRow) -> bool:
    fail_abs = fail_abs_for_metric(row.metric_id, report.budgets)
    if fail_abs is not None:
        return row.value >= fail_abs
    thresholds = rating_thresholds_for_metric(
        row.metric_id,
        budgets=report.budgets,
        memory_profile=report.memory_profile,
    )
    return bool(thresholds and row.value >= thresholds[2])


def _render_executive_cards(
    report: SummaryReportData,
    overall_label: str,
    overall_class: str,
    primary_risk: str,
) -> str:
    evidence_value = _evidence_coverage_text(report)
    run_hint = (
        f"{_scenario_name_cn(report.scenario_id)} · {report.platform} · "
        f"{report.package_name}"
    )
    serial = report.diagnosis.serial if report.diagnosis else "unknown"
    cards = [
        ("综合结论", overall_label, primary_risk, f"tone-{overall_class}", True),
        ("运行 ID", report.run_id, run_hint, "", False),
        ("设备", serial, f"状态：{_status_cn(report.status)}", "", False),
        ("证据覆盖", evidence_value, f"指标 {report.metric_count} · artifacts {report.artifact_count}", "", False),
    ]
    html_rows = []
    for label, value, hint, cls, large in cards:
        value_cls = f"value {'hero-value ' + cls if large else ''}".strip()
        html_rows.append(
            "<div class=\"headline-tile\">"
            f"<span class=\"label\">{html.escape(label)}</span>"
            f"<span class=\"{html.escape(value_cls)}\">{html.escape(value)}</span>"
            f"<p class=\"muted\">{html.escape(hint)}</p>"
            "</div>"
        )
    return "".join(html_rows)


def _evidence_coverage_text(report: SummaryReportData) -> str:
    categories = {artifact.category for artifact in report.artifacts}
    labels = []
    for key, label in [
        ("frame", "Frame"),
        ("cpu", "CPU"),
        ("trace", "Trace"),
        ("memory", "Memory"),
        ("stability", "Logcat"),
    ]:
        if key in categories:
            labels.append(label)
    if not labels:
        return "未发现证据"
    return " / ".join(labels)


def _render_signal_group(title: str, description: str, signals: Sequence[dict[str, str]]) -> str:
    visible = [signal for signal in signals if signal.get("value") != "-"]
    if not visible:
        visible = list(signals[:3])
    signal_html = "".join(
        "<div class=\"signal\">"
        f"<span class=\"label\">{html.escape(signal['label'])}</span>"
        f"<span class=\"value {html.escape(signal.get('tone', ''))}\">{html.escape(signal['value'])}</span>"
        f"<p class=\"hint\">{html.escape(signal.get('hint', ''))}</p>"
        "</div>"
        for signal in visible
    )
    return (
        "<div class=\"metric-section\">"
        f"<h3>{html.escape(title)}</h3>"
        f"<p class=\"muted\">{html.escape(description)}</p>"
        f"<div class=\"signal-grid\">{signal_html}</div>"
        "</div>"
    )


def _metric_signal(
    report: SummaryReportData,
    metric_id: str,
    label: str,
    *,
    as_percent: bool = False,
) -> dict[str, str]:
    row = _find_metric(report, metric_id)
    if row is None:
        return {"label": label, "value": "-", "hint": metric_id, "tone": "tone-none"}
    rating_label, _range_text, rating_cls = _rate_metric(
        row.metric_id,
        row.value,
        budgets=report.budgets,
        memory_profile=report.memory_profile,
    )
    value = _format_percent(row.value) if as_percent else _format_metric_value(row)
    hint = f"{_confidence_cn(row.confidence)}置信度 · {rating_label} · {row.metric_id}"
    return {"label": label, "value": value, "hint": hint, "tone": f"tone-{_tone_from_rating(rating_cls)}"}


def _find_metric(report: SummaryReportData, metric_id: str) -> SummaryMetricRow | None:
    for row in report.metrics:
        if row.metric_id == metric_id:
            return row
    return None


def _tone_from_rating(rating_cls: str) -> str:
    if rating_cls in {"excellent", "good"}:
        return "good"
    if rating_cls == "fair":
        return "fair"
    if rating_cls == "poor":
        return "poor"
    return "none"


def _format_metric_value(row: SummaryMetricRow) -> str:
    if row.unit == "ratio":
        return _format_percent(row.value)
    if row.unit and row.unit not in {"-", "count"}:
        return f"{_format_number(row.value)} {row.unit}"
    return _format_number(row.value)


def _format_percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def _render_diagnosis_section(report: SummaryReportData, *, include_echarts_src: bool = False) -> str:
    diagnosis = report.diagnosis
    if diagnosis is None:
        return _render_signal_group(
            "CPU / Trace",
            "本次没有 diagnosis.json；CPU 热点和 Perfetto trace 状态只能从 artifact 列表间接判断。",
            [
                {"label": "CPU 采样", "value": "未采集", "hint": "运行 diagnose android --mode cpu/full", "tone": "tone-none"},
                {"label": "Perfetto Trace", "value": "未采集", "hint": "运行 diagnose android --mode trace/full", "tone": "tone-none"},
            ],
        )

    signal_html = _render_signal_group(
        "CPU / Trace",
        "full 模式会在同一次聚焦流程内同时启动 simpleperf 与 Perfetto；这里展示它们是否真的生成了可用证据。",
        [
            {
                "label": "Frame 快照",
                "value": _diagnosis_status_label(diagnosis.frame_status),
                "hint": "gfxinfo / meminfo / logcat",
                "tone": f"tone-{_status_tone(diagnosis.frame_status)}",
            },
            {
                "label": "CPU 采样",
                "value": _diagnosis_status_label(diagnosis.cpu_status),
                "hint": "simpleperf perf.data + report",
                "tone": f"tone-{_status_tone(diagnosis.cpu_status)}",
            },
            {
                "label": "Perfetto Trace",
                "value": _diagnosis_status_label(diagnosis.trace_status),
                "hint": ".pftrace，可用 Perfetto UI / Android Studio 打开",
                "tone": f"tone-{_status_tone(diagnosis.trace_status)}",
            },
            {
                "label": "采集时长",
                "value": f"{diagnosis.duration_seconds}s" if diagnosis.duration_seconds else "-",
                "hint": f"mode={diagnosis.mode} · {', '.join(diagnosis.modes) or '-'}",
                "tone": "tone-none",
            },
        ],
    )
    excerpt = ""
    if diagnosis.simpleperf_excerpt:
        excerpt = (
            "<details class=\"detail\">"
            "<summary>simpleperf-report.txt 摘要</summary>"
            f"<pre class=\"prebox\">{html.escape(chr(10).join(diagnosis.simpleperf_excerpt))}</pre>"
            "</details>"
        )
    # 火焰图区块 + stat 卡（flamegraph-compare-missing 缺口，2026-11-10）：
    # 由 artifacts/simpleperf-report.txt 解析（与后端 deep_analysis 数据面同一解析）；
    # report 不可解析时如实保留原 12 行摘录，不回退伪造。
    simpleperf_block = ""
    face = diagnosis.simpleperf_face
    if face is not None and (face.get("tree") or face.get("events")):
        simpleperf_block = _render_simpleperf_block(face, include_echarts_src=include_echarts_src)
    return signal_html + simpleperf_block + excerpt


def _diagnosis_status_label(status: str) -> str:
    return {"pass": "可用", "warn": "需确认", "fail": "失败", "unknown": "未采集"}.get(status, status)


def _status_tone(status: str) -> str:
    return {"pass": "good", "warn": "fair", "fail": "poor"}.get(status, "none")


def _render_artifact_groups(artifacts: Sequence[SummaryArtifact]) -> str:
    if not artifacts:
        return "<p class=\"muted\">无 artifact</p>"
    grouped: dict[str, list[SummaryArtifact]] = defaultdict(list)
    for artifact in artifacts:
        grouped[artifact.category].append(artifact)
    labels = {
        "frame": "流畅度证据",
        "cpu": "CPU 证据",
        "trace": "Trace 证据",
        "memory": "内存证据",
        "stability": "稳定性证据",
        "other": "其他证据",
    }
    parts = []
    for category in sorted(grouped, key=_artifact_group_order):
        rows = "".join(_render_artifact_row(artifact) for artifact in grouped[category])
        open_attr = " open" if category in {"frame", "cpu", "trace"} else ""
        parts.append(
            f"<details class=\"detail\"{open_attr}>"
            f"<summary>{html.escape(labels.get(category, category))}（{len(grouped[category])}）</summary>"
            f"<div class=\"evidence-list\">{rows}</div>"
            "</details>"
        )
    return "".join(parts)


def _render_artifact_row(artifact: SummaryArtifact) -> str:
    metrics = ", ".join(artifact.related_metrics) if artifact.related_metrics else "-"
    state = "pass" if artifact.present and artifact.note == "可用" else "warn"
    actions = _render_artifact_actions(artifact)
    return (
        "<div class=\"evidence-row\">"
        f"<span><span class=\"label\">collector</span>{html.escape(artifact.collector)}</span>"
        f"<span><span class=\"label\">path</span><a href=\"{html.escape(_safe_href(artifact.href))}\"><code>{html.escape(artifact.path)}</code></a></span>"
        f"<span><span class=\"label\">size</span>{html.escape(_format_size(artifact.size_bytes))}</span>"
        f"<span><span class=\"status-pill {state}\">{html.escape(artifact.note)}</span></span>"
        f"<span class=\"muted\" style=\"grid-column: 1 / -1;\">related: {html.escape(metrics)}</span>"
        f"{actions}"
        "</div>"
    )


def _render_artifact_actions(artifact: SummaryArtifact) -> str:
    if artifact.category != "trace" or not artifact.path.endswith(".pftrace"):
        return ""
    android_studio = f"perfeval open {shlex.quote(artifact.path)} --viewer android-studio"
    perfetto_ui = f"perfeval open {shlex.quote(artifact.path)} --viewer perfetto-ui"
    safe_href = _safe_href(artifact.href)
    return (
        "<div class=\"artifact-actions\">"
        f"<a href=\"{html.escape(safe_href)}\">打开/下载 trace 文件</a>"
        f"<a href=\"https://ui.perfetto.dev/\" data-open-perfetto data-trace-href=\"{html.escape(safe_href)}\" data-cli-command=\"{html.escape(perfetto_ui)}\">Perfetto UI 直接打开</a>"
        f"<button type=\"button\" data-copy-command=\"{html.escape(android_studio)}\">复制 Android Studio 打开命令</button>"
        f"<button type=\"button\" data-copy-command=\"{html.escape(perfetto_ui)}\">复制 Perfetto UI 打开命令</button>"
        f"<code>{html.escape(android_studio)}</code>"
        f"<code>{html.escape(perfetto_ui)}</code>"
        "</div>"
    )


def _safe_href(value: str) -> str:
    stripped = value.strip()
    scheme = urllib.parse.urlsplit(stripped).scheme.lower()
    if scheme and scheme not in {"http", "https", "file"}:
        if not (len(scheme) == 1 and len(stripped) > 1 and stripped[1] == ":"):
            return "#"
    return value


def _render_diagnostics(report: SummaryReportData) -> str:
    items: list[str] = []
    raw_summary_diagnostics = report.raw_summary.get("diagnostics")
    if isinstance(raw_summary_diagnostics, Sequence) and not isinstance(raw_summary_diagnostics, (str, bytes, bytearray)):
        items.extend(_diagnostic_text(item) for item in raw_summary_diagnostics)
    elif raw_summary_diagnostics:
        items.append(str(raw_summary_diagnostics))
    if report.diagnosis is not None:
        items.extend(report.diagnosis.diagnostics)
    items = [item for item in dict.fromkeys(items) if item]
    if not items:
        return "<p class=\"muted\">没有诊断异常或采集错误。</p>"
    rows = "".join(f"<li><code>{html.escape(item)}</code></li>" for item in items)
    return (
        "<details class=\"detail\" open>"
        f"<summary>诊断消息（{len(items)}）</summary>"
        f"<ul>{rows}</ul>"
        "</details>"
    )


def _diagnostic_text(item: object) -> str:
    if isinstance(item, Mapping):
        data = item.get("data") if isinstance(item.get("data"), Mapping) else item
        code = data.get("code") if isinstance(data, Mapping) else None
        message = data.get("message") if isinstance(data, Mapping) else None
        name = item.get("name")
        return str(message or code or name or item)
    return str(item)


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "-"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _render_metric_section(title: str, description: str, rows: Sequence[SummaryMetricRow]) -> str:
    max_value = max((row.value for row in rows), default=1.0)
    if max_value <= 0:
        max_value = 1.0
    if title.startswith("启动"):
        max_value = max(max_value, 500.0)
    elif title.startswith("帧时序") and max_value < 10:
        max_value = 10.0
    elif title.startswith("内存") and max_value < 64:
        max_value = 64.0
    elif title.startswith("稳定性") and max_value < 1:
        max_value = 1.0
    elif title.startswith("帧质量") and max_value < 1:
        max_value = 1.0

    row_html = []
    for row in rows:
        fill_width = min(100.0, (row.value / max_value) * 100.0)
        row_html.append(
            f"""
            <div class="metric-row">
              <div>
                <div class="name">{html.escape(_metric_name_cn(row.metric_id))}<span class="metric-id-hint">{html.escape(row.metric_id)}</span></div>
                <div class="meta">样本数 {row.sample_count} · 证据 {html.escape(row.evidence)}</div>
              </div>
              <div class="track"><div class="fill {html.escape(row.confidence)}" style="width: {fill_width:.2f}%"></div></div>
              <div class="metric-value">{_format_number(row.value)}</div>
              <div class="metric-value">{html.escape(row.unit)}</div>
              <div class="badge {html.escape(row.confidence)}">{html.escape(_confidence_cn(row.confidence))}</div>
            </div>
            """
        )
    return f"""
    <div class="metric-section">
      <h3>{html.escape(title)}</h3>
      <p class="muted" style="margin-bottom: 10px;">{html.escape(description)}</p>
      <div class="metric-rows">
        {''.join(row_html)}
      </div>
    </div>
    """


def _render_raw_metric_row(report: SummaryReportData, row: SummaryMetricRow) -> str:
    rating_label, range_text, rating_cls = _rate_metric(
        row.metric_id,
        row.value,
        budgets=report.budgets,
        memory_profile=report.memory_profile,
    )
    return (
        "<tr>"
        f"<td>{html.escape(_metric_name_cn(row.metric_id))}<span class=\"metric-id-hint\">{html.escape(row.metric_id)}</span></td>"
        f"<td>{_format_number(row.value)}</td>"
        f"<td>{html.escape(row.unit)}</td>"
        f"<td>{row.sample_count}</td>"
        f"<td>{html.escape(_confidence_cn(row.confidence))}</td>"
        f"<td>{html.escape(row.evidence)}</td>"
        f'<td><span class="rating {rating_cls}">{rating_label}</span></td>'
        f'<td class="range-text">{html.escape(range_text)}</td>'
        "</tr>"
    )


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _coerce_text(value: object | None) -> str:
    if value is None:
        return "unknown"
    text = str(value).strip()
    return text if text else "unknown"


# ---------------------------------------------------------------------------
# Multi-run report (多轮对比报告)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiRunMetricRow:
    metric_id: str
    unit: str
    direction: str
    confidence: str
    evidence: str
    n: int
    min_val: float
    max_val: float
    mean: float
    median: float
    p75: float
    p90: float
    p95: float
    mad: float
    samples: list[dict[str, Any]]


@dataclass(frozen=True)
class MultiRunReportData:
    run_id: str
    scenario_id: str
    status: str
    package_name: str
    platform: str
    memory_profile: str
    budgets: dict[str, dict[str, Any]]
    repeats: int
    metrics: list[MultiRunMetricRow]
    run_ids: list[str]
    diagnostics: list[dict[str, Any]]


_TREND_MAX_SERIES_PER_GROUP = 5


def _trend_unit_key(unit: str) -> str:
    normalized = unit.strip().lower().replace(" ", "_")
    return normalized or "unknown"


def _trend_group_spec(metric: MultiRunMetricRow) -> dict[str, Any]:
    metric_id = metric.metric_id
    unit_key = _trend_unit_key(metric.unit)
    is_ratio = unit_key in {"ratio", "percent", "percentage", "%"} or "rate" in metric_id
    is_count = unit_key in {"count", "counts", "event", "events"} or metric_id.endswith(".count")

    if metric_id.startswith("startup."):
        return {
            "key": f"startup:{unit_key}",
            "order": 0,
            "eyebrow": "启动 · TIME",
            "title": "启动耗时",
            "description": "冷启动、温启动与热启动共享耗时坐标，独立缩放以保留轮次间差异。",
            "unit": metric.unit,
            "multiplier": 1.0,
            "axis_min_zero": False,
            "integer_axis": False,
        }
    if metric_id.startswith("frame.duration."):
        return {
            "key": f"frame-duration:{unit_key}",
            "order": 10,
            "eyebrow": "帧时序 · TIME",
            "title": "帧耗时分位",
            "description": "P50、P90、P95、P99 等帧耗时在同一毫秒坐标中比较。",
            "unit": metric.unit,
            "multiplier": 1.0,
            "axis_min_zero": False,
            "integer_axis": False,
        }
    if metric_id.startswith("frame.") and is_ratio:
        multiplier = 1.0 if unit_key in {"percent", "percentage", "%"} else 100.0
        return {
            "key": "frame-rate:percent",
            "order": 20,
            "eyebrow": "流畅度 · RATE",
            "title": "卡顿与慢帧比例",
            "description": "比例指标统一换算为百分比，零值也能在坐标轴中准确显示。",
            "unit": "%",
            "multiplier": multiplier,
            "axis_min_zero": True,
            "integer_axis": False,
        }
    if metric_id == "frame.total_count":
        return {
            "key": "frame-volume:count",
            "order": 30,
            "eyebrow": "采样规模 · COUNT",
            "title": "渲染帧规模",
            "description": "总帧数单独成轴，避免数量级过大而压低卡顿事件曲线。",
            "unit": "帧",
            "multiplier": 1.0,
            "axis_min_zero": False,
            "integer_axis": True,
            "direction_override": "neutral",
        }
    if metric_id.startswith("frame.") and is_count:
        return {
            "key": "frame-events:count",
            "order": 40,
            "eyebrow": "异常事件 · COUNT",
            "title": "帧异常事件",
            "description": "卡顿帧、丢 VSync 与 UI 线程慢帧等事件共享次数坐标。",
            "unit": "次",
            "multiplier": 1.0,
            "axis_min_zero": True,
            "integer_axis": True,
        }
    if metric_id.startswith("memory."):
        return {
            "key": f"memory:{unit_key}",
            "order": 50,
            "eyebrow": "资源 · MEMORY",
            "title": "内存占用",
            "description": "同量纲内存指标共用坐标，独立缩放以突出跨轮次漂移。",
            "unit": metric.unit,
            "multiplier": 1.0,
            "axis_min_zero": False,
            "integer_axis": False,
        }
    if metric_id.startswith("stability."):
        return {
            "key": "stability:count",
            "order": 60,
            "eyebrow": "稳定性 · COUNT",
            "title": "稳定性事件",
            "description": "崩溃、ANR 与违规事件从零开始计数，异常抬升会直接显现。",
            "unit": "次",
            "multiplier": 1.0,
            "axis_min_zero": True,
            "integer_axis": True,
        }

    family = metric_id.split(".", 1)[0] if "." in metric_id else "other"
    family_names = {
        "cpu": "CPU 指标",
        "gpu": "GPU 指标",
        "network": "网络指标",
        "energy": "能耗指标",
        "io": "I/O 指标",
    }
    display_unit = "%" if is_ratio else ("次" if is_count else metric.unit)
    multiplier = 100.0 if is_ratio and unit_key not in {"percent", "percentage", "%"} else 1.0
    return {
        "key": f"other:{family}:{display_unit}",
        "order": 100,
        "eyebrow": f"{family.upper()} · {display_unit}",
        "title": family_names.get(family, "其他指标"),
        "description": f"按 {display_unit} 量纲归组，避免与不可比指标共用坐标。",
        "unit": display_unit,
        "multiplier": multiplier,
        "axis_min_zero": is_ratio or is_count,
        "integer_axis": is_count,
    }


def _trend_group_summary(series: Sequence[Mapping[str, Any]], unit: str) -> tuple[str, str]:
    changes: list[tuple[float, str, str]] = []
    comparable_count = 0
    stable_count = 0

    for item in series:
        values = [float(value) for value in item.get("values", []) if isinstance(value, (int, float))]
        if len(values) < 2:
            continue
        comparable_count += 1
        first = values[0]
        last = values[-1]
        delta = last - first
        if abs(delta) < 1e-12:
            stable_count += 1
            continue

        direction = str(item.get("direction") or "-")
        if direction == "lower_better":
            tone = "good" if delta < 0 else "bad"
        elif direction == "higher_better":
            tone = "good" if delta > 0 else "bad"
        else:
            tone = "neutral"

        arrow = "↓" if delta < 0 else "↑"
        if unit == "%":
            change_text = f"{arrow} {abs(delta):.2f} 个百分点"
            score = abs(delta)
        elif first:
            relative_change = delta / abs(first) * 100.0
            change_text = f"{arrow} {abs(relative_change):.1f}%"
            score = abs(relative_change)
        else:
            change_text = f"{_format_number(first)} → {_format_number(last)} {unit}"
            score = abs(delta)

        changes.append((score, f"{item.get('name', '指标')} {change_text}", tone))

    if changes:
        _score, change_text, tone = max(changes, key=lambda item: item[0])
        direction_text = {"good": "方向向好", "bad": "需要关注", "neutral": "方向待结合业务判断"}[tone]
        return (f"有效首末轮变化最大：{change_text} · {direction_text}", tone)
    if comparable_count and stable_count == comparable_count:
        return (f"{stable_count} 项指标在有效首末轮保持不变", "neutral")
    return ("至少需要 2 轮有效样本才能判断趋势", "neutral")


def _build_metric_trend_groups(
    metrics: Sequence[MultiRunMetricRow],
    run_ids: Sequence[str],
) -> list[dict[str, Any]]:
    grouped_metrics: dict[str, list[MultiRunMetricRow]] = defaultdict(list)
    group_specs: dict[str, dict[str, Any]] = {}

    for metric in metrics:
        spec = _trend_group_spec(metric)
        grouped_metrics[spec["key"]].append(metric)
        group_specs[spec["key"]] = spec

    groups: list[dict[str, Any]] = []
    ordered_keys = sorted(grouped_metrics, key=lambda key: (group_specs[key]["order"], key))
    for key in ordered_keys:
        spec = group_specs[key]
        rows = sorted(grouped_metrics[key], key=lambda row: row.metric_id)
        chunk_count = (len(rows) + _TREND_MAX_SERIES_PER_GROUP - 1) // _TREND_MAX_SERIES_PER_GROUP
        for chunk_index in range(chunk_count):
            chunk = rows[
                chunk_index * _TREND_MAX_SERIES_PER_GROUP : (chunk_index + 1) * _TREND_MAX_SERIES_PER_GROUP
            ]
            series: list[dict[str, Any]] = []
            for metric in chunk:
                value_map = _sample_value_map(metric)
                multiplier = float(spec["multiplier"])
                values = [
                    round(value_map[run_id] * multiplier, 9) if run_id in value_map else None
                    for run_id in run_ids
                ]
                series.append(
                    {
                        "metric_id": metric.metric_id,
                        "name": _metric_name_cn(metric.metric_id),
                        "unit": spec["unit"],
                        "direction": spec.get("direction_override", metric.direction),
                        "values": values,
                    }
                )

            summary, summary_tone = _trend_group_summary(series, str(spec["unit"]))
            title = str(spec["title"])
            if chunk_count > 1:
                title = f"{title}（{chunk_index + 1}/{chunk_count}）"
            groups.append(
                {
                    "key": f"{key}:{chunk_index}",
                    "eyebrow": spec["eyebrow"],
                    "title": title,
                    "description": spec["description"],
                    "unit": spec["unit"],
                    "axis_min_zero": spec["axis_min_zero"],
                    "integer_axis": spec["integer_axis"],
                    "summary": summary,
                    "summary_tone": summary_tone,
                    "series": series,
                }
            )
    return groups


def _render_trend_cards(groups: Sequence[Mapping[str, Any]]) -> str:
    if not groups:
        return '<div class="trend-empty">暂无可用于趋势分析的逐轮指标。</div>'

    accent_tokens = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5", "--chart-6"]
    cards: list[str] = []
    for index, group in enumerate(groups):
        accent = accent_tokens[index % len(accent_tokens)]
        series_count = len(group.get("series", []))
        aria_label = f"{group.get('title', '指标趋势')}，{series_count} 项指标，纵轴单位 {group.get('unit', '-')}"
        cards.append(
            f"""
            <article class="trend-card" style="--trend-accent: var({accent});">
              <div class="trend-card-head">
                <div>
                  <div class="trend-eyebrow">{html.escape(str(group.get('eyebrow', 'TREND')))}</div>
                  <h3>{html.escape(str(group.get('title', '指标趋势')))}</h3>
                  <p>{html.escape(str(group.get('description', '')))}</p>
                </div>
                <span class="trend-axis-pill">独立 Y 轴 · {html.escape(str(group.get('unit', '-')))}</span>
              </div>
              <div class="trend-summary {html.escape(str(group.get('summary_tone', 'neutral')))}">
                <span class="trend-summary-mark" aria-hidden="true"></span>
                {html.escape(str(group.get('summary', '')))}
              </div>
              <div id="trend-chart-{index}" class="trend-chart" role="img" aria-label="{html.escape(aria_label)}"></div>
              <div class="trend-card-foot"><span>{series_count} 项指标</span><span>线性坐标 · 缺失样本保留断点</span></div>
            </article>
            """
        )
    return "".join(cards)


def write_multi_run_report_bundle(summary: Mapping[str, Any], output_dir: str | Path) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {
        "report_html": directory / "report.html",
        "summary_json": directory / "summary.json",
    }
    report_data = _build_multi_run_data(summary)
    outputs["report_html"].write_text(_render_multi_run_html(report_data), encoding="utf-8")
    write_json_document(outputs["summary_json"], summary)
    vendor_src = Path(__file__).parent / "vendor" / "echarts.min.js"
    echarts_dst = directory / "echarts.min.js"
    if vendor_src.exists() and vendor_src.is_file():
        shutil.copyfile(vendor_src, echarts_dst)
    else:
        echarts_dst.write_text("/* echarts.min.js missing */", encoding="utf-8")
    return outputs


def _build_multi_run_data(summary: Mapping[str, Any]) -> MultiRunReportData:
    raw_metrics = summary.get("metrics", {})
    metrics: list[MultiRunMetricRow] = []
    run_ids_ordered: list[str] = []

    for metric_id, metric_data in raw_metrics.items():
        if not isinstance(metric_data, Mapping):
            continue
        value = numeric_value_from_mapping(metric_data)
        if value is None:
            continue
        raw_samples = metric_data.get("samples", [])
        if not isinstance(raw_samples, list):
            raw_samples = []
        samples: list[dict[str, Any]] = []
        for sample in raw_samples:
            if isinstance(sample, Mapping):
                samples.append(dict(sample))
                rid = sample.get("runId")
                if isinstance(rid, str) and rid and rid not in run_ids_ordered:
                    run_ids_ordered.append(rid)
        metrics.append(
            MultiRunMetricRow(
                metric_id=str(metric_id),
                unit=_coerce_text(metric_data.get("unit") or "-"),
                direction=_coerce_text(metric_data.get("direction") or "-"),
                confidence=_coerce_text(metric_data.get("confidence") or "low"),
                evidence=_coerce_text(metric_data.get("evidenceLevel") or "-"),
                n=int(metric_data.get("n") or 0),
                min_val=float(metric_data.get("min") or 0),
                max_val=float(metric_data.get("max") or 0),
                mean=float(metric_data.get("mean") or 0),
                median=float(metric_data.get("median") or 0),
                p75=float(metric_data.get("p75") or 0),
                p90=float(metric_data.get("p90") or 0),
                p95=float(metric_data.get("p95") or 0),
                mad=float(metric_data.get("mad") or 0),
                samples=samples,
            )
        )

    metrics.sort(key=lambda m: (_group_order(_metric_group(m.metric_id)), m.metric_id))

    run_id = _coerce_text(summary.get("runId") or "run")
    scenario_id = _coerce_text(summary.get("scenarioId") or _scenario_id_from_summary(summary) or "unknown")
    status = _coerce_text(summary.get("status") or "unknown")
    app = summary.get("app") if isinstance(summary.get("app"), Mapping) else {}
    package_name = _coerce_text(app.get("packageName") if isinstance(app, Mapping) else "unknown")
    platform = _coerce_text(app.get("platform") if isinstance(app, Mapping) else "unknown")
    memory_profile, _profile_diagnostic = normalize_memory_profile(summary.get("memoryProfile"))
    budgets = normalize_budget_metrics(summary.get("budgets"))
    repeats = int(summary.get("repeats") or len(run_ids_ordered) or 1)
    diagnostics = summary.get("diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []

    return MultiRunReportData(
        run_id=run_id,
        scenario_id=scenario_id,
        status=status,
        package_name=package_name,
        platform=platform,
        memory_profile=memory_profile,
        budgets=budgets,
        repeats=repeats,
        metrics=metrics,
        run_ids=run_ids_ordered,
        diagnostics=diagnostics,
    )


def _sample_value_map(row: MultiRunMetricRow) -> dict[str, float]:
    """返回 runId -> 该轮样本中位数的映射。"""
    grouped: dict[str, list[float]] = defaultdict(list)
    for sample in row.samples:
        rid = sample.get("runId")
        val = sample.get("value")
        if isinstance(rid, str) and isinstance(val, (int, float)) and not isinstance(val, bool):
            grouped[rid].append(float(val))
    return {run_id: float(median(values)) for run_id, values in grouped.items() if values}


def _render_multi_run_html(data: MultiRunReportData) -> str:
    run_labels = [f"#{i + 1}" for i in range(len(data.run_ids))]
    run_id_labels = data.run_ids

    # 概览卡片
    overview_cards = [
        ("运行 ID", data.run_id),
        ("场景", _scenario_name_cn(data.scenario_id)),
        ("包名", data.package_name),
        ("平台", data.platform),
        ("重复次数", str(data.repeats)),
        ("状态", _status_cn(data.status)),
        ("指标数", str(len(data.metrics))),
    ]
    overview_html = "".join(
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div></div>'
        for label, value in overview_cards
    )

    # 逐次对比表
    per_run_header = "".join(f"<th>{html.escape(label)}</th>" for label in run_labels)
    per_run_rows = []
    for metric in data.metrics:
        value_map = _sample_value_map(metric)
        cells = "".join(
            f"<td>{_format_number(value_map[rid])}</td>" if rid in value_map else "<td>-</td>"
            for rid in run_id_labels
        )
        per_run_rows.append(
            f"<tr><td class=\"metric-name\">{html.escape(_metric_name_cn(metric.metric_id))}"
            f"<span class=\"metric-id-hint\">{html.escape(metric.metric_id)}</span></td>"
            f"<td>{html.escape(metric.unit)}</td>{cells}</tr>"
        )

    # 汇总统计表
    stats_rows = []
    for metric in data.metrics:
        rating_label, range_text, rating_cls = _rate_metric(
            metric.metric_id,
            metric.median,
            budgets=data.budgets,
            memory_profile=data.memory_profile,
        )
        stats_rows.append(
            f"<tr>"
            f"<td class=\"metric-name\">{html.escape(_metric_name_cn(metric.metric_id))}"
            f"<span class=\"metric-id-hint\">{html.escape(metric.metric_id)}</span></td>"
            f"<td>{html.escape(metric.unit)}</td>"
            f"<td>{metric.n}</td>"
            f"<td>{_format_number(metric.min_val)}</td>"
            f"<td>{_format_number(metric.max_val)}</td>"
            f"<td>{_format_number(metric.mean)}</td>"
            f"<td class=\"highlight\">{_format_number(metric.median)}</td>"
            f"<td>{_format_number(metric.p90)}</td>"
            f"<td>{_format_number(metric.p95)}</td>"
            f"<td>{_format_number(metric.mad)}</td>"
            f"<td><span class=\"badge {html.escape(metric.confidence)}\">{html.escape(_confidence_cn(metric.confidence))}</span></td>"
            f'<td><span class="rating {rating_cls}">{rating_label}</span></td>'
            f'<td class="range-text">{html.escape(range_text)}</td>'
            f"</tr>"
        )

    # 诊断表
    diag_rows = []
    for diag in data.diagnostics:
        if not isinstance(diag, Mapping):
            continue
        d = diag.get("data") if isinstance(diag.get("data"), Mapping) else diag
        severity = d.get("severity", "-")
        code = d.get("code", "-")
        message = d.get("message", "-")
        source = diag.get("source", {}) if isinstance(diag.get("source"), Mapping) else {}
        run_ref = diag.get("runId") or (source.get("runId", "-") if isinstance(source, Mapping) else "-")
        diag_rows.append(
            f"<tr><td>{html.escape(str(run_ref))}</td>"
            f"<td><span class=\"badge {html.escape(str(severity))}\">{html.escape(_severity_cn(str(severity)))}</span></td>"
            f"<td>{html.escape(str(code))}</td>"
            f"<td class=\"diag-msg\">{html.escape(str(message))}</td></tr>"
        )
    if not diag_rows:
        diag_rows.append("<tr><td colspan=\"4\" class=\"muted\">无诊断信息</td></tr>")

    # 图表数据：按性能语义和量纲拆分为多个独立坐标系。
    trend_groups = _build_metric_trend_groups(data.metrics, run_id_labels)
    trend_cards_html = _render_trend_cards(trend_groups)
    chart_json = json.dumps(
        {"run_labels": run_labels, "run_ids": run_id_labels, "groups": trend_groups},
        ensure_ascii=False,
    ).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PerfEval Lite 多轮对比报告 - {html.escape(data.run_id)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --ink: #162235;
      --muted: #5d6a7a;
      --line: #d9e1ec;
      --accent: #1c6dd0;
      --good: #1f7a45;
      --warn: #9a6500;
      --bad: #b42318;
      --low: #7a8797;
      --surface-subtle: #fcfdff;
      --surface-accent-soft: #edf5ff;
      --surface-good-soft: #e9f7ef;
      --surface-bad-soft: #fff0ee;
      --chart-grid: #e6ebf2;
      --chart-axis: #8492a6;
      --chart-zoom-fill: rgba(36,104,216,0.12);
      --chart-1: #2468d8;
      --chart-2: #008b8b;
      --chart-3: #b56a00;
      --chart-4: #7a54c7;
      --chart-5: #c84f5a;
      --chart-6: #3f7d4c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
    }}
    .page {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 18px;
      box-shadow: 0 1px 2px rgba(16,24,40,0.04);
      margin-bottom: 18px;
      min-width: 0;
    }}
    h1, h2, h3, p {{ margin: 0; }}
    h1 {{ font-size: 26px; margin-bottom: 6px; }}
    h2 {{ font-size: 18px; margin-bottom: 12px; }}
    .subtitle, .muted {{ color: var(--muted); }}
    .status-badge {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 13px;
    }}
    .status-badge.completed, .status-badge.pass {{ background: #e7f7ee; color: var(--good); }}
    .status-badge.partial, .status-badge.warn {{ background: #fff5d6; color: var(--warn); }}
    .status-badge.failed, .status-badge.fail {{ background: #fde9e7; color: var(--bad); }}
    .status-badge.unknown {{ background: #edf2f7; color: var(--low); }}
    .overview {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fcfdff;
      padding: 12px;
    }}
    .card .label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .card .value {{ display: block; font-size: 15px; font-weight: 700; word-break: break-word; }}
    .chart-box {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface-subtle);
      padding: 12px;
    }}
    .trend-section-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 16px;
    }}
    .trend-section-head h2 {{ margin-bottom: 6px; }}
    .trend-method {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      max-width: 520px;
    }}
    .method-chip {{
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface-subtle);
      color: var(--muted);
      padding: 5px 10px;
      font-size: 12px;
      white-space: nowrap;
    }}
    .trend-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .trend-card {{
      --trend-accent: var(--chart-1);
      position: relative;
      min-width: 0;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--surface-subtle);
      padding: 18px 18px 12px;
    }}
    .trend-card::before {{
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 3px;
      background: var(--trend-accent);
    }}
    .trend-card-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
    }}
    .trend-card-head h3 {{ font-size: 16px; margin: 2px 0 4px; }}
    .trend-card-head p {{ color: var(--muted); font-size: 12px; max-width: 470px; }}
    .trend-eyebrow {{
      color: var(--trend-accent);
      font: 700 11px/1.2 ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
      letter-spacing: 0.08em;
    }}
    .trend-axis-pill {{
      flex: 0 0 auto;
      border: 1px solid color-mix(in srgb, var(--trend-accent) 35%, var(--line));
      border-radius: 8px;
      background: color-mix(in srgb, var(--trend-accent) 7%, white);
      color: var(--ink);
      padding: 5px 8px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .trend-summary {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 34px;
      margin-top: 13px;
      border-radius: 9px;
      padding: 7px 10px;
      background: var(--surface-accent-soft);
      color: var(--muted);
      font-size: 12px;
    }}
    .trend-summary-mark {{
      width: 7px;
      height: 7px;
      flex: 0 0 auto;
      border-radius: 50%;
      background: currentColor;
    }}
    .trend-summary.good {{ background: var(--surface-good-soft); color: var(--good); }}
    .trend-summary.bad {{ background: var(--surface-bad-soft); color: var(--bad); }}
    .trend-chart {{ width: 100%; height: 310px; margin-top: 4px; }}
    .trend-card-foot {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      border-top: 1px solid var(--chart-grid);
      padding-top: 9px;
      color: var(--muted);
      font: 11px/1.4 ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
    }}
    .trend-empty {{
      border: 1px dashed var(--line);
      border-radius: 12px;
      padding: 32px;
      text-align: center;
      color: var(--muted);
      background: var(--surface-subtle);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      text-align: center;
      padding: 8px 10px;
      white-space: nowrap;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    td.metric-name, th:first-child {{ text-align: left; font-weight: 700; }}
    td.range-text {{ text-align: left; white-space: normal !important; }}
    td.diag-msg {{ text-align: left; white-space: normal; max-width: 400px; }}
    td.highlight {{ font-weight: 700; color: var(--accent); }}
    .table-scroll {{ max-width: 100%; overflow-x: auto; }}
    .badge {{
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 11px;
      border: 1px solid var(--line);
      color: var(--muted);
    }}
    .badge.high {{ color: var(--good); border-color: #b7e4c7; }}
    .badge.medium {{ color: var(--warn); border-color: #f5d38f; }}
    .badge.low {{ color: var(--low); border-color: #d3dae6; }}
    .badge.error {{ color: var(--bad); border-color: #f4c7c3; background: #fff4f3; }}
    .badge.warning {{ color: var(--warn); border-color: #f5d38f; background: #fff8e8; }}
    .badge.info {{ color: var(--accent); border-color: #c4d9f0; }}
    .rating {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
    }}
    .rating.excellent {{ background: #e7f7ee; color: var(--good); }}
    .rating.good {{ background: #eef9f0; color: #2a9d4f; }}
    .rating.fair {{ background: #fff5d6; color: var(--warn); }}
    .rating.poor {{ background: #fde9e7; color: var(--bad); }}
    .rating.none {{ background: #edf2f7; color: var(--low); }}
    .range-text {{ font-size: 11px; color: var(--muted); white-space: normal !important; max-width: 220px; }}
    .metric-id-hint {{ display: block; font-size: 11px; color: var(--muted); font-weight: normal; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace; font-size: 12px; }}
    @media (max-width: 980px) {{
      .trend-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 680px) {{
      .page {{ padding: 12px; }}
      .panel {{ padding: 14px; }}
      .trend-section-head, .trend-card-head {{ display: block; }}
      .trend-method {{ justify-content: flex-start; margin-top: 12px; }}
      .trend-axis-pill {{ display: inline-flex; margin-top: 10px; }}
      .trend-chart {{ height: 280px; }}
      .trend-card-foot {{ display: grid; }}
    }}
  </style>
  <script src="echarts.min.js"></script>
</head>
<body>
  <div class="page">
    <section class="panel">
      <h1>PerfEval Lite 多轮性能对比报告</h1>
      <p class="subtitle">{html.escape(data.run_id)} · {data.repeats} 次重复采集</p>
      <span class="status-badge {html.escape(data.status.lower())}">{html.escape(_status_cn(data.status))}</span>
      <div class="overview">{overview_html}</div>
    </section>

    <section class="panel">
      <div class="trend-section-head">
        <div>
          <h2>指标趋势矩阵</h2>
          <p class="muted">按性能语义与量纲分组，每张卡片使用独立线性坐标。下方逐次对比表保留精确数值。</p>
        </div>
        <div class="trend-method" aria-label="趋势图阅读说明">
          <span class="method-chip">同类指标同轴</span>
          <span class="method-chip">比例统一为百分比</span>
          <span class="method-chip">首末轮变化提示</span>
        </div>
      </div>
      <div class="trend-grid">{trend_cards_html}</div>
    </section>

    <section class="panel">
      <h2>逐次对比</h2>
      <p class="muted" style="margin-bottom: 10px;">每一轮采集的原始指标值，便于横向对比。</p>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>指标</th>
              <th>单位</th>
              {per_run_header}
            </tr>
          </thead>
          <tbody>
            {''.join(per_run_rows)}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>汇总统计</h2>
      <p class="muted" style="margin-bottom: 10px;">跨所有轮次的聚合统计，中位数为核心参考值。</p>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>指标</th>
              <th>单位</th>
              <th>样本数</th>
              <th>最小值</th>
              <th>最大值</th>
              <th>平均值</th>
              <th>中位数</th>
              <th>P90</th>
              <th>P95</th>
              <th>MAD</th>
              <th>置信度</th>
              <th>评级</th>
              <th>参考范围</th>
            </tr>
          </thead>
          <tbody>
            {''.join(stats_rows) if stats_rows else '<tr><td colspan="13" class="muted">无指标数据</td></tr>'}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>诊断信息</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>轮次</th>
              <th>级别</th>
              <th>代码</th>
              <th>消息</th>
            </tr>
          </thead>
          <tbody>
            {''.join(diag_rows)}
          </tbody>
        </table>
      </div>
    </section>
  </div>
  <script>
    var __chartData = {chart_json};
    (function() {{
      if (typeof echarts === 'undefined') {{
        console.warn('echarts.min.js not loaded');
        return;
      }}
      var data = window.__chartData || __chartData;
      var labels = data.run_labels || [];
      var runIds = data.run_ids || [];
      var groups = data.groups || [];
      var styles = getComputedStyle(document.documentElement);
      var charts = [];
      var palette = ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6'].map(function(name) {{
        return styles.getPropertyValue(name).trim();
      }});
      var symbols = ['circle', 'diamond', 'triangle', 'rect', 'roundRect'];
      var ink = styles.getPropertyValue('--ink').trim();
      var muted = styles.getPropertyValue('--muted').trim();
      var line = styles.getPropertyValue('--line').trim();
      var chartGrid = styles.getPropertyValue('--chart-grid').trim();
      var chartAxis = styles.getPropertyValue('--chart-axis').trim();
      var chartZoomFill = styles.getPropertyValue('--chart-zoom-fill').trim();
      var panel = styles.getPropertyValue('--panel').trim();
      var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      function escapeHtml(value) {{
        return String(value).replace(/[&<>"']/g, function(character) {{
          return {{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}}[character];
        }});
      }}

      function formatValue(value, unit) {{
        if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
        var number = Number(value);
        var digits = Math.abs(number) >= 100 ? 0 : (Math.abs(number) >= 10 ? 1 : 2);
        var formatted = number.toLocaleString('zh-CN', {{ maximumFractionDigits: digits }});
        return formatted + (unit ? ' ' + unit : '');
      }}

      groups.forEach(function(group, groupIndex) {{
        var node = document.getElementById('trend-chart-' + groupIndex);
        if (!node) return;
        var series = group.series || [];
        var hasZoom = labels.length > 10;
        var yAxis = {{
          type: 'value',
          name: group.unit || '',
          nameTextStyle: {{ color: muted, fontSize: 11, padding: [0, 0, 0, 2] }},
          scale: !group.axis_min_zero,
          axisLine: {{ show: false }},
          axisTick: {{ show: false }},
          axisLabel: {{
            color: muted,
            fontSize: 11,
            formatter: function(value) {{ return formatValue(value, group.unit === '%' ? '%' : ''); }}
          }},
          splitLine: {{ lineStyle: {{ color: chartGrid, type: 'dashed' }} }},
          splitNumber: 4
        }};
        if (group.axis_min_zero) yAxis.min = 0;
        if (group.integer_axis) yAxis.minInterval = 1;

        var chart = echarts.init(node, null, {{ renderer: 'canvas' }});
        chart.setOption({{
          animationDuration: reduceMotion ? 0 : 260,
          color: palette,
          aria: {{ enabled: true, decal: {{ show: true }} }},
          tooltip: {{
            trigger: 'axis',
            axisPointer: {{ type: 'line', lineStyle: {{ color: chartAxis, type: 'dashed' }} }},
            borderColor: line,
            textStyle: {{ color: ink, fontSize: 12 }},
            formatter: function(params) {{
              if (!params.length) return '';
              var runIndex = params[0].dataIndex;
              var heading = escapeHtml(params[0].axisValue);
              if (runIds[runIndex]) heading += ' · ' + escapeHtml(runIds[runIndex]);
              var lines = ['<strong>' + heading + '</strong>'];
              params.forEach(function(param) {{
                var metric = series[param.seriesIndex];
                if (!metric || param.value === null || param.value === undefined) return;
                lines.push(param.marker + escapeHtml(param.seriesName) + '：' + escapeHtml(formatValue(param.value, metric.unit || '')));
              }});
              return lines.join('<br/>');
            }}
          }},
          legend: {{
            data: series.map(function(item) {{ return item.name; }}),
            top: 4,
            left: 4,
            right: 4,
            itemWidth: 18,
            itemHeight: 9,
            itemGap: 14,
            textStyle: {{ color: ink, fontSize: 11 }}
          }},
          grid: {{ left: 58, right: 18, top: 70, bottom: hasZoom ? 72 : 42, containLabel: false }},
          xAxis: {{
            type: 'category',
            data: labels,
            boundaryGap: false,
            axisLine: {{ lineStyle: {{ color: chartAxis }} }},
            axisTick: {{ show: false }},
            axisLabel: {{ color: muted, fontSize: 11, interval: labels.length > 12 ? 'auto' : 0 }}
          }},
          yAxis: yAxis,
          dataZoom: hasZoom ? [
            {{ type: 'inside', start: 0, end: 60 }},
            {{ type: 'slider', height: 16, bottom: 12, borderColor: line, fillerColor: chartZoomFill }}
          ] : [],
          series: series.map(function(metric, seriesIndex) {{
            return {{
              name: metric.name,
              type: 'line',
              data: metric.values,
              smooth: false,
              showSymbol: true,
              symbol: symbols[seriesIndex % symbols.length],
              symbolSize: 8,
              connectNulls: false,
              lineStyle: {{ width: 2.2 }},
              itemStyle: {{ borderColor: panel, borderWidth: 1.5 }},
              emphasis: {{ focus: 'series', lineStyle: {{ width: 3.2 }} }}
            }};
          }})
        }});
        charts.push(chart);
      }});
      window.addEventListener('resize', function() {{ charts.forEach(function(chart) {{ chart.resize(); }}); }});
    }})();
  </script>
</body>
</html>"""
