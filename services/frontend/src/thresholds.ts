/**
 * 阈值默认值与指标 → 阈值规格映射（R31，[I§2.9]「阈值是用户可调参数，默认值可一键还原」）。
 *
 * 默认值来自统计口径建议（对齐 TDR 标准口径，契约 §12 fpsGe 缺省 18,25 同源）：
 * - FPS>=18 / FPS>=25 两档（fps 组 ge 键，[F§3.1]）；
 * - FrameTime >= 100ms（FTime 族，[F§3.2] Delta(FTime)>100ms 同一口径线）。
 *
 * R31 契约裁决（API_CONTRACT §18 注）：阈值持久化/还原无服务端端点 —— 前端本地状态，
 * 刷新还原默认值（同 [I§2.9]「参数显示设置…刷新恢复默认值」语义）。
 */

export interface ThresholdState {
  /** FPS>=x[%] 占比阈值列表（升序；作为 fpsGe 查询参数参与统计） */
  fpsGe: number[]
  /** FrameTime >= _ ms（fTime 族达标线） */
  ftimeMs: number
}

export const THRESHOLD_DEFAULTS: ThresholdState = {
  fpsGe: [18, 25],
  ftimeMs: 100,
}

/** 深拷贝默认值（调用方可就地改列表而不污染常量） */
export function defaultThresholds(): ThresholdState {
  return { fpsGe: [...THRESHOLD_DEFAULTS.fpsGe], ftimeMs: THRESHOLD_DEFAULTS.ftimeMs }
}

/** 图右上阈值输入规格（PerfDog [I§2.9]：FPS 图两档输入、FrameTime 图一档输入） */
export type ThresholdSpec = 'fps' | 'ftime'

/** 指标 → 阈值输入规格；无 TDR 口径线的指标返回 null（不渲染阈值输入） */
export function thresholdSpecOf(metricId: string): ThresholdSpec | null {
  if (metricId === 'frame.fps.mean') return 'fps'
  if (metricId.startsWith('frame.ftime.') || metricId.includes('frameTime')) return 'ftime'
  return null
}

/** 达标着色判定（R31「阈值联动数值卡达标着色」）：fps 族达到最低档阈值、fTime 族不超线 */
export function fpsMeetsThreshold(v: number | null, t: ThresholdState): boolean {
  return v !== null && v >= Math.min(...t.fpsGe)
}

export function ftimeMeetsThreshold(v: number | null, t: ThresholdState): boolean {
  return v !== null && v <= t.ftimeMs
}
