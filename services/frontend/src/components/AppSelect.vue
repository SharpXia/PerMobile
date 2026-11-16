<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { AppEntry, ProcessEntry } from '../api-client'
import { useStore } from '../store'

/**
 * 应用选择器（R14 + R20，对标 [I§2.3]）：
 * - 运行状态「颜色 + 文字」双通道（官方标注：深色=正在运行顶层进程（前台），
 *   浅色=后台正在运行进程——不依赖单一视觉信号）；
 * - 大型应用展开子进程列表（R20，§21.6 processes[]）：可单独选中主/子进程作为
 *   采集目标（默认主进程；子进程随 POST /api/sessions processName 下发）；
 * - 下拉展开即刷新应用/进程列表（UI 反馈 #3：进程态随开关拉最新，不依赖换设备）；
 *   长名（应用名/包名/进程名）悬停 title 展示全名。
 */
const store = useStore()
const open = ref(false)
const rootEl = ref<HTMLDivElement>()
/** 已展开子进程列表的应用包名集合 */
const expanded = ref<Set<string>>(new Set())
/** 模糊搜索（实机验证修复：86+ 三方包无检索找不到目标应用；多关键词空格分隔取交集） */
const search = ref('')

defineProps<{ disabled?: boolean }>()

// 展开即刷新（静默拉最新进程态；失败走 store.setError 如实反馈，不打断展开）
watch(open, (v) => {
  if (v) void store.refreshApps()
})

/** 悬浮气泡（UI 反馈 #3 强化）：行内文字截断（运行态 chip 挤压成「CO…」），原生
 *  title 有系统级延迟且形态不可控——自绘即时气泡：应用行=名称+包名两行、子进程行=
 *  完整进程名（pid）。跟随行位置（fixed 定位，pointer-events:none 不挡行）。 */
const tip = ref<{ text: string; top: number; left: number } | null>(null)

function showRowTip(ev: MouseEvent, a: AppEntry): void {
  const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  tip.value = {
    text: `${a.name}\n${a.packageName}`,
    top: rect.top + 2,
    left: Math.min(rect.right + 8, window.innerWidth - 280),
  }
}

function showProcTip(ev: MouseEvent, p: ProcessEntry): void {
  const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  tip.value = {
    text: `${p.name}（pid ${p.pid}）${p.isMain ? ' · 主进程，缺省' : ''}`,
    top: rect.top + 2,
    left: Math.min(rect.right + 8, window.innerWidth - 280),
  }
}

const selected = computed(() => store.state.apps.find((a) => a.packageName === store.state.selectedApp) ?? null)

/**
 * 过滤 + 排序（对标 PerfDog：正在运行的进程排 top，前台最先）：
 * 前台(0) → 后台运行(1) → 未运行(2)，同组按包名字母序。
 */
const visibleApps = computed<AppEntry[]>(() => {
  const q = search.value.trim().toLowerCase()
  const tokens = q ? q.split(/\s+/) : []
  const matched = store.state.apps.filter((a) => {
    if (tokens.length === 0) return true
    const hay = `${a.name}\n${a.packageName}`.toLowerCase()
    return tokens.every((t) => hay.includes(t))
  })
  const rank = (a: AppEntry): number => (a.isForeground ? 0 : a.running ? 1 : 2)
  return [...matched].sort((x, y) => {
    const byState = rank(x) - rank(y)
    return byState !== 0 ? byState : x.packageName.localeCompare(y.packageName)
  })
})

const selectedProcess = computed<ProcessEntry | null>(() => {
  const name = store.state.selectedProcessName
  if (!name || !selected.value) return null
  return selected.value.processes.find((p) => p.name === name) ?? null
})

/**
 * 状态双通道：cls（颜色通道——深色前台 / 浅色后台）+ text（文字通道）。
 * 前台=深绿底深字（深色）、后台=浅绿底（浅色）、未运行=灰。
 */
function statusOf(a: AppEntry): { text: string; cls: string } {
  if (a.running === null) return { text: '状态未知', cls: 'gray' }
  if (!a.running) return { text: '未运行', cls: 'gray' }
  if (a.isForeground) return { text: '前台运行（顶层）', cls: 'fg' }
  return { text: '后台运行', cls: 'bg' }
}

function hasSubProcesses(a: AppEntry): boolean {
  return (a.processes?.length ?? 0) > 1
}

function toggleExpand(pkg: string): void {
  const next = new Set(expanded.value)
  if (next.has(pkg)) next.delete(pkg)
  else next.add(pkg)
  expanded.value = next
}

/** 选应用 = 默认主进程（R20 默认主进程；processName 缺省下发即主进程语义） */
function select(a: AppEntry): void {
  store.selectApp(a.packageName)
  open.value = false
}

/** 选子进程 = 应用 + processName（主进程行点击等价选应用） */
function selectProcess(a: AppEntry, p: ProcessEntry): void {
  store.selectApp(a.packageName)
  store.selectProcess(p.isMain ? '' : p.name)
  open.value = false
}

function onDocClick(e: MouseEvent): void {
  if (rootEl.value && !rootEl.value.contains(e.target as Node)) open.value = false
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="rootEl" class="side-block">
    <div class="side-title">应用选择器</div>
    <button class="selector-btn" :disabled="disabled" @click="open = !open">
      <span class="label" :title="selected ? `${selected.name}（${selected.packageName}）` : '请选择测试应用'">
        {{ selected ? selected.name : '请选择测试应用' }}
        <span v-if="selectedProcess" class="as-proc-suffix" :title="`目标子进程（§21.6 processName）：${selectedProcess.name}`">
          · {{ selectedProcess.name.split(':').pop() }}
        </span>
      </span>
      <span v-if="selected" class="chip" :class="statusOf(selected).cls">{{ statusOf(selected).text }}</span>
      <span class="caret">▼</span>
    </button>
    <div v-if="open" class="dropdown-panel">
      <!-- 模糊搜索（实机验证修复）：区分大小写无关子串匹配，多关键词空格分隔 -->
      <div class="as-search">
        <input
          v-model="search"
          class="as-search-input"
          type="text"
          placeholder="搜索应用名 / 包名…"
          spellcheck="false"
          @click.stop
        />
        <span class="as-count" title="过滤后 / 总数（运行中排前）">{{ visibleApps.length }}/{{ store.state.apps.length }}</span>
      </div>
      <template v-for="a in visibleApps" :key="a.packageName">
        <div class="dropdown-row" @click="select(a)" @mouseenter="showRowTip($event, a)" @mouseleave="tip = null">
          <span class="as-icon" title="应用图标（合成应用以首字母代图标）">{{ a.name.slice(0, 1) }}</span>
          <div style="flex: 1; min-width: 0">
            <div class="name">{{ a.name }}</div>
            <div class="pkg">{{ a.packageName }}</div>
          </div>
          <!-- 双通道：颜色（深/浅）+ 文字 -->
          <span class="chip" :class="statusOf(a).cls">{{ statusOf(a).text }}</span>
          <button
            v-if="hasSubProcesses(a)"
            class="as-expand"
            :title="`子进程（${a.processes.length}）展开选择（R20，§21.6）`"
            @click.stop="toggleExpand(a.packageName)"
          >
            {{ expanded.has(a.packageName) ? '▾' : '▸' }}
          </button>
        </div>
        <!-- R20 子进程列表（默认主进程；当前选中子进程高亮；悬停气泡显全名） -->
        <div v-if="expanded.has(a.packageName)" class="as-proc-list">
          <button
            v-for="p in a.processes"
            :key="p.pid"
            class="as-proc-row"
            :class="{
              main: p.isMain,
              active: a.packageName === store.state.selectedApp && (p.isMain ? store.state.selectedProcessName === '' : store.state.selectedProcessName === p.name),
            }"
            @mouseenter="showProcTip($event, p)"
            @mouseleave="tip = null"
            @click.stop="selectProcess(a, p)"
          >
            <span class="as-proc-dot" :class="{ main: p.isMain }"></span>
            <span class="as-proc-name">{{ p.name }}</span>
            <span class="as-proc-pid">pid {{ p.pid }}</span>
            <span v-if="p.isMain" class="chip gray">主进程</span>
          </button>
          <div class="hint" style="padding: 2px 8px 6px">
            采样按 pid 定位目标进程（§21.6）；多子进程 run 不参与报告对比（官方 [F§8] 限制，如实声明）。
          </div>
        </div>
      </template>
      <div v-if="store.state.apps.length === 0" class="empty-hint">设备上未发现应用（离线设备无应用列表）</div>
      <div v-else-if="visibleApps.length === 0" class="empty-hint">无匹配「{{ search }}」的应用</div>
    </div>
    <!-- 悬浮气泡（fixed 脱离面板裁剪；pointer-events:none 不挡行悬停） -->
    <div v-if="tip" class="as-tip" :style="{ top: `${tip.top}px`, left: `${tip.left}px` }">{{ tip.text }}</div>
  </div>
</template>

<style scoped>
/* 搜索行（实机验证修复）：输入框 + 过滤计数 */
.as-search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}

/* 悬浮气泡（UI 反馈 #3 强化）：即时全名（应用名+包名 / 完整进程名），白空间保留 \n 换行 */
.as-tip {
  position: fixed;
  z-index: 60;
  max-width: 340px;
  background: #0d1117f2;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 9px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text);
  white-space: pre-line;
  word-break: break-all;
  pointer-events: none;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
}

.as-search-input {
  flex: 1;
  min-width: 0;
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--text);
  outline: none;
}

.as-search-input:focus {
  border-color: var(--accent);
}

.as-count {
  flex: none;
  font-size: 10px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

/* R14 双通道的状态色：fg=前台（深色=正在运行顶层进程），bg=后台（浅色） */
.chip.fg {
  background: #1d5c33;
  color: #7ee2a8;
  border-color: #2c7a4b;
}

.chip.bg {
  background: #22303f;
  color: #9fb8a9;
  border-color: #33475c;
}

.as-icon {
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: 6px;
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--text-dim);
}

.as-expand {
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  padding: 2px 5px;
  font-size: 11px;
}

.as-proc-suffix {
  color: #79b8ff;
  font-size: 11px;
}

.as-proc-list {
  border-top: 1px solid var(--border);
  background: var(--bg);
}

.as-proc-row {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  border-bottom: 1px solid var(--border);
  padding: 5px 10px 5px 16px;
  cursor: pointer;
  font-size: 11.5px;
  color: var(--text-dim);
}

.as-proc-row:hover {
  background: var(--bg-hover);
}

.as-proc-row.active {
  background: var(--accent-soft);
  color: var(--text);
}

.as-proc-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-faint);
  flex: none;
}

.as-proc-dot.main {
  background: var(--accent);
}

.as-proc-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.as-proc-pid {
  font-size: 10px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
</style>
