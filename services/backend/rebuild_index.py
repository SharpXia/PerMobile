#!/usr/bin/env python3
"""case 索引重建入口（契约 §14 POST /api/cases/rebuild 的命令行等价物）。

用法（仓库根目录）：

    python -m services.backend.rebuild_index [--runs-dir <dir>] [--db <cases.db>]

缺省：runs 目录 `<repo>/runs/`、索引文件 `services/backend/data/cases.db`
（契约 §14 / ARCH §4.7）。扫描 runs 根目录全部 run 目录按事实源（manifest.json /
samples.ndjson / timeline.ndjson）重建索引，幂等；事实源已消失的旧行物理清除；
索引文件缺失或损坏时以新建空库恢复。与 POST /api/cases/rebuild 共用同一实现
（app/services/case_index.rebuild_case_index）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="case 索引重建（契约 §14）")
    parser.add_argument(
        "--runs-dir",
        default=None,
        help="runs 根目录（缺省走 app.config._default_runs_dir 单一事实源：开发态仓库 runs/；冻结态用户级数据目录；PD_RUNS_DIR 恒胜出）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="索引文件路径（默认 services/backend/data/cases.db）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from app.config import _default_runs_dir
    from app.services.case_index import CaseIndex, rebuild_case_index

    # 2026-11-06 复核：runs 目录缺省不再自推导 REPO_ROOT/runs（与 run.py 同步收敛
    # 到 config 单一事实源），避免冻结态/自定义 PD_RUNS_DIR 环境下 rebuild 扫错
    # 目录重建出空索引。
    runs_dir = Path(args.runs_dir) if args.runs_dir else _default_runs_dir()
    db_path = Path(args.db) if args.db else (BACKEND_DIR / "data" / "cases.db")
    index = CaseIndex(db_path)
    total = rebuild_case_index(runs_dir, index)
    print(json.dumps({"db": str(db_path.resolve()), "runsDir": str(runs_dir), "total": total},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
