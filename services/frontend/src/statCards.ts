/**
 * 统计量数值卡元数据（R41，UI 层专属：显示名/单位/tooltip 口径说明）。
 *
 * FPS Tab 的 ~20 卡矩阵对标 [I§2.9] Web Case 报告页 FPS Tab 两行大数字卡 +
 * [I§4.4] 汇总层数值卡体系；tooltip 口径说明逐条对应契约 §12「字段单位与口径」表
 * （依据 research/perfdog-features.md [F§3.1-3.5]）。
 *
 * 口径声明规则（契约 §12 计算规则 1/4）：
 * - Smooth 为近似口径卡（approx），tooltip 恒带 note 原文，数值与 PerfDog 不可直接对比；
 * - 组不可计算时数值字段为 null（不填假零值），卡片置灰、tooltip 携带组级 note 原因。
 */

import type { Statistics } from './api-client'
import { fpsMeetsThreshold, ftimeMeetsThreshold, type ThresholdState } from './thresholds'

export interface StatCardDef {
  key: string
  label: string
  unit: string
  /** tooltip 口径说明（hover 展示） */
  tooltip: string
  /** 近似口径卡：显示「近似口径」角标（§12 规则 4：报告侧必须携带 note 原文） */
  approx?: boolean
  read: (s: Statistics) => number | null
}

/** FPS 族（[F§3.1]）——FPS Tab 第一行 */
export const FPS_FAMILY_CARDS: StatCardDef[] = [
  { key: 'fps.avg', label: 'Avg(FPS)', unit: 'fps', tooltip: 'fps 点列的均值 [F§3.1]', read: (s) => s.fps.avg },
  { key: 'fps.std', label: 'Std(FPS)', unit: 'fps', tooltip: 'fps 点列标准差（总体口径）[F§3.1]', read: (s) => s.fps.std },
  { key: 'fps.var', label: 'Var(FPS)', unit: 'fps²', tooltip: 'fps 点列方差（总体口径）[F§3.1]', read: (s) => s.fps.var },
  {
    key: 'fps.drop',
    label: 'Drop(FPS)',
    unit: '/h',
    tooltip: '相邻两个 FPS 点下降 >8 帧的次数 × 3600000 / durationMs [F§3.1]',
    read: (s) => s.fps.drop,
  },
  { key: 'fps.min', label: 'Min(FPS)', unit: 'fps', tooltip: 'fps 点列最小值 [F§3.1]', read: (s) => s.fps.min },
  { key: 'fps.median', label: 'Median', unit: 'fps', tooltip: 'fps 点列中位数 [F§3.1]', read: (s) => s.fps.median },
  {
    key: 'fps.medRange',
    label: 'MedRange',
    unit: '%',
    tooltip: '偏离中位数 ±20% 以内点的占比 [F§3.1]',
    read: (s) => s.fps.medRange,
  },
]

/** Jank 族（[F§3.3][F§3.4]）+ Smooth/1%Low（[F§3.5]）——FPS Tab 第二行 */
export const JANK_FAMILY_CARDS: StatCardDef[] = [
  {
    key: 'smooth.value',
    label: 'Smooth(稳帧指数)',
    unit: '',
    // note 原文随响应携带（§12 规则 4）；tooltip 追加官方建议阈值方向说明
    tooltip: '近似口径：官方未公开算法公式，数值与 PerfDog 不可直接对比（[F§3.5][F§10.1]）；方向与官方建议阈值一致——越低越平稳：游戏 <8、应用滑动 <20',
    approx: true,
    read: (s) => s.smooth.value,
  },
  {
    key: 'lowFps.p1',
    label: '1%Low(FPS)',
    unit: 'fps',
    tooltip: '帧耗时最长 1% 的均值换算 fps [F§3.5]（可精确复刻）',
    read: (s) => s.lowFps.p1,
  },
  {
    key: 'jank.small',
    label: 'SmallJank',
    unit: '次',
    tooltip: '双条件判定：帧耗时 > 前三帧平均×2 且 > 1000/24 ms（≈41.66ms）[F§3.3]',
    read: (s) => s.jank.small,
  },
  {
    key: 'jank.big',
    label: 'BigJank',
    unit: '次',
    tooltip: '双条件判定：帧耗时 > 前三帧平均×2 且 > 125ms（1000/24×3）[F§3.3]',
    read: (s) => s.jank.big,
  },
  {
    key: 'jank.jank',
    label: 'Jank',
    unit: '次',
    tooltip: '双条件判定：帧耗时 > 前三帧平均×2 且 > 83.33ms（1000/24×2）[F§3.3]',
    read: (s) => s.jank.jank,
  },
  {
    key: 'jank.stutter',
    label: 'Stutter',
    unit: '%',
    tooltip: 'Σ Jank 帧耗时 / durationMs × 100 [F§3.4]',
    read: (s) => s.jank.stutter,
  },
  {
    key: 'jank.per10min',
    label: 'Jank(/10min)',
    unit: '/10min',
    tooltip: 'Jank 次数 × 600000 / durationMs [F§3.3]（BigJank(/10min) 本期不单列）',
    read: (s) => s.jank.per10min,
  },
]

/** FTime 族（[F§3.2]）——FPS Tab 第三行 */
export const FTIME_FAMILY_CARDS: StatCardDef[] = [
  { key: 'frameTime.avg', label: 'Avg(FTime)', unit: 'ms', tooltip: '逐帧耗时均值 [F§3.2]', read: (s) => s.frameTime.avg },
  { key: 'frameTime.std', label: 'Std(FTime)', unit: 'ms', tooltip: '逐帧耗时标准差 [F§3.2]', read: (s) => s.frameTime.std },
  { key: 'frameTime.var', label: 'Var(FTime)', unit: 'ms²', tooltip: '逐帧耗时方差 [F§3.2]', read: (s) => s.frameTime.var },
  {
    key: 'frameTime.delta',
    label: 'Delta(FTime)',
    unit: '/h',
    tooltip: '相邻两帧时间差 >100ms 的次数 × 3600000 / durationMs（Delta(FTime)）[F§3.2]',
    read: (s) => s.frameTime.delta,
  },
]

/** FPS>=x[%] 阈值卡（[F§3.1]；阈值来自响应 ge 键，缺省 18/25 对齐 TDR 口径） */
export function fpsGeCards(s: Statistics): StatCardDef[] {
  return Object.keys(s.fps.ge)
    .sort((a, b) => Number(a) - Number(b))
    .map((t) => ({
      key: `fps.ge.${t}`,
      label: `FPS>=${t}`,
      unit: '%',
      tooltip: `FPS ≥ ${t} 的点占比（阈值由 fpsGe 查询参数定，缺省 18,25 对齐 TDR 口径）[F§3.1]`,
      read: (st: Statistics) => st.fps.ge[t] ?? null,
    }))
}

/** FPS Tab 全部 ~20 卡：FPS 族 + ge 阈值卡 + Jank 族 + FTime 族（[I§2.9] 两行大数字卡） */
export function fpsTabCards(s: Statistics): StatCardDef[] {
  return [...FPS_FAMILY_CARDS, ...fpsGeCards(s), ...JANK_FAMILY_CARDS, ...FTIME_FAMILY_CARDS]
}

/** 组级 note（组不可计算时说明原因，随组级 note 原文进卡片 tooltip） */
export function groupNote(s: Statistics, card: StatCardDef): string | undefined {
  if (card.key.startsWith('fps.')) return s.fps.note
  if (card.key.startsWith('frameTime.')) return s.frameTime.note
  if (card.key.startsWith('jank.')) return s.jank.note
  if (card.key.startsWith('smooth.')) return s.smooth.note
  if (card.key.startsWith('lowFps.')) return s.lowFps.note
  return undefined
}

/**
 * R31 阈值联动着色（[I§2.9] 阈值输入 + 图右上；口径可判定才着色，null 不着色）：
 * - fps.family（Avg/Min/Median/1%Low/ge 占比）：值 ≥ 最低档阈值（缺省 18）= good；
 *   ge 占比卡满分 100% 单独给 good、>0 未满给 warn（阈值未全达）；
 * - frameTime.family（Avg/Std）：值 ≤ ftime 阈值（缺省 100ms）= good，超线 = bad；
 * - 其余卡（Var/Drop/Smooth/Jank 族无 TDR 口径线）不参与着色 → undefined。
 */
export function cardTone(card: StatCardDef, value: number | null, t: ThresholdState): 'good' | 'warn' | 'bad' | undefined {
  if (value === null || !Number.isFinite(value)) return undefined
  if (card.key.startsWith('fps.ge.')) {
    if (value >= 100) return 'good'
    return value > 0 ? 'warn' : 'bad'
  }
  if (
    card.key === 'fps.avg' ||
    card.key === 'fps.min' ||
    card.key === 'fps.median' ||
    card.key === 'lowFps.p1'
  ) {
    return fpsMeetsThreshold(value, t) ? 'good' : 'bad'
  }
  if (card.key === 'frameTime.avg') {
    return ftimeMeetsThreshold(value, t) ? 'good' : 'bad'
  }
  return undefined
}
