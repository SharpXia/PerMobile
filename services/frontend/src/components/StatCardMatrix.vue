<script lang="ts">
/** 数值卡视图模型（value 由页面格式化为字符串；null → '—' 置灰） */
export interface StatCardVM {
  key: string
  label: string
  unit: string
  value: string
  tooltip: string
  approx?: boolean
  /** 数值不可计算（null）→ 置灰 */
  dim?: boolean
  /** R31 阈值达标着色（good=达标 / warn=占比未满 / bad=超线；statCards.cardTone 判定） */
  tone?: 'good' | 'warn' | 'bad'
}
</script>

<script setup lang="ts">
/**
 * 数值卡矩阵（R41，[I§2.9]/[I§4.4] 汇总层）：
 * - 大数字卡 + hover tooltip（口径说明，data-tip 自绘 tooltip）；
 * - 近似口径卡（Smooth）带「近似口径」角标，note 原文由页面在矩阵下方可见地携带；
 * - null 值卡置灰显示 —（不填假零值，[R§8.6]），tooltip 携带组级 note 说明原因；
 * - R31：tone 达标着色（阈值输入联动，statCards.cardTone 判定；无 TDR 口径线的卡不着色）。
 */
withDefaults(defineProps<{ cards: StatCardVM[]; noteLines?: string[] }>(), { noteLines: () => [] })
</script>

<template>
  <div>
    <div class="stat-matrix">
      <div
        v-for="c in cards"
        :key="c.key"
        class="stat-card"
        :class="{ dim: c.dim, good: c.tone === 'good', warn: c.tone === 'warn', bad: c.tone === 'bad' }"
        :data-tip="c.tooltip"
      >
        <div class="sc-label">
          {{ c.label }}
          <span v-if="c.approx" class="sc-approx" :data-tip="c.tooltip">近似口径</span>
        </div>
        <div class="sc-value">
          {{ c.value }}<span v-if="c.unit && c.value !== '—'" class="unit">{{ c.unit }}</span>
        </div>
      </div>
    </div>
    <div v-if="noteLines.length > 0" class="stat-notes">
      <div v-for="(n, i) in noteLines" :key="i" class="stat-note">※ {{ n }}</div>
    </div>
  </div>
</template>

<style scoped>
.stat-matrix {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: 8px;
}

.stat-card {
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
  position: relative;
}

.stat-card.dim {
  opacity: 0.55;
}

/* R31 阈值达标着色（good=绿 / warn=橙 / bad=红；口径可判定才着色） */
.stat-card.good {
  border-color: rgba(63, 185, 80, 0.55);
}

.stat-card.good .sc-value {
  color: #3fb950;
}

.stat-card.warn {
  border-color: rgba(227, 179, 65, 0.55);
}

.stat-card.warn .sc-value {
  color: #e3b341;
}

.stat-card.bad {
  border-color: rgba(229, 83, 75, 0.55);
}

.stat-card.bad .sc-value {
  color: #e5534b;
}

.sc-label {
  font-size: 11px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: 5px;
}

.sc-approx {
  flex: none;
  font-size: 9.5px;
  padding: 0 5px;
  border-radius: 8px;
  border: 1px solid rgba(227, 179, 65, 0.5);
  color: #e3b341;
  background: rgba(210, 153, 34, 0.14);
  cursor: help;
}

.sc-value {
  font-size: 21px;
  font-weight: 650;
  margin-top: 3px;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.sc-value .unit {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-dim);
  margin-left: 3px;
}

/* hover tooltip：data-tip 自绘（原生 title 延迟大且不换行） */
.stat-card::after,
.sc-approx::after {
  content: attr(data-tip);
  position: absolute;
  left: 6px;
  top: calc(100% + 5px);
  z-index: 30;
  width: max-content;
  max-width: 320px;
  background: #1a222d;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 11px;
  font-weight: 400;
  line-height: 1.5;
  padding: 6px 9px;
  white-space: normal;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.1s;
}

.stat-card:hover::after,
.sc-approx:hover::after {
  opacity: 1;
}

.stat-notes {
  margin-top: 8px;
}

.stat-note {
  font-size: 11px;
  color: var(--text-dim);
  line-height: 1.6;
}
</style>
