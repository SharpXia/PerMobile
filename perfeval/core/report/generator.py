from __future__ import annotations

import csv
import html
import json
import shutil
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


STATUS_ORDER = {
    "fail": 0,
    "warn": 1,
    "pass": 2,
    "unknown": 3,
    "invalid": 4,
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 中文状态文案
STATUS_LABEL_CN = {
    "fail": "失败",
    "warn": "警告",
    "pass": "通过",
    "unknown": "未知",
    "invalid": "无效",
}

# ECharts 判定颜色
JUDGEMENT_COLOR = {
    "fail": "#b42318",
    "warn": "#9a6500",
    "pass": "#1f7a45",
    "unknown": "#56657a",
    "invalid": "#56657a",
}


@dataclass(frozen=True)
class ArtifactLink:
    label: str
    path: str
    collector: str = ""
    metric: str = ""


@dataclass(frozen=True)
class MetricRow:
    metric_id: str
    priority: str
    unit: str
    direction: str
    baseline_median: str
    candidate_median: str
    delta_abs: str
    delta_pct: str
    budget: str
    judgement: str
    confidence: str
    evidence: str
    reason: str
    source_run_ids: list[str] = field(default_factory=list)
    baseline_stats: dict[str, str] = field(default_factory=dict)
    candidate_stats: dict[str, str] = field(default_factory=dict)
    artifacts: list[ArtifactLink] = field(default_factory=list)
    # 用于绘图的原始数值
    baseline_value: float | None = None
    candidate_value: float | None = None
    delta_pct_value: float | None = None
    # stream 指标适配（CM-TASK-024）：均值/峰值/p95 三列对比行 + series 叠加图数据
    stream: Mapping[str, Any] = field(default_factory=dict)
    stream_rows: tuple[dict[str, str], ...] = field(default_factory=tuple)


def build_compare_report(compare: Mapping[str, Any]) -> str:
    report = _normalize_compare(compare)
    return _render_html(report)


def write_compare_json(compare: Mapping[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(compare, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path


def write_metrics_csv(compare: Mapping[str, Any], output_path: str | Path) -> Path:
    report = _normalize_compare(compare)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "compare_id",
        "mode",
        "scenario",
        "metric",
        "priority",
        "unit",
        "direction",
        "baseline_median",
        "candidate_median",
        "delta_abs",
        "delta_pct",
        "judgement",
        "confidence",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report.all_metrics:
            writer.writerow(
                {
                    "compare_id": report.compare_id,
                    "mode": report.mode,
                    "scenario": report.scenario,
                    "metric": row.metric_id,
                    "priority": row.priority,
                    "unit": row.unit,
                    "direction": row.direction,
                    "baseline_median": row.baseline_median,
                    "candidate_median": row.candidate_median,
                    "delta_abs": row.delta_abs,
                    "delta_pct": row.delta_pct,
                    "judgement": row.judgement,
                    "confidence": row.confidence,
                }
            )
    return path


def write_runs_csv(compare: Mapping[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_id",
        "group",
        "app_version",
        "device",
        "scenario",
        "status",
        "metric",
        "value",
        "unit",
    ]
    rows = _collect_run_rows(compare)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def write_report_bundle(compare: Mapping[str, Any], output_dir: str | Path) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {
        "report_html": directory / "report.html",
        "compare_json": directory / "compare.json",
        "metrics_csv": directory / "metrics.csv",
        "echarts_js": directory / "echarts.min.js",
    }
    outputs["report_html"].write_text(build_compare_report(compare), encoding="utf-8")
    write_compare_json(compare, outputs["compare_json"])
    write_metrics_csv(compare, outputs["metrics_csv"])
    runs_path = directory / "runs.csv"
    if _collect_run_rows(compare):
        write_runs_csv(compare, runs_path)
        outputs["runs_csv"] = runs_path
    # 复制 vendored echarts.min.js 以支持离线分发
    vendor_src = Path(__file__).parent / "vendor" / "echarts.min.js"
    if vendor_src.exists() and vendor_src.is_file():
        shutil.copyfile(vendor_src, outputs["echarts_js"])
    else:
        outputs["echarts_js"].write_text(
            "/* echarts.min.js missing — charts will not render */",
            encoding="utf-8",
        )
    return outputs


@dataclass(frozen=True)
class ReportData:
    compare_id: str
    status: str
    mode: str
    baseline_group: str
    candidate_group: str
    scenario: str
    device: str
    run_count: str
    compare_mode_text: str
    status_note: str
    key_metrics: list[MetricRow]
    all_metrics: list[MetricRow]
    artifacts: list[ArtifactLink]
    risks: list[str]
    raw_compare: Mapping[str, Any]


def _normalize_compare(compare: Mapping[str, Any]) -> ReportData:
    metrics = _normalize_metrics(compare)
    overall_status = _coerce_text(
        _first_present(compare, "status", default=_infer_overall_status(metrics))
    )
    compare_id = _coerce_text(
        _first_present(compare, "compareId", "compare_id", default="compare")
    )
    mode = _coerce_text(_first_present(compare, "mode", default="unknown"))
    baseline_group = _coerce_text(
        _first_present(compare, "baselineGroup", "baseline_group", default="baseline")
    )
    candidate_group = _coerce_text(
        _first_present(compare, "candidateGroup", "candidate_group", default="candidate")
    )
    scenario = _coerce_text(
        _first_present(
            compare,
            "scenario",
            "scenarioId",
            "scenario_id",
            default="unknown",
        )
    )
    device = _coerce_text(
        _first_present(
            compare,
            "device",
            "deviceName",
            "deviceId",
            "deviceProfile",
            default="unknown",
        )
    )
    run_count = _coerce_text(
        _first_present(compare, "runCount", "run_count", default=_infer_run_count(compare))
    )
    compare_mode_text = f"{mode} / {baseline_group} vs {candidate_group}"
    status_note = _coerce_text(
        _first_present(
            compare,
            "statusText",
            "status_text",
            default=_status_note(overall_status, metrics),
        )
    )
    artifacts = _collect_artifacts(compare, metrics)
    risks = _collect_risks(compare)
    key_metrics = [row for row in metrics if row.judgement in {"fail", "warn"}]
    if not key_metrics:
        key_metrics = metrics[: min(3, len(metrics))]
    return ReportData(
        compare_id=compare_id,
        status=overall_status,
        mode=mode,
        baseline_group=baseline_group,
        candidate_group=candidate_group,
        scenario=scenario,
        device=device,
        run_count=run_count,
        compare_mode_text=compare_mode_text,
        status_note=status_note,
        key_metrics=key_metrics,
        all_metrics=metrics,
        artifacts=artifacts,
        risks=risks,
        raw_compare=compare,
    )


def _render_html(report: ReportData) -> str:
    status_class = report.status if report.status in {"pass", "warn", "fail", "unknown", "invalid"} else "unknown"
    status_cn = STATUS_LABEL_CN.get(report.status, "未知")
    metrics_rows = []
    for row in report.all_metrics:
        open_attr = " open" if row.judgement in {"fail", "warn"} else ""
        metrics_rows.append(_render_metric_row(row, open_attr))
    key_metrics_html = "".join(_render_key_metric_item(row) for row in report.key_metrics)
    empty_key_metrics_html = (
        '<span class="chip invalid">输入无效，未执行指标判定</span>'
        if report.status == "invalid"
        else '<span class="chip">未检测到退化</span>'
    )
    risk_html = "".join(
        f"<li>{html.escape(risk)}</li>" for risk in report.risks
    ) or "<li>无</li>"
    artifact_html = "".join(_render_artifact_item(artifact) for artifact in report.artifacts)
    if not artifact_html:
        artifact_html = "<li class=\"muted\">无产物记录</li>"

    overview_fields = [
        ("状态", status_cn.upper()),
        ("对比模式", report.compare_mode_text),
        ("场景", report.scenario),
        ("设备", report.device),
        ("基线组", report.baseline_group),
        ("对照组", report.candidate_group),
        ("运行次数", report.run_count),
    ]
    overview_html = "".join(
        f"<div class=\"overview-item\"><span class=\"label\">{html.escape(label)}</span>"
        f"<span class=\"value\">{html.escape(value)}</span></div>"
        for label, value in overview_fields
    )

    chart_data = _prepare_chart_data(report)
    chart_json = json.dumps(chart_data, ensure_ascii=False)
    # 防止 </script> 注入
    chart_json = chart_json.replace("</", "<\\/")

    charts_section = _render_charts_section(chart_data)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PerfEval Lite 性能对比报告 - {html.escape(report.compare_id)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --ink: #142033;
      --muted: #596579;
      --line: #d9e1ec;
      --pass: #1f7a45;
      --warn: #9a6500;
      --fail: #b42318;
      --unknown: #56657a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    .page {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1, h2, h3, p {{ margin: 0; }}
    h1 {{ font-size: 28px; margin-bottom: 6px; }}
    h2 {{ font-size: 18px; margin-bottom: 12px; }}
    .subtitle {{ color: var(--muted); margin-bottom: 18px; }}
    .grid {{
      display: grid;
      gap: 16px;
    }}
    .overview-grid {{
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .status-pass {{ background: #e7f7ee; color: var(--pass); }}
    .status-warn {{ background: #fff5d6; color: var(--warn); }}
    .status-fail {{ background: #fde9e7; color: var(--fail); }}
    .status-unknown {{ background: #edf2f7; color: var(--unknown); }}
    .status-invalid {{ background: #edf2f7; color: var(--unknown); }}
    .overview-item {{
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fcfdff;
    }}
    .overview-item .label {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .overview-item .value {{
      display: block;
      font-size: 14px;
      font-weight: 600;
      word-break: break-word;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .chip {{
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fafbfc;
      color: var(--ink);
      font-size: 12px;
    }}
    .chip.fail {{ border-color: #f4c7c3; background: #fff4f3; color: var(--fail); }}
    .chip.warn {{ border-color: #f5d38f; background: #fff8e8; color: var(--warn); }}
    .chip.unknown {{ border-color: #d3dae6; background: #f5f7fb; color: var(--unknown); }}
    .section {{
      margin-top: 20px;
    }}
    .section-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .section-actions {{
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    button {{
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 8px;
      padding: 8px 12px;
      font: inherit;
      cursor: pointer;
    }}
    button:hover {{ background: #f4f7fb; }}
    .chart-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }}
    @media (min-width: 1100px) {{
      .chart-grid {{
        grid-template-columns: 1fr 1fr;
      }}
    }}
    .chart-box {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fcfdff;
      padding: 12px;
    }}
    .chart-title {{
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 6px;
      color: var(--ink);
    }}
    .chart-note {{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .chart-canvas {{
      width: 100%;
      height: 320px;
    }}
    .metric-table {{
      display: grid;
      gap: 10px;
    }}
    .metric-header,
    .metric-row {{
      display: grid;
      grid-template-columns: 80px minmax(170px, 2fr) repeat(7, minmax(110px, 1fr));
      gap: 8px;
      align-items: center;
    }}
    .metric-header {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--bg);
      padding: 10px 8px;
      color: var(--muted);
      font-size: 12px;
      border-bottom: 1px solid var(--line);
    }}
    .metric-item {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      overflow: hidden;
    }}
    .metric-item[open] {{
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .metric-item summary {{
      list-style: none;
      padding: 12px 10px;
      cursor: pointer;
    }}
    .metric-item summary::-webkit-details-marker {{ display: none; }}
    .metric-item.fail {{ border-left: 5px solid var(--fail); }}
    .metric-item.warn {{ border-left: 5px solid var(--warn); }}
    .metric-item.pass {{ border-left: 5px solid var(--pass); }}
    .metric-item.unknown {{ border-left: 5px solid var(--unknown); }}
    .metric-row span {{
      display: block;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .metric-row .metric-name {{
      font-weight: 700;
    }}
    .metric-row .muted {{ color: var(--muted); }}
    .metric-body {{
      border-top: 1px solid var(--line);
      padding: 12px 14px 14px;
      background: #fcfdff;
    }}
    .metric-body-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    }}
    .metric-card {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      background: #ffffff;
    }}
    .metric-card h3 {{
      font-size: 13px;
      margin-bottom: 8px;
    }}
    .metric-card dl {{
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 4px 10px;
      margin: 0;
    }}
    .metric-card dt {{
      color: var(--muted);
    }}
    .metric-card dd {{
      margin: 0;
      word-break: break-word;
    }}
    .metric-card-span {{
      grid-column: 1 / -1;
    }}
    .stream-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    .stream-table th,
    .stream-table td {{
      border: 1px solid var(--line);
      padding: 6px 10px;
      text-align: left;
      overflow-wrap: anywhere;
    }}
    .stream-table th {{
      color: var(--muted);
      font-weight: 600;
      background: #fafbfc;
    }}
    .artifact-list {{
      display: grid;
      gap: 8px;
      padding-left: 18px;
      margin: 0;
    }}
    .artifact-list a {{
      color: #1849a9;
      text-decoration: none;
    }}
    .artifact-list a:hover {{ text-decoration: underline; }}
    .muted {{ color: var(--muted); }}
    .hide-unknown [data-judgement="unknown"] {{
      display: none;
    }}
    .artifacts-table {{
      display: grid;
      gap: 8px;
    }}
    .artifact-row {{
      display: grid;
      grid-template-columns: minmax(220px, 2fr) minmax(120px, 0.8fr) minmax(120px, 0.8fr) minmax(120px, 0.8fr);
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      background: #fcfdff;
    }}
    .artifact-row div {{
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .artifact-row .label {{
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 3px;
    }}
    .artifact-row a {{ color: #1849a9; text-decoration: none; }}
    .artifact-row a:hover {{ text-decoration: underline; }}
    @media (max-width: 1100px) {{
      .metric-header,
      .metric-row {{
        grid-template-columns: 80px minmax(160px, 1.8fr) repeat(7, minmax(96px, 1fr));
      }}
    }}
    @media (max-width: 900px) {{
      .metric-header {{
        display: none;
      }}
      .metric-row {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .metric-row .metric-name {{
        grid-column: 1 / -1;
      }}
      .artifact-row {{
        grid-template-columns: 1fr;
      }}
      .chart-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
  <script src="echarts.min.js"></script>
</head>
<body>
  <div class="page">
    <header class="panel">
      <h1>PerfEval Lite 性能对比报告</h1>
      <p class="subtitle">{html.escape(report.compare_id)}</p>
      <span class="status-badge status-{html.escape(status_class)}">总体状态：{html.escape(status_cn)}</span>
      <p style="margin-top: 12px; color: var(--muted);">{html.escape(report.status_note)}</p>
      <div class="chips">
        {''.join(f'<span class="chip {html.escape(row.judgement)}">{html.escape(row.priority)} {html.escape(row.metric_id)} {html.escape(STATUS_LABEL_CN.get(row.judgement, row.judgement))}</span>' for row in report.key_metrics)}
      </div>
    </header>

    <section class="section panel">
      <div class="section-header">
        <h2>概览</h2>
        <div class="section-actions">
          <button type="button" onclick="toggleUnknownMetrics()">切换未知指标显示</button>
        </div>
      </div>
      <div class="grid overview-grid">
        {overview_html}
      </div>
      <div style="margin-top: 14px;">
        <h3 style="font-size: 13px; margin-bottom: 8px;">关键退化指标</h3>
        <div class="chips">
          {key_metrics_html or empty_key_metrics_html}
        </div>
      </div>
      <div style="margin-top: 14px;">
        <h3 style="font-size: 13px; margin-bottom: 8px;">环境风险</h3>
        <ul style="margin: 0; padding-left: 18px; color: var(--muted);">
          {risk_html}
        </ul>
      </div>
    </section>

    <section class="section panel">
      <div class="section-header">
        <h2>可视化图表</h2>
      </div>
      {charts_section}
    </section>

    <section class="section panel">
      <div class="section-header">
        <h2>指标差异</h2>
      </div>
      <div class="metric-table">
        <div class="metric-header">
          <span>优先级</span>
          <span>指标</span>
          <span>基线中位数</span>
          <span>对照中位数</span>
          <span>绝对差</span>
          <span>相对差</span>
          <span>预算</span>
          <span>判定</span>
          <span>置信度</span>
          <span>证据</span>
        </div>
        {''.join(metrics_rows)}
      </div>
    </section>

    <section class="section panel">
      <div class="section-header">
        <h2>产物</h2>
      </div>
      <div class="artifacts-table">
        {artifact_html}
      </div>
    </section>
  </div>
  <script>
    var __chartData = {chart_json};
    { _render_charts_script() }
    function toggleUnknownMetrics() {{
      document.body.classList.toggle("hide-unknown");
    }}
  </script>
</body>
</html>
"""


def _render_charts_section(chart_data: Mapping[str, Any]) -> str:
    boxes: list[str] = []
    if chart_data.get("metric_ids"):
        boxes.append(f"""
      <div class="chart-box">
        <div class="chart-title">相对差异（delta %，按判定着色）</div>
        <div class="chart-note">正值通常表示对照相较基线退化；颜色对应判定结果。</div>
        <div id="chart-delta" class="chart-canvas"></div>
      </div>
      <div class="chart-box">
        <div class="chart-title">基线 vs 对照中位数（对数轴）</div>
        <div class="chart-note">不同单位指标按 log10 等距展示，柱形统一从最小数量级基线起绘制；悬停查看原始数值与单位。</div>
        <div id="chart-compare" class="chart-canvas"></div>
      </div>
    """)
    # stream 指标 series 叠加图（CM-TASK-024，REPORT_PRESENTATION §3）：
    # 每个 stream 指标一图，基线蓝 / 对照橙；X 轴为各 run 相对起点的秒，
    # 多 repeat 按 run 分段（series ts 跨 run 回绕，不跨 run 连线）。
    for index, spec in enumerate(chart_data.get("stream_charts") or []):
        title_metric = html.escape(_coerce_text(spec.get("metric_id")))
        unit_suffix = f"（{html.escape(spec.get('unit'))}）" if spec.get("unit") and spec.get("unit") != "-" else ""
        boxes.append(f"""
      <div class="chart-box">
        <div class="chart-title">连续指标叠加：{title_metric}{unit_suffix}</div>
        <div class="chart-note">X 轴为各 run 相对起点的秒（跨 run epoch 不对齐）；多次 repeat 按 run 分段绘制，series 时间戳跨 run 回绕，不跨 run 连线；蓝=基线，橙=对照。</div>
        <div id="chart-stream-{index}" class="chart-canvas"></div>
      </div>
    """)
    if not boxes:
        return '<p class="muted">无可用数值指标，跳过图表渲染。</p>'
    return f"""
    <div class="chart-grid">{''.join(boxes)}
    </div>
    """


def _render_charts_script() -> str:
    return """
    (function () {
      if (typeof echarts === 'undefined') {
        console.warn('echarts.min.js not loaded; charts skipped');
        return;
      }
      var data = window.__chartData || __chartData;
      var metricIds = data.metric_ids || [];
      var baselineValues = data.baseline_values || [];
      var candidateValues = data.candidate_values || [];
      var deltaPcts = data.delta_pcts || [];
      var deltaColors = data.delta_colors || [];
      var units = data.units || [];
      var streamCharts = data.stream_charts || [];

      // 指标 ID / runId 等数据侧字符串进入 tooltip HTML 前统一转义（XSS 防护）
      function escHtml(text) {
        return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }

      function fmt(v, unit) {
        if (v === null || v === undefined) { return '-'; }
        var s = (Math.abs(v) >= 1000) ? v.toFixed(0) : v.toFixed(3).replace(/\\.?0+$/, '');
        return unit ? s + ' ' + unit : s;
      }

      if (metricIds.length > 0 && document.getElementById('chart-delta')) {
        var deltaPctDisplay = deltaPcts.map(function (p) { return Number((p * 100).toFixed(2)); });
        var chartDelta = echarts.init(document.getElementById('chart-delta'));
        chartDelta.setOption({
          grid: { left: 48, right: 24, top: 24, bottom: 80 },
          tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: function (params) {
              var p = params[0];
              var idx = p.dataIndex;
              var val = deltaPctDisplay[idx];
              var color = deltaColors[idx];
              return escHtml(p.name) + '<br/>相对差: ' + (val > 0 ? '+' : '') + val + '%';
            }
          },
          xAxis: {
            type: 'category',
            data: metricIds,
            axisLabel: { rotate: 30, interval: 0, fontSize: 11 }
          },
          yAxis: {
            type: 'value',
            name: '相对差 (%)',
            axisLabel: { formatter: '{value}%' }
          },
          series: [{
            type: 'bar',
            data: deltaPctDisplay.map(function (v, i) {
              return { value: v, itemStyle: { color: deltaColors[i] } };
            }),
            label: {
              show: true,
              position: 'top',
              formatter: function (p) {
                var v = p.value;
                return (v > 0 ? '+' : '') + v + '%';
              },
              fontSize: 11
            }
          }]
        });
        window.addEventListener('resize', function () { chartDelta.resize(); });
      }

      if (metricIds.length > 0 && document.getElementById('chart-compare')) {
        var positiveValues = baselineValues.concat(candidateValues).filter(function (v) {
          return typeof v === 'number' && isFinite(v) && v > 0;
        });
        var logFloor = positiveValues.length > 0
          ? Math.floor(Math.log(Math.min.apply(null, positiveValues)) / Math.LN10)
          : 0;
        var logCeil = positiveValues.length > 0
          ? Math.ceil(Math.log(Math.max.apply(null, positiveValues)) / Math.LN10)
          : 1;
        var logSpan = Math.max(1, logCeil - logFloor);
        function toLogHeight(v) {
          return Math.log(v) / Math.LN10 - logFloor;
        }
        function formatLogTick(v) {
          var raw = Math.pow(10, v + logFloor);
          if (raw >= 1000) { return raw.toFixed(0); }
          if (raw >= 1) { return raw.toFixed(2).replace(/\\.?0+$/, ''); }
          return raw.toPrecision(2).replace(/\\.?0+$/, '');
        }
        var baselineLogValues = baselineValues.map(toLogHeight);
        var candidateLogValues = candidateValues.map(toLogHeight);
        var chartCompare = echarts.init(document.getElementById('chart-compare'));
        chartCompare.setOption({
          grid: { left: 56, right: 24, top: 32, bottom: 80 },
          tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: function (params) {
              var idx = params[0].dataIndex;
              var unit = units[idx] || '';
              var lines = [escHtml(params[0].name)];
              params.forEach(function (p) {
                var rawValue = p.seriesName === '基线' ? baselineValues[idx] : candidateValues[idx];
                lines.push(p.marker + p.seriesName + ': ' + fmt(rawValue, unit));
              });
              return lines.join('<br/>');
            }
          },
          legend: { data: ['基线', '对照'], top: 0 },
          xAxis: {
            type: 'category',
            data: metricIds,
            axisLabel: { rotate: 30, interval: 0, fontSize: 11 }
          },
          yAxis: {
            type: 'value',
            min: 0,
            max: logSpan,
            interval: 1,
            name: '数值（对数轴）',
            axisLabel: { formatter: formatLogTick }
          },
          series: [
            {
              name: '基线',
              type: 'bar',
              data: baselineLogValues,
              itemStyle: { color: '#1c6dd0' }
            },
            {
              name: '对照',
              type: 'bar',
              data: candidateLogValues,
              itemStyle: { color: '#e8833a' }
            }
          ]
        });
        window.addEventListener('resize', function () { chartCompare.resize(); });
      }

      // stream 指标 series 叠加图（CM-TASK-024）：基线蓝 / 对照橙，
      // 多 repeat 按 run 分段（series ts 跨 run 回绕），X 轴 = 相对 run 秒
      streamCharts.forEach(function (spec, index) {
        var node = document.getElementById('chart-stream-' + index);
        if (!node) { return; }
        var groupColors = { baseline: '#1c6dd0', candidate: '#e8833a' };
        var groupNames = { baseline: '基线', candidate: '对照' };
        var runs = spec.runs || [];
        var legendNames = [];
        var seriesList = [];
        runs.forEach(function (run) {
          var groupName = escHtml(groupNames[run.group] || run.group || '-');
          if (legendNames.indexOf(groupName) === -1) { legendNames.push(groupName); }
          seriesList.push({
            name: groupName,
            type: 'line',
            showSymbol: false,
            sampling: 'lttb',
            data: (run.points || []).map(function (point) { return [point[0], point[1]]; }),
            itemStyle: { color: groupColors[run.group] || '#56657a' },
            lineStyle: { width: 1.5, opacity: 0.85 },
            emphasis: { focus: 'series' }
          });
        });
        if (!seriesList.length) { return; }
        var streamChart = echarts.init(node);
        streamChart.setOption({
          grid: { left: 56, right: 24, top: 32, bottom: 56 },
          tooltip: {
            trigger: 'axis',
            formatter: function (params) {
              if (!params.length) { return ''; }
              var run = runs[params[0].seriesIndex] || {};
              var lines = [escHtml(run.runId || '-')];
              params.forEach(function (p) {
                var label = escHtml(groupNames[p.seriesName] || p.seriesName || '-');
                lines.push(p.marker + label + ': ' + fmt(p.value[1], spec.unit));
              });
              lines.push('t=' + params[0].value[0] + 's');
              return lines.join('<br/>');
            }
          },
          legend: { data: legendNames, top: 0 },
          xAxis: { type: 'value', name: '相对 run 时间（s）', axisLabel: { fontSize: 11 } },
          yAxis: { type: 'value', scale: true, name: spec.unit && spec.unit !== '-' ? spec.unit : '' },
          series: seriesList
        });
        window.addEventListener('resize', function () { streamChart.resize(); });
      });
    })();
    """


# stream 叠加图单侧（baseline/candidate）最大点数：超出按 run 分段等距抽稀（仅影响图，不影响统计）
STREAM_CHART_MAX_POINTS_PER_SIDE = 3000


def _prepare_chart_data(report: ReportData) -> dict[str, Any]:
    """从 ReportData 提取数值序列供 ECharts 渲染。"""
    metric_ids: list[str] = []
    baseline_values: list[float] = []
    candidate_values: list[float] = []
    delta_pcts: list[float] = []
    delta_colors: list[str] = []
    units: list[str] = []
    stream_charts: list[dict[str, Any]] = []

    for row in report.all_metrics:
        if row.baseline_value is None or row.candidate_value is None:
            continue
        if row.baseline_value <= 0 or row.candidate_value <= 0:
            # 对数轴无法表达非正值，跳过
            continue
        metric_ids.append(row.metric_id)
        baseline_values.append(row.baseline_value)
        candidate_values.append(row.candidate_value)
        delta_pcts.append(row.delta_pct_value if row.delta_pct_value is not None else 0.0)
        delta_colors.append(JUDGEMENT_COLOR.get(row.judgement, JUDGEMENT_COLOR["unknown"]))
        units.append(row.unit)
        stream_chart = _stream_chart_spec(row)
        if stream_chart is not None:
            stream_charts.append(stream_chart)

    return {
        "metric_ids": metric_ids,
        "baseline_values": baseline_values,
        "candidate_values": candidate_values,
        "delta_pcts": delta_pcts,
        "delta_colors": delta_colors,
        "units": units,
        "stream_charts": stream_charts,
    }


def _stream_chart_spec(row: MetricRow) -> dict[str, Any] | None:
    """stream 指标叠加图数据：按 runId 分段的基线/候选序列，X = 相对 run 秒。

    series ts 为相对 run 起点的毫秒且跨 repeat 回绕（Batch A 评审已知），
    因此逐 run 分段输出、绝不跨 run 合并连线；runId 在服务端做 HTML 转义
    （tooltip 会以 HTML 内插渲染）。
    """
    if not row.stream_rows:
        return None
    series_block = row.stream.get("series")
    if not isinstance(series_block, Mapping):
        return None
    runs: list[dict[str, Any]] = []
    for group in ("baseline", "candidate"):
        segments = series_block.get(group)
        if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes, bytearray)):
            continue
        group_points: list[list[float]] = []
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            points: list[list[float]] = []
            for point in segment.get("points") or []:
                if not isinstance(point, Mapping):
                    continue
                ts = point.get("ts")
                value = point.get("v")
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                points.append([round(ts / 1000.0, 3), float(value)])
            if not points:
                continue
            points.sort(key=lambda item: item[0])
            runs.append(
                {
                    "group": group,
                    # 服务端转义：tooltip formatter 直接内插 runId
                    "runId": html.escape(_coerce_text(segment.get("runId"))),
                    "points": points,
                }
            )
            group_points.extend(points)
        if len(group_points) > STREAM_CHART_MAX_POINTS_PER_SIDE:
            stride = -(-len(group_points) // STREAM_CHART_MAX_POINTS_PER_SIDE)
            for run in runs:
                if run["group"] == group:
                    run["points"] = run["points"][::stride]
    if not runs:
        return None
    return {"metric_id": row.metric_id, "unit": row.unit, "runs": runs}


def _render_metric_row(row: MetricRow, open_attr: str) -> str:
    metric_name = html.escape(row.metric_id)
    judgement_cn = STATUS_LABEL_CN.get(row.judgement, row.judgement)
    return f"""
<details class="metric-item {html.escape(row.judgement)}" data-judgement="{html.escape(row.judgement)}"{open_attr}>
  <summary>
    <div class="metric-row">
      <span>{html.escape(row.priority)}</span>
      <span class="metric-name">{metric_name}</span>
      <span>{html.escape(row.baseline_median)}</span>
      <span>{html.escape(row.candidate_median)}</span>
      <span>{html.escape(row.delta_abs)}</span>
      <span>{html.escape(row.delta_pct)}</span>
      <span>{html.escape(row.budget)}</span>
      <span>{html.escape(judgement_cn)}</span>
      <span>{html.escape(row.confidence)}</span>
      <span>{html.escape(row.evidence)}</span>
    </div>
  </summary>
  <div class="metric-body">
    <div class="metric-body-grid">
      <div class="metric-card">
        <h3>基线</h3>
        { _render_stats_dl(row.baseline_stats) }
      </div>
      <div class="metric-card">
        <h3>对照</h3>
        { _render_stats_dl(row.candidate_stats) }
      </div>
      { _render_stream_card(row) }
      <div class="metric-card">
        <h3>判定</h3>
        <dl>
          <dt>方向</dt><dd>{html.escape(row.direction)}</dd>
          <dt>原因</dt><dd>{html.escape(row.reason or "-")}</dd>
          <dt>置信度</dt><dd>{html.escape(row.confidence)}</dd>
          <dt>证据</dt><dd>{html.escape(row.evidence)}</dd>
        </dl>
      </div>
      <div class="metric-card">
        <h3>产物</h3>
        { _render_metric_artifacts(row.artifacts) }
      </div>
      <div class="metric-card">
        <h3>来源运行</h3>
        { _render_source_runs(row.source_run_ids) }
      </div>
    </div>
  </div>
</details>
"""


def _render_stream_card(row: MetricRow) -> str:
    """stream 指标专属卡片：均值/峰值/P95 三统计量基线 vs 对照（CM-TASK-024）。"""
    if not row.stream_rows:
        return ""
    body_rows = "".join(
        "<tr>"
        f"<td>{html.escape(item['stat'])}</td>"
        f"<td>{html.escape(item['baseline'])}</td>"
        f"<td>{html.escape(item['candidate'])}</td>"
        f"<td>{html.escape(item['delta_abs'])}</td>"
        f"<td>{html.escape(item['delta_pct'])}</td>"
        "</tr>"
        for item in row.stream_rows
    )
    sample_count = row.stream.get("sampleCount")
    confidence = row.stream.get("confidence")
    notes: list[str] = []
    if isinstance(sample_count, Mapping):
        baseline_samples = sample_count.get("baseline")
        candidate_samples = sample_count.get("candidate")
        if baseline_samples is not None or candidate_samples is not None:
            notes.append(
                "采样点数/run：基线 "
                f"{html.escape(_coerce_text(baseline_samples))}，对照 {html.escape(_coerce_text(candidate_samples))}"
            )
    if isinstance(confidence, Mapping):
        baseline_confidence = confidence.get("baseline")
        candidate_confidence = confidence.get("candidate")
        if baseline_confidence or candidate_confidence:
            notes.append(
                "流内置信度：基线 "
                f"{html.escape(_coerce_text(baseline_confidence))}，对照 {html.escape(_coerce_text(candidate_confidence))}"
                "（按跨 run 次数分档，不随采样点数虚高）"
            )
    note_html = f'<p class="muted">{"；".join(notes)}</p>' if notes else ""
    return f"""
      <div class="metric-card metric-card-span">
        <h3>流式聚合对比（各 run 统计量取跨 run 中位数）</h3>
        <table class="stream-table">
          <thead><tr><th>统计量</th><th>基线</th><th>对照</th><th>绝对差</th><th>相对差</th></tr></thead>
          <tbody>{body_rows}</tbody>
        </table>
        {note_html}
      </div>
    """


def _render_key_metric_item(row: MetricRow) -> str:
    judgement_cn = STATUS_LABEL_CN.get(row.judgement, row.judgement)
    parts = [
        html.escape(row.priority),
        html.escape(row.metric_id),
        html.escape(judgement_cn),
    ]
    return f"<span class=\"chip {html.escape(row.judgement)}\">{' '.join(parts)}</span>"


def _render_artifact_item(artifact: ArtifactLink) -> str:
    href = _safe_href(artifact.path)
    return (
        "<div class=\"artifact-row\">"
        f"<div><div class=\"label\">路径</div><a href=\"{html.escape(href)}\">{html.escape(artifact.path)}</a></div>"
        f"<div><div class=\"label\">指标</div>{html.escape(artifact.metric or '-')}</div>"
        f"<div><div class=\"label\">采集器</div>{html.escape(artifact.collector or '-')}</div>"
        f"<div><div class=\"label\">标签</div>{html.escape(artifact.label or '-')}</div>"
        "</div>"
    )


def _render_metric_artifacts(artifacts: Sequence[ArtifactLink]) -> str:
    if not artifacts:
        return '<p class="muted">无指标产物</p>'
    items = "".join(
        f"<li><a href=\"{html.escape(_safe_href(artifact.path))}\">{html.escape(artifact.path)}</a>"
        f" <span class=\"muted\">({html.escape(artifact.label)})</span></li>"
        for artifact in artifacts
    )
    return f"<ul class=\"artifact-list\">{items}</ul>"


def _safe_href(value: str) -> str:
    stripped = value.strip()
    scheme = urllib.parse.urlsplit(stripped).scheme.lower()
    if scheme and scheme not in {"http", "https", "file"}:
        if not (len(scheme) == 1 and len(stripped) > 1 and stripped[1] == ":"):
            return "#"
    return value


def _render_source_runs(source_run_ids: Sequence[str]) -> str:
    if not source_run_ids:
        return '<p class="muted">无来源运行 ID</p>'
    items = "".join(f"<li>{html.escape(run_id)}</li>" for run_id in source_run_ids)
    return f"<ul class=\"artifact-list\">{items}</ul>"


def _render_stats_dl(stats: Mapping[str, str]) -> str:
    if not stats:
        return '<p class="muted">无统计数据</p>'
    parts = []
    for key, value in stats.items():
        parts.append(f"<dt>{html.escape(key)}</dt><dd>{html.escape(value)}</dd>")
    return f"<dl>{''.join(parts)}</dl>"


def _normalize_metrics(compare: Mapping[str, Any]) -> list[MetricRow]:
    raw_metrics = compare.get("metrics", {})
    metric_pairs: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(raw_metrics, Mapping):
        metric_pairs = [(str(metric_id), metric_data) for metric_id, metric_data in raw_metrics.items()]
    elif isinstance(raw_metrics, Sequence) and not isinstance(raw_metrics, (str, bytes, bytearray)):
        for metric_data in raw_metrics:
            if isinstance(metric_data, Mapping):
                metric_id = str(
                    _first_present(
                        metric_data,
                        "metricId",
                        "metric_id",
                        "metric",
                        default="metric",
                    )
                )
                metric_pairs.append((metric_id, metric_data))
    metric_rows = [_normalize_metric(metric_id, metric_data) for metric_id, metric_data in metric_pairs]
    metric_rows.sort(
        key=lambda row: (
            STATUS_ORDER.get(row.judgement, 99),
            PRIORITY_ORDER.get(row.priority, 99),
            row.metric_id,
        )
    )
    return metric_rows


# stream 三统计量展示名（CM-TASK-024，REPORT_PRESENTATION §3：均值 / 峰值（max）/ p95）
STREAM_STAT_LABELS = {"mean": "均值", "peak": "峰值", "p95": "P95"}
STREAM_STAT_ORDER = ("mean", "peak", "p95")


def _normalize_metric(metric_id: str, metric_data: Mapping[str, Any]) -> MetricRow:
    baseline = _as_mapping(metric_data.get("baseline"))
    candidate = _as_mapping(metric_data.get("candidate"))
    delta = _as_mapping(metric_data.get("delta"))
    confidence = _as_mapping(metric_data.get("confidence"))
    evidence = _as_mapping(metric_data.get("evidence"))
    artifacts = _collect_metric_artifacts(metric_data, evidence)
    baseline_median_raw = _first_present(baseline, "median", default=_first_present(metric_data, "baselineMedian", "baseline_median"))
    candidate_median_raw = _first_present(candidate, "median", default=_first_present(metric_data, "candidateMedian", "candidate_median"))
    delta_abs_raw = _first_present(metric_data, "deltaAbs", "delta_abs", default=_first_present(delta, "abs", default=None))
    delta_pct_raw = _first_present(metric_data, "deltaPct", "delta_pct", default=_first_present(delta, "pct", default=None))
    stream = _as_mapping(metric_data.get("stream"))
    return MetricRow(
        metric_id=metric_id,
        priority=_coerce_text(_first_present(metric_data, "priority", default=_infer_priority(metric_id))),
        unit=_coerce_text(_first_present(metric_data, "unit", default="-")),
        direction=_coerce_text(_first_present(metric_data, "direction", default="-")),
        baseline_median=_format_number(baseline_median_raw),
        candidate_median=_format_number(candidate_median_raw),
        delta_abs=_format_number(delta_abs_raw),
        delta_pct=_format_delta_pct(delta_pct_raw),
        budget=_format_budget(metric_data),
        judgement=_coerce_text(_first_present(metric_data, "judgement", default="unknown")),
        confidence=_format_confidence(confidence, metric_data),
        evidence=_format_evidence(evidence, metric_data),
        reason=_coerce_text(_first_present(metric_data, "reason", default="-")),
        source_run_ids=_collect_strings(_first_present(metric_data, "sourceRunIds", "source_run_ids", default=[])),
        baseline_stats=_format_stats(baseline, metric_data.get("baseline")),
        candidate_stats=_format_stats(candidate, metric_data.get("candidate")),
        artifacts=artifacts,
        baseline_value=_parse_float(baseline_median_raw),
        candidate_value=_parse_float(candidate_median_raw),
        delta_pct_value=_parse_float(delta_pct_raw),
        stream=stream,
        stream_rows=_format_stream_rows(stream),
    )


def _format_stream_rows(stream: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """stream 三统计量对比行（均值/峰值/P95 × 基线/对照/差异）；非 stream 或缺统计量时为空。"""
    if stream.get("isStream") is not True:
        return ()
    statistics = stream.get("statistics")
    if not isinstance(statistics, Mapping):
        return ()
    rows: list[dict[str, str]] = []
    for stat_key in STREAM_STAT_ORDER:
        stat = statistics.get(stat_key)
        if not isinstance(stat, Mapping):
            continue
        rows.append(
            {
                "stat": STREAM_STAT_LABELS.get(stat_key, stat_key),
                "baseline": _format_number(stat.get("baseline")),
                "candidate": _format_number(stat.get("candidate")),
                "delta_abs": _format_number(stat.get("deltaAbs")),
                "delta_pct": _format_delta_pct(stat.get("deltaPct")),
            }
        )
    return tuple(rows)


def _collect_metric_artifacts(metric_data: Mapping[str, Any], evidence: Mapping[str, Any]) -> list[ArtifactLink]:
    artifact_values: list[Any] = []
    for source in (
        metric_data.get("artifacts"),
        metric_data.get("artifactRefs"),
        metric_data.get("artifact_refs"),
        evidence.get("artifacts"),
    ):
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
            artifact_values.extend(source)
    artifacts: list[ArtifactLink] = []
    for value in artifact_values:
        if isinstance(value, Mapping):
            path = str(
                _first_present(
                    value,
                    "path",
                    "href",
                    "url",
                    default=_first_present(value, "id", default=""),
                )
            )
            if not path:
                continue
            artifacts.append(
                ArtifactLink(
                    label=_coerce_text(_first_present(value, "id", "label", "name", default=path)),
                    path=path,
                    collector=_coerce_text(_first_present(value, "collector", "source", default="")),
                    metric=_coerce_text(_first_present(value, "metric", "metricId", default="")),
                )
            )
        else:
            path = str(value)
            if path:
                artifacts.append(ArtifactLink(label=path, path=path))
    return artifacts


def _collect_artifacts(compare: Mapping[str, Any], metrics: Sequence[MetricRow]) -> list[ArtifactLink]:
    artifact_values: list[Any] = []
    for source in (compare.get("artifacts"), compare.get("artifactRefs"), compare.get("artifact_refs")):
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
            artifact_values.extend(source)
    artifacts: list[ArtifactLink] = []
    for metric in metrics:
        artifacts.extend(metric.artifacts)
    for value in artifact_values:
        if isinstance(value, Mapping):
            path = str(
                _first_present(value, "path", "href", "url", default=_first_present(value, "id", default=""))
            )
            if not path:
                continue
            artifacts.append(
                ArtifactLink(
                    label=_coerce_text(_first_present(value, "id", "label", "name", default=path)),
                    path=path,
                    collector=_coerce_text(_first_present(value, "collector", "source", default="")),
                    metric=_coerce_text(_first_present(value, "metric", "metricId", default="")),
                )
            )
        else:
            path = str(value)
            if path:
                artifacts.append(ArtifactLink(label=path, path=path))
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[ArtifactLink] = []
    for artifact in artifacts:
        key = (artifact.label, artifact.path, artifact.collector, artifact.metric)
        if key not in seen:
            seen.add(key)
            unique.append(artifact)
    unique.sort(key=lambda artifact: (artifact.metric, artifact.collector, artifact.label, artifact.path))
    return unique


def _collect_risks(compare: Mapping[str, Any]) -> list[str]:
    risks: list[str] = []
    errors = compare.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes, bytearray)):
        risks.extend(f"输入校验失败：{error}" for error in errors)
    focused_windows = compare.get("focusedWindows")
    if isinstance(focused_windows, Mapping) and "focused_window_mismatch" in (errors or []):
        baseline = ", ".join(str(item) for item in focused_windows.get("baseline", []) or []) or "-"
        candidate = ", ".join(str(item) for item in focused_windows.get("candidate", []) or []) or "-"
        risks.append(f"前台页面不一致：baseline={baseline}；candidate={candidate}")
    for key in ("environmentRisks", "environment_risks", "risks", "warnings", "diagnostics"):
        value = compare.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                if isinstance(item, Mapping):
                    message = item.get("message") or item.get("text") or item.get("reason") or item.get("code")
                    if message:
                        risks.append(str(message))
                else:
                    risks.append(str(item))
        elif isinstance(value, Mapping):
            message = value.get("message") or value.get("text") or value.get("reason") or value.get("code")
            if message:
                risks.append(str(message))
    unique: list[str] = []
    for risk in risks:
        if risk not in unique:
            unique.append(risk)
    return unique


def _collect_run_rows(compare: Mapping[str, Any]) -> list[dict[str, str]]:
    runs = compare.get("runs", [])
    rows: list[dict[str, str]] = []
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes, bytearray)):
        return rows
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        base = {
            "run_id": _coerce_text(_first_present(run, "runId", "run_id", default="")),
            "group": _coerce_text(_first_present(run, "group", "runGroup", default="")),
            "app_version": _coerce_text(_first_present(run, "appVersion", "app_version", default="")),
            "device": _coerce_text(_first_present(run, "device", "deviceName", "deviceId", default="")),
            "scenario": _coerce_text(_first_present(run, "scenario", "scenarioId", "scenario_id", default="")),
            "status": _coerce_text(_first_present(run, "status", default="")),
            "metric": "",
            "value": "",
            "unit": "",
        }
        measurements = run.get("measurements")
        if isinstance(measurements, Sequence) and not isinstance(measurements, (str, bytes, bytearray)):
            for measurement in measurements:
                if not isinstance(measurement, Mapping):
                    continue
                row = base.copy()
                row["metric"] = _coerce_text(_first_present(measurement, "metric", "metricId", default=""))
                row["value"] = _format_number(_first_present(measurement, "value", default=""))
                row["unit"] = _coerce_text(_first_present(measurement, "unit", default=""))
                rows.append(row)
        else:
            rows.append(base)
    return rows


def _infer_overall_status(metrics: Sequence[MetricRow]) -> str:
    if any(row.judgement == "fail" for row in metrics):
        return "fail"
    if any(row.judgement == "warn" for row in metrics):
        return "warn"
    if any(row.judgement == "unknown" for row in metrics):
        return "unknown"
    if metrics:
        return "pass"
    return "unknown"


def _infer_run_count(compare: Mapping[str, Any]) -> str:
    run_count = compare.get("runCount")
    if isinstance(run_count, Mapping):
        values = [_coerce_text(value) for value in run_count.values()]
        values = [value for value in values if value]
        return ", ".join(values) if values else "-"
    if isinstance(run_count, Sequence) and not isinstance(run_count, (str, bytes, bytearray)):
        return ", ".join(_coerce_text(value) for value in run_count if _coerce_text(value)) or "-"
    if run_count is not None:
        return _coerce_text(run_count)
    source_runs = compare.get("sourceRunIds")
    if isinstance(source_runs, Sequence) and not isinstance(source_runs, (str, bytes, bytearray)):
        return str(len(source_runs))
    return "-"


def _status_note(status: str, metrics: Sequence[MetricRow]) -> str:
    if status == "fail":
        top_metrics = ", ".join(row.metric_id for row in metrics if row.judgement == "fail")
        return f"失败指标：{top_metrics}" if top_metrics else "一个或多个指标未通过预算检查。"
    if status == "warn":
        top_metrics = ", ".join(row.metric_id for row in metrics if row.judgement == "warn")
        return f"警告指标：{top_metrics}" if top_metrics else "一个或多个指标需要复查。"
    if status == "unknown":
        return "部分指标证据不足以做出判定。"
    if status == "invalid":
        return "对比输入无效。"
    return "所有可见指标均在预算范围内。"


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _format_number(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".6g")
    return str(value)


def _format_delta_pct(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    percent = number * 100
    if percent.is_integer():
        return f"{int(percent)}%"
    return f"{format(percent, '.4g')}%"


def _format_budget(metric_data: Mapping[str, Any]) -> str:
    budget = metric_data.get("budget")
    if isinstance(budget, Mapping):
        items = []
        for key in ("failAbs", "warnAbs", "failRegressionPct", "warnRegressionPct", "failRegressionAbs", "warnRegressionAbs", "minDeltaAbs"):
            if key in budget and budget[key] not in (None, ""):
                items.append(f"{key}={_format_number(budget[key])}")
        if items:
            return "; ".join(items)
    if "failAbs" in metric_data or "warnAbs" in metric_data:
        items = []
        for key in ("failAbs", "warnAbs", "failRegressionPct", "warnRegressionPct", "failRegressionAbs", "warnRegressionAbs", "minDeltaAbs"):
            if key in metric_data and metric_data[key] not in (None, ""):
                items.append(f"{key}={_format_number(metric_data[key])}")
        if items:
            return "; ".join(items)
    return "-"


def _format_confidence(confidence: Mapping[str, Any], metric_data: Mapping[str, Any]) -> str:
    if confidence:
        level = confidence.get("level")
        if level:
            return _coerce_text(level)
    return _coerce_text(_first_present(metric_data, "confidence", default="-"))


def _format_evidence(evidence: Mapping[str, Any], metric_data: Mapping[str, Any]) -> str:
    if evidence:
        level = evidence.get("level")
        if level:
            return _coerce_text(level)
    return _coerce_text(_first_present(metric_data, "evidenceLevel", "evidence_level", default="-"))


def _format_stats(stats: Mapping[str, Any], raw: Any) -> dict[str, str]:
    if not isinstance(stats, Mapping):
        return {}
    output: dict[str, str] = {}
    for key in ("n", "min", "max", "mean", "median", "p75", "p90", "p95", "mad"):
        if key in stats:
            output[key] = _format_number(stats.get(key))
    if isinstance(raw, Mapping):
        samples = raw.get("samples")
        if isinstance(samples, Sequence) and not isinstance(samples, (str, bytes, bytearray)):
            output["samples"] = ", ".join(_format_number(value) for value in samples)
    return output


def _infer_priority(metric_id: str) -> str:
    if metric_id.startswith(("startup.cold", "startup.first_frame", "frame.jank_rate", "stability.")):
        return "P0"
    if metric_id.startswith(("frame.", "memory.", "cpu.")):
        return "P1"
    return "P2"


def _coerce_text(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    text = str(value)
    return text if text else "-"


def _first_present(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _collect_strings(values: Any) -> list[str]:
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        return [str(value) for value in values]
    if values in (None, ""):
        return []
    return [str(values)]
