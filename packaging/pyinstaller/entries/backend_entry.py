"""perfeval-backend sidecar 入口（PyInstaller 打包，桌面壳流 2026-11-11）。

与 services/backend/run.py 同参面（--host/--port/--runs-dir/--provider/--transport），
经 ``import run`` 复用其 argparse 与装配（单一事实源，不复制实现）。frozen 态落点
由 app.config 冻结分支接管（runs_dir = 用户级稳定数据目录/runs，cases_db = 同根
cases.db），前端 dist 由 spec 以 ``frontend-dist/`` 数据目录打包、main.py
``_static_dist_dir`` 冻结分支解析——Electron 壳的产物落位约定见
services/desktop/backend/README.md。
"""

from run import main


if __name__ == "__main__":
    raise SystemExit(main())
