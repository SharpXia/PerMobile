# Changelog

本文件按版本整理 PerMobile / PerfEval Lite 的变更记录；里程碑与行号细节文档保留于开发仓库，不随发布分发。

## [0.2.0] - 2026-11-04

PerfDog 核心功能对标（PD-001 perfdog-parity）：新增前后端分离的 Web 版对标产品与
iOS Simulator 采集切片；桌面 GUI 保持 0.1.0 期能力不变。

### 新增

- **Web 版对标产品（perfdog-v2）**：`services/backend/`（FastAPI，默认端口 8737，
  契约 §1–§25）+
  `services/frontend/`（Vue3 + Vite + ECharts，`api-client` 隔离门禁 H6）。
  里程碑（30808ee→00d36d9）：
  - M1 实时监控竖切片（设备/应用选择 → 开始/停止 → SSE 实时指标流 → 看板曲线）；
  - M2 PerfDog 统计量口径复刻 + 收口保存 + Excel 导出 + 报告数值卡；
  - M3 本地 case 库 + 对比池/对比报告 + 回归评审；
  - M4 录屏/截图（clockAnchors 原样）+ 回放视图 + markers sidecar + 区间统计；
  - M5 场景 Label/批注标定 + 框选统计 + PERFEVAL_EVENT 打点通道；
  - M6 能力门禁矩阵 + 多设备 + WiFi/CPU 高阶/内存明细/StartupTiming/Log 面板；
  - M7 深度分析联动（Perfetto 数据面）+ 根因诊断卡 + 定时截图；
  - 真机/模拟器 real 全链实测（Android 16 真机 SM_F9560 + AVD Pixel 9），
    FPS 三级通道（gfxinfo 轮询 / SF latency / timestats uid 归属）；
  - M-iOS：iOS Simulator 采集（`xcrun simctl`，宿主口径 CPU/RSS，契约 §23）；
  - M8+ 本地切片：网络测速（契约 §24，链路口径声明）、MCP stdio server
    （契约 §25，零新增依赖）、Service CLI（start/stop/status）、
    `GET /api/runs` 与 `/api/runs/{runId}` 报告获取（§9）。
- **测试基线**：后端 393 passed + 3 skipped（`services/backend`）；主工程
  592 passed + 2 skipped；前端 build（vue-tsc）与 `lint:isolation` 门禁通过。
- **PolyForm Noncommercial 1.0.0 License**（`LICENSE`，仅限非商业用途，Copyright (c) 2026 xxd / PerMobile contributors）。
- **Windows 打包 CI 备用**：`.github/workflows/windows-build.yml`
  （仓库当前无 remote，文件备用未运行过）。

### 明确不做（对标矩阵逐行决策，详见 STATUS）

R34 悬浮窗、R23 云真机、R47/R49 任务协作、R48 BenchMark（云端数据面/设备端 SDK，
超出本地单机范围）；R24 iOS 真机、R45/R46/R54/R57/R59 维持 M8+ 远期。

## [0.1.0] - 2026-09-09

首个完整交付：Android Host-first 性能评估 + 桌面 GUI + 打包（CM-001 连续指标终验
12/12 AC pass，72f65aa）。

### 新增

- CLI 框架（9 子命令：init/devices/run/ab/diagnose/compare/report/open/validate）、
  YAML 子集场景解析、合成数据 6 用例、摘要/对比引擎、中文 HTML 报告 + ECharts + CSV。
- Android 采集：adb 设备选择/preflight、安装/清理/启动/采集全流程；gfxinfo
  （jank/p95/framestats）、meminfo（PSS/Java Heap）、logcat（crash/ANR/StrictMode、
  PERFEVAL_EVENT）解析；连续指标采样（CPU/内存/网络/功耗/GPU/温度）+ 录屏同步；
  Perfetto artifact + 可选 trace_processor frame timeline；simpleperf 诊断
  （frame/cpu/trace/full）；多 APK A/B 顺序采集与逐对比较。
- 桌面 GUI（PySide6）：任务中心、多模型 LLM 解读（系统凭据库存 Key）、历史结果、
  llm 解读三档深度；macOS arm64 冻结产物（onedir + CLI sidecar + 自动 smoke）。
- Android fixture（最小 Gradle 源码树，可注入延迟/卡顿/内存/崩溃）。

### 依赖

- CLI 核心零第三方 pip 依赖；GUI 依赖 PySide6 + keyring；构建期 PyInstaller。

[0.2.0]: #020---2026-11-04
[0.1.0]: #010---2026-09-09
