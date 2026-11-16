# backend/ — 后端 sidecar 打包位（预留目录）

electron-builder 的 `extraResources` 把本目录原样拷入应用资源：

```
services/desktop/backend/  →  <应用>/Contents/Resources/backend/   （macOS）
                           →  <安装目录>/resources/backend/        （Windows）
```

主进程生产态按以下顺序解析 sidecar（见 `main.js` 的 `resolveBackendCommand`）：

1. 环境变量 `PERFEVAL_BACKEND_BIN`（优先，指向二进制绝对路径）；
2. `<Resources>/backend/perfeval-backend`（win32: `perfeval-backend.exe`）。

## sidecar 二进制约定

- 须接受 `--host 127.0.0.1 --port 8737` 参数（与 `services/backend/run.py` 同参面）；
- 生产态窗口直接加载 `http://127.0.0.1:8737`，即 sidecar 须自行静态托管 Vue3 前端
  dist——开发态的等价物是 FastAPI `_mount_static` 挂载的 `services/frontend/dist`
  （`services/backend/app/main.py` 的 `_mount_static`）；
- 冻结（PyInstaller 等）环境下如何定位前端 dist 属后端打包实现细节，本壳只约定
  产物落位（本目录）与参数面（`--host/--port`）。

当前本目录仅含本说明文件，作为 extraResources 的占位来源；放入二进制后
`npm run dist:dir` 会自动带上。

## sidecar 二进制从哪来（PyInstaller spec，2026-11-11 补齐）

后端打包形态由 `packaging/pyinstaller/perfeval-backend.spec`（入口
`packaging/pyinstaller/entries/backend_entry.py`，经 `import run` 复用
`services/backend/run.py` 的 argparse 与装配）产出 onedir 命令行二进制：

```bash
cd <仓库根>
.venv/bin/pyinstaller --noconfirm --clean \
  --distpath packaging/pyinstaller/dist-backend \
  --workpath /tmp/pd-backend-work \
  packaging/pyinstaller/perfeval-backend.spec
cp -R packaging/pyinstaller/dist-backend/perfeval-backend/perfeval-backend \
      services/desktop/backend/perfeval-backend        # 二进制 + _internal/
```

spec 事实（2026-11-11 实测，macOS arm64 / PyInstaller 6.22.0）：

- 前端 dist 打进 `frontend-dist/` 数据目录，运行期由
  `app.main._static_dist_dir` 冻结分支（`sys._MEIPASS`）解析——dist 缺失时构建
  即失败（先 `cd services/frontend && npm run build`）；
- runs 目录与 case 索引由 `app.config` 冻结分支接管（用户级稳定数据目录：
  macOS `~/Library/Application Support/PerfEval/`），SQLite WAL 可写、不随进程
  清理；壳另经 spawn env 显式注入 `PD_RUNS_DIR`（同根 runs/，见 main.js）；
- 实测通过：`--host 127.0.0.1 --port 8738` 启动 → `/api/health` ok、`GET /`
  返回打包的前端（200）、`/api/cases/rebuild` 在冻结数据目录落 `cases.db`。
