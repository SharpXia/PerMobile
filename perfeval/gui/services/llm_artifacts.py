from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from ..models.history import HistoryEntry
from ..models.llm import (
    INTERPRETATION_DEPTH_LABELS,
    INTERPRETATION_DEPTH_MAX_OUTPUT_TOKENS,
    InterpretationDepth,
    InterpretationSnapshot,
    InterpretationSource,
    InterpretationState,
    LlmProfile,
)
from ..models.preset import TaskLaunchConfig
from ..models.result import ResultDescriptor, ResultType, TaskResult
from .llm_protocols import redact_sensitive_text


INTERPRETATION_SCHEMA = "perfeval.llm.interpretation.v1"
PROMPT_VERSION = "perfeval.performance-analysis.zh.v3"
MAX_INPUT_BYTES = 256 * 1024
MAX_DOCUMENTS = 20
MAX_RESPONSE_BYTES = 1024 * 1024
_CONTEXT_BUDGET = 220_000
_BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_FRAME_FINDING_TERMS = (
    "frame.",
    "jank",
    "vsync",
    "choreographer",
    "卡顿",
    "掉帧",
    "慢帧",
    "帧耗时",
    "刷新率",
    "帧",
    "渲染",
    "流畅",
)
_LOG_LIMITS: Mapping[InterpretationDepth, tuple[int, int, int]] = {
    InterpretationDepth.BRIEF: (2, 16 * 1024, 8 * 1024),
    InterpretationDepth.STANDARD: (3, 48 * 1024, 16 * 1024),
    InterpretationDepth.DEEP: (5, 96 * 1024, 32 * 1024),
}
_LINK_START = "<!-- perfeval-llm-link:start -->"
_LINK_END = "<!-- perfeval-llm-link:end -->"


@dataclass(frozen=True)
class PromptBundle:
    system_prompt: str
    user_prompt: str
    input_digest: str
    truncated: bool
    document_count: int
    input_bytes: int
    depth: InterpretationDepth
    max_output_tokens: int
    log_count: int
    logs_truncated: bool
    assessment_calibration: Mapping[str, Any]


def source_from_history(entry: HistoryEntry) -> InterpretationSource:
    root = entry.directory.resolve(strict=False)
    data_paths: list[Path] = []
    if entry.summary_path is not None:
        data_paths.append(entry.summary_path)
    data_paths.extend(_known_json_paths(root))
    report_paths = (entry.report_path,) if entry.report_path is not None else ()
    return InterpretationSource(
        source_id=entry.entry_id,
        display_name=entry.name,
        root=root,
        report_paths=report_paths,
        data_paths=tuple(data_paths),
        log_paths=_discover_log_paths(root),
        artifact_paths=tuple(path for path in (entry.report_path, entry.summary_path) if path),
        metadata={
            "taskKind": entry.task_kind.value,
            "status": entry.status,
            "platform": entry.platform,
            "source": entry.source.value,
        },
    )


def source_from_task(launch: TaskLaunchConfig, result: TaskResult) -> InterpretationSource:
    task = launch.task
    raw_experiment_root = task.metadata.get("experiment_root")
    root = task.expected_output_root.resolve(strict=False)
    if isinstance(raw_experiment_root, str) and raw_experiment_root.strip():
        candidate = Path(raw_experiment_root).expanduser().resolve(strict=False)
        allowed = (task.working_directory / ".perfeval" / "experiments").resolve(strict=False)
        if _is_relative_to(candidate, allowed):
            root = candidate

    reports: list[Path] = []
    data_paths: list[Path] = []
    log_paths: list[Path] = []
    artifacts: list[Path] = []
    for descriptor in result.results:
        path = descriptor.path
        if path is None or not _is_relative_to(path.resolve(strict=False), root):
            continue
        artifacts.append(path)
        if descriptor.type is ResultType.REPORT:
            reports.append(path)
        elif descriptor.type in {ResultType.SUMMARY, ResultType.COMPARE, ResultType.RESULT}:
            if path.suffix.casefold() == ".json":
                data_paths.append(path)
        elif descriptor.type is ResultType.LOG:
            log_paths.append(path)
    data_paths.extend(_known_json_paths(root))
    log_paths.extend(_discover_log_paths(root))
    return InterpretationSource(
        source_id=result.task_id,
        display_name=launch.title,
        root=root,
        report_paths=tuple(reports),
        data_paths=tuple(data_paths),
        log_paths=tuple(log_paths),
        artifact_paths=tuple(artifacts),
        metadata={
            "taskKind": launch.task_kind.value,
            "outcome": result.outcome.value,
            "packageName": launch.package_name,
            "deviceLabel": launch.device_label,
        },
    )


def build_prompt(
    source: InterpretationSource,
    depth: InterpretationDepth | str = InterpretationDepth.STANDARD,
) -> PromptBundle:
    resolved_depth = InterpretationDepth(depth)
    context: dict[str, Any] = {
        "schemaVersion": "perfeval.llm.input.v3",
        "interpretationDepth": resolved_depth.value,
        "source": {
            "id": source.source_id,
            "name": source.display_name,
            "metadata": dict(source.metadata),
        },
        "documents": [],
        "logExcerpts": [],
        "artifacts": _artifact_metadata(source),
        "truncation": {
            "applied": False,
            "omittedDocuments": 0,
            "omittedLogs": 0,
            "logsTruncated": False,
        },
    }
    candidates = [path for path in source.data_paths if path.suffix.casefold() == ".json"]
    loaded_documents: list[tuple[Path, str, Any]] = []
    for path in candidates[:MAX_DOCUMENTS]:
        document = _read_json_document(path)
        if document is not None:
            content, value = document
            loaded_documents.append((path, content, value))
    assessment_calibration = _build_assessment_calibration(loaded_documents)
    context["assessmentCalibration"] = assessment_calibration

    truncated = False
    omitted = 0
    omitted_logs = 0
    logs_truncated = False
    log_file_limit, log_budget, per_file_limit = _LOG_LIMITS[resolved_depth]
    log_candidates = list(source.log_paths)
    if len(log_candidates) > log_file_limit:
        omitted_logs = len(log_candidates) - log_file_limit
        logs_truncated = True
    remaining_log_budget = log_budget
    for path in log_candidates[:log_file_limit]:
        if remaining_log_budget <= 0:
            omitted_logs += 1
            logs_truncated = True
            continue
        per_file_budget = max(1, min(per_file_limit, remaining_log_budget))
        content, excerpt_truncated = _read_log_excerpt(path, max_bytes=per_file_budget)
        if not content:
            continue
        item = {
            "path": _relative_label(source.root, path),
            "kind": "log_excerpt",
            "content": content,
        }
        included, item_truncated = _append_with_budget(
            context,
            item,
            collection="logExcerpts",
        )
        logs_truncated = logs_truncated or excerpt_truncated or item_truncated
        if not included:
            omitted_logs += 1
            continue
        remaining_log_budget -= len(content.encode("utf-8"))

    for path, content, _value in loaded_documents:
        item = {
            "path": _relative_label(source.root, path),
            "kind": "json",
            "content": content,
        }
        included, item_truncated = _append_with_budget(
            context,
            item,
            collection="documents",
        )
        truncated = truncated or item_truncated
        if not included:
            omitted += 1
    if len(candidates) > MAX_DOCUMENTS:
        truncated = True
        omitted += len(candidates) - MAX_DOCUMENTS

    if not context["documents"]:
        for report_path in source.report_paths[:1]:
            visible_text = _read_visible_html(report_path)
            if visible_text:
                item = {
                    "path": _relative_label(source.root, report_path),
                    "kind": "report_text_fallback",
                    "content": visible_text,
                }
                _included, item_truncated = _append_with_budget(
                    context,
                    item,
                    collection="documents",
                )
                truncated = truncated or item_truncated

    if not context["documents"]:
        raise ValueError("没有可发送给 LLM 的 summary、compare、diagnosis 或报告文本")

    context["truncation"] = {
        "applied": truncated or logs_truncated,
        "omittedDocuments": omitted,
        "omittedLogs": omitted_logs,
        "logsTruncated": logs_truncated,
    }
    context_json = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(context_json.encode("utf-8")) > _CONTEXT_BUDGET:
        raise ValueError("LLM 输入在截断后仍超过安全上限")

    system_prompt = (
        "你是审慎的移动应用性能评估专家。必须先评估采样有效性，再判断用户体验风险。"
        "只能依据提供的数据进行判断，不得编造未出现的指标、调用栈或根因。"
        "预算失败只表示命中当前测试配置的阈值，不等同于用户实际体验严重，也不能自动提升问题等级。"
        "日志内容仅为受限片段，必须将事实、推断和待验证假设明确区分。"
        "请用中文输出严格 JSON，不要输出 Markdown 或 HTML。"
    )
    user_prompt = f"""请以“{INTERPRETATION_DEPTH_LABELS[resolved_depth]}”档位解读以下 PerfEval Lite 性能报告数据。

输出 JSON object，字段必须为：
- executiveSummary: string，按所选档位给出总体结论；
- findings: array，每项包含 severity（critical/high/medium/low/info）、title、evidence、analysis、recommendation；
- positives: string array；
- nextSteps: string array，按优先级排序；
- limitations: string array，说明证据不足、截断或模型不确定性。

要求：证据字段引用实际指标名称和数值；建议必须可执行；没有证据时明确写入 limitations。
{_depth_prompt_requirements(resolved_depth)}
严重程度校准规则：
- critical 仅用于已确认的不可用、崩溃、ANR、长时间冻结，或有充分持续采样/trace 证明的灾难性退化；
- high 需要充足样本和至少两类相互独立的证据证明持续、明显影响交互；
- 预算失败只能表述为“超过当前预算阈值”，不得直接写成“实际体验严重卡顿”；
- 当每轮 frame.total_count 少于 60 时，jank_rate 容易被少数帧放大；少于 20 时，P90/P95 往往接近最慢一两帧。若无 ANR、冻结或 trace 等独立证据，流畅度问题最高评为 medium，并表述为“短时慢帧风险/需扩大样本复测”；
- 必须同时参考绝对卡顿帧数、P50、采样帧数、重复轮次、刷新率/测试阶段和稳定性指标；不能只看比例或单个高分位；
- 不得默认设备始终为 60 Hz。刷新率不明确或动态变化时，不得把帧耗时机械换算成固定数量的刷新周期；
- 日志中的时间邻近或组件活跃不等于因果关系。没有 trace 或调用栈时，第三方 SDK、IPC、布局等根因只能列为待验证假设；
- 如果指标结论与实际体验可能不一致，优先说明证据局限和复测方法，不使用“严重恶化”“必然肉眼可见”等确定性措辞。
日志要求：日志片段只能作为辅助证据；引用时标明文件和关键原文，不得假设未提供的完整上下文。
输入数据：
{context_json}
"""
    total_bytes = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
    if total_bytes > MAX_INPUT_BYTES:
        raise ValueError("LLM 提示超过 256 KiB 安全上限")
    return PromptBundle(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        input_digest=hashlib.sha256(context_json.encode("utf-8")).hexdigest(),
        truncated=truncated or logs_truncated,
        document_count=len(context["documents"]),
        input_bytes=total_bytes,
        depth=resolved_depth,
        max_output_tokens=INTERPRETATION_DEPTH_MAX_OUTPUT_TOKENS[resolved_depth],
        log_count=len(context["logExcerpts"]),
        logs_truncated=logs_truncated,
        assessment_calibration=assessment_calibration,
    )


def build_interpretation_record(
    source: InterpretationSource,
    profile: LlmProfile,
    bundle: PromptBundle,
    response_text: str,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    normalized, fallback = _normalize_response(response_text)
    calibration = dict(bundle.assessment_calibration)
    adjusted_findings = _apply_severity_calibration(normalized, calibration)
    if adjusted_findings:
        calibration["adjustedFindingCount"] = adjusted_findings
    limitations = list(normalized["limitations"])
    if bundle.truncated and not any("截断" in value for value in limitations):
        limitations.append("发送给模型的输入已按 256 KiB 上限截断。")
    if bundle.logs_truncated and not any("日志" in value and "片段" in value for value in limitations):
        limitations.append("日志仅发送了受限片段，可能不包含完整上下文。")
    if fallback:
        limitations.append("模型未返回预期 JSON 结构，当前报告保留了原始文本作为降级结果。")
    calibration_note = str(calibration.get("limitation") or "").strip()
    if calibration_note:
        limitations.append(calibration_note)
    normalized["limitations"] = _unique_text(limitations)
    return {
        "schemaVersion": INTERPRETATION_SCHEMA,
        "generatedAtUtc": _utc_now(),
        "source": {
            "id": source.source_id,
            "name": source.display_name,
            "root": ".",
        },
        "model": {
            "profileId": profile.profile_id,
            "profileName": profile.name,
            "apiFormat": profile.api_format.value,
            "baseUrl": profile.base_url,
            "model": profile.model,
        },
        "promptVersion": PROMPT_VERSION,
        "assessmentCalibration": calibration,
        "interpretationDepth": {
            "id": bundle.depth.value,
            "label": INTERPRETATION_DEPTH_LABELS[bundle.depth],
        },
        "input": {
            "sha256": bundle.input_digest,
            "bytes": bundle.input_bytes,
            "documentCount": bundle.document_count,
            "truncated": bundle.truncated,
            "logCount": bundle.log_count,
            "logsTruncated": bundle.logs_truncated,
        },
        "usage": dict(usage),
        "result": normalized,
        "rawText": response_text if fallback else None,
        "notice": "AI 生成，仅供参考；请结合原始数据与实际业务场景复核。",
    }


def write_interpretation(
    source: InterpretationSource,
    profile: LlmProfile,
    record: Mapping[str, Any],
) -> ResultDescriptor:
    _ensure_llm_directory(source)
    rendered_html = render_interpretation_html(source, record)
    _atomic_json(source.interpretation_json_path, record)
    _atomic_text(source.interpretation_html_path, rendered_html)
    write_status(
        source,
        InterpretationState.COMPLETED,
        message="LLM 解读已完成。",
        profile=profile,
        depth=_depth_from_record(record),
    )
    _update_experiment_manifest(source, record)
    for report_path in source.report_paths:
        try:
            inject_report_link(report_path, source.interpretation_html_path)
        except (OSError, UnicodeError, ValueError):
            continue
    return source.interpretation_descriptor


def write_status(
    source: InterpretationSource,
    state: InterpretationState,
    *,
    message: str = "",
    profile: LlmProfile | None = None,
    depth: InterpretationDepth | None = None,
    job_id: str | None = None,
) -> None:
    _ensure_llm_directory(source)
    payload = {
        "schemaVersion": "perfeval.llm.status.v1",
        "sourceId": source.source_id,
        "state": state.value,
        "message": str(message)[:1000],
        "updatedAtUtc": _utc_now(),
        "processId": os.getpid(),
        "jobId": job_id,
        "profileId": profile.profile_id if profile else None,
        "model": profile.model if profile else None,
        "depth": depth.value if depth else None,
        "htmlPath": "interpretation.html" if source.interpretation_html_path.is_file() else None,
        "jsonPath": "interpretation.json" if source.interpretation_json_path.is_file() else None,
    }
    _atomic_json(source.status_path, payload)


def read_snapshot(root: str | Path) -> InterpretationSnapshot:
    directory = Path(root).expanduser().resolve(strict=False)
    llm_dir = directory / "llm"
    if llm_dir.is_symlink() or (llm_dir.exists() and not llm_dir.is_dir()):
        return InterpretationSnapshot(InterpretationState.FAILED, message="LLM 结果目录不安全。")
    html_path = llm_dir / "interpretation.html"
    json_path = llm_dir / "interpretation.json"
    status_path = llm_dir / "status.json"
    html_exists = html_path.is_file() and not html_path.is_symlink()
    json_exists = json_path.is_file() and not json_path.is_symlink()
    if not status_path.is_file() or status_path.is_symlink():
        return InterpretationSnapshot(
            InterpretationState.COMPLETED if html_exists else InterpretationState.NOT_GENERATED,
            html_path=html_path if html_exists else None,
            json_path=json_path if json_exists else None,
            depth=_read_record_depth(json_path) if json_exists else None,
        )
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        state = InterpretationState(str(payload.get("state") or ""))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, AttributeError):
        state = InterpretationState.COMPLETED if html_exists else InterpretationState.FAILED
        payload = {}
    process_id = payload.get("processId") if isinstance(payload, Mapping) else None
    if state in {InterpretationState.QUEUED, InterpretationState.RUNNING} and process_id != os.getpid():
        state = InterpretationState.INTERRUPTED
    return InterpretationSnapshot(
        state=state,
        html_path=html_path if html_exists else None,
        json_path=json_path if json_exists else None,
        message=str(payload.get("message") or "") if isinstance(payload, Mapping) else "",
        profile_id=str(payload.get("profileId")) if isinstance(payload, Mapping) and payload.get("profileId") else None,
        model=str(payload.get("model")) if isinstance(payload, Mapping) and payload.get("model") else None,
        depth=(
            _optional_depth(payload.get("depth") if isinstance(payload, Mapping) else None)
            or (_read_record_depth(json_path) if json_exists else None)
        ),
        updated_at_utc=str(payload.get("updatedAtUtc")) if isinstance(payload, Mapping) and payload.get("updatedAtUtc") else None,
    )


def render_interpretation_html(source: InterpretationSource, record: Mapping[str, Any]) -> str:
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    model = record.get("model") if isinstance(record.get("model"), Mapping) else {}
    findings = result.get("findings") if isinstance(result, Mapping) else []
    finding_html = "".join(_render_finding(item) for item in findings if isinstance(item, Mapping))
    if not finding_html:
        finding_html = '<p class="muted">模型没有给出结构化问题项。</p>'
    positives = _render_list(result.get("positives") if isinstance(result, Mapping) else None, "暂无明确积极项。")
    next_steps = _render_list(result.get("nextSteps") if isinstance(result, Mapping) else None, "暂无明确后续步骤。")
    limitations = _render_list(result.get("limitations") if isinstance(result, Mapping) else None, "未声明额外局限性。")
    report_href = _relative_href(source.interpretation_html_path, source.report_paths[0]) if source.report_paths else ""
    report_link = f'<a class="button" href="{html.escape(report_href)}">返回原始报告</a>' if report_href else ""
    generated = html.escape(_format_beijing_time(record.get("generatedAtUtc")))
    depth = record.get("interpretationDepth") if isinstance(record.get("interpretationDepth"), Mapping) else {}
    input_info = record.get("input") if isinstance(record.get("input"), Mapping) else {}
    calibration = record.get("assessmentCalibration") if isinstance(record.get("assessmentCalibration"), Mapping) else {}
    calibration_html = _render_calibration(calibration)
    summary = html.escape(str(result.get("executiveSummary") or "未生成总体结论。")) if isinstance(result, Mapping) else "未生成总体结论。"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LLM 解读报告 - {html.escape(source.display_name)}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f8fb; --panel:#fff; --ink:#142033; --muted:#596579; --line:#d9e1ec; --accent:#2468d8; --good:#1f7a45; --warn:#9a6500; --bad:#b42318; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--ink); }}
    .page {{ max-width:1120px; margin:0 auto; padding:24px; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:20px; margin-bottom:16px; box-shadow:0 1px 2px rgba(16,24,40,.04); }}
    h1,h2,h3,p {{ margin-top:0; }} h1 {{ font-size:28px; margin-bottom:6px; }} h2 {{ font-size:19px; }}
    .muted {{ color:var(--muted); }} .notice {{ border-left:4px solid var(--warn); background:#fff8e8; padding:10px 12px; border-radius:8px; }}
    .calibration {{ border-left:4px solid var(--warn); background:#fffaf0; }} .calibration strong {{ color:#7a4e00; }}
    .meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-top:16px; }}
    .meta div {{ border:1px solid var(--line); border-radius:9px; padding:9px 11px; background:#fcfdff; }} .meta span {{ display:block; color:var(--muted); font-size:12px; }}
    .finding {{ border:1px solid var(--line); border-left:5px solid var(--accent); border-radius:10px; padding:14px; margin:12px 0; }}
    .finding.critical,.finding.high {{ border-left-color:var(--bad); }} .finding.medium {{ border-left-color:var(--warn); }} .finding.low,.finding.info {{ border-left-color:var(--good); }}
    .severity {{ display:inline-block; padding:2px 8px; border-radius:999px; background:#edf2f7; font-size:12px; font-weight:700; }}
    dt {{ color:var(--muted); font-weight:600; }} dd {{ margin:0 0 8px; }} ul {{ padding-left:22px; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; }} .button {{ color:#1849a9; text-decoration:none; border:1px solid var(--line); border-radius:8px; padding:8px 12px; background:#fff; }}
    @media (max-width:680px) {{ .page {{ padding:12px; }} .panel {{ padding:15px; }} h1 {{ font-size:23px; }} }}
    @media print {{ body {{ background:#fff; }} .page {{ max-width:none; padding:0; }} .panel {{ box-shadow:none; break-inside:avoid; }} .actions {{ display:none; }} }}
  </style>
</head>
<body>
  <main class="page">
    <section class="panel">
      <h1>LLM 性能解读报告</h1>
      <p class="muted">{html.escape(source.display_name)}</p>
      <p class="notice">AI 生成，仅供参考；请结合原始数据与实际业务场景复核。</p>
      <div class="meta">
        <div><span>模型配置</span>{html.escape(str(model.get('profileName') or '-'))}</div>
        <div><span>模型</span>{html.escape(str(model.get('model') or '-'))}</div>
        <div><span>API 格式</span>{html.escape(str(model.get('apiFormat') or '-'))}</div>
        <div><span>解读档位</span>{html.escape(str(depth.get('label') or '标准'))}</div>
        <div><span>日志片段</span>{html.escape(str(input_info.get('logCount') or 0))} 个</div>
        <div><span>生成时间（北京时间）</span>{generated}</div>
      </div>
    </section>
    {calibration_html}
    <section class="panel"><h2>总体结论</h2><p>{summary}</p></section>
    <section class="panel"><h2>关键问题与证据</h2>{finding_html}</section>
    <section class="panel"><h2>积极项</h2>{positives}</section>
    <section class="panel"><h2>建议的后续步骤</h2>{next_steps}</section>
    <section class="panel"><h2>局限性</h2>{limitations}</section>
    <section class="panel actions">{report_link}</section>
  </main>
</body>
</html>
"""


def inject_report_link(report_path: Path, interpretation_path: Path) -> None:
    if report_path.is_symlink() or interpretation_path.is_symlink():
        raise ValueError("拒绝修改或链接符号链接报告")
    report = report_path.expanduser().resolve(strict=True)
    interpretation = interpretation_path.expanduser().resolve(strict=True)
    text = report.read_text(encoding="utf-8")
    text = re.sub(
        re.escape(_LINK_START) + r".*?" + re.escape(_LINK_END),
        "",
        text,
        flags=re.DOTALL,
    )
    href = html.escape(_relative_href(report, interpretation))
    banner = f"""{_LINK_START}
<aside style="max-width:1400px;margin:16px auto;padding:0 24px;font:14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif">
  <a href="{href}" style="display:inline-flex;align-items:center;padding:9px 13px;border:1px solid #d9e1ec;border-radius:9px;background:#fff;color:#1849a9;text-decoration:none;font-weight:600">查看 LLM 解读报告</a>
</aside>
{_LINK_END}"""
    body_match = re.search(r"<body(?:\s[^>]*)?>", text, flags=re.IGNORECASE)
    if body_match:
        text = text[: body_match.end()] + "\n" + banner + text[body_match.end() :]
    else:
        text = banner + "\n" + text
    _atomic_text(report, text)


def _normalize_response(response_text: str) -> tuple[dict[str, Any], bool]:
    payload = _extract_json_object(response_text)
    fallback = not isinstance(payload, Mapping)
    if fallback:
        payload = {"executiveSummary": response_text.strip()}
    findings: list[dict[str, str]] = []
    raw_findings = payload.get("findings")
    if isinstance(raw_findings, Sequence) and not isinstance(raw_findings, (str, bytes)):
        for item in raw_findings[:20]:
            if not isinstance(item, Mapping):
                continue
            severity = str(item.get("severity") or "info").strip().lower()
            if severity not in {"critical", "high", "medium", "low", "info"}:
                severity = "info"
            findings.append(
                {
                    "severity": severity,
                    "title": _text(item.get("title"), "未命名问题"),
                    "evidence": _text(item.get("evidence"), "未提供明确证据"),
                    "analysis": _text(item.get("analysis"), "未提供进一步分析"),
                    "recommendation": _text(item.get("recommendation"), "建议结合原始数据复核"),
                }
            )
    return (
        {
            "executiveSummary": _text(payload.get("executiveSummary"), response_text.strip() or "模型未返回可用内容"),
            "findings": findings,
            "positives": _string_list(payload.get("positives")),
            "nextSteps": _string_list(payload.get("nextSteps")),
            "limitations": _string_list(payload.get("limitations")),
        },
        fallback,
    )


def _apply_severity_calibration(
    normalized: dict[str, Any],
    calibration: Mapping[str, Any],
) -> int:
    frame_evidence = calibration.get("frameEvidence")
    if not isinstance(frame_evidence, Mapping):
        return 0
    ceiling = str(frame_evidence.get("severityCeiling") or "").strip().lower()
    ceiling_order = _SEVERITY_ORDER.get(ceiling)
    if ceiling_order is None:
        return 0
    findings = normalized.get("findings")
    if not isinstance(findings, list):
        return 0
    adjusted = 0
    for finding in findings:
        if not isinstance(finding, dict) or not _is_frame_finding(finding):
            continue
        severity = str(finding.get("severity") or "info").strip().lower()
        if _SEVERITY_ORDER.get(severity, 0) <= ceiling_order:
            continue
        finding["severity"] = ceiling
        adjusted += 1
    return adjusted


def _is_frame_finding(finding: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(finding.get(field) or "")
        for field in ("title", "evidence", "analysis", "recommendation")
    ).casefold()
    return any(term in text for term in _FRAME_FINDING_TERMS)


def _extract_json_object(text: str) -> Mapping[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, Mapping) else None
    return None


def _append_with_budget(
    context: dict[str, Any],
    item: dict[str, str],
    *,
    collection: str,
) -> tuple[bool, bool]:
    values = context[collection]
    values.append(item)
    if _json_size(context) <= _CONTEXT_BUDGET:
        return True, False
    values.pop()
    original = item["content"]
    low, high = 0, len(original)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate_text = original[:middle] + "\n…内容已截断…"
        candidate = {**item, "content": candidate_text, "truncated": True}
        values.append(candidate)
        size = _json_size(context)
        values.pop()
        if size <= _CONTEXT_BUDGET:
            best = candidate_text
            low = middle + 1
        else:
            high = middle - 1
    if not best:
        return False, True
    values.append({**item, "content": best, "truncated": True})
    return True, True


def _depth_prompt_requirements(depth: InterpretationDepth) -> str:
    return {
        InterpretationDepth.BRIEF: (
            "档位要求：总体结论 1-2 句；只列 1-3 个最高优先级问题；"
            "建议不超过 3 条，避免展开低影响细节。"
        ),
        InterpretationDepth.STANDARD: (
            "档位要求：总体结论 2-4 句；列出 3-6 个有明确证据的问题；"
            "说明影响、证据局限，并给出最多 6 条按优先级排序的建议。"
        ),
        InterpretationDepth.DEEP: (
            "档位要求：总体结论 4-8 句；列出不超过 8 个有充分证据的问题或观察，宁缺毋滥；"
            "跨指标、重复轮次和日志进行关联分析；量化影响和不确定性；"
            "根因只能作为待验证假设并说明验证方法；给出 6-10 条建议，"
            "每条包含实施动作、验证指标和预期信号。"
        ),
    }[depth]


def _read_log_excerpt(path: Path, *, max_bytes: int) -> tuple[str, bool]:
    try:
        if path.is_symlink() or not path.is_file() or max_bytes < 1:
            return "", False
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size <= max_bytes:
                payload = stream.read(max_bytes)
                return _sanitize_log_text(payload.decode("utf-8", errors="replace")), False
            head_size = max(1, max_bytes // 3)
            tail_size = max(1, max_bytes - head_size)
            head = stream.read(head_size).decode("utf-8", errors="replace")
            stream.seek(max(0, size - tail_size))
            tail = stream.read(tail_size).decode("utf-8", errors="replace")
    except OSError:
        return "", False
    text = head.rstrip() + "\n…日志中间内容已截断…\n" + tail.lstrip()
    return _sanitize_log_text(text), True


def _sanitize_log_text(text: str) -> str:
    redacted = redact_sensitive_text(text)
    return "".join(
        character if character in "\n\r\t" or ord(character) >= 32 else "�"
        for character in redacted
    ).strip()


def _json_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _read_json_document(path: Path) -> tuple[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), value


def _build_assessment_calibration(
    documents: Sequence[tuple[Path, str, Any]],
) -> dict[str, Any]:
    calibration: dict[str, Any] = {
        "rulesVersion": "perfeval.performance-severity.v1",
        "budgetFailureMeans": "configured_threshold_exceeded_not_confirmed_user_experience",
    }
    metrics = _best_metrics_mapping(documents)
    if metrics is None:
        return calibration

    total_frames = _metric_number(metrics, "frame.total_count", "value")
    min_frames = _metric_number(metrics, "frame.total_count", "min")
    max_frames = _metric_number(metrics, "frame.total_count", "max")
    repeat_count = _metric_number(metrics, "frame.total_count", "n")
    janky_frames = _metric_number(metrics, "frame.janky_count", "value")
    jank_rate = _metric_number(metrics, "frame.jank_rate", "value")
    p50_ms = _metric_number(metrics, "frame.duration.p50.ms", "value")
    p95_ms = _metric_number(metrics, "frame.duration.p95.ms", "value")
    p95_max_ms = _metric_number(metrics, "frame.duration.p95.ms", "max")
    anr_count = _metric_number(metrics, "stability.anr.count", "value") or 0.0
    crash_count = _metric_number(metrics, "stability.crash.count", "value") or 0.0
    if total_frames is None:
        return calibration

    if total_frames < 30:
        quality = "very_low"
        quality_label = "很低（每轮帧样本过少）"
    elif total_frames < 120:
        quality = "low"
        quality_label = "偏低（建议扩大交互采样）"
    else:
        quality = "adequate"
        quality_label = "基本充足"

    has_independent_severe_signal = (
        anr_count > 0
        or crash_count > 0
        or (p95_max_ms is not None and p95_max_ms >= 250.0)
    )
    severity_ceiling = "medium" if total_frames < 60 and not has_independent_severe_signal else None
    frame_evidence: dict[str, Any] = {
        "quality": quality,
        "qualityLabel": quality_label,
        "medianFramesPerRepeat": _compact_number(total_frames),
        "minFramesPerRepeat": _compact_number(min_frames),
        "maxFramesPerRepeat": _compact_number(max_frames),
        "repeatCount": _compact_number(repeat_count),
        "medianJankyFrames": _compact_number(janky_frames),
        "medianJankRate": jank_rate,
        "p50Ms": p50_ms,
        "p95Ms": p95_ms,
        "p95MaxMs": p95_max_ms,
        "anrCount": _compact_number(anr_count),
        "crashCount": _compact_number(crash_count),
        "severityCeiling": severity_ceiling,
        "appliesTo": "frame_smoothness_findings",
    }
    calibration["frameEvidence"] = {
        key: value for key, value in frame_evidence.items() if value is not None
    }
    if severity_ceiling:
        frame_range = _frame_range_text(total_frames, min_frames, max_frames)
        limitation = (
            f"流畅度采样量很小：{frame_range}；比例和高分位容易被少数慢帧放大；"
            "在没有 ANR、长时间冻结或 trace 等独立证据时，不能据此判定实际体验存在严重卡顿。"
        )
        calibration["limitation"] = limitation
        calibration["notice"] = (
            f"本次{frame_range}。jank_rate 反映的是少量观测帧中的比例，"
            "P90/P95 也可能接近最慢的一两帧。当前证据更适合描述为短时慢帧风险，"
            "不应直接等同于实际体验中的严重卡顿；建议延长并重复真实交互后复测。"
        )
    return calibration


def _best_metrics_mapping(
    documents: Sequence[tuple[Path, str, Any]],
) -> Mapping[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    for _path, _content, value in documents:
        metrics = value.get("metrics") if isinstance(value, Mapping) else None
        if isinstance(metrics, Mapping):
            candidates.append(metrics)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda value: sum(
            name in value
            for name in (
                "frame.total_count",
                "frame.janky_count",
                "frame.jank_rate",
                "frame.duration.p50.ms",
                "frame.duration.p95.ms",
            )
        ),
    )


def _metric_number(
    metrics: Mapping[str, Any],
    name: str,
    field: str,
) -> float | None:
    metric = metrics.get(name)
    value = metric.get(field) if isinstance(metric, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _compact_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def _frame_range_text(
    median_frames: float,
    min_frames: float | None,
    max_frames: float | None,
) -> str:
    median_text = f"每轮帧样本中位数约 {_compact_number(median_frames)} 帧"
    if min_frames is None or max_frames is None:
        return median_text
    return (
        f"每轮仅采集 {_compact_number(min_frames)}–{_compact_number(max_frames)} 帧"
        f"（中位数 {_compact_number(median_frames)} 帧）"
    )


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())


def _read_visible_html(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            return ""
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    parser = _VisibleTextParser()
    try:
        parser.feed(text)
    except Exception:
        return ""
    return "\n".join(parser.parts)


def _artifact_metadata(source: InterpretationSource) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for path in source.artifact_paths[:50]:
        try:
            stat = path.stat()
        except OSError:
            continue
        values.append(
            {
                "path": _relative_label(source.root, path)[:500],
                "suffix": path.suffix.casefold(),
                "sizeBytes": stat.st_size,
            }
        )
    return values


def _known_json_paths(root: Path) -> tuple[Path, ...]:
    paths = [
        root / "experiment.json",
        root / "summary.json",
        root / "compare.json",
        root / "diagnosis.json",
        root / "manifest.json",
        root / "compare" / "compare.json",
    ]
    return tuple(path for path in paths if path.is_file() and not path.is_symlink())


def _discover_log_paths(root: Path) -> tuple[Path, ...]:
    root = root.resolve(strict=False)
    stack: list[tuple[Path, int]] = [(root, 0)]
    values: list[Path] = []
    scanned = 0
    while stack and scanned < 500 and len(values) < 20:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError:
            continue
        for child in children:
            scanned += 1
            if scanned > 500:
                break
            try:
                if child.is_symlink():
                    continue
                path = Path(child.path)
                if child.is_dir(follow_symlinks=False):
                    if depth < 8 and child.name.casefold() not in {"llm", "report"}:
                        stack.append((path, depth + 1))
                elif child.is_file(follow_symlinks=False) and _looks_like_log(path):
                    values.append(path.resolve(strict=False))
                    if len(values) >= 20:
                        break
            except OSError:
                continue
    return tuple(sorted(dict.fromkeys(values), key=lambda path: path.as_posix().casefold()))


def _looks_like_log(path: Path) -> bool:
    name = path.name.casefold()
    return (
        path.suffix.casefold() == ".log"
        or name == "logcat.txt"
        or name.endswith("-logcat.txt")
        or (path.suffix.casefold() == ".txt" and any(token in name for token in ("stdout", "stderr")))
    )


def _update_experiment_manifest(
    source: InterpretationSource,
    record: Mapping[str, Any],
) -> None:
    path = source.root / "experiment.json"
    if not path.is_file() or path.is_symlink():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or payload.get("schemaVersion") != "perfeval.gui.experiment.v1":
        return
    payload["llmInterpretation"] = {
        "statusPath": "llm/status.json",
        "jsonPath": "llm/interpretation.json",
        "htmlPath": "llm/interpretation.html",
        "updatedAtUtc": _utc_now(),
        "depth": _depth_from_record(record).value,
    }
    _atomic_json(path, payload)


def _depth_from_record(record: Mapping[str, Any]) -> InterpretationDepth:
    value = record.get("interpretationDepth")
    raw = value.get("id") if isinstance(value, Mapping) else value
    return _optional_depth(raw) or InterpretationDepth.STANDARD


def _read_record_depth(path: Path) -> InterpretationDepth | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _depth_from_record(value) if isinstance(value, Mapping) else None


def _optional_depth(value: object) -> InterpretationDepth | None:
    try:
        return InterpretationDepth(str(value or ""))
    except ValueError:
        return None


def _render_finding(item: Mapping[str, Any]) -> str:
    severity = str(item.get("severity") or "info")
    return f"""<article class="finding {html.escape(severity)}">
  <span class="severity">{html.escape(severity.upper())}</span>
  <h3>{html.escape(str(item.get('title') or '未命名问题'))}</h3>
  <dl>
    <dt>证据</dt><dd>{html.escape(str(item.get('evidence') or '-'))}</dd>
    <dt>分析</dt><dd>{html.escape(str(item.get('analysis') or '-'))}</dd>
    <dt>建议</dt><dd>{html.escape(str(item.get('recommendation') or '-'))}</dd>
  </dl>
</article>"""


def _render_calibration(calibration: Mapping[str, Any]) -> str:
    notice = str(calibration.get("notice") or "").strip()
    frame_evidence = calibration.get("frameEvidence")
    if not notice or not isinstance(frame_evidence, Mapping):
        return ""
    quality = html.escape(str(frame_evidence.get("qualityLabel") or "证据有限"))
    adjusted = frame_evidence.get("adjustedFindingCount") or calibration.get("adjustedFindingCount")
    adjusted_text = ""
    if isinstance(adjusted, int) and adjusted > 0:
        adjusted_text = f" 已按该规则自动下调 {adjusted} 个流畅度问题的严重等级。"
    return f'''<section class="panel calibration">
      <h2>证据质量校准</h2>
      <p><strong>{quality}</strong></p>
      <p>{html.escape(notice + adjusted_text)}</p>
    </section>'''


def _render_list(value: object, empty: str) -> str:
    values = _string_list(value)
    if not values:
        return f'<p class="muted">{html.escape(empty)}</p>'
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in values) + "</ul>"


def _relative_href(from_file: Path, to_file: Path) -> str:
    relative = os.path.relpath(to_file, start=from_file.parent).replace(os.sep, "/")
    return quote(relative, safe="/.-_~")


def _relative_label(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        return path.name


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _ensure_llm_directory(source: InterpretationSource) -> None:
    root = source.root
    if root.is_symlink() or not root.is_dir():
        raise ValueError("结果目录不可用，无法保存 LLM 解读")
    directory = source.llm_directory
    if directory.is_symlink():
        raise ValueError("拒绝向符号链接 LLM 目录写入结果")
    if directory.exists() and not directory.is_dir():
        raise ValueError("LLM 结果路径不是目录")
    directory.mkdir(parents=False, exist_ok=True)
    resolved = directory.resolve(strict=True)
    if not _is_relative_to(resolved, root) or resolved != directory:
        raise ValueError("LLM 结果目录越出原结果范围")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return _unique_text(_text(item, "") for item in value)


def _unique_text(values: Sequence[str] | Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text[:4000])
    return result


def _text(value: object, default: str) -> str:
    text = str(value).strip() if value is not None else ""
    return (text or default)[:8000]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_beijing_time(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "INTERPRETATION_SCHEMA",
    "MAX_INPUT_BYTES",
    "MAX_RESPONSE_BYTES",
    "PROMPT_VERSION",
    "PromptBundle",
    "build_interpretation_record",
    "build_prompt",
    "inject_report_link",
    "read_snapshot",
    "render_interpretation_html",
    "source_from_history",
    "source_from_task",
    "write_interpretation",
    "write_status",
]
