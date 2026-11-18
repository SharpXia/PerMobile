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
 *      生产态加载后端同源页面（默认 http://127.0.0.1:8737）。
 *   5. 退出时关闭 Windows 打包后端的父进程管道，其他环境发送 SIGTERM；超时强制结束。
 *
 * 渲染进程隔离 Node；preload 只经 contextBridge 暴露 appVersion / platform。
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
const backendOriginFor = (port) => `http://${BACKEND_HOST}:${port}`;
const BACKEND_ORIGIN = backendOriginFor(BACKEND_PORT);
const HEALTH_PATH = '/api/health';
const HEALTH_TIMEOUT_MS = 30_000;
const HEALTH_POLL_INTERVAL_MS = 500;
const BACKEND_SHUTDOWN_GRACE_MS = 5_000;
const WINDOWS_BACKEND_SHUTDOWN_GRACE_MS = 15_000; // 给 Android 录屏 pull 留出时间
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
 * @param {{isPackaged?: boolean, resourcesPath?: string|null, repoRoot?: string, port?: number}} opts
 * @returns {{command: string, args: string[], cwd: string}}
 */
function resolveBackendCommand(opts = {}) {
  const {
    isPackaged = false,
    resourcesPath = null,
    repoRoot = REPO_ROOT,
    port = BACKEND_PORT,
  } = opts;
  const args = ['--host', BACKEND_HOST, '--port', String(port)];
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
 * sidecar 使用独立的数据目录和 CORS 配置。Windows 安装包用自身的 ADB，且只改子进程 PATH。
 * @param {{isPackaged?: boolean, resourcesPath?: string|null, port?: number, platform?: string, environ?: NodeJS.ProcessEnv}} opts
 * @returns {Record<string, string>}
 */
function resolveSidecarEnv(opts = {}) {
  const {
    isPackaged = false,
    resourcesPath = null,
    port = BACKEND_PORT,
    platform = process.platform,
    environ = process.env,
  } = opts;
  const env = {};
  const runsDir = resolveRunsDir(opts);
  if (runsDir) {
    env.PD_RUNS_DIR = runsDir;
  }
  env.PD_CORS_ORIGINS = [...DEV_CORS_ORIGINS, backendOriginFor(port)].join(',');
  if (isPackaged && platform === 'win32') {
    if (!resourcesPath) {
      throw new Error('无法定位 Windows 安装包资源目录。');
    }
    const adbPath = path.resolve(resourcesPath, 'platform-tools', 'adb.exe');
    if (!fs.statSync(adbPath, { throwIfNoEntry: false })?.isFile()) {
      throw new Error(`安装目录缺少 ADB：${adbPath}\n请使用完整的 PerfEval Desktop Windows 安装包重新安装。`);
    }
    const pathKey = Object.keys(environ).find((name) => name.toUpperCase() === 'PATH') || 'Path';
    env[pathKey] = [path.dirname(adbPath), environ[pathKey]].filter(Boolean).join(';');
    env.PERFEVAL_ADB_PATH = adbPath;
    env.PERFEVAL_PARENT_STDIN = '1';
  }
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

function findAvailableBackendPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen({ host: BACKEND_HOST, port: 0 }, () => {
      const { port } = server.address();
      server.close((err) => (err ? reject(err) : resolve(port)));
    });
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
  if (process.platform === 'win32') return Promise.resolve(false);
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
let backendExited = false; // startBackend 后 sidecar 是否已退出（健康轮询提前失败用）
let backendShutdownPromise = null; // 重复 quit 等待同一次收尾
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
  const sidecarEnv = resolveSidecarEnv(opts);
  const useParentPipe = sidecarEnv.PERFEVAL_PARENT_STDIN === '1';
  console.log(
    `[perfeval-desktop] 启动后端 sidecar: ${command} ${args.join(' ')} (cwd=${cwd})`
  );
  const proc = spawn(command, args, {
    cwd,
    stdio: [useParentPipe ? 'pipe' : 'ignore', 'pipe', 'pipe'],
    windowsHide: true,
    env: { ...process.env, PYTHONUNBUFFERED: '1', PERFEVAL_PARENT_STDIN: '0', ...sidecarEnv },
  });
  backendProc = proc;
  backendExited = false;
  backendShutdownPromise = null;
  backendLogTail.length = 0;

  proc.stdin?.on('error', (err) => {
    pushBackendLog(`关闭后端管道失败：${err.message}`);
  });

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
    if (!proc.pid) backendExited = true;
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
 * Windows 安装版以 stdin EOF 触发 ASGI 正常收尾，超时结束自己启动的子进程。
 */
function shutdownBackend() {
  if (!backendProc) return Promise.resolve();
  if (backendShutdownPromise) return backendShutdownPromise;
  const proc = backendProc;
  const graceMs = proc.stdin ? WINDOWS_BACKEND_SHUTDOWN_GRACE_MS : BACKEND_SHUTDOWN_GRACE_MS;
  backendShutdownPromise = new Promise((resolve) => {
    let forceTimer;
    let finishTimer;
    const finish = () => {
      clearTimeout(forceTimer);
      clearTimeout(finishTimer);
      proc.off('exit', finish);
      resolve();
    };
    if (backendExited || !proc.pid) {
      finish();
      return;
    }
    proc.once('exit', finish);
    forceTimer = setTimeout(() => {
      console.warn(`[perfeval-desktop] sidecar ${graceMs}ms 内未退出，尝试强制结束`);
      try {
        proc.kill('SIGKILL');
      } catch (err) {
        console.warn(`[perfeval-desktop] 强制结束 sidecar 失败：${err.message}`);
      }
      finishTimer = setTimeout(finish, 1_000);
    }, graceMs);
    try {
      if (proc.stdin) {
        proc.stdin.end();
      } else {
        proc.kill('SIGTERM');
      }
    } catch (err) {
      console.warn(`[perfeval-desktop] 请求后端退出失败：${err.message}`);
    }
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
      sandbox: true,
      additionalArguments: [`--perfeval-app-version=${app.getVersion()}`],
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

/** 加载页面：生产态为后端同源，开发态使用 Vite dev server。 */
async function loadApp(backendOrigin) {
  const url = app.isPackaged ? backendOrigin : DEV_ORIGIN;
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
    let backendOrigin = BACKEND_ORIGIN;
    try {
      let port = BACKEND_PORT;
      if (await probePortOccupied()) {
        if (process.platform === 'win32' && app.isPackaged) {
          port = await findAvailableBackendPort();
          console.log(`[perfeval-desktop] 端口 ${BACKEND_PORT} 已占用，后端改用 ${port}`);
        } else if (!(await reapStaleBackend())) {
          dialog.showErrorBox(
            `端口 ${BACKEND_PORT} 已被占用`,
            `请关闭占用 ${BACKEND_ORIGIN} 的应用后重试，或重新配置后端和前端代理的端口。`
          );
          app.quit();
          return;
        }
      }
      backendOrigin = backendOriginFor(port);
      startBackend({
        isPackaged: app.isPackaged,
        resourcesPath: app.isPackaged ? process.resourcesPath : null,
        port,
      });
      const health = await waitForBackendHealthy({ origin: backendOrigin });
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
    await loadApp(backendOrigin);
  });

  app.on('window-all-closed', async () => {
    // 三平台一致：窗口全关即收口 sidecar 再退出（壳=应用生命周期）
    await shutdownBackend();
    app.quit();
  });
  app.on('before-quit', (event) => {
    // 等待 sidecar 实际退出，确保强制结束定时器仍有机会运行。
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
