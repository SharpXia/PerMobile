"""报告/运行记录路由（契约 §9/§12/§13.1；ARCH §4.7：run 侧统计量归 reports router）。

M2 本文件承载 `GET /api/runs/{runId}/statistics`（perfdog.statistics.v1，
statistics_service 装配）与 `GET /api/runs/{runId}/export.xlsx`（§13.1，
excel_export 实时生成）；M3 起承载 `POST /api/reports/compare`（§9，
compare_summaries 引擎透传，compare_service 装配）。
M8+（2026-11-04）实装 §9 runs 列表/详情（`GET /api/runs` / `GET /api/runs/{runId}`，
case_index 轻索引 + artifact_service 工件索引同源）——M2 的 not_implemented
占位随实装移除（main.py）；MCP server（契约 §25）报告获取工具依赖此两端点。
S3（2026-11-08）§9 compare 请求体扩展（budget/fromMs/toMs/candidateRunIds，
§11.1 不升版本，契约 delta 待 S1 落笔）与 §21.4 startup capture（run 级旁路
测量 POST /runs/{runId}/startup/capture，结果落 startup.ndjson）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request, Response

from ..errors import ApiError, validation_error
from ..services.case_index import list_runs as build_runs_index
from ..services.case_index import run_detail as build_run_detail
from ..services.compare_service import (
    DEFAULT_COMPARE_MODE,
    compare_runs,
    parse_candidate_run_ids,
    parse_run_ids,
    parse_window,
    validate_budget,
)
from ..services.excel_export import XLSX_MEDIA_TYPE, build_run_export_bytes, content_disposition
from ..services.startup_capture import capture_startup, read_startup_ndjson
from ..services.statistics_service import (
    build_label_segments,
    build_run_statistics,
    parse_by_label_segment,
    parse_fps_ge,
    parse_interval_ms,
)

router = APIRouter(prefix="/api", tags=["reports"])

# M6 R18（契约 §21.4）→ S3 落地更新（契约 delta 待 S1 落笔同步 golden note）：
# TTID 等价采集已落地（startup capture 作业：logcat 『Displayed』行解析，
# ActivityTaskManager/ActivityManager 同解析）；TTFD 需应用配合（reportFullyDrawn()
# 才有『Fully drawn』行，未调用 → ttfdMs null 而非 0）。两值为 logcat 行解析口径，
# 与 PerfDog 官方（FrameMetrics/reportFullyDrawn 回调链）语义近似、数值不可直接对比。
STARTUP_NOTE = (
    "口径声明：TotalTime 来自 am start -W（perfeval startup.cold.ms 同源，[R§2.5]）；"
    "TTID 为启动捕获作业的 logcat『Displayed』行等价采集（ActivityTaskManager/"
    "ActivityManager 同解析，run 级旁路测量，不随实时监控会话自动执行）；TTFD 需"
    "应用代码调用 reportFullyDrawn()（[F§3.11]）产生『Fully drawn』行，未调用则 "
    "ttfdMs 为 null 而非 0——两值为 logcat 行解析口径，与 PerfDog 官方"
    "（FrameMetrics/reportFullyDrawn 回调链）语义近似、数值不可直接对比"
)
STARTUP_NOTE_NO_DATA = (
    STARTUP_NOTE + "；本 run 未包含启动测量数据（am start -W 未执行或未产出 "
    "startup.cold.ms 事件），totalTimeMs 为 null 而非 0"
)
STARTUP_NOTE_NO_CAPTURE = (
    "；本 run 未执行启动捕获作业（POST /runs/{runId}/startup/capture）或捕获未观测到"
    "对应行，缺省值以 null 承载而非 0"
)


@router.post("/reports/compare")
def compare_reports(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """对比报告（契约 §9）：perfeval.compare.v1 原样透出（compare_summaries，
    [R§3.3]），顶层附可选 meta（§9 M3 扩展，§11.1 不升版本）。

    前提校验失败（设备/应用/版本错配）由引擎结果 errors/reliability.reasons
    承载，HTTP 仍 200；入参非法（<1 条 baseline、runId 非字符串、budget 结构
    非法、fromMs/toMs 非法）→ 400 validation_error；run 不存在或无 summary.json
    → 404 report_not_found。
    对比池上限 5 条由前端执行（契约 §9 M3 扩展）；后端候选组上限 9（引擎 N 路
    2–10 组：基线 1 组 + 候选 ≤9，`candidateRunIds` 超限 → 400）。
    S3 扩展（§11.1 可选请求字段）：budget（逐指标预算门禁）、fromMs/toMs
    （时间区间窗口化）、candidateRunIds（N 路，旧 candidateRunId 保留兼容）。
    """
    baseline_run_ids = parse_run_ids(body.get("baselineRunIds"), "baselineRunIds")
    candidate_run_ids = parse_candidate_run_ids(body)
    budget = body.get("budget")
    validate_budget(budget)
    from_ms, to_ms = parse_window(body)
    return compare_runs(
        request.app.state.settings.runs_dir,
        baseline_run_ids,
        candidate_run_ids,
        mode=body.get("mode", DEFAULT_COMPARE_MODE),
        budget=budget,
        from_ms=from_ms,
        to_ms=to_ms,
    )


# ---- §9 runs 轻索引（M8+ 实装；MCP list_runs/get_run 依赖）----


@router.get("/runs")
def runs_index(request: Request) -> dict[str, Any]:
    """runs 目录实时轻索引（契约 §9）：saved 会话 + 旧 run 目录兼容导入。

    与 `/api/cases`（§14）的分工见契约 §14 注：本端点每次直接枚举 runs 目录
    （事实源），不带统计快照、无筛选分页；`device` 为 manifest 设备标识
    （device_id 字符串，§14 行同源）。
    """
    return build_runs_index(request.app.state.settings.runs_dir)


@router.get("/runs/{run_id}")
def run_detail_endpoint(run_id: str, request: Request) -> dict[str, Any]:
    """单 run 详情（契约 §9）：轻索引行 + manifest 原样 + summary.json 原样
    （无 summary → null，不伪造空 summary）+ artifacts 索引（§17 同源）。
    run 不存在 → 404 report_not_found。
    """
    return build_run_detail(request.app.state.settings.runs_dir, run_id)


@router.get("/runs/{run_id}/statistics")
def run_statistics(run_id: str, request: Request) -> dict[str, Any]:
    """run 统计量（契约 §12/§18）：fpsGe 缺省 18,25；fromMs/toMs 可选闭区间
    （§18，区间内无点 → none 基线同形 + 回显）；run 不存在 → 404 report_not_found。

    `byLabelSegment=1`（§18 R32 实现期扩展，§11.1 新增可选参数不升版本）：顶层
    body 仍为 §12/§18 整段统计（即分段统计表的 All 行），另附 ``labelSegments``
    段行数组（build_label_segments，同一统计内核按 label 段边界切分）。
    """
    fps_ge = parse_fps_ge(request.query_params.get("fpsGe"))
    from_ms, to_ms = parse_interval_ms(request.query_params)
    by_label_segment = parse_by_label_segment(request.query_params.get("byLabelSegment"))
    runs_dir = request.app.state.settings.runs_dir
    body = build_run_statistics(runs_dir, run_id, fps_ge, from_ms=from_ms, to_ms=to_ms)
    if by_label_segment:
        body["labelSegments"] = build_label_segments(
            runs_dir, run_id, fps_ge, from_ms=from_ms, to_ms=to_ms
        )
    return body


@router.get("/runs/{run_id}/export.xlsx")
def run_export_xlsx(run_id: str, request: Request) -> Response:
    """Excel 导出（契约 §13.1，[I§2.5]）：实时生成二进制 xlsx，无缓存文件。

    文件名 = run 目录名 + .xlsx；非 ASCII 案例名按 RFC 6266 附加
    filename*=UTF-8''。run 不存在 → 404 report_not_found。
    """
    runs_dir = request.app.state.settings.runs_dir
    payload, filename = build_run_export_bytes(runs_dir, run_id)
    return Response(
        content=payload,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": content_disposition(filename)},
    )


# ---- M6 R18：StartupTiming（契约 §21.4）→ S3 落地 capture 旁路作业 ----


@router.post("/runs/{run_id}/startup/capture")
def run_startup_capture(run_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """run 级启动旁路测量（契约 §21.4 实现期编排口落地，S3）。

    执行 `am start -W`（TotalTime，perfeval runner _derive_startup_event 同源
    口径）+ 并行 logcat 尾随解析：『Displayed pkg/… +Nms』→ TTID、
    『Fully drawn』行 → TTFD（仅对调用 reportFullyDrawn() 的 App 产出）。
    结果落 run 目录 `startup.ndjson`（契约 §10 文件清单 delta 待 S1 落笔）；
    GET /runs/{runId}/startup 回填 ttidMs/ttfdMs。

    请求体：`deviceSerial` 必填非空字符串（调用方从设备列表取；WiFi 行 id 带
    `-wifi` 后缀时经 provider.adb_serial 映射回 host:port，映射口径同
    collector serial 参数——sessions.py 同款接线）；`packageName` 可选（缺省
    取 run manifest 的 packageName）。

    错误：404 report_not_found（run 不存在）；400 validation_error
    （deviceSerial 非法）；501 not_implemented（synthetic/simulator 无 adb
    通道）；503 adb_not_found（adb 缺失/logcat 流不可用）。
    该端点会重新拉起 APP（[F§3.11]），调用方须在无实时会话采集干扰时执行
    （契约 §21.4：默认不在实时监控会话内自动执行）。
    """
    # 入参校验先行（400 优先于模式判定：请求体非法与 provider 模式无关）
    device_serial = body.get("deviceSerial")
    if not isinstance(device_serial, str) or not device_serial.strip():
        raise validation_error("deviceSerial 必填且为非空字符串", field="deviceSerial")
    provider = request.app.state.provider
    if getattr(provider, "mode", None) in ("synthetic", "simulator"):
        raise ApiError(
            "not_implemented",
            "启动捕获为 real-only 作业（am start -W + logcat 需真机 adb 通道），"
            "synthetic/simulator 无对应实现",
        )
    # WiFi 行（契约 §21.1）：id 带 -wifi 后缀 → 经 provider.adb_serial 映射回
    # host:port（映射口径同 collector serial 参数；USB 行原样）
    resolve_serial = getattr(provider, "adb_serial", None)
    if resolve_serial is not None:
        device_serial = resolve_serial(device_serial)

    package_name = body.get("packageName")
    if package_name is not None and (not isinstance(package_name, str) or not package_name.strip()):
        raise validation_error("packageName 须为非空字符串", field="packageName")
    return capture_startup(
        request.app.state.settings.runs_dir,
        run_id,
        device_serial.strip(),
        package_name=package_name,
    )


@router.get("/runs/{run_id}/startup")
def run_startup(run_id: str, request: Request) -> dict[str, Any]:
    """启动耗时（契约 §21.4）：TotalTime + TTID/TTFD（capture 回填）+ note。

    - TotalTime：run 产物 timeline.ndjson 的 ``startup.cold.ms`` 指标事件
      （perfeval runner _derive_startup_event 同源，am start -W 解析，
      runner.py:864-895 [R§2.5]）；整数毫秒透出；
    - TTID/TTFD：run 目录 startup.ndjson 最近一次已完成捕获的回填（S3 落地，
      logcat『Displayed』/『Fully drawn』行解析口径）；无捕获产物或未观测到
      → null + note（不填 0，[R§8.6]）；
    - run 无启动测量数据 → totalTimeMs:null + note（200，无数据是常态不是错误）；
    - run 不存在 → 404 report_not_found；note 显式声明口径差异（矩阵 M6 行既定，
      文案随 capture 落地更新，契约 delta 待 S1 落笔）。
    """
    from perfeval.core.timeline import TimelineRowError, read_timeline

    from ..services.statistics_service import resolve_run_dir

    run_path = resolve_run_dir(request.app.state.settings.runs_dir, run_id)
    total_ms: int | None = None
    timeline_path = run_path / "timeline.ndjson"
    if timeline_path.is_file():
        try:
            with timeline_path.open("r", encoding="utf-8") as source:
                events = read_timeline(source)
        except (TimelineRowError, OSError, UnicodeDecodeError):
            events = []
        for event in events:
            if event.type == "metric" and event.name == "startup.cold.ms":
                value = event.data.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    total_ms = int(value)
                break  # 首个启动测量事件（runner 产 seq=1 单事件）

    ttid_ms, ttfd_ms = read_startup_ndjson(run_path)
    has_capture = ttid_ms is not None or ttfd_ms is not None
    if total_ms is None:
        note = STARTUP_NOTE_NO_DATA
    elif has_capture:
        note = STARTUP_NOTE
    else:
        note = STARTUP_NOTE + STARTUP_NOTE_NO_CAPTURE
    return {
        "runId": run_id,
        "totalTimeMs": total_ms,
        "ttidMs": ttid_ms,
        "ttfdMs": ttfd_ms,
        "note": note,
    }
