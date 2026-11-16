"""会话路由（契约 §6/§7/§8/§15.2/§16.1/§16.2）：创建/查询/状态机/SSE/统计/截图/标记。"""

from __future__ import annotations

import asyncio
import queue
import time
from typing import Any

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import StreamingResponse

from ..config import SERIES_MAX_POINTS
from ..errors import ApiError, validation_error
from ..registry import available_metric_ids, get_metric, is_registered
from ..providers.composite import provider_device_mode
from ..services.capability_matrix import metric_available_on_device
from ..services.collector_service import create_collector
from ..services.marker_service import validate_marker_body
from ..services.screen_capture import capture_screenshot_bytes
from ..services.statistics_service import build_session_statistics, parse_fps_ge, parse_interval_ms
from ..services.timed_screenshot import parse_interval_ms as parse_timed_interval_ms
from ..sse import SseBus, now_ms
from perfeval.collectors.android.adb import AdbClient

router = APIRouter(prefix="/api", tags=["sessions"])


def _manager(request: Request) -> Any:
    return request.app.state.sessions


@router.post("/sessions", status_code=201)
def create_session(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    manager = _manager(request)
    session = manager.create(
        device_id=body.get("deviceId"),
        package_name=body.get("packageName"),
        metric_entries=body.get("metrics"),
        record_screen=body.get("recordScreen", False),
        # M6 会话选项（契约 §21.5/§21.6/§21.7）：校验与回显在 manager.create
        fps_window=body.get("fpsWindow"),
        process_name=body.get("processName"),
        log_level=body.get("logLevel"),
        # 契约 §6.1：launchOnCreate=true 创建即执行 app_prepare（实机验证修复：
        # 此前该字段被 router 丢弃、launch 语义从未生效）
        launch_on_create=body.get("launchOnCreate", False),
    )
    # 采集即刻开始（monitoring 即出点，ARCH §4.4「接入即实时显示」）
    settings = request.app.state.settings
    # WiFi 行（契约 §21.1）：device_id 带 -wifi 后缀，采样目标须映射回 adb serial。
    # 装配流 S4：聚合面下 iOS 设备 adb_serial 短路返回 None（不解析、不分发到
    # adb 源，udid 不冒充 serial）；单源 real provider 行为不变。
    resolve_serial = getattr(request.app.state.provider, "adb_serial", None)
    session.collector = create_collector(
        session, session.provider_mode, session.bus,
        ping_s=settings.sse_ping_heartbeat_s,
        serial=resolve_serial(session.device_id) if resolve_serial else None,
    )
    session.collector.start()
    return session.detail()


@router.get("/sessions")
def list_sessions(request: Request) -> dict[str, Any]:
    return {"sessions": [s.detail() for s in _manager(request).list()]}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    return _manager(request).get(session_id).detail()


@router.post("/sessions/{session_id}/record/start")
def record_start(session_id: str, request: Request) -> dict[str, Any]:
    return _manager(request).start_record(_manager(request).get(session_id)).detail()


@router.post("/sessions/{session_id}/record/stop")
def record_stop(session_id: str, request: Request) -> dict[str, Any]:
    return _manager(request).stop_record(_manager(request).get(session_id)).detail()


@router.patch("/sessions/{session_id}/metrics")
def patch_metrics(
    session_id: str, request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    manager = _manager(request)
    session = manager.get(session_id)
    if session.state not in ("monitoring", "recording"):
        raise ApiError(
            "session_state_conflict",
            f"session {session_id} 指标启停仅限 monitoring|recording",
            details={"state": session.state},
        )
    enable = body.get("enable") or []
    disable = body.get("disable") or []
    if not enable and not disable:
        raise validation_error("enable/disable 至少给一个且非空", field="enable")
    if not isinstance(enable, list) or not isinstance(disable, list):
        raise validation_error("enable/disable 必须是数组")

    provider = request.app.state.provider
    # 装配流 S4（契约 §20.3 判定 mode 来源改造 + PATCH 平台门）：判定 mode 按
    # 设备源通道（会话创建时落定的 session.device_channel；单源回退 provider.mode
    # 逐字段不变），模式级全集随通道取；启用分支补设备 profile 交集判定
    # （capability_matrix.metric_available_on_device，契约 §20.3）——堵「Android
    # 会话 PATCH 启用 iOS 指标（或反向）→ 采集器无映射 → 静默无数据」。
    channel = provider_device_mode(provider, session.device_id)
    if channel == provider.mode:
        available = set(provider.list_metric_ids())
    else:
        available = set(available_metric_ids(channel))
    profile = provider.get_profile(session.device_id)
    enabled: dict[str, int] = dict(session.metrics)
    for entry in enable or []:
        if not isinstance(entry, dict) or "metricId" not in entry:
            raise validation_error("enable 元素必须含 metricId", field="enable")
        metric_id = entry["metricId"]
        if not is_registered(metric_id):
            raise validation_error(f"未注册的 metricId: {metric_id}", field="enable")
        if metric_id not in available:
            raise ApiError(
                "metric_unavailable",
                f"指标 {metric_id} 不可用（available=false）",
                details={"metricId": metric_id},
            )
        # 设备 profile 交集判定（§20.3，create 落点同门 PATCH 补齐）：平台/
        # 连接模式/资源依赖任一不过 → 409 metric_unavailable（details 带通道
        # reason 机器可读码）
        mode_ok, reason = metric_available_on_device(metric_id, channel, profile)
        if not mode_ok:
            raise ApiError(
                "metric_unavailable",
                f"指标 {metric_id} 对设备 {session.device_id} 不可用"
                "（capabilities ∩ profile 交集判定，契约 §20.3）",
                details={
                    "metricId": metric_id,
                    "deviceId": session.device_id,
                    **({"reason": reason} if reason else {}),
                },
            )
        spec = get_metric(metric_id)
        interval = entry.get("intervalMs", spec.default_interval_ms)
        if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
            raise validation_error("intervalMs 必须为正整数", field="intervalMs")
        enabled[metric_id] = interval
    for metric_id in disable or []:
        if not isinstance(metric_id, str) or not is_registered(metric_id):
            raise validation_error(f"未注册的 metricId: {metric_id}", field="disable")
        enabled.pop(metric_id, None)  # 已采数据保留，仅停止出点（契约 §6）

    collector = session.collector
    if collector is not None and hasattr(collector, "set_enabled"):
        collector.set_enabled(enabled)
    else:
        session.set_metrics(enabled)
    session.bus.publish(
        "metrics.config",
        metrics=[{"metricId": k, "intervalMs": v} for k, v in enabled.items()],
    )
    return session.detail()


@router.post("/sessions/{session_id}/save")
def save_session(
    session_id: str, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """收口保存（契约 §6 + §13.2 M2 扩展）。

    `exportExcel`（可选 boolean，缺省 false）：true 时 run 目录写盘后生成同名 .xlsx，
    响应附 `exportPath`（成功）或 `exportError`（失败，二者互斥）；导出失败不使
    save 失败（xlsx 是派生产物，可经 §13.1 重试）。
    """
    export_excel = body.get("exportExcel", False)
    if not isinstance(export_excel, bool):
        raise validation_error("exportExcel 必须是布尔值", field="exportExcel")
    return _manager(request).save(
        _manager(request).get(session_id), body.get("caseName"), export_excel=export_excel
    )


@router.post("/sessions/{session_id}/discard", status_code=204, response_class=Response)
def discard_session(session_id: str, request: Request) -> Response:
    _manager(request).discard(_manager(request).get(session_id))
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204, response_class=Response)
def delete_session(session_id: str, request: Request) -> Response:
    _manager(request).delete(_manager(request).get(session_id))
    return Response(status_code=204)


# ---- 实时通道（契约 §8）----


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request) -> StreamingResponse:
    manager = _manager(request)
    session = manager.get(session_id)
    last_event_id_raw = request.headers.get("last-event-id")
    try:
        last_event_id = int(last_event_id_raw) if last_event_id_raw else 0
    except ValueError:
        raise validation_error("Last-Event-ID 必须是整数") from None

    # 订阅先行（边界），hello 首条（契约 §8.3）；带 Last-Event-ID 头（含显式 "0"）
    # 才重放历史事件（hello 之前的 seq），订阅点之后的实时事件经队列续流并按 seq 去重。
    q = session.bus.subscribe()
    hello = session.bus.publish("hello", state=session.state)
    if last_event_id_raw is not None:
        replay_events, gap = session.bus.replay(last_event_id)
        replay_events = [env for env in replay_events if env["seq"] < hello["seq"]]
    else:
        replay_events, gap = [], None

    async def stream() -> Any:  # AsyncIterator[str]
        comment_s = request.app.state.settings.sse_comment_heartbeat_s
        sent_seqs = [hello["seq"]] + [env["seq"] for env in replay_events]
        if gap is not None:
            sent_seqs.append(gap["seq"])
        last_sent = max(sent_seqs)
        last_comment = time.monotonic()
        try:
            yield SseBus.format_event(hello)
            if gap is not None:
                yield SseBus.format_event(gap)
            for env in replay_events:
                yield SseBus.format_event(env)
            while True:
                now = time.monotonic()
                if now - last_comment >= comment_s:
                    last_comment = now
                    yield SseBus.comment(" ping")
                try:
                    env = await asyncio.wait_for(
                        asyncio.to_thread(q.get, True, 0.5), timeout=1.0
                    )
                except (asyncio.TimeoutError, queue.Empty):
                    continue
                if env["seq"] <= last_sent:
                    continue
                last_sent = env["seq"]
                yield SseBus.format_event(env)
        finally:
            session.bus.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- 指标数据（契约 §7）----


@router.get("/sessions/{session_id}/metrics")
def session_metrics(session_id: str, request: Request) -> dict[str, Any]:
    session = _manager(request).get(session_id)
    params = request.query_params
    metric_ids_raw = params.get("metricIds")
    if metric_ids_raw:
        metric_ids = [m.strip() for m in metric_ids_raw.split(",") if m.strip()]
        for metric_id in metric_ids:
            if not is_registered(metric_id):
                raise validation_error(f"未注册的 metricId: {metric_id}", field="metricIds")
    else:
        metric_ids = sorted(session.metrics)
    try:
        max_points = int(params.get("maxPoints", SERIES_MAX_POINTS))
    except ValueError:
        raise validation_error("maxPoints 必须是整数", field="maxPoints") from None
    max_points = max(1, min(max_points, SERIES_MAX_POINTS))
    from_ms = params.get("fromMs")
    to_ms = params.get("toMs")
    from_ms = int(from_ms) if from_ms else None
    to_ms = int(to_ms) if to_ms else None

    rows = session.records()
    series: list[dict[str, Any]] = []
    downsampled = False
    for metric_id in metric_ids:
        points = [
            {"ts": row["tsMs"], "value": row["value"]}
            for row in rows
            if row["metricId"] == metric_id
            and (from_ms is None or row["tsMs"] >= from_ms)
            and (to_ms is None or row["tsMs"] <= to_ms)
        ]
        points.sort(key=lambda p: p["ts"])
        if len(points) > max_points:
            points = _downsample(points, max_points)
            downsampled = True
        entry: dict[str, Any] = {"metricId": metric_id, "unit": get_metric(metric_id).unit}
        if points:
            entry["points"] = points
        else:
            # 无数据点：字段缺失 ≠ 0（契约 §1 空值约定）
            entry["points"] = []
        series.append(entry)
    body: dict[str, Any] = {
        "sessionId": session.id,
        "fromMs": from_ms if from_ms is not None else (rows[0]["tsMs"] if rows else 0),
        "toMs": to_ms if to_ms is not None else (rows[-1]["tsMs"] if rows else 0),
        "series": series,
        "downsampled": downsampled,
    }
    if downsampled:
        body["sampling"] = "lttb"
    return body


def _downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """分桶降采样、保峰（对齐 perfeval engine._downsample_series 口径）。"""
    import math as _math

    bucket_size = _math.ceil(len(points) / max_points)
    out: list[dict[str, Any]] = []
    for start in range(0, len(points), bucket_size):
        bucket = points[start : start + bucket_size]
        if len(bucket) == 1:
            out.append(bucket[0])
            continue
        values = [p["value"] for p in bucket]
        lo, hi = min(values), max(values)
        mean = sum(values) / len(values)
        keep = hi if (hi - mean) >= (mean - lo) else lo
        out.append(next(p for p in bucket if p["value"] == keep))
    return out


@router.get("/sessions/{session_id}/statistics")
def session_statistics(session_id: str, request: Request) -> dict[str, Any]:
    """会话统计量（契约 §12/§18，perfdog.statistics.v1）：内存缓冲现算，随时可算。

    fpsGe 缺省 18,25；fromMs/toMs 可选闭区间（§18，M2 内核先过滤再现算，不另算
    第二份）；session 不存在 → 404 session_not_found（manager.get）。
    """
    session = _manager(request).get(session_id)
    fps_ge = parse_fps_ge(request.query_params.get("fpsGe"))
    from_ms, to_ms = parse_interval_ms(request.query_params)
    return build_session_statistics(session, fps_ge, from_ms=from_ms, to_ms=to_ms)


@router.get("/sessions/{session_id}/summary")
def session_summary(session_id: str, request: Request) -> dict[str, Any]:
    from perfeval.collectors.android.samples import parse_samples_events
    from perfeval.core.summary import aggregate_summary

    session = _manager(request).get(session_id)
    numeric = [
        r
        for r in session.records()
        if isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool)
    ]
    if not numeric:
        raise ApiError(
            "session_state_conflict",
            f"session {session_id} 未记录任何数据",
            details={"state": session.state},
        )
    return aggregate_summary(
        session.id,
        timeline_events=parse_samples_events(
            [
                {
                    "tsMs": r["tsMs"],
                    "metricId": r["metricId"],
                    "value": r["value"],
                    "tags": r["tags"],
                }
                for r in numeric
            ],
            run_id=session.id,
            # 与 save 落盘同口径（sim-2/sim-3 修复，2026-11-04 体检；装配流 S4
            # 修订）：平台取会话创建时按设备源通道落定的 device_platform——
            # simulator 会话标 ios、真机 iOS 会话亦标 ios（聚合面
            # provider_mode="real" 不再错标 android）、Android 会话标 android；
            # tsMonotonicNs 以会话创建时刻为原点的 run 内相对纳秒
            platform=session.device_platform,
            ts_origin_ms=session.created_at,
        ),
    )


# ---- 截图（契约 §16.2，M4/R36：real-only）----


@router.post("/sessions/{session_id}/screenshot", status_code=201)
def take_screenshot(session_id: str, request: Request) -> dict[str, Any]:
    """截图（契约 §16.2；real=screencap / M-iOS simulator=simctl screenshot）。

    - synthetic 模式：501 not_implemented + SSE diagnostic(screenshot.unavailable)；
    - 状态约束：recording|stopped（staging run 目录已存在）可用；monitoring 无
      run 目录可落、saved|discarded 已收口 → 409 session_state_conflict；
    - 产物直接写 ``<run 目录>/artifacts/screenshot-<tsMs>.png``，save 后经 §17
      索引与下载服务。
    """
    session = _manager(request).get(session_id)
    if session.provider_mode == "synthetic":
        session.bus.publish(
            "diagnostic",
            code="screenshot.unavailable",
            severity="info",
            message="synthetic 模式无真机截图（real-only），会话其余能力不受影响",
            detail=None,
        )
        raise ApiError(
            "not_implemented", "截图为 real-only 能力（exec-out screencap），synthetic 模式未实现"
        )
    if (
        session.provider_mode == "real"
        and getattr(session, "device_platform", "android") == "ios"
    ):
        # 装配流 S4（契约 §26）：真机 iOS 截图「本期不接线」（devicectl capture
        # screenshot 命令面存在、可用性未勘察）——契约错误码如实拒绝，不静默
        # 不走 Android screencap 路径（exec-out screencap 对 iOS 设备必败）。
        session.bus.publish(
            "diagnostic",
            code="screenshot.unavailable",
            severity="info",
            message="真机 iOS 截图本期未接线（契约 §26），会话其余能力不受影响",
            detail=None,
        )
        raise ApiError(
            "not_implemented",
            "真机 iOS 截图本期未接线（exec-out screencap 为 Android 链路能力，契约 §26）",
        )
    if session.state not in ("recording", "stopped"):
        raise ApiError(
            "session_state_conflict",
            f"session {session_id} 截图仅限 recording|stopped",
            details={"state": session.state},
        )
    run_path = session.run_path
    if run_path is None:  # 防御：状态约束已保证，缺失按冲突处理
        raise ApiError(
            "session_state_conflict",
            f"session {session_id} 无 run 目录可落截图",
            details={"state": session.state},
        )
    if session.provider_mode == "simulator":
        # M-iOS §23.3：simctl io screenshot（经临时文件；失败 artifact_pull_failed）
        from ..services.simulator_sampler import capture_simulator_screenshot_bytes

        png = capture_simulator_screenshot_bytes(session.device_id)
    else:
        png = capture_screenshot_bytes(AdbClient(), session.device_id)
    artifacts_dir = run_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    name = f"screenshot-{now_ms()}.png"
    (artifacts_dir / name).write_bytes(png)
    return {
        "sessionId": session.id,
        "artifact": {"name": name, "kind": "screenshot", "bytes": len(png)},
    }


# ---- M7 R38：深度分析定时截图（契约 §22.3，real-only 周期 screencap）----


@router.post("/sessions/{session_id}/timed-screenshot", status_code=202)
def start_timed_screenshot(
    session_id: str, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """定时截图循环（契约 §22.3）：``intervalMs`` 缺省 2000、允许 500–60000。

    - 校验顺序（如实声明）：请求参数（400）→ 模式门（synthetic 501 +
      SSE ``timed-screenshot.unavailable`` info 诊断）→ 状态门（仅
      ``recording`` 可启动，其余 → 409 session_state_conflict）；
    - real/simulator：首帧同步截取（设备故障当场 502 artifact_pull_failed），
      后续帧由 ``TimedScreenshotLoop`` 异步产出（``screenshot-<tsMs>.png`` 同
      §16.2 命名，经 §17 artifacts 索引；simulator 走 simctl screenshot，M-iOS
      §23.3）；数量上限防滥用（循环自然封顶，timed_screenshot 模块常量）；
      重复 POST 以新 intervalMs 幂等重启（先停旧循环，不叠加）；
    - 随 ``record/stop``/save/discard/delete 自动终止（SessionManager 接线）；
    - 「自动配比到当前分析帧」是前端交互（截图 ts 对齐 §22.1 frames[].tsEpochMs），
      数据面由 artifacts 索引 + deep-analysis 帧列表供给。
    """
    manager = _manager(request)
    session = manager.get(session_id)  # 404 session_not_found
    interval_ms = parse_timed_interval_ms(body.get("intervalMs"))  # 400
    if session.provider_mode == "synthetic":
        session.bus.publish(
            "diagnostic",
            code="timed-screenshot.unavailable",
            severity="info",
            message="synthetic 模式无真机定时截图（real-only），会话其余能力不受影响",
            detail=None,
        )
        raise ApiError(
            "not_implemented",
            "定时截图为 real-only 能力（exec-out screencap 循环），synthetic 模式未实现",
        )
    if (
        session.provider_mode == "real"
        and getattr(session, "device_platform", "android") == "ios"
    ):
        # 装配流 S4（契约 §26）：真机 iOS 定时截图同截图路径本期不接线，
        # 契约错误码如实拒绝。
        session.bus.publish(
            "diagnostic",
            code="timed-screenshot.unavailable",
            severity="info",
            message="真机 iOS 定时截图本期未接线（契约 §26），会话其余能力不受影响",
            detail=None,
        )
        raise ApiError(
            "not_implemented",
            "真机 iOS 定时截图本期未接线（exec-out screencap 循环为 Android 链路能力）",
        )
    if session.state != "recording":
        raise ApiError(
            "session_state_conflict",
            f"session {session_id} 定时截图仅限 recording",
            details={"state": session.state},
        )
    if session.provider_mode == "simulator":
        # M-iOS §23.3：周期 simctl screenshot（capture 注入面按 provider 路由）
        from ..services.simulator_sampler import capture_simulator_screenshot_bytes

        manager.start_timed_screenshot(
            session, interval_ms,
            capture=lambda: capture_simulator_screenshot_bytes(session.device_id),
        )
    else:
        manager.start_timed_screenshot(session, interval_ms)
    return {"sessionId": session.id, "intervalMs": interval_ms, "state": "started"}


# ---- 标记与标注（契约 §15.2，perfdog.markers.v1：save 前仅存会话内存）----


def _marker_session(request: Request, session_id: str) -> Any:
    """POST/DELETE 共用状态门：monitoring|recording|stopped 可写（§15.2）。"""
    session = _manager(request).get(session_id)
    if session.state not in ("monitoring", "recording", "stopped"):
        raise ApiError(
            "session_state_conflict",
            f"session {session_id} 标记仅可在 monitoring|recording|stopped 修改",
            details={"state": session.state},
        )
    return session


@router.post("/sessions/{session_id}/labels", status_code=201)
def add_label(session_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    session = _marker_session(request, session_id)
    marker = session.add_marker("label", validate_marker_body("label", body, now_ms()))
    _publish_marker_event(session, "add", marker)
    return marker


@router.post("/sessions/{session_id}/notes", status_code=201)
def add_note(session_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    session = _marker_session(request, session_id)
    marker = session.add_marker("note", validate_marker_body("note", body, now_ms()))
    _publish_marker_event(session, "add", marker)
    return marker


@router.post("/sessions/{session_id}/marks", status_code=201)
def add_mark(session_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    session = _marker_session(request, session_id)
    marker = session.add_marker("mark", validate_marker_body("mark", body, now_ms()))
    _publish_marker_event(session, "add", marker)
    return marker


@router.get("/sessions/{session_id}/markers")
def list_markers(session_id: str, request: Request) -> dict[str, Any]:
    # 任意状态可读（契约 §15.2 golden）
    session = _manager(request).get(session_id)
    return {"sessionId": session.id, "markers": session.markers()}


@router.delete("/sessions/{session_id}/markers/{marker_id}", status_code=204, response_class=Response)
def delete_marker(session_id: str, marker_id: str, request: Request) -> Response:
    session = _marker_session(request, session_id)
    if session.remove_marker(marker_id):
        _publish_marker_event(session, "remove", {"id": marker_id})
    return Response(status_code=204)  # 未知 id 同样 204（幂等，契约 §15.2）


def _publish_marker_event(session: Any, action: str, marker: dict[str, Any]) -> None:
    """契约 §15.2 SSE 新 type ``marker``（§8.1 兼容：消费方忽略未知 type）。"""
    session.bus.publish("marker", action=action, marker=marker)
