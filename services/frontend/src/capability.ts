/**
 * 能力门禁判定（M6，R21/R26，契约 §20.3）：
 *
 * 勾选面板可用性 = 指标 capabilities ∩ 设备 profile 的交集判定 ——
 * 1. capabilities.platform 与设备 platform 一致；且
 * 2. capabilities.modes 与 profile 中为 true 的连接模式交集非空；且
 * 3. 设备资源满足硬性依赖（gpu.* 要求 profile.gpuSource 非空）。
 *
 * 三条全过 → 可勾选；任一不过 → 禁勾 + reason 映射文案（[I§6]「指标行随模式/平台
 * 动态增减」）。服务端 §5 available 仍是权威判定 —— usable = 本地判定 ∩ 服务端 available，
 * POST /api/sessions 落点时仍受 metric_unavailable（§3）拦截（两端不漂移的兜底）。
 */

import type { ConnectionMode, Device, MetricDef } from './api-client'

/** §20.1 reason 码 → 人工文案映射表（对齐 §3「原文透传、前端映射」先例） */
const CAPABILITY_REASON_TEXT: Record<string, string> = {
  kgsl_missing: '设备无 kgsl GPU 源（profile.gpuSource=null），GPU 利用率不可用',
  power_wifi_only: 'Battery Power 类指标仅 WiFi 模式有意义（USB 连线在充电，测功率无意义）',
  not_landed: '采集通道在当前模式尚未落地（available=false，不写假零值）',
  platform_unsupported: '平台不支持（Android 指标在 iOS 设备 / iOS 指标在 Android 设备均不可用，§20.3/§23.0）',
  power_sysfs_blocked: '设备屏蔽了 /sys/class/power_supply 电池节点（厂商 SELinux 策略），功耗指标在该设备无数据源（2026-11-03 实机验证新增）',
}

/** reason 码 → 文案（未注册码原样透传，不臆测语义） */
export function capabilityReasonText(code: string): string {
  return CAPABILITY_REASON_TEXT[code] ?? `能力缺失（reason=${code}）`
}

export interface MetricUsability {
  /** 本地 ∩ 判定 + 服务端 available 的合成结论 */
  usable: boolean
  /** 服务端注册表 available（权威判定） */
  serverAvailable: boolean
  /** 本地 ∩ 判定（capabilities ∩ profile）结论 */
  gateOk: boolean
  /** 禁勾原因（人工文案，多因素时逐条列出；reason 码优先，未注册码原样透传） */
  reasons: string[]
}

/** 单指标 × 单设备的可用性判定（纯函数；设备 profile 缺省按「无约束」处理，§11.1） */
export function metricUsability(def: MetricDef, device: Device | null): MetricUsability {
  const reasons: string[] = []
  const caps = def.capabilities
  const profile = device?.profile

  // 条件 1：平台一致（capabilities.platform 缺省视为无约束）
  if (caps?.platform && device && caps.platform !== device.platform) {
    reasons.push(capabilityReasonText('platform_unsupported'))
  }

  // 条件 2：modes ∩ profile 可用连接模式 交集非空（§23.0 M-iOS：simulator 第三模式）
  const modeFlags: Array<{ mode: 'usb' | 'wifi' | 'simulator'; on: boolean | undefined }> = [
    { mode: 'usb', on: profile?.usb },
    { mode: 'wifi', on: profile?.wifi },
    { mode: 'simulator', on: profile?.simulator },
  ]
  const declared = caps?.modes
  if (declared && declared.length > 0) {
    const known = modeFlags.filter((f) => f.on === true).map((f) => f.mode)
    const usableModes = profile ? known : declared // profile 未下发时不做模式排除（缺省无约束）
    if (profile && usableModes.length > 0 && !declared.some((m) => usableModes.includes(m))) {
      reasons.push(`该指标仅在 ${declared.join('/')} 模式有意义；当前设备可用连接模式为 ${usableModes.join('/') || '无'}`)
    }
    if (profile && usableModes.length === 0) {
      reasons.push('设备未声明任何可用连接模式（profile.usb/wifi/simulator 均为 false）')
    }
  }

  // 条件 3：硬性资源依赖（gpu.* 要求 profile.gpuSource 非空；§20.1 kgsl_missing）
  if (def.metricId.startsWith('gpu.') && device && !device.profile?.gpuSource) {
    reasons.push(capabilityReasonText('kgsl_missing'))
  }

  // reason 码映射（仅在服务端判不可用时携带；未注册码原样透传）
  if (!def.available && caps?.reason) {
    reasons.push(capabilityReasonText(caps.reason))
  }

  const gateOk = reasons.length === 0
  return {
    usable: gateOk && def.available,
    serverAvailable: def.available,
    gateOk,
    reasons,
  }
}

/* ---------------- iOS 双指标常量（S3；metric id 以 S2 契约冻结为准，本流只引用） ---------------- */

/** §23.2（M-iOS）：iOS Simulator 宿主口径双指标（modes=["simulator"]，tags.basis="host"） */
export const IOS_SIMULATOR_METRICS: string[] = ['cpu.proc.host.pct', 'memory.rss.mb']

/** §26（M-iOS 真机切片，S2 契约冻结）：iOS 真机 xctrace 口径双指标（modes=["usb","wifi"]；
 * Activity Monitor 模板 %CPU + physical footprint，宿主侧采集 ≠ PerfDog 口径） */
export const IOS_REAL_METRICS: string[] = ['cpu.proc.xctrace.pct', 'memory.footprint.mb']

/** Android 矩阵卡既有默认采集集（对标手机悬浮窗 FPS/CPU/MEM 口径 [I§2.4]，保持不变） */
export const ANDROID_MATRIX_METRICS: string[] = ['frame.fps.mean', 'cpu.proc.pct', 'memory.pss.peak_mb']

/** M6 勾选组（[I§2.4] 指标面板分组渲染；CPU 高阶 / Memory Detail 按组入面板） */
export const METRIC_GROUPS: Array<{ id: string; name: string; prefix?: string; metricIds?: string[]; note?: string }> = [
  { id: 'frame', name: '帧', metricIds: ['frame.fps.mean'] },
  { id: 'cpu-basic', name: 'CPU 基础', metricIds: ['cpu.total.pct', 'cpu.proc.pct'] },
  {
    id: 'cpu-adv',
    name: 'CPU 高阶（R16）',
    metricIds: ['cpu.freq.mhz', 'cpu.proc.normalized.pct', 'cpu.proc.thread.pct'],
    note: 'Clock 各核频率（观察降频）/ 规范化口径 / 线程级占用；线程级需 /proc/<pid>/task 遍历，采集开销须评估',
  },
  { id: 'memory-basic', name: '内存', metricIds: ['memory.pss.peak_mb'] },
  {
    id: 'memory-detail',
    name: 'Memory Detail（R17）',
    metricIds: ['memory.detail.native_mb', 'memory.detail.dalvik_mb', 'memory.detail.gfx_mb', 'memory.detail.gl_mb'],
    // 官方损耗提示文案随组渲染（§21.3 description；R26 开销提示联动）
    note: '官方提示：极限测试（超高帧率）建议不勾选 Memory Detail，部分低端机型有性能损耗（[F§3.8][I§2.4]）',
  },
  { id: 'gpu', name: 'GPU', metricIds: ['gpu.busy.pct'] },
  { id: 'net', name: '网络', metricIds: ['net.rx.kbps', 'net.tx.kbps'] },
  { id: 'power', name: '功耗', metricIds: ['power.current.ma', 'power.level.pct', 'power.energy.mah'] },
  { id: 'thermal', name: '温度', metricIds: ['thermal.max.c'] },
  // §23.2（M-iOS）：iOS Simulator 宿主口径组（tags.basis=host，数值与真机 iOS 不可直接对比）
  {
    id: 'ios-simulator',
    name: 'iOS Simulator（宿主口径）',
    metricIds: IOS_SIMULATOR_METRICS,
    note: '模拟器宿主口径：App 进程在本机（macOS）的 CPU/RSS；与真机 iOS 口径不可直接对比（§23.2）',
  },
  // §26（M-iOS 真机切片，S2 契约冻结）：iOS 真机 xctrace 口径组（modes usb/wifi，
  // 与注册行 capabilities.modes 同构；desc 携带「≠ PerfDog 口径」声明）
  {
    id: 'ios-real',
    name: 'iOS 真机（xctrace 口径）',
    metricIds: IOS_REAL_METRICS,
    note: '真机 iOS 口径：xctrace（Activity Monitor）进程 CPU / physical footprint；宿主侧采集口径，与 PerfDog 及 Android 指标不可直接对比（§26）',
  },
]

/** 未归入任何组的 metricId 兜底组（新指标先可用再归类，不在面板上消失） */
export function ungroupedMetricIds(defs: MetricDef[]): string[] {
  const grouped = new Set<string>()
  for (const g of METRIC_GROUPS) {
    for (const id of g.metricIds ?? []) grouped.add(id)
    if (g.prefix) for (const d of defs) if (d.metricId.startsWith(g.prefix)) grouped.add(d.metricId)
  }
  return defs.map((d) => d.metricId).filter((id) => !grouped.has(id))
}

/* ---------------- 设备矩阵交互面纯函数（S3 收口；零 Vue 依赖，check:matrix 直测） ---------------- *
 *
 * 背景（S3 施工流）：设备矩阵卡此前把 Android 形态硬编码在模板里 ——
 * 1. connection 图标只认 usb/wifi，simulator 行落 else 误显 📎 +「USB 模式设备行」；
 * 2. 平台图标恒 🤖，iOS 设备行错挂 Android 图标；
 * 3. ⋮ 菜单恒渲染 WiFi 两项（real-only adb 能力）—— iOS 真机卡片可点进
 *    wifi-mode/speedtest 的 real-only duck-call 路径（§21.1/§24.3）；
 * 4. 矩阵卡默认采集集与迷你读数恒 Android 三指标 —— iOS 设备点「开会话」携带
 *    platform_unsupported 指标被 session_manager metric_unavailable 拒绝（§20.3 落点）。
 * 全部收敛为下面的纯函数（结构化入参，不依赖完整 Device），组件只做绑定；
 * 行为断言由 scripts/check-frontend-ios-matrix.mjs（npm run check:matrix）承载。
 */

/** 交互面纯函数的结构化入参（§11.1 消费面最小字段；完整 Device 满足该形态） */
export type DeviceLike = Pick<Device, 'platform' | 'connection' | 'state'>

/** 平台 → 图标（[I§2.2] 平台分组形态；未知平台回退通用手机图标） */
export function platformIcon(platform: string): string {
  if (platform === 'ios') return '🍎'
  if (platform === 'android') return '🤖'
  if (platform === 'windows') return '🪟'
  return '📱'
}

/** 连接模式图标（[I§2.2]：回形针=USB 行，扇形=WiFi 行；S3 补 🖥️=Simulator 宿主行，§23.0） */
export function connIcon(d: Pick<DeviceLike, 'connection'>): string {
  if (d.connection === 'wifi') return '📶'
  if (d.connection === 'simulator') return '🖥️'
  return '📎'
}

/** 连接模式 tooltip（同上三分支；simulator 行如实声明宿主口径差异，§23.2） */
export function connTitle(d: Pick<DeviceLike, 'connection'>): string {
  if (d.connection === 'wifi') return 'WiFi 模式设备行（功率测试仅 WiFi 模式有意义 [F§4]）'
  if (d.connection === 'simulator') return 'iOS Simulator 设备行（宿主口径，§23.2；数值与真机/PerfDog 不可直接对比）'
  return 'USB 模式设备行（截图/深度分析等 USB 专属能力可用）'
}

/** ⋮ 菜单项描述（kind 驱动组件分发到对应动作；icon/label/title 纯展示） */
export interface DeviceMenuItem {
  kind: 'wifi-enable' | 'wifi-connect' | 'sim-boot'
  icon: string
  label: string
  title: string
  /** 整菜单共享的底部说明文案（组件取首项渲染一次） */
  note: string
}

const WIFI_MENU_NOTE = 'WiFi 模式才能测功率（[F§4]）；synthetic 模式下两操作均返回 501 + 不可用诊断。'
const WIFI_MENU_ITEMS: DeviceMenuItem[] = [
  {
    kind: 'wifi-enable',
    icon: '📶',
    label: 'WiFi 模式：启用 tcpip（enable）',
    title: 'adb tcpip 5555：重启 adbd 于 TCP 模式（real-only）',
    note: WIFI_MENU_NOTE,
  },
  {
    kind: 'wifi-connect',
    icon: '📶',
    label: 'WiFi 模式：连接设备（connect…）',
    title: 'adb connect host:5555（real-only；synthetic 不可用）',
    note: WIFI_MENU_NOTE,
  },
]

const SIM_BOOT_MENU_ITEM: DeviceMenuItem = {
  kind: 'sim-boot',
  icon: '🚀',
  label: '启动模拟器',
  title: 'simctl boot + bootstatus -b（§23.1 simulator-only；已 Booted 幂等返回，上限 ~180s）',
  note: 'sim-boot 为 simulator-only 能力（xcrun simctl）；启动成功后设备行自动转在线（§23.1）。',
}

/**
 * ⋮ 操作菜单按设备分流（S3，500 穿透预防的 UI 侧）：
 * - simulator：offline → 仅「启动模拟器」（§23.1 sim-boot；online 无操作）；
 * - Android usb/wifi：WiFi 两项（§21.1 enable/connect，real-only，synthetic 501 诊断反馈）；
 * - iOS 真机（usb/wifi）与其余平台：空 —— WiFi 模式是 adb 专属，对 iOS 设备只会
 *   打到 wifi-mode/speedtest 的 real-only duck-call 路径（devices.py/speedtest.py），
 *   与 sim-boot 无关，隐藏入口而非靠 501 报错兜底。
 */
export function menuItemsFor(d: DeviceLike): DeviceMenuItem[] {
  if (d.connection === 'simulator') {
    return d.state === 'offline' ? [SIM_BOOT_MENU_ITEM] : []
  }
  if (d.platform === 'android' && (d.connection === 'usb' || d.connection === 'wifi')) {
    return WIFI_MENU_ITEMS
  }
  return []
}

/** 矩阵卡迷你读数行（key=展示键，metricId=读数来源，suffix=单位，title=口径提示） */
export interface MiniReadoutRow {
  key: 'FPS' | 'CPU' | 'MEM'
  metricId: string
  suffix: string
  title: string
}

/**
 * 矩阵卡迷你读数按设备分流（S3）：
 * - Android（usb/wifi）：既有 FPS/CPU/MEM 三行不变；
 * - iOS Simulator：CPU/MEM 换宿主口径双指标（cpu.proc.host.pct / memory.rss.mb，§23.2）；
 * - iOS 真机：CPU/MEM 换 xctrace 口径双指标（cpu.proc.xctrace.pct / memory.footprint.mb，§26）；
 * - FPS 行对 iOS 平台一律不渲染（§23.2：模拟器无逐帧公开 API，真机 FPS 本期不接线
 *   —— 恒无数据，不出行不造数）。
 */
export function miniReadoutFor(d: DeviceLike): MiniReadoutRow[] {
  if (d.platform === 'ios') {
    const rows =
      d.connection === 'simulator'
        ? [
            { key: 'CPU' as const, metricId: 'cpu.proc.host.pct', suffix: '%', title: '进程 CPU（cpu.proc.host.pct，模拟器宿主口径 §23.2）' },
            { key: 'MEM' as const, metricId: 'memory.rss.mb', suffix: 'MB', title: '宿主 RSS（memory.rss.mb，模拟器宿主口径 §23.2）' },
          ]
        : [
            { key: 'CPU' as const, metricId: 'cpu.proc.xctrace.pct', suffix: '%', title: '进程 CPU（cpu.proc.xctrace.pct，xctrace 口径 §26）' },
            { key: 'MEM' as const, metricId: 'memory.footprint.mb', suffix: 'MB', title: '物理足迹（memory.footprint.mb，xctrace 口径 §26）' },
          ]
    return rows
  }
  return [
    { key: 'FPS', metricId: 'frame.fps.mean', suffix: '', title: 'FPS（frame.fps.mean）' },
    { key: 'CPU', metricId: 'cpu.proc.pct', suffix: '%', title: '进程 CPU（cpu.proc.pct）' },
    { key: 'MEM', metricId: 'memory.pss.peak_mb', suffix: 'MB', title: '内存 PSS（memory.pss.peak_mb）' },
  ]
}

/**
 * 平台 → 缺省指标集（S3：MATRIX_DEFAULT_METRICS 的平台分流载体）：
 * - android：既有 FPS/CPU/MEM 三指标（MATRIX_DEFAULT_METRICS 原样迁移，§20.4）；
 * - ios：对应双指标 —— 缺省（simulator/未指明连接）取 §23.2 宿主口径双指标，
 *   真机连接（usb/wifi）取 §26 xctrace 口径双指标；
 * - 其余/未知平台：回退 Android 三指标（保持分流前的既有行为，不新造语义）。
 * 「缺省」仅是 UI 预填 —— 落点仍受服务端 metric_unavailable 拦截（§20.3 兜底）。
 */
export function platformDefaultMetrics(platform: string, connection?: ConnectionMode): string[] {
  if (platform === 'ios') {
    return connection === 'usb' || connection === 'wifi' ? [...IOS_REAL_METRICS] : [...IOS_SIMULATOR_METRICS]
  }
  return [...ANDROID_MATRIX_METRICS]
}
