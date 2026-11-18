# realios fixtures（R24 真机 iOS 切片 · 勘察/实测行为存档）

本目录是契约 §26（真机 iOS 采集）解析层的 golden 语料与**本机实测行为存档**，
供离线复核——fixture 驱动单测（`test_real_ios_provider.py` /
`test_real_ios_sampler.py`）不依赖真机在网；xctrace record/export 链路真值对拍
需真机在网（`test_real_ios_live_smoke.py`，无设备时 skip 不伪装通过，§26.6）。

本目录的设备标识、序列号、局域网地址和应用安装标识均使用测试值。用户安装应用的名称与路径使用示例值。

实测环境：macOS 27.0.1（arm64，M5P MacBook Pro）· xcrun **72** · xctrace
**27.2 (27B52)** · devicectl **651.32**（jsonVersion **5**）· 真机 PerfEval iPhone
（iPhone 15 Pro Max / iPhone16,2 / iOS **27.2.1** / UDID
`00008130-000955B7C2D1E84A`，localNetwork 在网）。实测日期 **2026-11-16**。

## 文件清单

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `devices-sample.json` | **脱敏实测 + 1 合成行** | `xcrun devicectl list devices --json-output <path>` 全量 8 行：7 行来自 2026-11-16 实测（未配对 ECID 行 / watchOS 行 / 模拟器行 / 真机 localNetwork 行），末 1 行为**合成** wired+connected 行（见下「合成行声明」） |
| `apps-raw.json` | 脱敏实测 | `devicectl device info apps --device <udid> --json-output`（3 个用户安装 App；`defaultAppsIncluded:false` 时不出系统应用） |
| `processes-raw.json` | 脱敏实测 | `devicectl device info processes --device <udid> --json-output`（322 进程全表；`result.runningProcesses[].executable` 为 `file://` URL） |
| `launch-raw.json` | 脱敏实测 | `devicectl device process launch --device <udid> com.perfeval.demo --json-output`（pid=15436；`result.process.processIdentifier`） |
| `lockstate.json` | 脱敏实测 | `devicectl device info lockState --device <udid> --json-output`（passcodeRequired/unlockedSinceBoot 布尔对） |
| `details-raw.json` | 脱敏实测 | `devicectl device info details --device <udid> --json-output`（osVersionNumber/marketingName/cpuCount(6)/serialNumber/reality=physical 读数源；非 §26.1 契约字段源） |
| `activity-monitor-process-live.xml` | **真文法 + 重构行值** | `xctrace export --xpath '…table[@schema="activity-monitor-process-live"]'` 的行文法（单元格列位对齐/`ref` 引用/`sentinel`），表头 `<col>` 块逐字取自本机真导出；4 行行值按勘察口径重构（见下「XML 行值声明」） |
| `am-process-live-host-sentinel.xml` | 真机原文（宿主 trace） | 本机宿主 `xctrace record --attach yes`（CPU 满载进程）4s 真导出 verbatim——**cpu-percent 单元格恒 `<sentinel/>`**（宿主录制该列不出值），作解析器负例 golden：sentinel→null 不猜 |

## devicectl 命令面实测（--help / 实跑）

- `list devices` 的 JSON 参数**只有 `--json-output <path>`**；`--json` 短参不存在
  （契约 §26.1 实测确认）。`--json-output -` 支持 stdout 形态（info 子命令
  --help），`list devices` 亦支持；实装走临时文件（与契约数据源原文一致）。
- `device info` 子命令面（--help 实测）：`apps` / `processes` / `lockState` /
  `details` / `displays` 等；`device process launch --device <uuid|ecid|serial|
  udid|name|dns_name> <bundle-id>`（**bundle id 为位置参数**）。
- `--device` 接受 uuid/ecid/serial_number/**udid**/name/dns_name（launch
  --help 原文）；`xctrace --device` 只认 UDID/设备名（见下 exit 实测）。
- jsonVersion 5 结构（`devices-sample.json` 存档）：
  - 顶层 `{"info": {...}, "result": {"devices": [...]}}`；
  - 行内 `hardwareProperties/deviceProperties/connectionProperties` 带
    `_deprecationNotice`（replacement = `properties` 字典，两处取值等价）；
  - **行 `identifier` 是 coredevice UUID**（如 `6F2A9C41-…`），**不是 UDID**；
    UDID 在 `hardwareProperties.udid`（配对行在场、未配对行缺失）。
    §26.1 golden 的 `id="00008130-000955B7C2D1E84A"` 即 UDID——实装取
    `hardwareProperties.udid`（identifier 仅回退），xctrace `--device` 用同一 id。

### 行级字段实测值（7/7 行，`devices-sample.json` 可复核）

- `hardwareProperties.platform`：`iOS`（真机+模拟器）/ `watchOS`（Watch7,2 行）；
- `hardwareProperties.reality`：`physical`（真机）/ `simulated`（模拟器）/
  `null`（watch 行与未配对行）；
- `connectionProperties.pairingState`：`paired` / `unsupported`（仅带 ECID 无
  UDID 的未配对行，该行同时 `tunnelState:"unavailable"`、`transportType:null`）；
- `connectionProperties.transportType`：`localNetwork`（真机 WiFi 在网 +
  watch 行）/ `sameMachine`（全部模拟器行）/ `null`（未配对行）；
  **wired（USB 有线）原文值本机未实测**（未接 USB 线）——契约 §26.1 原文
  「transportType 原文值待真机 USB 接入实测回填，S1 实装以 fixture 固化」，
  实装按 `wired` 值映射 `usb`（合成行固化该映射），**待 USB 复测回填**；
  **隧道完全断开形态（2026-11-16 追测）**：真机 `tunnelState` 由
  `disconnected` 迁移 `unavailable` 时 `transportType` 同步回退 `null`
  （localNetwork 行不再在册）——该形态不出行（同「拔线设备不出 adb devices」
  语义）；paired 且 transportType 在册但 tunnelState=disconnected 的行照常
  出行（offline），live smoke 的「paired 但隧道断开」skip 分支即该形态；
- `connectionProperties.tunnelState`：`connected` / `disconnected` /
  `unavailable`（三值均在场）；
- `deviceProperties.connectionState`：**7/7 行恒 `null`**——不作在线信号
  （tunnel 是唯一在线判据，§26.1）；合成行同 null；
- `deviceProperties.bootState`：`booted` / `shutdown`；模拟器 booted 行
  `tunnelState:"connected"` 也会出场（实装靠 reality/sameMachine 拦截，
  tunnel 信号不参与过滤）；
- `deviceProperties.osVersionNumber`：真机行 `"27.2.1"`（list 输出即在）；
  watch/部分行缺失 → `info.os` 置 null。

### 合成行声明（devices-sample.json 末行）

以真机行（row 6）为底复制，仅改：`transportType:"wired"`（待 USB 复测回填的
映射值）、`tunnelState:"connected"`（在线形态）、移除
`tunnelTransportProtocol`。用途：锚定 §26.1 golden 的
`connection:"usb" + state:"online"` 映射形态与 profile usb=true/wifi=false/
simulator=false。其余 7 行来自脱敏实测，保留过滤规则使用的 platform、
reality、sameMachine 和 unsupported 等字段值。

## xctrace 行为实测（勘察 + 2026-11-16 复测）

| 行为 | 实测值 | 出处 |
| --- | --- | --- |
| 隧道冷启动 record | **exit 13** → 录制前跑 devicectl 预热（lockState）后成功率 100% | 勘察（契约 §26.4 采纳）；复测预热命令面 exit 0 |
| `--attach <pid>` | **exit 21**，`Cannot find process for provided pid: 15436` | 勘察；2026-11-16 在网真机复现同值——全链路禁 PID attach，仅进程名 |
| `--attach <进程名>`（localNetwork 在网、隧道刚建立） | **exit 19**，`Cannot find process matching name: <名>`（SpringBoard/AgentTool/Runner 均复现；xctrace `list devices` 能见设备） | 2026-11-16 复测——WiFi 传输形态下 xctrace 进程枚举不可用，**实录对拍需 USB（wired）接入**（live smoke 对该形态按环境性失败 skip） |
| `--attach` + `--time-limit 4s`（勘察通道） | 4s×2 trace 成功；export 抽到 **%CPU 10ms 粒度**与 **29.89 MiB** footprint 真值行 | 勘察（契约 §26 章头注） |
| .trace 体积 | **4s ≈ 11.7MB**（勘察）；宿主 4s 同量级（16MB）→ time-limit 受控 + export 后即删 | 勘察 + 宿主复测 |
| `export --toc` | `activity-monitor-process-live` 表在场（documentation: "Tracks the most accurate totals of process activity at a given interval in time."）；同 TOC 另有 `activity-monitor-process-ledger` / `sysmon-system` / `tick`(100ms) / `device-thermal-state-intervals` 等 | 宿主真导出复核 |
| 模板 | `xctrace list templates` 含 **Activity Monitor** 与 **Logging**（第 18 行）等 27 项 | 2026-11-16 复核 |
| record 命令面 | `--template <name>` / `--device <name|UDID>` / `--attach <pid|name>` / `--time-limit <time[ms\|s\|m\|h]>` / `--no-prompt` / `--output <path>` | record --help 实测 |
| export 命令面 | `--input <trace>` + `--toc` 或 `--xpath <expr>`（互斥两模式）+ `--output <path>` | export --help 实测 |

### activity-monitor-process-live 表结构（真导出语法，27 列）

表头 27 列 mnemonics（`<col><mnemonic>…`）：`start, process,
responsible-process, duration, pid, uid, cpu-percent, cpu-total, thread-count,
mach-port-count, memory-physical-footprint, memory-real, memory-real-private,
memory-real-shared, arch-kind, sudden-termination, sandbox, restricted,
idle-wakeups, app-nap, memory-purgeable, memory-compressed, disk-bytes-written,
disk-bytes-read, disk-bytes-written-per-second, disk-bytes-read-per-second,
preventing-sleep`。

- **单元格按列位与表头一一对应**（本机两表真导出均 1:1；单元格标签 =
  engineering-type 名，如 start→`<start-time>`、cpu-total→`<duration-on-core>`、
  memory-physical-footprint→`<size-in-bytes>`）；解析器按**列位**取值，不依赖
  单元格标签名；
- `<X id="N">raw</X>` 原始值 + `fmt` 显示串；`<Y ref="N"/>` 引用先行单元格；
  `<sentinel/>` = 该格无数据（宿主录制 cpu-percent 恒 sentinel——真设备行才出
  %CPU 值）；
- `start` 列 raw 为**纳秒**（run 起点相对时）；`memory-physical-footprint`
  raw 为**字节**（29.89 MiB 形态 = 31342592 B）。

### XML 行值声明（activity-monitor-process-live.xml）

- 表头 `<col>` 块与单元格文法：逐字取自本机真导出（同宿主 sentinel fixture 的
  文法）；
- 4 行行值（offset 0/1004/2010/3014 ms、cpu 12.34~33.3、footprint
  29.89 MiB→30.75 MiB、pid 15436 Runner）为**按勘察口径重构**的设备行形态
  （勘察未将原始设备 XML 入库；真机 USB 接入后 live 对拍可复核）；
  cpu-percent 单元格标签按 engineering-type 写作 `<system-cpu-percent>`——
  该标签名未经真设备行实录验证，解析器不依赖标签名（列位取值），标签名漂移
  不影响解析。

## 契约锚点

- 设备过滤/映射规则、golden 设备行见契约 **§26.1**。UDID 使用测试值，
  marketingName 和 osVersionNumber 保留实测值。
- 应用列表合成规则：契约 **§26.2**（bundle 目录匹配；isForeground 恒 null；
  attach 目标名 = processes[].name）；
- 指标行（`cpu.proc.xctrace.pct` / `memory.footprint.mb`，tags 恒带
  `basis="xctrace"`）：契约 **§26.3**，注册表行（app/registry.py）S2 已落地；
- 窗口批语义/锚点/清理/串行化：契约 **§26.4**；诊断语义（app.process.not_found
  / xctrace.window.failed / device.tunnel.offline / xctrace_unavailable）：
  契约 **§26.5**；错误码行（503 xctrace_unavailable，details.reason 四态）：
  契约 **§3** 新增行。
