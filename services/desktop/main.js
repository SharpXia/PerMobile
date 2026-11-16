'use strict';

/**
 * PerfEval Desktop — Electron 主进程（CommonJS，无构建步骤）。
 *
 * 职责（ARCH §4.6 桌面形态）：
 *   1. 单实例锁（二次启动只聚焦既有窗口）。
 *   2. 拉起后端 sidecar：
 *        - 开发态：spawn `python services/backend/run.py --port 8737`（仓库根为 cwd）；
 *          python 解析顺序：PERFEVAL_PYTHON 环境变量 → 仓库 .venv（services/frontend
 *          README 的既有约定）→ python3（darwin/linux）/ python（win32）。
 *        - 打包态：PERFEVAL_BACKEND_BIN（优先）→ <resources>/backend/perfeval-backend
 *          （win32: perfeval-backend.exe）。二进制须与 run.py 同参（--host/--port），
 *          并自行静态托管 Vue3 dist（约定见 README「打包约定」）。
 *   3. 轮询 GET /api/health 直到 status === 'ok'（30s 超时弹错误对话框）。
 *   4. BrowserWindow：开发态加载 http://localhost:5173（Vite dev server），
 *      生产态加载 http://127.0.0.1:8737（后端静态托管前端 dist）。
 *   5. window-all-closed：SIGTERM → 5s 超时 SIGKILL 收口后端，再 app.quit()。
 *
 * 安全模型：contextIsolation=true / nodeIntegration=false；preload 只经
 * contextBridge 白名单暴露 appVersion / platform。sandbox=false 是刻意选择——
 * sandboxed preload 的 electron 模块子集没有 app，取不到 app.getVersion()；
 * 渲染层仍与 Node 完全隔离。
 *
 * 可测性：本文件在普通 Node 下 require 时不做任何 Electron 引导（process.type
 * 仅在 Electron 主进程为 'browser'），导出的纯逻辑函数供 /tmp 冒烟脚本复用。
 */

const { app, BrowserWindow, Menu, dialog, shell, session } = require('electron');
const { spawn, execFile } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');

const BACKEND_HOST = '127.0.0.1';
// 端口可经环境变量另指（默认 8737 与 run.py/契约一致）——占用探测给出可行动出口时用
const BACKEND_PORT = Number.parseInt(process.env.PERFEVAL_BACKEND_PORT || '8737', 10) || 8737;
const BACKEND_ORIGIN = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const HEALTH_PATH = '/api/health';
const HEALTH_TIMEOUT_MS = 30_000;
const HEALTH_POLL_INTERVAL_MS = 500;
const BACKEND_SHUTDOWN_GRACE_MS = 5_000;
const DEV_ORIGIN = 'http://localhost:5173';
const REPO_ROOT = path.join(__dirname, '..', '..');
// Vite dev server 既有 CORS 来源（services/backend/app/config.py 缺省）+ 壳同源：
// 打包态窗口加载 BACKEND_ORIGIN 本身同源（CORS 理论不触发，显式声明兜底）。
const DEV_CORS_ORIGINS = ['http://localhost:5173', 'http://127.0.0.1:5173'];
const PORT_PROBE_TIMEOUT_MS = 1_000;

// ---------------------------------------------------------------------------
// 后端 sidecar：解析命令 / 启动 / 健康轮询 / 收口（均不触碰 Electron API）
// ---------------------------------------------------------------------------

/** 开发态 python 解释器：PERFEVAL_PYTHON → 仓库 .venv → python3/python。 */
function resolveDevPython(repoRoot = REPO_ROOT) {
  if (process.env.PERFEVAL_PYTHON) {
    return process.env.PERFEVAL_PYTHON;
  }
  const venvPython =
    process.platform === 'win32'
      ? path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
      : path.join(repoRoot, '.venv', 'bin', 'python');
  if (fs.existsSync(venvPython)) {
    return venvPython;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

/**
 * 解析 sidecar 启动命令。
 * @param {{isPackaged?: boolean, resourcesPath?: string|null, repoRoot?: string}} opts
 * @returns {{command: string, args: string[], cwd: string}}
 */
function resolveBackendCommand(opts = {}) {
  const {
    isPackaged = false,
    resourcesPath = null,
    repoRoot = REPO_ROOT,
  } = opts;
  const args = ['--host', BACKEND_HOST, '--port', String(BACKEND_PORT)];
  if (isPackaged) {
    const bin =
      process.env.PERFEVAL_BACKEND_BIN ||
      path.join(
        resourcesPath || '',
        'backend',
        process.platform === 'win32' ? 'perfeval-backend.exe' : 'perfeval-backend'
      );
    if (!fs.existsSync(bin)) {
      throw new Error(
        `未找到后端 sidecar：${bin}\n` +
          '请将二进制放入应用 resources/backend/（见 services/desktop/README.md「打包约定」），' +
          '或用 PERFEVAL_BACKEND_BIN 指向已有产物。'
      );
    }
    // 打包态恒为 real provider：GUI 直启（Dock/Finder/launchd）只有精简 PATH，
    // auto 探测（detect_mode）找不到 adb 时误落 synthetic，且该判定在启动时
    // 一次性绑定整个会话期——设备后插上也回不来。与「模拟数据已移除」决策
    // 一致：无设备 = 真实空设备列表，绝不演示流兜底。PERFEVAL_PROVIDER 可覆盖。
    args.push('--provider', process.env.PERFEVAL_PROVIDER || 'real');
    return { command: bin, args, cwd: path.dirname(bin) };
  }
  // 开发态：`python services/backend/run.py --port 8737`（相对仓库根）
  return {
    command: resolveDevPython(repoRoot),
    args: ['services/backend/run.py', ...args],
    cwd: repoRoot,
  };
}

/**
 * 打包态 runs 目录（PD_RUNS_DIR 注入，壳流前置第③项）——镜像
 * services/backend/app/config.py 的 `_frozen_runs_dir`（用户级稳定数据目录，
 * 进程退出不清理）。后端侧 PD_RUNS_DIR 恒胜出，这里显式注入即免依赖镜像漂移。
 * 开发态返回 null（不注入，后端走仓库 runs/ 既有口径）。
 * @param {{isPackaged?: boolean}} opts
 * @returns {string|null}
 */
function resolveRunsDir(opts = {}) {
  const { isPackaged = false } = opts;
  if (process.env.PERFEVAL_RUNS_DIR) {
    return process.env.PERFEVAL_RUNS_DIR;
  }
  if (!isPackaged) {
    return null;
  }
  if (process.platform === 'win32') {
    const base = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
    return path.join(base, 'PerfEval', 'runs');
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'PerfEval', 'runs');
  }
  const base = process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  return path.join(base, 'PerfEval', 'runs');
}

/**
 * sidecar 进程注入环境（壳流前置第③⑤项）：PD_RUNS_DIR（打包态）+ PD_CORS_ORIGINS
 * （dev 来源 + 壳同源；后端缺省只含 dev 来源，壳侧扩展为同一清单 + BACKEND_ORIGIN）。
 * @param {{isPackaged?: boolean}} opts
 * @returns {Record<string, string>}
 */
function resolveSidecarEnv(opts = {}) {
  const env = {};
  const runsDir = resolveRunsDir(opts);
  if (runsDir) {
    env.PD_RUNS_DIR = runsDir;
  }
  env.PD_CORS_ORIGINS = [...DEV_CORS_ORIGINS, BACKEND_ORIGIN].join(',');
  return env;
}

/**
 * 端口占用探测（壳流前置第④项）：BACKEND_PORT 已被监听 → true。
 * lane 已踩过的失败模式：孤儿 run.py 占住 8737，壳照常 spawn → 子进程绑定失败退出
 * → 健康轮询只报「sidecar 已退出」掩盖真因。spawn 前显式探测并把修复动作给到用户。
 * @param {{host?: string, port?: number, timeoutMs?: number}} opts
 * @returns {Promise<boolean>}
 */
function probePortOccupied(opts = {}) {
  const { host = BACKEND_HOST, port = BACKEND_PORT, timeoutMs = PORT_PROBE_TIMEOUT_MS } = opts;
  return new Promise((resolve) => {
    const socket = net.connect({ host, port, timeout: timeoutMs });
    const finish = (occupied) => {
      socket.destroy();
      resolve(occupied);
    };
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

/**
 * 残留 sidecar 自动清理（2026-11-11 真机验收修复）：仅当占用者经 /api/health
 * 指纹确认是本应用后端（backend.framework=fastapi）时，按 pid SIGTERM→2s→SIGKILL
 * 收口并返回 true；外部占用者不动，返回 false（调用方走错误对话框）。
 * 背景：quit 时 SSE 长连接挂住 uvicorn 优雅关闭、主进程先退导致孤儿 sidecar 占
 * 端口（before-quit 已改为阻塞收口，此项兜住更早版本遗留的孤儿）。
 * @returns {Promise<boolean>} 是否清理了自家残留
 */
function reapStaleBackend() {
  const healthFingerprint = new Promise((resolve) => {
    const req = http.get(`${BACKEND_ORIGIN}${HEALTH_PATH}`, { timeout: 2000 }, (res) => {
      let data = '';
      res.on('data', (chunk) => {
        data += chunk;
      });
      res.on('end', () => {
        try {
          resolve(JSON.parse(data)?.backend?.framework === 'fastapi');
        } catch {
          resolve(false);
        }
      });
    });
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
    req.on('error', () => resolve(false));
  });
  return healthFingerprint.then((isOurs) => {
    if (!isOurs) return false;
    return new Promise((resolve) => {
      // 全量拉取后按行解析、只取 (LISTEN) 行的 pid：-ti 裸用会把 ESTABLISHED 等
      // 连接的对端进程一并列出（SIGKILL 误伤用户浏览器等无辜进程）；-s TCP:LISTEN
      // 在 macOS 上过滤不可靠（冒烟实录 14 个 pid 含非监听者）。行形如
      // `perfeval-  85806 user  7u IPv4 ... TCP 127.0.0.1:8737 (LISTEN)`。
      execFile('lsof', ['-i', `:${BACKEND_PORT}`, '-P'], { timeout: 5000 }, (err, stdout) => {
        const pids = err
          ? []
          : String(stdout)
              .split('\n')
              .filter((line) => line.endsWith('(LISTEN)'))
              .map((line) => Number(line.trim().split(/\s+/)[1]))
              .filter((pid) => Number.isInteger(pid) && pid > 0);
        if (pids.length === 0) {
          resolve(false);
          return;
        }
        for (const pid of pids) {
          try {
            process.kill(pid, 'SIGTERM');
          } catch {
            /* 已退出即目标达成 */
          }
        }
        setTimeout(() => {
          let escalated = 0;
          for (const pid of pids) {
            try {
              process.kill(pid, 0); // 存活探测
              process.kill(pid, 'SIGKILL');
              escalated += 1;
            } catch {
              /* 已退出 */
            }
          }
          console.log(
            `[perfeval-desktop] 清理残留 sidecar：${pids.length} 个进程（SIGKILL 升级 ${escalated} 个）`
          );
          setTimeout(() => resolve(true), 500); // 等内核释放 LISTEN
        }, 2_000);
      });
    });
  });
}

// sidecar 进程状态（单实例：同一时刻至多一个）
let backendProc = null;
let backendStopping = false;
let backendExited = false; // startBackend 后 sidecar 是否已退出（健康轮询提前失败用）
const backendLogTail = []; // stderr/stdout 尾部环形缓冲，超时对话框里给用户看现场

function pushBackendLog(line) {
  backendLogTail.push(line);
  if (backendLogTail.length > 12) {
    backendLogTail.shift();
  }
}

/**
 * 启动后端 sidecar。opts 透传 resolveBackendCommand。
 * @returns {import('node:child_process').ChildProcess}
 */
function startBackend(opts = {}) {
  const { command, args, cwd } = resolveBackendCommand(opts);
  console.log(
    `[perfeval-desktop] 启动后端 sidecar: ${command} ${args.join(' ')} (cwd=${cwd})`
  );
  const proc = spawn(command, args, {
    cwd,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    env: { ...process.env, PYTHONUNBUFFERED: '1', ...resolveSidecarEnv(opts) },
  });
  backendProc = proc;
  backendStopping = false;
  backendExited = false;

  for (const streamName of ['stdout', 'stderr']) {
    proc[streamName].setEncoding('utf8');
    proc[streamName].on('data', (chunk) => {
      for (const line of chunk.split(/\r?\n/)) {
        if (!line) continue;
        pushBackendLog(`[backend:${streamName === 'stderr' ? 'err' : 'out'}] ${line}`);
        console.log(`[perfeval-desktop] ${line}`);
      }
    });
  }
  proc.on('error', (err) => {
    // spawn 本身失败（ENOENT 等）；健康轮询 30s 兜底，这里只记现场
    pushBackendLog(`spawn 失败: ${err.message}`);
    console.error(`[perfeval-desktop] sidecar spawn 失败: ${err.message}`);
  });
  proc.on('exit', (code, signal) => {
    backendExited = true;
    console.log(
      `[perfeval-desktop] sidecar 退出 code=${code} signal=${signal}`
    );
    pushBackendLog(`sidecar 退出 code=${code} signal=${signal}`);
  });
  return proc;
}

/** 单次 GET /api/health；2xx 且可解析为 JSON 即 resolve，否则 reject。 */
function fetchHealthOnce(origin = BACKEND_ORIGIN, timeoutMs = 2_000) {
  return new Promise((resolve, reject) => {
    const req = http.get(`${origin}${HEALTH_PATH}`, { timeout: timeoutMs }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        if (res.statusCode !== 200) {
          reject(new Error(`HTTP ${res.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch (err) {
          reject(new Error(`响应不是 JSON：${err.message}`));
        }
      });
    });
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
  });
}

/**
 * 轮询 /api/health 直到 status === 'ok'。
 * sidecar 进程先于健康检查退出时立刻 reject（不傻等满 30s）。
 * @param {{timeoutMs?: number, origin?: string}} opts
 */
function waitForBackendHealthy(opts = {}) {
  const { timeoutMs = HEALTH_TIMEOUT_MS, origin = BACKEND_ORIGIN } = opts;
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const retry = (why) => {
      if (Date.now() >= deadline) {
        reject(
          new Error(
            `${why}；已等待 ${timeoutMs}ms 未通过 ${HEALTH_PATH} 健康检查` +
              (backendExited ? '（sidecar 进程已退出）' : '')
          )
        );
        return;
      }
      setTimeout(tick, HEALTH_POLL_INTERVAL_MS);
    };
    const tick = () => {
      if (backendExited) {
        reject(new Error('后端 sidecar 进程在健康检查通过前退出'));
        return;
      }
      fetchHealthOnce(origin)
        .then((body) => {
          if (body && body.status === 'ok') {
            resolve(body);
          } else {
            retry(`健康检查返回 status=${body && body.status}`);
          }
        })
        .catch((err) => retry(`健康检查失败：${err.message}`));
    };
    tick();
  });
}

/**
 * 收口 sidecar：SIGTERM，5s 超时升级 SIGKILL。幂等，返回 Promise（exit 后 resolve）。
 */
let backendShutdownPromise = null; // 唯一收口 promise：重复 quit 等同一次收口，不放行
function shutdownBackend() {
  if (!backendProc) return Promise.resolve();
  if (backendShutdownPromise) return backendShutdownPromise;
  // 收口只发起一次（SIGTERM → 5s 超时 SIGKILL）；此后任何调用者（before-quit /
  // window-all-closed 的多次重入）都 await 同一个 promise——否则第二次 quit 直接
  // 放行主进程退出，SIGKILL 升级定时器随进程死掉，卡在优雅关闭的 sidecar 成孤儿
  // （2026-11-11 用户「Cmd+Q 后重启异常」路径的精确机制）。
  backendStopping = true;
  const proc = backendProc;
  backendShutdownPromise = new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (!done) {
        done = true;
        resolve();
      }
    };
    proc.once('exit', finish);
    try {
      proc.kill('SIGTERM');
      console.log('[perfeval-desktop] 已向 sidecar 发送 SIGTERM');
    } catch (err) {
      console.warn(`[perfeval-desktop] SIGTERM 失败：${err.message}`);
      finish();
      return;
    }
    setTimeout(() => {
      if (done) return;
      console.warn(
        `[perfeval-desktop] sidecar ${BACKEND_SHUTDOWN_GRACE_MS}ms 内未退出，升级 SIGKILL`
      );
      try {
        proc.kill('SIGKILL');
      } catch (err) {
        console.warn(`[perfeval-desktop] SIGKILL 失败：${err.message}`);
      }
      setTimeout(finish, 1_000); // SIGKILL 后兜底，防句柄阻塞退出
    }, BACKEND_SHUTDOWN_GRACE_MS);
  }).finally(() => {
    backendProc = null;
  });
  return backendShutdownPromise;
}

// ---------------------------------------------------------------------------
// 窗口 / 菜单（Electron API 层）
// ---------------------------------------------------------------------------

let mainWindow = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    backgroundColor: '#0f1115',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // 刻意关闭：sandboxed preload 无 app 模块，取不到 app.getVersion()；
      // 渲染层仍被 contextIsolation 与 Node 隔离（见文件头安全模型注释）。
      sandbox: false,
      spellcheck: false,
    },
  });
  mainWindow.once('ready-to-show', () => mainWindow.show());
  // 桌面壳不放出新窗：target=_blank 一律转交系统浏览器
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  return mainWindow;
}

/** 加载页面：生产态=后端同源（8737），开发态=Vite dev server（5173）。 */
async function loadApp() {
  const url = app.isPackaged ? BACKEND_ORIGIN : DEV_ORIGIN;
  try {
    await mainWindow.loadURL(url);
  } catch (err) {
    if (!app.isPackaged) {
      dialog.showErrorBox(
        '无法加载开发页面',
        `加载 ${url} 失败：${err.message}\n\n` +
          '开发态页面由 Vite dev server 提供，请先启动：\n' +
          '  cd services/frontend && npm run dev\n' +
          '（后端 sidecar 已由本壳拉起，无需手工启动。）'
      );
    } else {
      dialog.showErrorBox(
        '无法加载页面',
        `加载 ${url} 失败：${err.message}\n\n请查看后端 sidecar 日志（终端输出）。`
      );
    }
  }
}

/**
 * 应用菜单：roles 优先，仅必要的加速键按壳约定显式覆盖。
 * 开发者工具仅开发态（!app.isPackaged）注册。
 */
function buildMenuTemplate() {
  const isMac = process.platform === 'darwin';
  const viewSubmenu = [
    { role: 'reload', label: '刷新' }, // Cmd/Ctrl+R
    { role: 'forceReload', label: '强制刷新' }, // Cmd/Ctrl+Shift+R
    { type: 'separator' },
    // role 默认加速键是 Cmd/Ctrl+Plus；壳约定 = 键，显式覆盖
    { role: 'zoomIn', label: '放大', accelerator: 'CommandOrControl+=' },
    { role: 'zoomOut', label: '缩小' }, // Cmd/Ctrl+-
    { role: 'resetZoom', label: '实际大小' }, // Cmd/Ctrl+0
    { type: 'separator' },
    // role 默认 mac 是 Ctrl+Cmd+F；壳约定 F11（macOS 若被系统「显示桌面」占用需先放开）
    { role: 'togglefullscreen', label: '全屏', accelerator: 'F11' },
  ];
  if (!app.isPackaged) {
    viewSubmenu.push(
      { type: 'separator' },
      { role: 'toggleDevTools', label: '开发者工具', accelerator: 'CommandOrControl+Shift+I' }
    );
  }

  const template = [];
  if (isMac) {
    template.push({
      label: app.name,
      submenu: [
        { role: 'about', label: `关于 ${app.name}` },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide', label: `隐藏 ${app.name}` },
        { role: 'hideOthers', label: '隐藏其他' },
        { role: 'unhide', label: '全部显示' },
        { type: 'separator' },
        { role: 'quit', label: `退出 ${app.name}` }, // Cmd+Q
      ],
    });
    template.push({ role: 'editMenu', label: '编辑' });
    template.push({ label: '视图', submenu: viewSubmenu });
    template.push({ role: 'windowMenu', label: '窗口' }); // 含 Cmd+W 关窗
  } else {
    template.push({
      label: '文件',
      submenu: [
        { role: 'close', label: '关闭窗口' }, // Ctrl+W
        { type: 'separator' },
        { role: 'quit', label: '退出', accelerator: 'CommandOrControl+Q' },
      ],
    });
    template.push({ role: 'editMenu', label: '编辑' });
    template.push({ label: '视图', submenu: viewSubmenu });
  }
  return template;
}

// ---------------------------------------------------------------------------
// 引导（仅 Electron 主进程执行；普通 Node require 只取导出函数）
// ---------------------------------------------------------------------------

function bootstrap() {
  const gotLock = app.requestSingleInstanceLock();
  if (!gotLock) {
    app.quit();
    return;
  }
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(Menu.buildFromTemplate(buildMenuTemplate()));
    // 清存量 HTTP 磁盘缓存（2026-11-11 真机验收修复）：SPA index.html 曾无
    // Cache-Control 头，Chromium 启发式缓存把旧 bundle 的 index.html 缓存在
    // 本目录 Cache/ 下——升级 app 后窗口仍命中旧 mock 页面（服务端早已是新
    // bundle，IAB 无缓存上下文验证 live 与用户窗口 mock 并存即此因）。服务端
    // 已加 no-cache（main.py _index_response），此处清掉存量旧缓存条目。
    try {
      await session.defaultSession.clearCache();
      console.log('[perfeval-desktop] 已清空 HTTP 磁盘缓存');
    } catch (err) {
      console.warn(`[perfeval-desktop] 清缓存失败（不阻断）：${err.message}`);
    }
    // 端口占用探测（壳流前置第④项）：spawn 前给出可行动的错误，而不是让子进程
    // 绑定失败后在健康轮询里以「sidecar 已退出」掩盖真因（lane 实际踩过：孤儿
    // run.py 占住 8737）。单实例锁只拦同应用二次启动，拦不住外部占用者。
    // 2026-11-11：自家残留（quit 时 SSE 挂住 uvicorn 产生的孤儿 sidecar）经
    // health 指纹确认后自动清理并继续启动，用户不再需要手动敲命令。
    if (await probePortOccupied()) {
      const reaped = await reapStaleBackend();
      if (!reaped) {
        const hint =
          process.platform === 'win32'
            ? `netstat -ano | findstr :${BACKEND_PORT} 后 taskkill /PID <pid> /F`
            : `lsof -ti :${BACKEND_PORT} | xargs kill`;
        dialog.showErrorBox(
          `端口 ${BACKEND_PORT} 已被占用`,
          `后端 sidecar 需要 ${BACKEND_HOST}:${BACKEND_PORT}，但该端口当前已有进程监听\n` +
            `（该占用进程不是本应用后端，无法自动清理）。\n\n` +
            `请退出占用进程后重试，例如：\n  ${hint}\n\n` +
            `或为后端另指端口：PERFEVAL_BACKEND_PORT 约定见 services/desktop/README.md。`
        );
        app.quit();
        return;
      }
    }
    try {
      startBackend({
        isPackaged: app.isPackaged,
        resourcesPath: app.isPackaged ? process.resourcesPath : null,
      });
      const health = await waitForBackendHealthy();
      console.log(
        `[perfeval-desktop] 后端健康检查通过（v${health.version ?? '?'}），加载窗口`
      );
    } catch (err) {
      dialog.showErrorBox(
        '后端启动失败',
        `${err.message}\n\n近端日志：\n${backendLogTail.join('\n') || '（无）'}`
      );
      app.quit();
      return;
    }
    createWindow();
    await loadApp();
  });

  app.on('window-all-closed', async () => {
    // 三平台一致：窗口全关即收口 sidecar 再退出（壳=应用生命周期）
    await shutdownBackend();
    app.quit();
  });
  app.on('before-quit', (event) => {
    // Cmd+Q / app.quit() 路径兜底；shutdownBackend 返回唯一收口 promise，重复
    // 调用等待同一次收口（见 shutdownBackend 注释）。
    // 2026-11-11 真机验收修复：原 `void shutdownBackend()` fire-and-forget——SSE
    // 长连接挂住 uvicorn 优雅关闭时，5s SIGKILL 升级定时器随主进程退出而失效，
    // sidecar 成孤儿占住 8737（复现实录）。改为阻塞：quit 被 preventDefault，
    // 收口（含 SIGKILL 升级）完成后再显式 quit；收口进行中的第二次 quit 同样
    // 拦截等待，不得放行（guard 只看 backendProc 是否仍在收尾）。
    if (backendProc) {
      event.preventDefault();
      shutdownBackend().finally(() => app.quit());
    }
  });
}

if (process.type === 'browser') {
  bootstrap();
}

// 纯逻辑导出（普通 Node 冒烟测试用；Electron 下 require.main 即本模块，不受影响）
module.exports = {
  CONSTANTS: {
    BACKEND_HOST,
    BACKEND_PORT,
    BACKEND_ORIGIN,
    HEALTH_PATH,
    HEALTH_TIMEOUT_MS,
    BACKEND_SHUTDOWN_GRACE_MS,
    DEV_ORIGIN,
    REPO_ROOT,
  },
  resolveDevPython,
  resolveBackendCommand,
  resolveRunsDir,
  resolveSidecarEnv,
  probePortOccupied,
  reapStaleBackend,
  startBackend,
  fetchHealthOnce,
  waitForBackendHealthy,
  shutdownBackend,
  buildMenuTemplate,
};
