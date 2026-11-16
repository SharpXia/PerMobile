<script setup lang="ts">
import { computed, ref } from 'vue'
import type { LogEntry } from '../store'
import { useStore } from '../store'

/**
 * 日志面板（R33，对标 [I§2.1]/[I§2.4] 底部 ☐Log 复选 + Log level 下拉 + 日志流）：
 * - 复选展开/收起（勾选语义：未开启日志流的会话不产生设备日志——§21.7 缺省不发 log）；
 * - Log level 下拉过滤（Verbose…Error，客户端按最低级别过滤显示；会话级采集门槛由
 *   创建时 logLevel 决定，§21.7）；
 * - crash / ANR 行高亮（FATAL EXCEPTION / ANR in 行内容识别 + error 级着色）。
 */
const store = useStore()

const LEVELS = ['verbose', 'debug', 'info', 'warn', 'error'] as const
const minLevel = ref<string>('verbose')

const lines = computed<LogEntry[]>(() => {
  const min = LEVELS.indexOf(minLevel.value as (typeof LEVELS)[number])
  const filtered = store.state.logs.filter((l) => {
    if (l.kind !== 'log') return true // phase/diagnostic/session/system 不参与 level 过滤
    const idx = LEVELS.indexOf((l.level === '' ? 'info' : l.level) as (typeof LEVELS)[number])
    return idx === -1 ? true : idx >= min
  })
  return [...filtered].reverse().slice(0, 160)
})

/** crash / ANR 高亮判定（按行内容识别，与 level 无关的强信号） */
function isFatal(l: LogEntry): boolean {
  return l.kind === 'log' && (l.text.includes('FATAL EXCEPTION') || l.text.includes('ANR in'))
}

function levelCls(l: LogEntry): string {
  if (isFatal(l)) return 'fatal'
  if (l.kind === 'log') return `log-${l.level}`
  return `kind-${l.kind}`
}

function levelTag(l: LogEntry): string {
  if (l.kind !== 'log') return { phase: 'PHASE', diagnostic: 'DIAG', session: 'SESS', system: 'SYS' }[l.kind] ?? ''
  return l.level.toUpperCase()
}

function fmtTs(ts: number): string {
  const d = new Date(ts)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function toggleLog(ev: Event): void {
  store.state.logPanelOpen = (ev.target as HTMLInputElement).checked
}
</script>

<template>
  <div class="log-panel">
    <div class="log-head">
      <label style="display: flex; align-items: center; gap: 5px; cursor: pointer">
        <input type="checkbox" :checked="store.state.logPanelOpen" @change="toggleLog" />
        <span style="font-size: 12px">Log</span>
      </label>
      <!-- R33：Log level 下拉（Verbose…Error；过滤显示，采集门槛由会话 logLevel 决定） -->
      <label v-if="store.state.logPanelOpen" class="lp-level" title="Log level：过滤显示的最低级别（会话采集门槛由创建时 logLevel 决定，§21.7）">
        Log level
        <select v-model="minLevel">
          <option v-for="l in LEVELS" :key="l" :value="l">{{ l[0]!.toUpperCase() + l.slice(1) }}</option>
        </select>
      </label>
      <span class="chip gray">设备日志流（R33）+ phase/diagnostic 事件</span>
      <span style="flex: 1"></span>
      <span class="hint">红底行 = crash / ANR（行内容识别高亮）</span>
      <button class="ghost" style="padding: 1px 8px; font-size: 11px" @click="store.clearLogs()">清空</button>
    </div>
    <div v-if="store.state.logPanelOpen" class="log-body">
      <div v-for="(l, i) in lines" :key="`${l.ts}-${i}`" class="log-line" :class="levelCls(l)">
        <span class="lt">{{ fmtTs(l.ts) }}</span>
        <span class="lv">{{ levelTag(l) }}</span>
        <span v-if="l.source" class="ls">[{{ l.source }}]</span>
        <span>{{ l.text }}</span>
      </div>
      <div v-if="lines.length === 0" class="empty-hint" style="padding: 4px 0">
        暂无日志 —— 会话创建时选择 logLevel 才开启实时日志流（§21.7；未开启不发 log 消息）
      </div>
    </div>
  </div>
</template>

<style scoped>
.lp-level {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-dim);
}

.lp-level select {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  font-size: 11px;
  padding: 1px 4px;
}

.log-line {
  display: flex;
  gap: 6px;
  align-items: baseline;
}

.log-line .lv {
  flex: none;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.4px;
  color: var(--text-faint);
  width: 52px;
}

.log-line .ls {
  color: #79b8ff;
  flex: none;
}

/* R33：级别着色 + crash/ANR 行高亮 */
.log-line.log-error .lv,
.log-line.kind-diagnostic.level-error .lv {
  color: var(--danger);
}

.log-line.log-warn .lv,
.log-line.kind-diagnostic.level-warn .lv {
  color: var(--warn);
}

.log-line.log-debug .lv {
  color: #8b949e;
}

.log-line.log-verbose .lv {
  color: var(--text-faint);
}

.log-line.fatal {
  background: rgba(229, 83, 75, 0.16);
  border-left: 2px solid var(--danger);
  padding-left: 4px;
}

.log-line.fatal .lv {
  color: #ff7b72;
}
</style>
