"""run 侧工件/回放路由（M4，契约 §15.3/§17/§19，DEVELOPMENT_PLAN R35/R37）。

- §17：artifacts 索引（run 目录 artifacts/ 扫描）、video 端点（HTTP Range 206）、
  逐工件下载；
- §15.3：markers.ndjson sidecar 读回；
- §15.2 run 侧扩展：标注增量写 POST /runs/{id}/labels|notes|marks（sidecar 追加，
  marker_service.append_markers_sidecar 临时文件+rename 原子替换；契约 delta 已落笔
  API_CONTRACT.md §15.6）；
- §19：回放聚合一次拉取（series + markers + artifacts + clockAnchors + summary
  引用），series 复用 §7 降采样口径（单一事实源，routers.sessions._downsample）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from ..config import SERIES_MAX_POINTS
from ..errors import ApiError, validation_error
from ..registry import get_metric, is_registered
from ..services.artifact_service import (
    CONTENT_TYPES,
    build_artifact_index,
    resolve_artifact_path,
    select_video_artifact,
)
from ..services.marker_service import append_markers_sidecar, read_markers_body, validate_marker_body
from ..services.statistics_service import resolve_run_dir
from ..sse import now_ms
from .sessions import _downsample  # §7 降采样单一实现复用（单一事实源）

router = APIRouter(prefix="/api", tags=["artifacts"])

_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_VIDEO_MEDIA_TYPE = "video/mp4"


def _runs_dir(request: Request) -> Path:
    return request.app.state.settings.runs_dir


# ---- §17 工件索引 ----


@router.get("/runs/{run_id}/artifacts")
def run_artifacts(run_id: str, request: Request) -> dict[str, Any]:
    run_path = resolve_run_dir(_runs_dir(request), run_id)
    return {"runId": run_id, "artifacts": build_artifact_index(run_path)}


# ---- §17 video 端点（HTTP Range，RFC 9110）----


@router.get("/runs/{run_id}/video")
def run_video(run_id: str, request: Request) -> Response:
    run_path = resolve_run_dir(_runs_dir(request), run_id)
    name_param = request.query_params.get("name")
    if name_param is not None and not name_param:
        raise validation_error("name 不得为空字符串", field="name")
    name, path = select_video_artifact(run_path, name_param)
    if not name or not path.is_file():
        # run 无任何 video 工件（未开录屏/判废剔除）或 name 不在索引 → 404
        raise ApiError("report_not_found", f"run {run_id} 无可用 video 工件")
    total = path.stat().st_size

    range_header = request.headers.get("range")
    if range_header is None:
        # 无 Range → 200 全量 + Accept-Ranges（契约 §17）
        return _file_response(path, total, start=0, end=total - 1, partial=False)

    parsed = _parse_range(range_header, total)
    if parsed is None:
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{total}",
                "Accept-Ranges": "bytes",
            },
        )
    start, end = parsed
    return _file_response(path, total, start=start, end=end, partial=True)


def _parse_range(header: str, total: int) -> tuple[int, int] | None:
    """``bytes=a-b | a- | -n`` → (start, end) 闭区间；语法非法/起点越界 → None。

    后缀形式 ``-n`` 取最后 n 字节（n=0 或 n>total 时整文件语义按 RFC 9110 收敛为
    全量可满足区间）。多区间请求不支持（回放场景无此需求）→ 视为非法 416。
    """
    match = _RANGE_HEADER_RE.fullmatch(header.strip())
    if match is None:
        return None
    raw_start, raw_end = match.group(1), match.group(2)
    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":
        # 后缀：最后 raw_end 字节；suffix=0 或空表示 → 不可满足（416）
        suffix = int(raw_end)
        if suffix <= 0 or total == 0:
            return None
        length = min(suffix, total)
        return total - length, total - 1
    start = int(raw_start)
    if start >= total:
        return None  # 起点越界 → 416（RFC 9110 unsatisfiable）
    if raw_end == "":
        return start, total - 1
    end = int(raw_end)
    if end < start:
        return None
    return start, min(end, total - 1)


def _file_response(
    path: Path, total: int, *, start: int, end: int, partial: bool
) -> Response:
    """200 全量 / 206 分片统一响应：按字节窗口流式读，不整载大文件。"""

    def chunks() -> Iterator[bytes]:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = handle.read(min(65536, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return StreamingResponse(
        chunks(),
        status_code=206 if partial else 200,
        media_type=_VIDEO_MEDIA_TYPE,
        headers=headers,
    )


# ---- §17 逐工件下载 ----


@router.get("/runs/{run_id}/artifacts/{name}")
def run_artifact_download(run_id: str, name: str, request: Request) -> Response:
    run_path = resolve_run_dir(_runs_dir(request), run_id)
    path = resolve_artifact_path(run_path, name)
    if path is None:
        raise ApiError("report_not_found", f"run {run_id} 索引中无工件 {name}")
    suffix = path.suffix.lower()
    media_type = CONTENT_TYPES.get(suffix, "application/octet-stream")
    return FileResponse(path, media_type=media_type)


# ---- §15.3 run 侧标记读回 ----


@router.get("/runs/{run_id}/markers")
def run_markers(run_id: str, request: Request) -> dict[str, Any]:
    run_path = resolve_run_dir(_runs_dir(request), run_id)
    return read_markers_body(run_path, run_id)


# ---- §15.2 run 侧扩展：标注增量写（POST labels|notes|marks；delta 已落笔契约 §15.6）----

# 划界说明（如实）：run 目录本体是 §10 只读事实源；本组端点是对 §15.1 sidecar
# markers.ndjson 的 run 侧**追加**通道（save 期整体写之外的增量路径），会话侧端点
# （§15.2 POST /sessions/...）状态门 409 拦截 saved 后，run 侧标注即唯一可写面。
# 响应元素为落盘同形（无 id——id 是会话期 DELETE 句柄，§15.1 不落盘）。


def _run_marker_add(run_id: str, request: Request, kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST /runs/{run_id}/{labels|notes|marks} 共用实现：404 → 400 校验 → 原子追加 → 201。"""
    run_path = resolve_run_dir(_runs_dir(request), run_id)
    element = validate_marker_body(kind, body, now_ms())
    try:
        append_markers_sidecar(run_path, [element])
    except OSError as exc:
        raise ApiError("internal_error", f"markers.ndjson 写盘失败（run {run_id}）：{exc}") from exc
    return element


@router.post("/runs/{run_id}/labels", status_code=201)
def run_add_label(run_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _run_marker_add(run_id, request, "label", body)


@router.post("/runs/{run_id}/notes", status_code=201)
def run_add_note(run_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _run_marker_add(run_id, request, "note", body)


@router.post("/runs/{run_id}/marks", status_code=201)
def run_add_mark(run_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _run_marker_add(run_id, request, "mark", body)


# ---- §19 回放聚合 ----


@router.get("/runs/{run_id}/replay")
def run_replay(run_id: str, request: Request) -> dict[str, Any]:
    run_path = resolve_run_dir(_runs_dir(request), run_id)
    max_points = _parse_max_points(request.query_params.get("maxPoints"))
    from perfeval.collectors.android.samples import read_samples

    series: list[dict[str, Any]] = []
    first_ts: int | None = None
    last_ts: int | None = None
    downsampled = False
    by_metric: dict[str, list[dict[str, Any]]] = {}
    samples_path = run_path / "samples.ndjson"
    if samples_path.is_file():
        try:
            records = read_samples(samples_path)
        except OSError:
            records = []
        for record in records:
            by_metric.setdefault(record.metric_id, []).append(
                {"ts": int(record.ts_ms), "value": float(record.value)}
            )
    for metric_id in sorted(by_metric):
        points = sorted(by_metric[metric_id], key=lambda p: p["ts"])
        for point in points:
            if first_ts is None or point["ts"] < first_ts:
                first_ts = point["ts"]
            if last_ts is None or point["ts"] > last_ts:
                last_ts = point["ts"]
        if len(points) > max_points:
            points = _downsample(points, max_points)
            downsampled = True
        spec = get_metric(metric_id) if is_registered(metric_id) else None
        series.append(
            {
                "metricId": metric_id,
                "unit": spec.unit if spec is not None else None,
                "points": points,
            }
        )

    body: dict[str, Any] = {
        "runId": run_id,
        "caseName": run_path.name,
        "durationMs": (last_ts - first_ts) if first_ts is not None and last_ts is not None else 0,
        "series": series,
        "downsampled": downsampled,
        # §19：sidecar 读回（§15.3 同源）；无标记 → []
        "markers": _replay_markers(run_path, run_id),
        # §19：§17 索引元素原样
        "artifacts": build_artifact_index(run_path),
        # §19：manifest context.clockAnchors 原样；无锚点 → null
        "clockAnchors": _read_clock_anchors(run_path),
        # §19：summary 引用不内联（阈值参数由客户端附加）
        "summary": {
            "summaryAvailable": _summary_available(run_path),
            "statisticsUrl": f"/api/runs/{run_id}/statistics",
        },
    }
    if downsampled:
        body["sampling"] = "lttb"
    return body


def _replay_markers(run_path: Path, run_id: str) -> Any:
    """契约 §19：有 sidecar → §15.3 读回体（同形同源）；无标记 → []。"""
    body = read_markers_body(run_path, run_id)
    return body if body["markers"] else []


def _read_clock_anchors(run_path: Path) -> Any:
    import json

    manifest_path = run_path / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(manifest, dict):
        return None
    context = manifest.get("context")
    if not isinstance(context, dict):
        return None
    return context.get("clockAnchors")


def _summary_available(run_path: Path) -> bool:
    import json

    summary_path = run_path / "summary.json"
    if not summary_path.is_file():
        return False
    try:
        return isinstance(json.loads(summary_path.read_text(encoding="utf-8")), dict)
    except (json.JSONDecodeError, OSError):
        return False


def _parse_max_points(raw: str | None) -> int:
    if raw is None:
        return SERIES_MAX_POINTS
    try:
        value = int(raw)
    except ValueError:
        raise validation_error("maxPoints 必须是整数", field="maxPoints") from None
    return max(1, min(value, SERIES_MAX_POINTS))
