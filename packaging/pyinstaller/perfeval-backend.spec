# -*- mode: python ; coding: utf-8 -*-
"""perfeval-backend sidecar spec（桌面壳流蓝图「壳流前置」第①项，2026-11-11）。

产物 = onedir 命令行二进制（无 BUNDLE/.app——sidecar 不是应用包），落位到
services/desktop/backend/ 后由 electron-builder extraResources 带进
<Resources>/backend/（Electron 壳 spawn 约定 --host/--port，见
services/desktop/backend/README.md 与 services/desktop/README.md「打包约定」）。

与既有 GUI+CLI spec（perfeval.spec）的关系：独立 Analysis/COLLECT（壳 sidecar 不含
PySide6；GUI/CLI 双 exe 单 collection 的形态不适用），共用 entries/ 目录风格。

与 run.py 的关系：入口 backend_entry.py 经 `import run` 复用 argparse 与装配；
frozen 落点（runs/cases_db/frontend dist）由 app.config / app.main 冻结分支接管。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parents[1]
BACKEND_DIR = ROOT / "services" / "backend"
FRONTEND_DIST = ROOT / "services" / "frontend" / "dist"

# 前端 dist（生产态窗口加载 http://127.0.0.1:8737，静态托管在 sidecar 侧）：
# 打进 frontend-dist/ 数据目录，运行期经 app.main._static_dist_dir 冻结分支解析。
if not (FRONTEND_DIST / "index.html").is_file():
    raise SystemExit(
        f"前端 dist 缺失：{FRONTEND_DIST}/index.html 不存在——"
        "请先构建（cd services/frontend && npm run build）再打包 sidecar"
    )

datas = [
    (str(FRONTEND_DIST), "frontend-dist"),
]
# perfeval 运行期数据面：echarts vendor（report.html 离线图表）+ golden fixtures
# （离线导入 parity 路径，与 GUI spec 同一 includes 口径）
datas += collect_data_files(
    "perfeval",
    includes=[
        "core/report/vendor/echarts.min.js",
        "fixtures/android/golden/*.txt",
        "fixtures/synthetic/golden/*.json",
    ],
)

backend_analysis = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "entries" / "backend_entry.py")],
    pathex=[str(ROOT), str(BACKEND_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=["uvicorn"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 壳 sidecar 无 GUI 依赖；pytest/openpyxl 之外的测试面排除
    excludes=[
        "PySide6",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)

backend_pyz = PYZ(backend_analysis.pure)

backend_exe = EXE(
    backend_pyz,
    backend_analysis.scripts,
    backend_analysis.dependencies,
    [],
    name="perfeval-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 命令行 sidecar：保留控制台输出（健康/装配日志走 stdout，壳读 stderr/stdout）
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    exclude_binaries=True,
    contents_directory="_internal",
)

collection = COLLECT(
    backend_exe,
    backend_analysis.binaries,
    backend_analysis.datas,
    strip=False,
    upx=False,
    name="perfeval-backend",
)
