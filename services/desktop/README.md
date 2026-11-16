# PerfEval Desktop（Electron 桌面壳）

把 Web 交付面（Vue3 前端 + FastAPI 后端 sidecar）装进 Electron 桌面应用，并提供
常用桌面快捷键。CommonJS 单文件主进程，无构建步骤。PySide6 桌面版（`perfeval/gui/`）
保持不动，两者并行存在。

依据：`services/backend/run.py`（后端唯一入口，`--port 8737`）、
（静态托管）、
`services/backend/app/main.py` 的 `health()` / `_mount_static`。

## 架构

```
┌─ Electron 主进程（services/desktop/main.js）──────────────────────────┐
│  BrowserWindow                                                        │
│   ├─ 开发态  加载 http://localhost:5173     （Vite dev server）        │
│   └─ 生产态  加载 http://127.0.0.1:8737     （后端同源静态托管 dist）  │
│  sidecar 生命周期：spawn → 轮询 GET /api/health → 退出时收口           │
│   （单实例锁；30s 健康超时弹错误对话框；SIGTERM→5s→SIGKILL）           │
└──────────────┬────────────────────────────────────────────────────────┘
               │ HTTP /api/**（开发态经 Vite proxy 5173→8737）
┌──────────────▼────────────────────────────────────────────────────────┐
│ FastAPI sidecar（services/backend/run.py --host 127.0.0.1 --port 8737）│
│  ├─ /api/**        契约面（API_CONTRACT.md）                           │
│  ├─ /api/health    健康检查（status:"ok" 才放行加载窗口）              │
│  └─ / 与 /assets   Vue3 dist 静态托管 + SPA fallback（_mount_static）  │
└──────────────┬────────────────────────────────────────────────────────┘
               │ provider（real=adb 采集链 / synthetic=合成流）
┌──────────────▼────────────────────────────────────────────────────────┐
│ Vue3 前端（services/frontend）— 实时监控看板                           │
│  preload 仅暴露 window.perfevalDesktop = { appVersion, platform }     │
└───────────────────────────────────────────────────────────────────────┘
```

进程模型要点：

- **单实例锁**：`app.requestSingleInstanceLock()`，二次启动只聚焦既有窗口。
- **sidecar 归属壳**：随壳启动（dev/prod 都拉起），窗口全关即收口（三平台一致：
  SIGTERM → 5s 超时 SIGKILL → `app.quit()`；Cmd/Ctrl+Q 走 `before-quit` 兜底）。
- **安全模型**：`contextIsolation: true` / `nodeIntegration: false`；preload 只经
  contextBridge 暴露 `appVersion` / `platform` 两个冻结值。`sandbox: false` 是刻意
  选择（sandboxed preload 无 `app` 模块，取不到 `app.getVersion()`），渲染层仍与
  Node 完全隔离。
- **外链**：`window.open`/`target=_blank` 一律拒绝新窗并转交系统浏览器。

## 快捷键（Menu accelerators，roles 优先）

| 快捷键 | 行为 | 实现 |
| --- | --- | --- |
| `Cmd/Ctrl+R` | 刷新 | role `reload`（默认加速键） |
| `Cmd/Ctrl+Shift+R` | 强制刷新（清缓存） | role `forceReload`（默认加速键） |
| `Cmd/Ctrl+Q` | 退出应用（收口 sidecar） | role `quit`；win/linux 显式挂 `CommandOrControl+Q` |
| `Cmd/Ctrl+W` | 关闭窗口 | role `close`（默认加速键） |
| `Cmd/Ctrl+=` | 放大 | role `zoomIn` + 显式加速键 `CommandOrControl+=`（role 默认是 Plus 键） |
| `Cmd/Ctrl+-` | 缩小 | role `zoomOut`（默认加速键） |
| `Cmd/Ctrl+0` | 恢复实际大小 | role `resetZoom`（默认加速键） |
| `F11` | 全屏切换 | role `togglefullscreen` + 显式加速键 `F11` |
| `Cmd/Ctrl+Shift+I` | 开发者工具 | role `toggleDevTools`——**仅开发态**（`!app.isPackaged`）注册 |

已知边界：macOS 上系统级 `F11`（调度中心「显示桌面」）可能拦截该键，需在
系统设置里放开后壳内全屏加速键才生效；Windows/Linux 无此问题。

## 运行

```bash
cd services/desktop
npm install

# 开发态（两个终端）：
#   终端 A：cd services/frontend && npm run dev        # Vite dev server（5173）
#   终端 B：npm start                                  # 壳自动拉起后端 sidecar 并轮询健康
```

环境变量：

| 变量 | 作用 | 缺省 |
| --- | --- | --- |
| `PERFEVAL_PYTHON` | 开发态 sidecar 解释器 | 仓库 `.venv/bin/python`（win: `.venv/Scripts/python.exe`）→ `python3`/`python` |
| `PERFEVAL_BACKEND_BIN` | 打包态 sidecar 二进制绝对路径（优先于 resources） | `<Resources>/backend/perfeval-backend`（win: `.exe`） |
| `PERFEVAL_BACKEND_PORT` | sidecar 端口（占用时另指） | `8737`（与 run.py/契约一致） |
| `PERFEVAL_RUNS_DIR` | 打包态 runs 目录显式覆盖（注入 spawn env `PD_RUNS_DIR`） | 用户级稳定数据目录（镜像 `app.config._frozen_runs_dir`） |
| `PERFEVAL_PROVIDER` | 打包态 sidecar `--provider` 覆盖 | `real`（恒真机；GUI 精简 PATH 下 auto 会误落 synthetic 且启动锁死，见 ADR-0002 补充决策） |

壳流前置项（2026-11-11 收口审读补齐，蓝图六项中此前仅落地一项）：

- **sidecar spawn env 显式注入** `PD_RUNS_DIR`（打包态，用户级稳定数据目录/runs，
  后端侧该环境变量恒胜出——冻结落点不依赖后端镜像分支漂移）与 `PD_CORS_ORIGINS`
  （dev 来源 + 壳同源，同源托管下非阻断、显式声明兜底）。
- **端口占用探测**：spawn 前探测 `BACKEND_PORT`，已占用 → 错误对话框给出修复命令
  （`lsof -ti :8737 | xargs kill`；或 `PERFEVAL_BACKEND_PORT` 另指端口），不再让
  子进程绑定失败后在健康轮询里以「sidecar 已退出」掩盖真因（孤儿 run.py 占住
  8737 的失败模式在壳流 lane 实际发生过）。

## 打包

```bash
npm run dist:dir   # 免签名目录产物：dist/mac-arm64/PerfEval Desktop.app（win: dist/win-unpacked）
npm run dist       # mac: dir + dmg；win: nsis + dir
./install-macos.sh # 装入 /Applications（含 LaunchServices 重注册，见下）
```

`electron-builder.yml` 关键约定：`appId: local.perfeval.desktop`；mac
`identity: null`（不签名/公证，本地分发）；`extraResources` 把
`services/desktop/backend/` 原样拷到 `<Resources>/backend/`。

### 覆盖安装必须重注册 LaunchServices（install-macos.sh）

反复 `rm -rf` + `cp -R` 覆盖 /Applications 里的 bundle 后 **inode 变化**，
LaunchServices 数据库仍指向旧记录 → `open -a` / Dock 点开**静默失败**（进程不
起、无报错；2026-11-11 实录，用户感知为「重启 app 异常」）。安装统一走
`./install-macos.sh`（自动退出运行中实例 → 替换 → `lsregister -f` 重注册）；
手工替换后至少执行一次：

```bash
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "/Applications/PerfEval Desktop.app"
```

### 后端 sidecar 如何放入 resources/backend

1. 用后端打包方案产出自包含二进制（须内嵌/定位 Vue3 dist——生产态窗口直接加载
   `http://127.0.0.1:8737`，静态托管在 sidecar 侧，开发态对应
   `services/backend/app/main.py` 的 `_mount_static`），并确保它与
   `services/backend/run.py` 同参（接受 `--host/--port`）。
2. 二进制命名为 `perfeval-backend`（win: `perfeval-backend.exe`），放入
   `services/desktop/backend/`（该目录是 `extraResources` 的来源，占位说明见
   `backend/README.md`），随后 `npm run dist:dir` 会自动带入
   `<Resources>/backend/`。
3. 或不落盘：`PERFEVAL_BACKEND_BIN=/path/to/perfeval-backend npm start` 直接指向
   已构建产物，优先级高于 resources 目录。

## 自验证

```bash
node --check main.js && node --check preload.js
npx electron --version
CSC_IDENTITY_AUTO_DISCOVERY=false npx electron-builder --dir
```

sidecar 逻辑（spawn/健康轮询/SIGTERM 收口）在普通 Node 下经
`main.js` 导出的纯逻辑函数冒烟验证（真实拉起 `services/backend/run.py`），
不占用 GUI 会话。
