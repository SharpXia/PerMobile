# PerfLab 前端（PD-001 perfdog-parity M1 竖切片）

对标 PerfDog 实时监控看板的最小竖切片：设备/应用选择 → 开始/停止（监控与记录两态分离）
→ 实时多指标曲线（Page 堆叠 + 叠加模式）→ 数值卡 → 测试记录列表。

依据：架构约定（H1-H8）、
API 契约与交互调研（保留于开发仓库）。

## 命令

```bash
cd services/frontend
npm install
npm run build           # vue-tsc --noEmit && vite build（类型检查是构建的一部分，H4）
npm run dev             # Vite dev server（5173，/api 代理到 127.0.0.1:8737）
npm run preview         # 预览 dist 产物
npm run lint            # eslint（含 H6 no-restricted-syntax 轨）
npm run lint:isolation  # H6 权威门禁：src/ 除 api-client 外禁止出现网络 API 字面量
```

## 数据模式（2026-11-12 起仅直连后端）

mock 模式（前端内置合成流）已移除——桌面 app 语境下假数据是干扰源。前端永远按
契约访问 FastAPI/stdlib 后端：dev 走 Vite proxy（127.0.0.1:8737），同源托管
（后端静态托管 / Electron 壳生产态）时 baseUrl 为 `''`；`?pdbase=` 可覆盖地址。
后端未启动时得到明确报错提示（不伪装成数据）；后端 synthetic provider 会显示
「合成模式」横幅（契约 §2）。默认预选 FPS / CPU（整机）/ 温度三页，对标 PerfDog 默认看板。
指标面板勾选持久化于 localStorage（`pd.selected.metrics.v1`），刷新页面不丢失。

## 刷新 / 换标签页（会话接管，契约 §6.2）

会话是后端内存态，刷新页面不会结束采集。前端把会话 id 记在 localStorage
（`pd.sessions.v1`，主会话 + 设备矩阵卡），加载时自动接管：回填设备/应用/指标选择、
用 `recordStartedAt` 还原「记录 mm:ss」、重连 SSE 并以 5 分钟缓冲回补曲线与标记；
`stopped` 会话自动重开收口决策对话框。未接管的存活会话列在「实时监控」页顶部的
后台会话条，可**接管**或**停止并放弃**（收口采集，不留孤儿进程）。

## 结构（ARCHITECTURE.md §3）

```
src/
├── api-client/          # ★ H6：唯一网络出口，零框架依赖
│   ├── index.ts         #   createApiClient({baseUrl}) 工厂 + ApiClient 接口
│   ├── types.ts         #   与 API_CONTRACT.md 手写对齐的 TS 类型
│   ├── http.ts          #   fetch 封装（错误体 §3、超时、AbortController、二进制 Blob）
│   └── sse.ts           #   EventSource 封装（自动重连带 Last-Event-ID、未知 type 忽略）
├── views/               # MonitorView（实时看板）/ RecordsView（测试记录）/ ReportView（报告页）——无网络调用
├── components/          # 选择器 / 图表页 / 数值卡 / 数值卡矩阵 / 指标面板 / 收口对话框 / 日志面板
├── statCards.ts         # 统计量数值卡元数据（R41：FPS Tab ~20 卡的 label/unit/tooltip 口径）
├── store.ts             # Vue reactive 状态与会话动作（状态机 monitoring→recording→stopped→saved|discarded）
└── main.ts              # 唯一读取运行时配置（URL 参数 / Vite env）的入口
```

## M2 报告与导出（R39/R40/R41）

- **收口保存对话框（R39，[I§2.6]）**：停止后弹出——Case name 默认 `PD_<时间戳>`、
  ☑ Save excel（契约 §13.2 `exportExcel`，默认勾选=本地优先）、确认/丢弃。勾选后保存
  成功即随 §13.1 下载同名 `.xlsx`；导出失败不使 save 失败（可从记录/报告页重试）。
- **报告页（R41，[I§2.9][I§4.4]）**：测试记录行「报告」或停止态「统计报告」进入；
  首屏 = 指标 Tab 顶部数值卡矩阵（FPS Tab 20 卡，tooltip 携带 §12 口径；Smooth 卡带
  「近似口径」角标，note 原文在矩阵下方可见携带）→ Label 分段统计占位（R28/R29 属
  M5，不虚构分段行）→ 全量曲线。数据源 `GET /{runs|sessions}/{id}/statistics`
  （perfdog.statistics.v1）；曲线走契约 §7 会话窗口（run 级时序读取端点契约未定义，
  曲线区如实说明，见下边界）。
- **Excel 导出（R40/§13.1）**：记录行「导出」/ 报告页「导出全部」→ `GET
  /api/runs/{id}/export.xlsx`。

## 已知边界（如实声明）

- **实时通道依赖后端实现契约 §8**（`GET /api/sessions/{id}/events` SSE）；后端属于
  另一交付件，未就绪时得到明确报错提示（不伪装成数据）。
- **指标口径**：CPU 未规范化（R02）、内存 peak 语义（R03）、网络进程级（R04）、电流带
  充放电方向（R05）、温度 max 单值（R06）、GPU 机型相关（R07）——图例与描述文案已标注。
- echarts 采用整包引入（构建体积 >1MB），M1 以稳定优先；后续可切 `echarts/core` 按需注册。
- **报告页曲线**：契约 §9 未定义 run 级时序读取端点时曲线区如实显示不可用说明（回放属 R37）。
- **Smooth/1%Low/Jank 族数值**：会话只有 `frame.fps.mean` 轮询通道
  （fpsSeries/none 基线），这些组不可计算，卡片置灰显示 —（不填假零值）；逐帧基线
  （frameEvents）的全组数值由后端统计层承担（已实现，golden 对拍见
  services/backend/tests/test_statistics_parity.py）。
- 录屏/对比/case 库等属 M2+（R35-R38、R43-R44）。
