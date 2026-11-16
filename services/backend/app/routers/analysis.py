"""深度分析与诊断路由（M7，契约 §22.1/§22.2，DEVELOPMENT_PLAN R50/R51）。

薄适配层（ARCH §4.1）：业务逻辑在 services.deep_analysis / services.diagnostics，
本文件只做查询参数解析与装配调用。run 侧端点与 reports/run_artifacts 同构
（404 report_not_found 由 resolve_run_dir 统一防护）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..services.diagnostics import build_diagnostics, parse_diagnostics_params
from ..services.deep_analysis import build_deep_analysis

router = APIRouter(prefix="/api", tags=["analysis"])


@router.get("/runs/{run_id}/deep-analysis")
def run_deep_analysis(run_id: str, request: Request) -> dict[str, Any]:
    """深度分析数据面（契约 §22.1）：缺省帧列表概览；``?frameIndex=N`` 单帧
    Overview 卡 + 该帧时间窗内 CPU 调度片段（thread_state 条带）。

    数据面复用 perfeval ``perfetto_trace.py`` FrameTimeline SQL 查询
    （``query_frame_timeline``，帧元素 = ``FrameRecord.to_dict`` 形状原样）；
    无 perfetto trace 工件（未采集/trace_processor 缺失）→ 200 空结构 + note
    （无数据是常态，不回退伪造，[R§8.6]）；frameIndex 越界 → 400
    validation_error；run 不存在 → 404 report_not_found。
    """
    frame_index_raw = request.query_params.get("frameIndex")
    frame_index: int | None = None
    if frame_index_raw is not None and frame_index_raw != "":
        try:
            frame_index = int(frame_index_raw)
        except ValueError:
            from ..errors import validation_error

            raise validation_error(
                f"frameIndex 必须是整数，收到 {frame_index_raw!r}", field="frameIndex"
            ) from None
    return build_deep_analysis(
        request.app.state.settings.runs_dir,
        run_id,
        frame_index,
    )


@router.get("/runs/{run_id}/diagnostics")
def run_diagnostics(run_id: str, request: Request) -> dict[str, Any]:
    """根因诊断卡数据面（契约 §22.2）：下降区间（默认 -15%/持续 1000ms，
    ``dropThresholdPct``/``minDurationMs`` 可调，``fromMs/toMs`` 可选分析窗口）
    + 区间内各指标与锚指标的皮尔逊相关系数 + 候选按 |score| 降序排序。

    - ``anchor`` 未注册 → 400 validation_error；锚指标无数据点 → 200 空结构 +
      note（不填假零值，[R§8.6]）；run 不存在 → 404 report_not_found；
    - ``evidence`` 为服务端规则生成的确定性文本，无 LLM 调用（[P§2.2]）。
    """
    params = parse_diagnostics_params(request.query_params)
    return build_diagnostics(request.app.state.settings.runs_dir, run_id, params)
