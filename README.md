# PerfEval Lite / PerMobile

轻量级移动应用性能评估工具，含两个交付面：

1. **桌面端（PerfEval Lite）**：Host CLI（`perfeval`）+ PySide6 GUI，驱动 Android
   设备采集、解析性能指标，生成可对比的回归报告和可视化 HTML。
2. **Web 版（PerfDog 对标，perfdog-parity PD-001）**：`services/backend`（FastAPI）+
   `services/frontend`（Vue3），对标 PerfDog 的「设备/应用选择 → 实时监控 →
   开始/停止记录 → 标记 → 回放/区间统计 → 历史对比 → 诊断联动」闭环；同一套
   `perfeval` 采集链做内核，API 契约先行
（API 契约先行，契约文档保留于开发仓库，不随发布分发）。

版本与变更记录见 [CHANGELOG.md](CHANGELOG.md)（当前 `0.2.0`）；许可证
[PolyForm Noncommercial 1.0.0](LICENSE)（仅限非商业用途，Copyright (c) 2026 xxd /
PerMobile contributors）。

GUI 快速启动的每次实验都会独立保存在当前 workspace 的
`.perfeval/experiments/YYYY/MM/<experiment-id>/`。目录内的 `experiment.json`
记录任务状态与报告索引，`data/` 保存 summary、HTML 报告和原始 artifact，
`logs/` 保存本次任务日志。新实验与“再次运行”都使用新目录，不覆盖历史数据；
历史结果页同时兼容扫描旧的 `runs/` 和 `reports/`。

## 已实现功能

### 桌面端（Host CLI + GUI）

| 模块 | 功能 | 状态 |
| --- | --- | --- |
| CLI 框架 | `perfeval` 命令行入口，9 个子命令（init/devices/run/ab/diagnose/compare/report/open/validate） | done |
| 场景配置 | YAML 子集解析器，支持 app/device/flow/collectors/budgets | done |
| 合成数据 | 6 个内置合成测试用例，覆盖 pass/warn/fail/unknown 场景 | done |
| 摘要引擎 | 从 timeline 事件聚合并计算 min/max/mean/median/p75/p90/p95/MAD/置信度 | done |
| 对比引擎 | median 对比、bootstrap CI、绝对/回归阈值判定、可靠性评分 | done |
| 报告生成 | 中文 HTML 报告 + ECharts 图表 + CSV 导出（离线可用） | done |
| LLM 解读 | 可配置 OpenAI-compatible / Anthropic / Gemini 模型，后台生成独立 HTML 解读报告 | done |
| Android 采集 | adb 设备选择/preflight、安装/清理/启动/采集全流程 | done |
| Android 解析 | gfxinfo（jank_rate/p95）、meminfo（PSS/Java Heap）、logcat（crash/ANR/startup） | done |
| Android 连续指标 | 逐帧 FPS、CPU/内存/网络/功耗/GPU/温度采样、录屏同步、series 报告与降级链 | done |
| Android fixture | 最小 Gradle 源码树，支持注入延迟/卡顿/内存/崩溃 | done |
| Perfetto | artifact 采集与 pull（可选） | done |
| Android 诊断 | `diagnose android` 支持 frame/cpu/trace/full 聚焦采集 | done |
| 桌面打包 | PyInstaller onedir（macOS 实测产出；Windows spec/文档/CI 备用就绪，Windows 实测未执行） | macOS done / Windows not_run |
| 自验证 | `validate` 命令运行 synthetic + android-parsers 测试套件 | done |

> Host CLI 的 iOS 采集（xcrun/xctrace，M3）和 SDK 集成（M4）仍未实现；iOS **模拟器**
> 采集在 Web 版已支持（见下表），详见
> 开发过程状态文档保留于开发仓库，不随发布分发。

### Web 版（perfdog-parity，`services/`）

| 模块 | 功能 | 状态 |
| --- | --- | --- |
| 后端服务 | FastAPI（`services/backend/run.py`，默认 ：8737，`GET /api/health`），synthetic / real / simulator 三 provider | done |
| 契约先行 | API_CONTRACT.md §1–§26：设备/应用/会话/实时流/统计/对比/导出/测速/MCP/iOS 真机采集，golden 示例 + 契约测试 | done |
| 实时监控 | SSE 指标流（自动重连 Last-Event-ID）、开始/停止、record 两态、FPS/CPU/内存/网络/温度/功耗/GPU 曲线 | done |
| 统计与报告 | PerfDog 统计量口径复刻、数值卡矩阵、Excel 导出（4 工作表 + Sum 行）、报告获取 `GET /api/runs` | done |
| 历史对比 | 本地 case 库、对比池/对比报告、回归评审 | done |
| 录屏/回放 | 录屏/截图/定时截图（clockAnchors 原样）、回放视图、markers sidecar、区间统计 | done |
| 标记 | Label/批注/标定、PERFEVAL_EVENT 打点通道 | done |
| 采集扩展 | 能力门禁矩阵、多设备、WiFi 模式、CPU 高阶/内存明细/StartupTiming、Log 面板 | done（WiFi 数据面待同网段复测） |
| 诊断联动 | 深度分析（Perfetto 数据面）、根因诊断卡、定时截图 | done |
| FPS 通道 | gfxinfo 周期轮询 / surfaceflinger latency / timestats uid 归属三级分流（真机/模拟器实测） | done |
| iOS Simulator | `xcrun simctl` 采集（设备发现/sim-boot/应用列表/前台判定/宿主口径 CPU+RSS/录屏/截图/日志流），宿主口径显式声明 | done（真机 iOS 见契约 §26：xctrace 双指标契约/注册表已落地，执行面在途） |
| 网络测速 | `GET /api/speedtest/payload` + `POST /api/devices/{id}/speedtest`（adb reverse + 设备侧 curl，链路口径声明，0 B/s 按失败处理） | done（真实 USB 链路数值未实测） |
| MCP server | `mcp_server.py` stdio JSON-RPC（MCP 2024-11-05），8 工具透传既有端点，零新增依赖 | done（宿主对接已实测且可复现：`scripts/mcp-host-smoke/` 官方 MCP TS SDK 客户端独立进程 initialize/tools/list(8)/tools/call ×8 全链通过，2026-11-06；Claude Desktop/Code 产品级宿主未测——本机 claude CLI 无输出挂起） |
| Service CLI | `service.py start/stop/status`（pidfile 进程管理，幂等 + SIGTERM→SIGKILL） | done |
| 明确不做 | R34 悬浮窗 / R23 云真机 / R47·R49 协作 / R48 BenchMark（需设备端 SDK 或云端数据面，本地单机无对标物） | 决策不做 |

里程碑明细与矩阵（R01–R59）见
契约文档保留于开发仓库，不随发布分发。

## Android 当前可用能力

Android 性能评估当前以 Host CLI 为主，已经可用于两类场景：

1. 离线解析已有采集产物：`perfeval run ... --android-artifacts <dir>` 会读取 `gfxinfo.txt`、`meminfo.txt`、`logcat.txt`，保留可选 `perfetto.pftrace`，生成 `timeline.ndjson`、`summary.json` 和中文 HTML 报告。
2. 连接真机或模拟器执行采集：`perfeval run ... --android-device-run` 会执行安装、force-stop、可选清理数据、清空 logcat、reset gfxinfo、可选 Perfetto、启动 Activity、等待或交互、采集 dumpsys/logcat、拉取 Perfetto，并生成同一套 run 产物。

已支持的 Android 场景步骤：

| step type | 作用 |
| --- | --- |
| `launch` | 通过 `adb shell am start -W -n <package>/<activity>` 启动 Activity，并从 `TotalTime` 派生 `startup.cold.ms` |
| `idle` | 等待指定毫秒数 |
| `manual` | `--interactive` 时暂停等待人工操作；可通过 `timeoutMs` 设置最长等待，超时后自动继续并写 diagnostic；非交互模式会跳过并写 diagnostic |
| `reset_gfxinfo` | 在交互流中重新 reset 目标包的 gfxinfo |
| `swipe` | 通过 `adb shell input swipe ...` 驱动滚动，可配置 repeat 和 interval |

内存评级支持场景级 `memoryProfile`，默认是 `mainstream`。可选值：

| memoryProfile | 适用场景 | `memory.pss.peak_mb` 评级阈值 |
| --- | --- | --- |
| `lightweight` | 轻工具、小型业务页 | 优<300 / 良<600 / 中<900 / 高风险>=900 MB |
| `mainstream` | 默认主流 App | 优<400 / 良<800 / 中<1200 / 高风险>=1200 MB |
| `heavy` | 游戏、视频、图片流、内容聚合 | 优<700 / 良<1200 / 中<1800 / 高风险>=1800 MB |

也可以在 `budgets.<metric>.ratingAbs` 覆盖评级阈值；优先级为 `ratingAbs` > `memoryProfile` > 内置默认。

## Android 当前边界

- Perfetto 采集 `.pftrace` artifact 后可经可选的 `trace_processor` 解析 frame timeline（逐帧 epoch 时间轴 + jank 标记，ADR-0001）；线程、调度、binder 维度解析暂未接入。二进制缺失时自动降级 framestats 路径并记录 `trace_processor.unavailable`。
- 帧指标优先从 `gfxinfo-framestats.txt` 的前台窗口段解析，避免同进程隐藏/历史 ViewRoot 污染；`window.txt` 保存采集时的前台窗口快照。找不到窗口段时回退到 `dumpsys gfxinfo` 进程摘要。
- 连续 sampler 默认按 1～2 秒采样 CPU、内存、网络、功耗、GPU/温度；采样器按设备能力逐项降级，原始 `samples.ndjson`、`timeline.ndjson` 和录屏 artifact 保留用于离线重算。
- screenrecord 与 flow marker、clock anchors 已接通；录屏不可用或为空时只降级回时间轴，不阻断其余指标。
- preflight 当前覆盖 adb 可用性、设备在线状态和自动/指定设备选择；电量、热状态、刷新率、API level、APK manifest 校验尚未写入 manifest。
- simpleperf 已在 `diagnose android --mode cpu/full` 中可采集 `perf.data` 和文本报告；JankStats SDK、Android Trace API SDK timeline pull 还未实现。
- parser 已有 golden 样本自验证，但不同 OEM/Android 版本的 dumpsys 输出仍需要持续补样本。

## 环境要求

- Python 3.11+
- CLI 核心无第三方 pip 依赖（YAML 解析、JSON、CSV、HTML 均为内置实现）
- 桌面 GUI 依赖 PySide6；LLM API Key 持久化使用 `keyring` 对接系统凭据库
- Web 版后端依赖见 `services/backend/requirements.txt`（fastapi/uvicorn/httpx/openpyxl）；
  前端开发和 Electron 构建使用 Node.js 24 与 npm。
- Android 采集需要 `adb`（Android Platform Tools）；iOS Simulator 采集需要 `xcrun simctl`（macOS 自带）
- 可选：Android 设备或模拟器、Perfetto
- 可选：`trace_processor_shell`（Perfetto 官方发布，Apache-2.0，单文件约 20MB）。缺失不影响其余指标；
  存在时可解析 `.pftrace` 的 frame timeline（逐帧 wall-clock 时间轴）。获取方式：
  `curl -LO https://get.perfetto.dev/trace_processor && chmod +x trace_processor`，
  放置位置按发现链生效：`PERFEVAL_TRACE_PROCESSOR` 环境变量 → GUI 工具路径设置 → `PATH` → 项目 `tools/` 目录（ADR-0001）

Electron 桌面版通过 GitHub Actions 构建 macOS Apple Silicon DMG 和 Windows x64 安装包。Windows 安装包内置后端与 ADB。两端安装包及 SHA-256 校验文件在同一个 GitHub Release 发布。 [桌面版发包流程](services/desktop/README.md#github-自动发布)

## 安装

```bash
git clone <repo-url> PerMobile
cd PerMobile
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools
python -m pip install -e .
```

如需运行桌面 GUI（含 LLM 系统凭据库支持），安装 GUI 可选依赖：

```bash
python -m pip install -e ".[gui]"
```

开发和执行 GUI 测试时可安装 `.[gui-dev]`。

安装后 `perfeval` 命令可用：

```bash
perfeval --version
# perfeval 0.2.0
```

## Web 版（perfdog-parity）安装与启动

后端与前端各自独立启动；前端默认跑内置模拟数据（无后端也能看实时曲线）。

```bash
# 1) 后端依赖（仓库 .venv 已装齐；新环境执行一次）
python -m pip install -r services/backend/requirements.txt

# 2) 启动后端（--provider auto|real|synthetic|simulator；默认端口 8737）
.venv/bin/python services/backend/run.py --port 8737 --provider auto
curl -s http://127.0.0.1:8737/api/health

# 2') 或用 Service CLI 常驻管理（start 幂等 / stop / status，退出码 0=运行 3=未运行）
.venv/bin/python services/backend/service.py start --port 8737 --provider auto
.venv/bin/python services/backend/service.py status
.venv/bin/python services/backend/service.py stop

# 3) 前端（dev server 5173，/api 代理到 127.0.0.1:8737）
npm --prefix services/frontend install
npm --prefix services/frontend run dev
# 浏览器打开 http://localhost:5173 （默认模拟数据；直连后端加 ?pdmode=live）

# 4) MCP stdio server（对接 MCP 宿主时；需后端已启动）
.venv/bin/python services/backend/mcp_server.py --base-url http://127.0.0.1:8737
#    宿主侧全链冒烟（官方 SDK 客户端；用法与边界见 scripts/mcp-host-smoke/README.md）
node scripts/mcp-host-smoke/host-driver.mjs
```

前端构建与门禁：

```bash
npm --prefix services/frontend run build           # vue-tsc 类型检查 + vite build
npm --prefix services/frontend run lint:isolation  # H6 门禁：src/ 除 api-client 外禁止网络 API 字面量
```

测试：

```bash
.venv/bin/python -m pytest tests/ -q                     # 主工程（含桌面端）
(cd services/backend && ../../.venv/bin/python -m pytest tests/ -q)   # Web 版后端（含契约 golden 测试）
```

也可以不安装，直接通过 Python 调用：

```bash
python3.11 -c "from perfeval.cli.main import main; import sys; sys.exit(main(['--help']))"
```

> macOS Apple Silicon 上建议使用 arm64 的 Python 3.11+（例如 Homebrew 的
> `/opt/homebrew/bin/python3.11`），并优先使用 `python -m pip`。不要直接使用裸
> `pip`，它可能命中旧的 `/Library/Frameworks/Python.framework/Versions/2.7/bin/pip`
> 或 `/usr/local/bin/pip`，从而触发 Rosetta/x86_64 运行错误。

## 快速上手

桌面 GUI 默认提供“开始测试”任务中心：选择启动速度、页面流畅度、内存稳定性、多版本 A/B 或深度诊断，确认设备和应用后即可启动。工作区 `scenarios/` 中适合一键执行的 YAML 会自动显示为项目场景；Fixture、示例、合成验证等辅助场景可通过 `gui.quickStart: false` 保留给 CLI 和自动化使用，同时不在快捷列表中展示。完整 YAML、输出路径、离线 artifact 和命令预览保留在“自定义”模式。

### LLM 解读报告

在“设置 → LLM 模型”中可保存多个模型档案并指定默认模型，支持：

- OpenAI-compatible Chat Completions
- Anthropic Messages
- Gemini generateContent

API Key 优先保存到 macOS Keychain、Windows Credential Manager 或 Linux Secret Service；系统凭据库不可用时只在当前应用会话中保留，不会明文写入普通设置、日志或报告。

完成单次测试或在历史结果中选择一份报告后，可手动启动后台解读，并选择“精简 / 标准 / 深度”三档分析深度。工具只发送有大小上限的 summary、compare、diagnosis 等结构化数据、artifact 元数据，以及当前结果目录内受限的 stdout、stderr、logcat 日志片段；不同档位使用不同的日志片段和输出上限。不会上传 APK、Perfetto trace、simpleperf data 或完整日志。结果保存在原结果目录的 `llm/interpretation.json` 和 `llm/interpretation.html`，并记录生成时使用的解读档位；原始 `report.html` 会增加跳转入口。

### 1. 初始化工作目录

```bash
perfeval init
```

创建 `scenarios/`、`runs/`、`reports/` 目录和一个示例场景文件。

### 2. 检查环境

```bash
perfeval devices
```

检测本机 `adb`、`xcrun`、`xctrace` 是否可用，并尝试选择 Android 设备。

### 3. 运行合成数据验证（无需设备）

```bash
perfeval validate synthetic
perfeval validate android-parsers
```

第一条运行 6 个内置合成测试用例，验证对比引擎和报告生成是否正确。第二条用 Android golden 样本验证 `gfxinfo`、`meminfo`、`logcat` parser。

### 4. 解析已有 Android 采集产物（无需连接设备）

如果你已经有 `gfxinfo.txt`、`meminfo.txt`、`logcat.txt` 文件：

```bash
perfeval run scenarios/android-fixture-startup.yaml \
  --android-artifacts /path/to/artifacts \
  --package-name com.example.app
```

工具会解析产物、生成 timeline 和 summary，并自动在 `runs/<run-id>/report/report.html` 生成中文可视化报告。

### 5. 连接设备执行完整采集

```bash
perfeval run scenarios/android-fixture-startup.yaml \
  --android-device-run \
  --package-name com.perfeval.fixture \
  --apk ./fixtures/android-fixture/app/build/outputs/apk/debug/app-debug.apk \
  --activity .MainActivity \
  --perfetto
```

完整流程：安装 APK → 清理数据 → 清除 logcat → 重置 gfxinfo → 启动 Perfetto（可选）→ `am start -W` → 等待 → 采集 gfxinfo/meminfo/logcat → 拉取 Perfetto → 解析全部产物 → 生成报告。

### 6. 当前页面人工滑动回归

部分真机不允许 `adb shell input swipe` 注入输入事件。此时使用人工滑动场景：工具先等待你导航到目标页面，再 reset gfxinfo，然后等待你手动滑动，最后采集报告。当前内置人工滑动窗口最长等待 60 秒；如果忘记按 Enter，工具会自动继续采集并在 diagnostics 中记录 `flow.manual.timeout`。

```bash
perfeval run scenarios/android-current-page-manual-scroll.yaml \
  --repeat 1 \
  --out runs/manual-scroll-regression \
  --android-device-run \
  --serial <device-serial> \
  --package-name <package-name> \
  --no-clear-data \
  --no-reinstall \
  --post-launch-wait 0 \
  --interactive
```

### 7. Android 问题定位采集

当报告显示滑动卡顿后，可以用统一诊断入口继续采集定位证据：

```bash
perfeval diagnose android \
  --serial <device-serial> \
  --package-name <package-name> \
  --case current-page-scroll \
  --out runs/android-diagnose \
  --mode frame \
  --interactive
```

`--mode` 支持：

| mode | 采集内容 | 适用问题 |
| --- | --- | --- |
| `frame` | `gfxinfo` / `framestats` / `meminfo` / `logcat` + HTML 报告 | 快速确认 jank、帧耗时、内存和稳定性 |
| `cpu` | `simpleperf record`、`perf.data`、`simpleperf report` | 定位 CPU 热点函数，需要 debug/profileable 支持时效果更好 |
| `trace` | Perfetto `.pftrace` | 定位主线程 stall、binder、scheduler gap、frame timeline |
| `full` | 同一次聚焦流程内同时覆盖 frame、cpu、trace | 一次操作保留完整定位证据 |

示例：采 simpleperf CPU 热点：

```bash
perfeval diagnose android \
  --serial <device-serial> \
  --package-name <package-name> \
  --case current-page-scroll-cpu \
  --out runs/android-diagnose \
  --mode cpu \
  --duration 30 \
  --interactive
```

示例：采 Perfetto trace：

```bash
perfeval diagnose android \
  --serial <device-serial> \
  --package-name <package-name> \
  --case current-page-scroll-trace \
  --out runs/android-diagnose \
  --mode trace \
  --duration 30 \
  --interactive
```

每次诊断会生成 `diagnosis.json`，并把证据统一放在 `runs/android-diagnose/<case>/artifacts/android/`。
同目录的 `report/report.html` 是综合 H5 报告：顶部给出整体结论和首要风险，下面按流畅度、CPU 采样、Perfetto Trace、内存、稳定性分区展示代表性指标；原始 metric、diagnostics 和 artifact 证据会放在可展开明细里。

`diagnose android --interactive` 中，准备阶段仍由你按 Enter 明确开始；采集开始后的操作窗口会使用 `--duration` 作为最长等待。到时未按 Enter 时，工具会打印自动结束提示、停止/等待采集并继续拉取证据，同时在 `diagnosis.json` 写入 `manual_flow.timeout:<duration>s` 或 `flow.manual.timeout`。

`.pftrace` 可以直接从报告页的 Trace 证据区进入，也可以用 CLI 快捷打开：

```bash
perfeval open runs/android-diagnose/<case>/report/report.html --viewer report
perfeval open runs/android-diagnose/<case>/artifacts/android/<trace>.pftrace --viewer android-studio
perfeval open runs/android-diagnose/<case>/artifacts/android/<trace>.pftrace --viewer perfetto-ui
```

推荐用 `--viewer report` 打开报告；它会临时在 `127.0.0.1` 提供 report 和 artifacts，报告里的 “Perfetto UI 直接打开” 会把对应 trace URL 带给 Perfetto UI。`--viewer perfetto-ui` 则会只服务单个 trace 文件，并打开带 `url=` 参数的 Perfetto UI。命令运行期间保持终端不要关闭。

### 8. 对比基线与对照版本

```bash
perfeval compare runs/baseline runs/candidate
```

对比两个运行目录或运行组，生成 `compare/compare.json`。退出码：`0`=pass，`1`=fail，`4`=invalid。

如果 baseline/candidate 是同一个 Android `packageName` 的两个 APK，不能同时安装，可以用一键 A/B 顺序采集：

```bash
perfeval ab scenarios/android-version-ab.example.yaml \
  --android-device-run \
  --baseline-apk /path/to/app-baseline.apk \
  --candidate-apk /path/to/app-candidate.apk \
  --package-name com.example.app \
  --activity .MainActivity \
  --serial <device-serial> \
  --out runs/feed-ab \
  --repeat 5 \
  --no-clear-data \
  --interactive
```

需要比较 3～5 个 APK 时，重复传入 `--candidate-apk ID=PATH`。baseline 只采集一次，每个 candidate 都会单独与 baseline 比较：

```bash
perfeval ab scenarios/android-version-ab.example.yaml \
  --android-device-run \
  --baseline-apk /path/to/app-v4.2.9.apk \
  --candidate-apk v4_3_0=/path/to/app-v4.3.0.apk \
  --candidate-apk v4_3_1=/path/to/app-v4.3.1.apk \
  --candidate-apk v4_3_2=/path/to/app-v4.3.2.apk \
  --candidate-apk v4_3_3=/path/to/app-v4.3.3.apk \
  --package-name com.example.app \
  --activity .MainActivity \
  --serial <device-serial> \
  --out runs/multi-version-ab \
  --repeat 5 \
  --interactive
```

也可以直接在场景的 `apps:` 中声明一个 baseline 和多个带 `artifact` 的 candidate；未传 `--candidate-id` 或 `--candidate-apk` 时，`ab` 会测试所有非 baseline Android app。只传路径、不写 `ID=` 时，候选组会自动命名为 `candidate`、`candidate-2`、`candidate-3` 等。ID 会转换为输出目录名；建议只使用英文字母、数字、点、下划线和连字符，并在 Windows 上避开 `CON`、`PRN`、`AUX`、`NUL` 等保留名。不同 ID 若转换为同一目录名，命令会拒绝执行。

同一模板可以重复使用，APK、包名、入口 Activity、设备和输出目录均由命令行传入；模板只维护公共操作流程、必需指标和性能预算。所有 APK 必须使用相同的 `packageName`，并建议使用相同签名。`ab` 会先清理该输出目录中上一轮生成的所有 variant、compare 和 report，再按 baseline、candidate-1、candidate-2 的顺序安装与采集。每轮 summary 会记录实际前台窗口；若某个 candidate 与 baseline 最终停留在不同页面族（例如详情页对首页），对应比较会标记为 `invalid`。深层页面测试可使用 `--no-clear-data` 保留登录态，需要完全干净的数据环境时改用 `--clear-data`。若覆盖安装失败，可显式加 `--allow-uninstall` 允许卸载后重装；该选项会卸载应用并清除其数据，只应在测试环境使用。

多 candidate 输出结构：

```text
runs/multi-version-ab/
  ab.json                         # 多 APK 元数据和总体状态
  baseline/                       # baseline 采集结果
  v4_3_0/                         # candidate 采集结果
  v4_3_1/
  compare/
    compare.json                  # 汇总清单
    v4_3_0/compare.json           # baseline vs v4_3_0
    v4_3_1/compare.json           # baseline vs v4_3_1
  report/
    report.html                   # 多 APK 总览
    v4_3_0/report.html            # 独立双边报告
    v4_3_1/report.html
```

总体状态取所有独立比较中的最严重状态；独立 `compare.json` 仍使用原有双边 schema，方便现有工具继续消费。

### 9. 生成对比报告

```bash
perfeval report compare/compare.json --out reports/latest
```

从 `compare.json` 或 `summary.json` 生成离线 HTML 报告（含 ECharts 图表）。当 `summary.json` 同级存在 `diagnosis.json` 时，报告会自动整合 simpleperf、Perfetto 和诊断 artifact 状态。

## 命令参考

### `perfeval init`

初始化工作目录。

```
perfeval init [--root .]
```

### `perfeval devices`

检测本机采集工具和 Android 设备。

### `perfeval run`

为场景创建运行产物。

```
perfeval run [scenario] [options]
```

| 选项 | 说明 |
| --- | --- |
| `scenario` | 场景配置文件路径，默认 `scenarios/startup.synthetic.yaml` |
| `--out` | 输出目录，默认 `runs` |
| `--repeat N` | 覆盖场景中的 `run.repeats` |
| `--dry-run` | 只解析不写文件 |
| `--android-artifacts DIR` | 解析已有 Android 产物目录 |
| `--android-device-run` | 连接设备执行完整采集流程 |
| `--package-name NAME` | Android 包名（logcat 过滤） |
| `--serial SERIAL` | 指定设备序列号 |
| `--apk PATH` | APK 文件路径 |
| `--activity NAME` | 启动 Activity，如 `.MainActivity` |
| `--no-clear-data` | 跳过 `pm clear` |
| `--no-reinstall` | 跳过安装 |
| `--perfetto` | 采集 Perfetto artifact |
| `--post-launch-wait S` | 启动后等待秒数，默认 2.0 |
| `--interactive` | 遇到 `manual` flow step 时暂停等待人工操作 |

### `perfeval diagnose`

采集 Android 聚焦诊断证据。

```
perfeval diagnose android --package-name NAME [options]
```

| 选项 | 说明 |
| --- | --- |
| `--package-name NAME` | Android 包名，必填 |
| `--serial SERIAL` | 指定设备序列号 |
| `--out DIR` | 诊断输出根目录，默认 `runs/android-diagnose` |
| `--case ID` | 诊断 case/run 目录名，默认 `android-diagnose` |
| `--mode frame|cpu|trace|full` | 采集证据类型，默认 `frame` |
| `--duration S` | simpleperf 或 Perfetto 采集秒数，默认 30 |
| `--interactive` | 采集期间提示你执行聚焦操作 |
| `--memory-profile lightweight|mainstream|heavy` | 诊断报告内存评级档位，默认 `mainstream` |
| `--first-party-regex REGEX` | 记录一方包名/模块正则，供热点筛选元数据使用 |

### `perfeval compare`

对比基线与对照运行组。

```
perfeval compare <baseline> <candidate> [--mode MODE] [--budget FILE] [--out DIR]
```

- `baseline` / `candidate`：运行目录、`summary.json` 文件或包含多个运行的目录
- `--mode`：对比模式，默认 `same_device_versions`
- `--budget`：预算配置文件（YAML/JSON）
- `--out`：输出目录，默认 `compare`

### `perfeval report`

从 JSON 生成静态 HTML 报告。

```
perfeval report <input_json> [--out DIR]
```

- `input_json`：`compare.json` 或 `summary.json`
- `--out`：报告输出目录，默认 `reports/latest`

### `perfeval open`

用本地 viewer 打开报告或 trace artifact。

```
perfeval open <path> [--viewer system|android-studio|perfetto-ui|report] [--serve-seconds N] [--dry-run]
```

- `path`：报告 HTML 或 artifact 文件路径
- `--viewer`：默认 `system`；`report` 会在 `127.0.0.1` 临时服务报告目录，`perfetto-ui` 会服务单个 `.pftrace` 并打开 Perfetto UI
- `--serve-seconds`：本地临时服务保留秒数，默认 300
- `--dry-run`：只打印将执行的打开动作

### `perfeval validate`

运行自验证测试套件。

```
perfeval validate [suite] [--case CASE] [--out DIR] [--reports DIR]
```

- `suite`：`synthetic`（默认）或 `android-parsers`
- `--case`：仅运行单个合成用例（`no_regression`、`startup_regression`、`jank_regression`、`memory_regression`、`noisy_unknown`、`crash_fail`）
- `--out`：运行产物输出目录，默认 `runs/synthetic-validation`
- `--reports`：报告输出目录，默认 `reports/synthetic-validation`

## 场景配置

场景使用 YAML 格式，示例见 [scenarios/startup.synthetic.yaml](scenarios/startup.synthetic.yaml) 和 [scenarios/android-fixture-startup.yaml](scenarios/android-fixture-startup.yaml)。

核心字段：

```yaml
schemaVersion: perfeval.scenario.v1
project: MyProject
scenarioId: startup_home
description: Cold start regression scenario

gui:
  quickStart: true              # 设为 false 时不在 GUI 快速启动列表展示

run:
  repeats: 5
  mode: same_device_versions      # 或 same_app_devices

apps:                              # Android 设备采集时使用
  - id: baseline
    platform: android
    packageName: com.example.app
    artifact: ./app.apk
    versionName: 1.0.0
    versionCode: 100

flow:
  steps:
    - type: launch
      activity: .MainActivity

collectors:
  android:
    logcat: true
    gfxinfo: true
    meminfo: true
    perfetto:
      enabled: auto
      durationMs: 20000

budgets:
  startup.cold.ms:
    direction: lower_better
    failAbs: 2500
    warnRegressionPct: 0.08
    failRegressionPct: 0.15
    minDeltaAbs: 80
```

### 预算参数说明

| 参数 | 说明 |
| --- | --- |
| `direction` | `lower_better` / `higher_better` / `neutral` |
| `failAbs` | 绝对值失败阈值 |
| `warnAbs` | 绝对值警告阈值 |
| `warnRegressionPct` | 回归百分比警告阈值（如 0.08 = 8%） |
| `failRegressionPct` | 回归百分比失败阈值 |
| `warnRegressionAbs` | 回归绝对值警告阈值 |
| `failRegressionAbs` | 回归绝对值失败阈值 |
| `minDeltaAbs` | 最小有效差异（低于此值忽略） |

## 输出产物

每次 `perfeval run` 生成以下文件：

```
runs/<run-id>/
  manifest.json       # 运行元数据（设备/应用/环境信息）
  summary.json        # 指标摘要（中位数/p95/置信度等）
  timeline.ndjson     # 原始事件时间轴（唯一事实表）
  diagnostics.ndjson  # 诊断事件
  tool.log            # 工具运行日志
  artifacts/          # 原始采集产物
    android/
      gfxinfo.txt
      gfxinfo-framestats.txt  # 设备采集时生成
      window.txt              # 采集时的前台窗口快照
      meminfo.txt
      logcat.txt
      perfetto.pftrace    # 可选
  report/
    report.html       # 中文可视化报告
    summary.json
```

`perfeval compare` 生成：

```
compare/
  compare.json        # 对比结果（指标差异/判定/置信度）
```

`perfeval report` 生成：

```
reports/<dir>/
  report.html         # 含 ECharts 图表的离线 HTML 报告
  compare.json        # 输入为 compare.json 时生成
  summary.json        # 输入为 summary.json 时生成
  metrics.csv         # 输入为 compare.json 时生成
  runs.csv            # 输入为 compare.json 且有 runs 时生成
  echarts.min.js      # 输入为 compare.json 时生成的离线图表库
```

## 支持的指标

| 指标 ID | 单位 | 来源 | 优先级 |
| --- | --- | --- | --- |
| `startup.cold.ms` | ms | `am start -W` TotalTime / logcat | P0 |
| `frame.jank_rate` | ratio | dumpsys gfxinfo | P0 |
| `frame.duration.p95.ms` | ms | dumpsys gfxinfo | P1 |
| `memory.pss.peak_mb` | MB | dumpsys meminfo | P1 |
| `memory.java_heap.mb` | MB | dumpsys meminfo | P1 |
| `stability.crash.count` | count | logcat | P0 |
| `stability.anr.count` | count | logcat | P0 |
| `stability.strict_mode.count` | count | logcat | P2 |

此外，gfxinfo 还会产出 `frame.total_count`、`frame.janky_count`、`frame.missed_vsync_count`、`frame.slow_ui_thread_count`、`frame.duration.p50/p90/p99.ms` 等辅助指标；meminfo 还会产出 `memory.native_heap.mb`、`memory.graphics.mb`。logcat 支持通过 `PERFEVAL_EVENT` JSON 标记上报自定义 metric。

## Android Fixture

项目包含一个最小 Android fixture 应用，位于 [fixtures/android-fixture/](fixtures/android-fixture/)，支持通过 launch extras 注入性能问题：

- `startup_delay_ms`：启动延迟
- `main_thread_block_ms`：主线程阻塞
- `jank_block_ms` / `jank_count`：帧回调阻塞
- `memory_alloc_mb`：内存分配
- `crash`：触发崩溃

详见 [fixtures/android-fixture/README.md](fixtures/android-fixture/README.md)。

## 设计文档

- 桌面端/评估内核的架构设计和需求文档保留于开发仓库（performance-evaluator-design/），不随发布分发。
- Web 版（PerfDog 对标 PD-001）的设计、契约与对标矩阵位于
  （契约与架构文档保留于开发仓库）
  （API_CONTRACT / ARCHITECTURE / DEVELOPMENT_PLAN / STATUS / research/）。

## 许可证

[PolyForm Noncommercial 1.0.0](LICENSE)（仅限非商业用途，Copyright (c) 2026 xxd /
PerMobile contributors）；打包产物内的第三方
许可声明见 `packaging/licenses/`。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q    # 主工程：592 passed, 2 skipped（2026-11-04 基线）
(cd services/backend && ../../.venv/bin/python -m pytest tests/ -q)
                                        # Web 版后端：393 passed, 3 skipped（2026-11-04 基线）
```

主工程测试覆盖：schema/timeline/artifact 验证、合成数据生成、摘要引擎、对比引擎、
Android 解析器与采集流程、报告生成、diagnose/open 命令和 CLI 端到端、GUI（pytest-qt）。
后端测试覆盖：契约 golden 示例、SSE 会话链路、统计口径对拍、录屏/截图/markers、
能力门禁、网络测速失败语义、MCP 协议层（含本进程 uvicorn e2e）、Service CLI。

若本地尚未安装 pytest，也可以先用内置验证入口检查核心可用性：

```bash
python3.11 -c "from perfeval.cli.main import main; import sys; sys.exit(main(['validate', 'synthetic']))"
python3.11 -c "from perfeval.cli.main import main; import sys; sys.exit(main(['validate', 'android-parsers']))"
```
