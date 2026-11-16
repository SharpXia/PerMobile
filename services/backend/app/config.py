"""后端配置（PD-001 ARCH §4.1）：端口/runs 目录/provider 模式/transport。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8737

# provider 模式：real（perfeval adb 采集链）| synthetic（合成流）| simulator
# （M-iOS：xcrun simctl 采集链）| auto（探测后择一，仅 real/synthetic）
VALID_PROVIDERS = ("auto", "real", "synthetic", "simulator")

# 内存有界缓冲：monitoring 态默认保留最近 5 分钟（ARCH §4.4）
BUFFER_WINDOW_MS = 5 * 60 * 1000

# SSE 环形缓冲条数（契约 §8.1）：Last-Event-ID 重放最近 1000 条
SSE_RING_CAPACITY = 1000

# 心跳节奏（契约 §8.1）：15s 注释行 + 60s ping 事件；Settings 可覆盖（测试用）
SSE_COMMENT_HEARTBEAT_S = 15.0
SSE_PING_HEARTBEAT_S = 60.0

# 契约 §7：区间查询 maxPoints 上限（对齐 perfeval SERIES_MAX_POINTS）
SERIES_MAX_POINTS = 600


def _default_runs_dir() -> Path:
    """runs 目录默认落点（2026-11-06 体检 runs-dir-retention 修复）。

    优先级（高→低）：
    1. ``PD_RUNS_DIR`` 环境变量（显式指定恒胜出；与 PD_CORS_ORIGINS 同约定）；
    2. 冻结态（PyInstaller 打包）：``__file__`` 会解析进解包目录——onefile 是
       ``_MEIxxxx`` 退出即清理的临时目录，onedir 是应用包内资源目录——
       ``parents[3]`` 推导落空后 saved 会话会写进随进程退出的路径而**静默丢失**
       （实测现象：2026-11-04 会话 PD_20261104_145547 原始数据丢失，runs/ 中
       同日其余会话均在）→ 改用用户级稳定数据目录（平台惯例，PerfEval 命名
       与 GUI QSettings org 一致）；
    3. 开发态：仓库根 ``runs/``（既有口径不变）。
    """
    env_override = os.environ.get("PD_RUNS_DIR")
    if env_override:
        return Path(env_override).expanduser()
    if getattr(sys, "frozen", False):
        return _frozen_runs_dir()
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "runs"


def _frozen_data_base() -> Path:
    """冻结态用户级稳定数据目录基根（平台惯例；进程退出不清理）。

    runs 目录（_frozen_runs_dir）与 case 索引（_default_cases_db_path 冻结分支）
    共用同一基根：SQLite WAL 写入在 PyInstaller 解包目录会失败或随进程清理丢失
    （桌面壳流收口审读 2026-11-11，蓝图「壳流前置」第②项）。
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "PerfEval"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PerfEval"
    # Linux/其他 POSIX：XDG_DATA_HOME 惯例
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "PerfEval"


def _frozen_runs_dir() -> Path:
    """冻结态 runs 目录：用户级稳定数据目录（平台惯例；进程退出不清理）。"""
    return _frozen_data_base() / "runs"


def _default_cases_db_path() -> Path:
    """case 索引文件落点（契约 §14 / ARCH §4.7）。

    开发态：services/backend/data/cases.db（既有口径不变）。冻结态（PyInstaller
    sidecar，桌面壳打包形态）：`__file__` 解析进解包目录，`data/cases.db` 推导落空
    → 与 runs 目录同根的用户级稳定数据目录（SQLite WAL 可写、进程退出不清理）。
    """
    if getattr(sys, "frozen", False):
        return _frozen_data_base() / "cases.db"
    backend_dir = Path(__file__).resolve().parents[1]
    return backend_dir / "data" / "cases.db"


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    provider: str = "auto"
    transport: str = "auto"
    runs_dir: Path = field(default_factory=_default_runs_dir)
    # M3 case 索引（契约 §14）：SQLite WAL 文件；目录不存在自动创建（ARCH §4.7）
    cases_db_path: Path = field(default_factory=_default_cases_db_path)
    version: str = "1.0.0"
    sse_comment_heartbeat_s: float = SSE_COMMENT_HEARTBEAT_S
    sse_ping_heartbeat_s: float = SSE_PING_HEARTBEAT_S
    # M6 R33：synthetic 实时日志流出行节拍（秒/行）；测试可调快（真实节拍 0.5s）
    synthetic_log_pace_s: float = 0.5

    def __post_init__(self) -> None:
        if self.provider not in VALID_PROVIDERS:
            raise ValueError(f"provider 必须是 {VALID_PROVIDERS} 之一，收到 {self.provider!r}")
        self.runs_dir = Path(self.runs_dir)
        self.cases_db_path = Path(self.cases_db_path)
        # 契约 §1 Base URL；环境变量 FRONTEND_ORIGIN 可覆盖 CORS 来源
        self.cors_origins = [
            origin.strip()
            for origin in os.environ.get(
                "PD_CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if origin.strip()
        ]
