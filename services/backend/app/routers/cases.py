"""case 库路由（契约 §14，ARCH §4.7；DEVELOPMENT_PLAN R42）。

四端点：GET /api/cases（筛选/排序/分页）、POST /api/cases/rebuild（幂等重建）、
PATCH /api/cases/{runId}（改名）、DELETE /api/cases/{runId}（删用例）。

DELETE 语义（契约 §14 口径，无分叉）：**一次删除事实源（run 目录）+ 索引行 +
同名 .xlsx**——只删索引会被 rebuild 复活，故必须连事实源一起删（与前端删除
确认框文案、store 成功日志同口径）。不存在 `?purge` 参数：曾有的「默认仅删
索引行」实现口径已废弃（与契约 §14 及 UI 承诺不一致，独立评审 blocking 项）。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request, Response

from ..errors import ApiError, validation_error
from ..services import case_index as case_index_service
from ..services.case_index import DEFAULT_LIMIT, CaseIndex
from ..services.statistics_service import resolve_run_dir
from ..sse import now_ms

logger = logging.getLogger("pd-backend.case-index")

router = APIRouter(prefix="/api", tags=["cases"])


def _index(request: Request) -> CaseIndex:
    return request.app.state.case_index


def _int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise validation_error(f"{name} 必须是整数", field=name) from None


@router.get("/cases")
def list_cases(request: Request) -> dict[str, Any]:
    """用例表（契约 §14 GET）：query 子串筛选 / device 精确匹配 / sort+order /
    limit(1–200) + offset；total = 筛选后总数；事实源已消失的行不展示。"""
    index = _index(request)
    params = request.query_params
    return case_index_service.list_cases(
        index,
        query=params.get("query") or None,
        device=params.get("device") or None,
        sort=params.get("sort") or "createdAt",
        order=params.get("order") or "desc",
        limit=_int_param(request, "limit", DEFAULT_LIMIT),
        offset=_int_param(request, "offset", 0),
    )


@router.post("/cases/rebuild")
def rebuild_cases(request: Request) -> dict[str, Any]:
    """全量重建（契约 §14）：扫 runs 根目录按事实源重建；幂等；含旧 run 兼容导入；
    索引文件缺失或损坏时以新建空库恢复（CaseIndex._ensure_schema 自愈 + 本端点）。"""
    settings = request.app.state.settings
    total = case_index_service.rebuild_case_index(settings.runs_dir, _index(request))
    return {"rebuiltAt": now_ms(), "total": total}


@router.patch("/cases/{run_id}")
def rename_case(
    run_id: str, request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """改名（契约 §14 PATCH）：只改显示名，不移动 run 目录，runId 不变；显示名
    同步写回该 run 目录 manifest.json 的 caseName（持久化在事实源内，rebuild 不丢）。
    """
    case_name = body.get("caseName")
    if not isinstance(case_name, str) or not case_name.strip():
        raise validation_error("caseName 必填且为非空字符串", field="caseName")
    settings = request.app.state.settings
    index = _index(request)
    if index.get(run_id) is None:
        raise ApiError("report_not_found", f"case {run_id} 不存在")
    run_dir = resolve_run_dir(settings.runs_dir, run_id)
    _write_manifest_case_name(run_dir, case_name)
    index.rename(run_id, case_name)
    updated = index.get(run_id)
    assert updated is not None  # rename 后行必在
    return updated


@router.delete("/cases/{run_id}", status_code=204, response_class=Response)
def delete_case(run_id: str, request: Request) -> Response:
    """删用例（契约 §14 DELETE）：删事实源（run 目录）+ 索引行 + 同名 .xlsx，
    一次删净、无 purge 分叉。已删除再删 → 404 report_not_found。"""
    index = _index(request)
    if not index.delete(run_id):
        raise ApiError("report_not_found", f"case {run_id} 不存在")
    _purge_run_artifacts(request.app.state.settings.runs_dir, run_id)
    return Response(status_code=204)


def _purge_run_artifacts(runs_dir: Path, run_id: str) -> None:
    """删除事实源产物：run 目录 + 同名 .xlsx（契约 §14 DELETE）。

    目录名取 runId 的 `run-` 前缀之后并做路径穿越防护；删除失败仅记日志并继续
    （索引行已删，残留事实源会在 rebuild 时重新登记——调用方应检查日志）。
    """
    name = run_id.removeprefix("run-")
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        return
    run_dir = runs_dir / name
    if run_dir.is_dir():
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            logger.warning("删除 run 目录失败（%s）：%s", run_dir, exc)
    try:
        (runs_dir / f"{name}.xlsx").unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除同名 .xlsx 失败：%s", exc)


def _write_manifest_case_name(run_dir: Path, case_name: str) -> None:
    """显示名写回事实源（manifest.json caseName 字段；ARCH §4.7）。

    manifest 由本后端 save 流写入（session_manager._write_run_dir），形状已知；
    manifest 缺失（旧 run 兼容导入）或写盘失败仅记日志——索引行已更新，rebuild 时
    build_case_row 会以目录名回填 caseName，用例不丢。
    """
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return
        manifest["caseName"] = case_name
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("改名写回 manifest 失败（%s）：%s", manifest_path, exc)
