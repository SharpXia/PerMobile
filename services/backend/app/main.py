"""FastAPI 应用装配（ARCH §4）：路由挂载、CORS、统一错误体、静态托管。

业务逻辑在 app/services 与 app/providers（不 import FastAPI），routers 是薄适配层，
为 ARCH §6.1 stdlib 降级留接缝。
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .errors import ApiError
from .providers.base import DeviceProvider
from .providers.composite import CompositeProvider, provider_device_mode
from .providers.real import RealProvider, detect_mode
from .providers.simulator import SimulatorProvider
from .providers.synthetic import SyntheticProvider
from .routers import (
    analysis,
    cases,
    devices,
    reports,
    run_artifacts,
    sessions,
    speedtest,
)
from .services.case_index import CaseIndex
from .services.session_manager import SessionManager

logger = logging.getLogger("pd-backend")


def _resolve_provider(settings: Settings) -> DeviceProvider:
    if settings.provider == "synthetic":
        return SyntheticProvider()
    if settings.provider == "real":
        # 装配流 S4（契约 §26 装配语义）：--provider real → 三设备源聚合面
        # （adb Android + devicectl iOS 真机 + simctl iOS 模拟器），单源缺失
        # 优雅降级。打包态 services/desktop/main.js 恒 --provider real——零改动
        # 即见 iOS 设备。simulator/synthetic 单源与 auto 探测择一保持不动
        # （ADR-0002 记录的 GUI 精简 PATH 下 auto 误判锁死回归，不扩散）。
        return CompositeProvider()
    if settings.provider == "simulator":
        # M-iOS（契约 §23）：iOS Simulator 采集链（xcrun simctl）
        return SimulatorProvider()
    mode = detect_mode()
    logger.info("provider auto -> %s", mode)
    return RealProvider() if mode == "real" else SyntheticProvider()


def create_app(
    settings: Settings | None = None,
    provider: DeviceProvider | None = None,
    screen_recorder_factory: Any = None,
) -> FastAPI:
    settings = settings or Settings()
    resolved_provider = provider or _resolve_provider(settings)
    # M6 R22 设备级命令队列（每设备串行窗口；[P§5.4] 按设备 actor 隔离）
    from .services.device_queue import DeviceCommandQueue

    device_queue = DeviceCommandQueue()
    # M3 case 索引（契约 §14 / ARCH §4.7）：SQLite WAL，目录不存在自动创建；
    # save 成功自动登记经 SessionManager 注入（登记失败不阻断 save，rebuild 兜底）
    case_index = CaseIndex(settings.cases_db_path)
    session_manager = SessionManager(
        resolved_provider,
        settings,
        case_index=case_index,
        # M4 录屏器工厂（契约 §16.1）：测试注入 fixture 工厂驱动真实录屏内核
        screen_recorder_factory=screen_recorder_factory,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        yield
        # 进程退出兜底：停采集线程/子进程，避免后端关停后遗留 adb logcat、
        # screenrecord 等孤儿采集（页面刷新不属于本路径，会话可被重新接管）
        session_manager.close_all()

    app = FastAPI(title="perfdog-parity backend", version=settings.version, lifespan=lifespan)
    app.state.settings = settings
    app.state.started_at = time.monotonic()
    app.state.provider = resolved_provider
    app.state.device_queue = device_queue
    app.state.case_index = case_index
    app.state.sessions = session_manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # ---- 统一错误体（契约 §3；FastAPI 默认 422 必须被覆写为本格式）----

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Any, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Any, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        field = errors[0]["loc"][-1] if errors else None
        message = errors[0].get("msg", "请求校验失败") if errors else "请求校验失败"
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "validation_error",
                    "message": str(message),
                    "details": {"field": str(field) if field is not None else None},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Any, exc: StarletteHTTPException) -> JSONResponse:
        """Keep unmatched API paths in the contract error envelope.

        A path such as ``/api/runs/run-a/b`` never reaches the ``{run_id}``
        handler because Starlette splits it into two segments.  Without this
        handler FastAPI returns its default ``{"detail": "Not Found"}`` body,
        bypassing the API error contract and making malformed run ids behave
        differently depending on whether the SPA fallback is mounted.
        """
        if exc.status_code == 404 and request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "report_not_found",
                        "message": f"路径不存在: {request.url.path}",
                        "details": {},
                    }
                },
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Any, exc: Exception) -> JSONResponse:
        logger.exception("internal error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": str(exc),
                    "details": {},
                }
            },
        )

    # ---- 路由 ----

    app.include_router(devices.router)
    app.include_router(reports.router)
    app.include_router(sessions.router)
    app.include_router(cases.router)
    app.include_router(run_artifacts.router)
    # M7（契约 §22）：深度分析 /deep-analysis + 根因诊断 /diagnostics
    app.include_router(analysis.router)
    # M8+ 最小切片（契约 §24）：网络测速 /speedtest/payload + /devices/{id}/speedtest
    app.include_router(speedtest.router)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        # H2：framework 如实报告当前 transport，不伪装
        return {
            "status": "ok",
            "version": settings.version,
            "backend": {"framework": "fastapi", "python": _python_version()},
            "providers": {
                "devices": app.state.provider.mode,
                "metrics": app.state.provider.mode,
            },
            "uptimeSec": int(time.monotonic() - app.state.started_at),
        }

    @app.get("/api/metrics")
    def metrics_registry(deviceId: str | None = None) -> dict[str, Any]:
        """契约 §5 注册表 + §20.1 M6 扩展：可选 ``deviceId`` 提供设备 profile 时
        available = 模式可用性 ∩ 设备能力交集判定（capability_matrix，契约 §20.3），
        不可用且原因可判 → capabilities.reason 机器可读码；未提供 deviceId 时
        available 仅按模式判定（无 reason，无设备上下文不猜）。

        装配流 S4（§20.3 判定 mode 来源改造）：判定 mode 按设备源通道驱动
        （provider_device_mode：ios+simulator 设备 → simulator 通道、其余 → real；
        单源 provider 回退 provider.mode 逐字段不变）——聚合面（provider.mode 恒
        "real"）下模拟器设备的双指标经 simulator 通道可勾，Android 设备仍走 real。"""
        from .errors import ApiError

        from .registry import registry_body

        provider = app.state.provider
        profile = None
        mode = provider.mode
        if deviceId:
            if not provider.device_exists(deviceId):
                raise ApiError("device_not_found", f"device {deviceId} 不存在")
            profile = provider.get_profile(deviceId)
            mode = provider_device_mode(provider, deviceId)
        return registry_body(mode, profile)

    # §9 runs 列表/详情改由 reports router 承载（M8+ 实装，M2 占位移除）

    _mount_static(app, settings)
    return app


def _python_version() -> str:
    import platform

    return platform.python_version()


def _static_dist_dir() -> Path | None:
    """前端 dist 落点（ARCH §4.6 单一事实源）。

    开发态：services/frontend/dist。冻结态（PyInstaller backend sidecar，桌面壳
    打包形态）：`__file__` 解析进解包目录，`parents[2]/frontend/dist` 推导落空 →
    spec 以 `frontend-dist/` 数据目录打包（packaging/pyinstaller/perfeval-backend.spec），
    运行期经 ``sys._MEIPASS`` 解析（onefile/onedir 均设置）。
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "frontend-dist"
        return None
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _mount_static(app: FastAPI, settings: Settings) -> None:
    """npm 构建产物静态托管（ARCH §4.6）：dist 存在才挂载，SPA fallback。"""
    dist = _static_dist_dir()
    if dist is None or not (dist / "index.html").exists():
        return

    def _index_response() -> FileResponse:
        # SPA 入口必须 no-cache（每次 304 协商）：无 Cache-Control 时 Chromium/Electron
        # 按启发式新鲜期直接用缓存旧 index.html → 窗口加载旧 mock bundle（2026-11-11
        # 桌面壳实测：升级 app 后窗口仍 mock，服务端早已是新 bundle）。assets 带
        # 内容 hash 文件名，可安全长缓存，不加头。
        response = FileResponse(dist / "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return _index_response()

    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> Any:
        if full_path.startswith("api/") or full_path == "api":
            raise ApiError("report_not_found", f"路径不存在: /{full_path}")
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return _index_response()
