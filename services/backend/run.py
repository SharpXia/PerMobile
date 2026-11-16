#!/usr/bin/env python3
"""后端唯一入口（H2）：python run.py [--port 8737] [--host 127.0.0.1]。

ARCH §4.1：--transport auto 先尝试 import fastapi，失败自动降级 stdlib 并打印日志。
M1 竖切片仅落地 fastapi transport（依赖已按 §8 安装成功，无需降级）；stdlib 降级
形态未实现——若 fastapi 导入失败将如实报错退出，而不是伪装可用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="perfdog-parity 后端（M1 竖切片）")
    parser.add_argument("--port", type=int, default=8737, help="默认 8737（H2）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--transport",
        choices=("auto", "fastapi", "stdlib"),
        default="auto",
        help="auto=优先 fastapi；stdlib 降级形态 M1 未实现",
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "real", "synthetic", "simulator"),
        default="auto",
        help=(
            "real=聚合设备面（adb Android + devicectl iOS 真机 + simctl iOS 模拟器，"
            "装配流 S4 契约 §26；单源缺失优雅降级）；synthetic=合成流（H5）；"
            "simulator=iOS Simulator 单源采集链（xcrun simctl，M-iOS 契约 §23）；"
            "auto=探测择一（仅 real/synthetic）"
        ),
    )
    parser.add_argument(
        "--runs-dir",
        default=None,
        help="saved 会话落盘目录（默认 <repo>/runs/）",
    )
    return parser


def build_settings(args: argparse.Namespace) -> "Settings":
    """args → Settings（可测；runs 目录单一事实源在 app.config._default_runs_dir）。

    2026-11-06 体检 runs-dir-retention：本文件不再自行推导 REPO_ROOT/runs——
    ``--runs-dir`` 缺省时走 Settings 默认工厂（开发态=仓库 runs/；冻结态=用户级
    稳定数据目录；PD_RUNS_DIR 恒胜出），避免冻结环境下重复落进临时解包目录。
    """
    from app.config import Settings

    kwargs: dict[str, object] = {
        "host": args.host,
        "port": args.port,
        "provider": args.provider,
        "transport": args.transport,
    }
    if args.runs_dir:
        kwargs["runs_dir"] = Path(args.runs_dir)
    return Settings(**kwargs)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.transport == "stdlib":
        print(
            "[pd-backend] stdlib 降级 transport 在 M1 未实现（fastapi 已安装，无需降级）；"
            "如需降级请按 ARCHITECTURE.md §6.1 实现后重试。",
            file=sys.stderr,
        )
        return 2

    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError as exc:
        print(
            f"[pd-backend] fastapi/uvicorn 导入失败（{exc}），auto 降级目标 stdlib 未实现，"
            "按 ARCH §6.1 属设计内失败；请先安装 services/backend/requirements.txt。",
            file=sys.stderr,
        )
        return 2

    from app.main import create_app

    settings = build_settings(args)
    app = create_app(settings)
    mode = app.state.provider.mode
    print(
        f"[pd-backend] listening http://{args.host}:{args.port}  provider={mode}"
        f"  runs_dir={settings.runs_dir}"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
