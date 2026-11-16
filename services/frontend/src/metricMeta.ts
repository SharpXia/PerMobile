/**
 * 指标展示元数据（UI 层专属：显示名/说明/配色）。
 * 口径说明与研究文档同源（DEVELOPMENT_PLAN R01-R07）：
 * - CPU 未规范化口径（R02）；内存 peak 语义（R03）；网络进程级口径（R04）；
 * - 电流取绝对值、原始符号见 tags.currentRawMa（R05 方向口径与 CM-001 的收敛
 *   待 owner 决策）；温度为 max 单值口径（R06）；GPU 机型相关能力（R07）。
 */

export interface MetricMeta {
  label: string
  desc: string
  color: string
}

const DEFAULT_COLOR = '#90a4ae'

export const METRIC_META: Record<string, MetricMeta> = {
  'frame.fps.mean': { label: 'FPS（均值）', desc: '屏幕真实刷新次数均值；Display 侧口径', color: '#4fc3f7' },
  'cpu.total.pct': { label: 'CPU 占用（整机）', desc: '整机 CPU 使用率，未规范化口径', color: '#81c784' },
  'cpu.proc.pct': { label: 'CPU 占用（进程）', desc: '目标进程 CPU 使用率，未规范化口径', color: '#66bb6a' },
  'memory.pss.peak_mb': { label: '内存 PSS', desc: 'PSS 峰值口径（tags.semantic=peak）', color: '#ffb74d' },
  'net.rx.kbps': { label: '下行流量', desc: '目标进程接收速率（进程级口径）', color: '#4dd0e1' },
  'net.tx.kbps': { label: '上行流量', desc: '目标进程发送速率（进程级口径）', color: '#ba68c8' },
  'power.current.ma': {
    label: '电流',
    desc: '取绝对值（厂商符号约定不一，CM-001）；原始带符号读数见 tags.currentRawMa',
    color: '#f06292',
  },
  'power.level.pct': {
    label: '掉电速率',
    desc: '电量差分速率 %/min（降级源；越高掉电越快）',
    color: '#aed581',
  },
  'power.energy.mah': { label: '估算能耗', desc: '单窗口能耗增量，会话总量按窗口求和（mAh，估算）', color: '#ffd54f' },
  'gpu.busy.pct': { label: 'GPU 利用率', desc: '仅部分机型可用（kgsl）；能力缺失不写 0', color: '#9575cd' },
  'thermal.max.c': { label: '温度（max）', desc: 'thermal_zone 最大温度，单值口径', color: '#ef5350' },
  /* --- M6（R16 CPU 高阶 / R17 内存明细），契约 §21.2/§21.3 注册表扩展 --- */
  'cpu.freq.mhz': {
    label: 'CPU 频率（Clock）',
    desc: '各核当前频率（/sys cpufreq），按核分曲线（tags.core），观察降频',
    color: '#a5d6a7',
  },
  'cpu.proc.normalized.pct': {
    label: 'CPU 占用（规范化）',
    desc: '规范化=未规范化×(Σ当前频率/Σ最大频率)；与未规范化口径数值不可直接对比',
    color: '#4caf50',
  },
  'cpu.proc.thread.pct': {
    label: '线程 CPU 占用',
    desc: '当前进程各线程 CPU 使用率（/proc/<pid>/task），按线程分曲线（tags.thread）',
    color: '#26a69a',
  },
  'memory.detail.native_mb': { label: 'Memory Detail · Native', desc: 'NativePss（dumpsys meminfo 明细段）', color: '#ff8a65' },
  'memory.detail.dalvik_mb': { label: 'Memory Detail · Dalvik', desc: 'Dalvik Heap + Dalvik Other', color: '#ffcc80' },
  'memory.detail.gfx_mb': { label: 'Memory Detail · Graphics', desc: 'Gfx dev + EGL mtrack（GL mtrack 单列 gl_mb）', color: '#ffe082' },
  'memory.detail.gl_mb': { label: 'Memory Detail · GL mtrack', desc: 'GL mtrack', color: '#fff59d' },
  /* --- M-iOS（S3，§23.2 模拟器双指标 + §26 真机双指标；desc 携口径声明：host/xctrace ≠ PerfDog 口径） --- */
  'cpu.proc.host.pct': {
    label: 'CPU 占用（模拟器宿主）',
    desc: '模拟器宿主口径：被测 App 进程在本机（macOS）的 CPU 占用率（ps %CPU，衰减均值，多核可 >100%）；与真机 iOS（Xcode 使用率/核数、PerfDog 未规范化口径）不可直接对比（§23.2，tags.basis=host）',
    color: '#8bc34a',
  },
  'memory.rss.mb': {
    label: '内存 RSS（模拟器宿主）',
    desc: '模拟器宿主口径：被测 App 进程在本机（macOS）的常驻内存 RSS；与 Android PSS（memory.pss.peak_mb）及真机 iOS FootPrint（[F§3.8]）口径均不同，数值不可直接对比（§23.2）',
    color: '#ffa726',
  },
  'cpu.proc.xctrace.pct': {
    label: 'CPU 占用（真机 xctrace）',
    desc: '真机 iOS 口径：xctrace（Activity Monitor 模板）目标进程 %CPU（宿主侧采集，≠ PerfDog 未规范化口径）；与 Android cpu.proc.pct 数值不可直接对比（§26）',
    color: '#43a047',
  },
  'memory.footprint.mb': {
    label: '内存 FootPrint（真机）',
    desc: '真机 iOS 口径：xctrace（Activity Monitor）physical footprint 物理足迹；与 Android PSS 及模拟器宿主 RSS（memory.rss.mb）口径均不同，数值不可直接对比（§26，[F§3.8]）',
    color: '#ff9800',
  },
}

export function metricMeta(metricId: string): MetricMeta {
  return METRIC_META[metricId] ?? { label: metricId, desc: '', color: DEFAULT_COLOR }
}

/**
 * 带标签序列的 series 键（M6）：per-core / per-thread 指标一个 metricId 出多条曲线，
 * store 以 `metricId@tagValue` 区分；本函数剥离标签回查 metricId 级元数据。
 */
export function baseMetricId(seriesKey: string): string {
  const at = seriesKey.indexOf('@')
  return at === -1 ? seriesKey : seriesKey.slice(0, at)
}

/** PerfDog 默认页对标（[I§2.1]：默认展示 FPS / CPU Usage / Temperature） */
export const DEFAULT_SELECTED_METRICS = ['frame.fps.mean', 'cpu.total.pct', 'thermal.max.c']
