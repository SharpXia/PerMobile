<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useStore } from './store'
import { ApiError } from './api-client'
import DeviceMatrixPanel from './components/DeviceMatrixPanel.vue'
import DeviceSelect from './components/DeviceSelect.vue'
import AppSelect from './components/AppSelect.vue'
import DeviceInfoCard from './components/DeviceInfoCard.vue'
import MetricPanel from './components/MetricPanel.vue'
import SaveDialog from './components/SaveDialog.vue'
import LogPanel from './components/LogPanel.vue'
import SessionRecoveryBar from './components/SessionRecoveryBar.vue'
import MonitorView from './views/MonitorView.vue'
import RecordsView from './views/RecordsView.vue'
import ReportView from './views/ReportView.vue'
import ReplayView from './views/ReplayView.vue'
import DeepAnalysisView from './views/DeepAnalysisView.vue'
import CasesView from './views/CasesView.vue'
import CompareView from './views/CompareView.vue'
import ReviewView from './views/ReviewView.vue'

/** PerfLab 实时监控主壳（对标 [I§2.1] 客户端主窗口：左栏选择器/控制 + 主区图表堆叠 + 底部 Log）。
 * M4：回放=主窗口复用（同布局切换数据源 [I§2.5]）——回放中左栏设备/应用选择器清空禁用，
 * 测试控制区替换为回放数据源信息，主区新增「回放」Tab。
 * M7：主区新增「深度分析」Tab（R50，[I§2.7]；入口=报告页/回放页）+
 * 会话控制区定时截图开关（R38，§22.3 real-only，synthetic 显示不可用）。 */
const store = useStore()
const panelOpen = ref(false)

const sessionActive = computed(
  () => Boolean(store.state.session && (store.state.session.state === 'monitoring' || store.state.session.state === 'recording')),
)
const replaying = computed(() => Boolean(store.state.replay))

const selectedDevice = computed(
  () => store.state.devices.find((d) => d.id === store.state.selectedDeviceId) ?? null,
)

const canStart = computed(() => Boolean(store.state.selectedDeviceId && store.state.selectedApp))

/** M6 会话扩展控件在会话进行中锁定（fpsWindow/processName/logLevel 均为创建时字段，§21.5-21.7） */
const extLocked = computed(() => sessionActive.value)

const LOG_LEVEL_OPTIONS: Array<{ value: 'verbose' | 'debug' | 'info' | 'warn' | 'error' | ''; label: string }> = [
  { value: '', label: '不开启（不发 log）' },
  { value: 'verbose', label: 'Verbose' },
  { value: 'debug', label: 'Debug' },
  { value: 'info', label: 'Info' },
  { value: 'warn', label: 'Warn' },
  { value: 'error', label: 'Error' },
]

function onLogLevelChange(ev: Event): void {
  const v = (ev.target as HTMLSelectElement).value
  store.setLogLevel(v === '' ? null : (v as 'verbose' | 'debug' | 'info' | 'warn' | 'error'))
  // 开启日志级即打开底部 Log 面板（[I§2.1] ☐Log 勾选语义联动）
  if (v !== '') store.state.logPanelOpen = true
}

const sessionStateText = computed(() => {
  const s = store.state.session?.state
  switch (s) {
    case 'monitoring':
      return '监控中'
    case 'recording':
      return '记录中'
    case 'stopped':
      return '已停止'
    case 'saved':
      return '已保存'
    case 'discarded':
      return '已放弃'
    default:
      return '未开始'
  }
})

const sessionChipCls = computed(() => {
  switch (store.state.session?.state) {
    case 'monitoring':
      return 'blue'
    case 'recording':
      return 'red'
    case 'stopped':
      return 'orange'
    default:
      return 'gray'
  }
})

const monDur = computed(() => store.sessionDurationSec())
const recDur = computed(() => store.recordDurationSec())

const providerBanner = computed<{ cls: string; text: string } | null>(() => {
  const h = store.state.health
  if (!h || !store.state.backendReachable) return null
  if (h.providers.metrics === 'synthetic') return { cls: 'synthetic', text: '合成模式（后端 synthetic）' }
  return { cls: 'real', text: '真机直连（real）' }
})

const backendDot = computed(() => (store.state.backendReachable ? 'online' : 'offline'))

const backendText = computed(() => {
  if (!store.state.backendReachable) return '后端不可达'
  const h = store.state.health
  return h ? `${h.backend.framework} · v${h.version}` : '连接中…'
})

function fmtDur(sec: number): string {
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

function setActiveTab(tab: 'monitor' | 'records' | 'cases' | 'compare' | 'review' | 'report' | 'replay' | 'deep'): void {
  store.state.activeTab = tab
  if (tab === 'records') void store.loadRuns()
  if (tab === 'cases') void store.loadCases()
  if (tab === 'review') void store.loadReviewCandidates()
}

/* ---------------- M7 R38：定时截图（§22.3，real-only；synthetic 501 + SSE 诊断） ---------------- */

const TS_INTERVALS = [1000, 2000, 5000, 10000]
const tsInterval = ref(2000)
const tsActive = ref(false)
const tsMsg = ref('')
const tsBusy = ref(false)

/** 契约 §22.3：仅 recording 可启动（monitoring 等 → 409）；随 record/stop 自动终止 */
const tsAvailable = computed(() => store.state.session?.state === 'recording')

watch(
  () => store.state.session?.state,
  (state, prev) => {
    if (prev === 'recording' && state !== 'recording' && tsActive.value) {
      tsActive.value = false
      tsMsg.value = '已随记录结束自动终止（契约 §22.3：随 record/stop/收口自动终止）'
    }
  },
)

async function toggleTimedScreenshot(): Promise<void> {
  const sessionId = store.state.session?.id
  if (!sessionId || tsBusy.value) return
  tsBusy.value = true
  tsMsg.value = ''
  try {
    await store.startTimedScreenshot(sessionId, { intervalMs: tsInterval.value })
    tsActive.value = true
    tsMsg.value = `定时截图已开启（${tsInterval.value}ms/帧；截图进 artifacts 索引 §17）`
  } catch (err) {
    const code = err instanceof ApiError ? err.code : ''
    if (code === 'not_implemented') {
      // synthetic：不伪造截图 —— 状态明示不可用（服务端同时经 SSE 广播
      // timed-screenshot.unavailable 诊断，Log 面板可见）
      tsActive.value = false
      tsMsg.value = 'synthetic 模式不可用（timed-screenshot 为 real-only，契约 §22.3；诊断已入 Log）'
    } else if (code === 'session_state_conflict') {
      tsActive.value = false
      tsMsg.value = '仅「记录中」可启动（契约 §22.3：monitoring/stopped → 409）'
    } else {
      tsMsg.value = `启动失败：${err instanceof Error ? err.message : String(err)}`
    }
  } finally {
    tsBusy.value = false
  }
}
</script>

<template>
  <header class="topbar">
    <div class="brand">
      PerfLab<span class="sub">实时监控 · 报告 · 对比评审 · 设备矩阵 M6 · 深度分析 M7 · PD-001 M1-M7</span>
    </div>
    <span v-if="providerBanner" class="banner" :class="providerBanner.cls">{{ providerBanner.text }}</span>
    <span style="flex: 1"></span>
    <span class="chip gray" :title="store.state.baseUrl || '同源（/api 走 Vite proxy 或后端静态托管）'">
      <span class="dot" :class="backendDot"></span>{{ backendText }}
    </span>
  </header>

  <div class="body">
    <aside class="sidebar">
      <!-- M6 R22：设备矩阵面板（多设备卡片/平台分组/USB-WiFi 图标/每设备独立会话 + R15 WiFi 入口） -->
      <DeviceMatrixPanel v-if="!replaying" />
      <!-- [I§2.5] 回放=主窗口复用：左栏设备选择器清空（回放中显示占位），其余布局不变 -->
      <DeviceSelect
        :disabled="sessionActive || replaying"
        :placeholder="replaying ? '请选择设备（回放中）' : undefined"
      />
      <AppSelect :disabled="sessionActive || replaying" />
      <DeviceInfoCard v-if="!replaying" :device="selectedDevice" />

      <div v-if="replaying" class="side-block">
        <div class="side-title">回放数据源</div>
        <div class="session-state">
          <span class="chip blue">回放中</span>
        </div>
        <div class="hint mono" style="word-break: break-all">
          {{ store.state.replay?.caseName ?? store.state.replay?.runId }}
        </div>
        <div class="btn-col" style="margin-top: 10px">
          <button @click="store.closeReplay()">← 退出回放</button>
        </div>
        <div class="hint" style="margin-top: 8px">
          回放 = 主窗口复用（同布局切换数据源 [I§2.5]）；录屏缩略图 / 标记色带 / 悬停单点卡 / ←/→ 逐采样点联动。
        </div>
      </div>

      <div v-else class="side-block">
        <div class="side-title">测试控制</div>
        <div class="session-state">
          <span class="chip" :class="sessionChipCls">{{ sessionStateText }}</span>
          <span v-if="store.state.session" class="hint">监控 {{ fmtDur(monDur) }}</span>
          <span v-if="recDur > 0" class="hint">记录 {{ fmtDur(recDur) }}</span>
        </div>
        <div class="btn-col">
          <button v-if="!store.state.session" class="primary" :disabled="!canStart" @click="store.startMonitoring()">
            开始监控
          </button>
          <button v-if="store.state.session?.state === 'monitoring'" class="primary" @click="store.startRecording()">
            ▶ 开始记录
          </button>
          <button v-if="store.state.session?.state === 'monitoring'" @click="store.requestStopMonitoring()">
            停止监控
          </button>
          <button v-if="store.state.session?.state === 'recording'" class="stop" @click="store.stopRecording()">
            ■ 停止记录
          </button>
          <button v-if="store.state.session?.state === 'stopped'" @click="store.reopenDecision()">
            收口决策…
          </button>
        </div>

        <!-- M6 会话扩展选择（R19 FPS 窗口 / R20 子进程在应用选择器内 / R33 日志级别） -->
        <div class="ext-grid">
          <label
            class="ext-field"
            title="R19（§21.5）：FPS 窗口类型——auto 缺省策略（游戏→SurfaceView / 应用→Activity），高阶可手动选择"
          >
            <span>FPS 窗口（R19）</span>
            <select :value="store.state.selectedFpsWindow" :disabled="extLocked" @change="store.setFpsWindow(($event.target as HTMLSelectElement).value as 'auto' | 'activity' | 'surface_view')">
              <option value="auto">auto（缺省策略）</option>
              <option value="surface_view">SurfaceView（游戏/直播/视频）</option>
              <option value="activity">Activity（传统应用/Web）</option>
            </select>
          </label>
          <label
            class="ext-field"
            title="R33（§21.7）：会话日志级别——提供即开启实时日志流（SSE log）；缺省不发 log 消息（☐Log 勾选语义）"
          >
            <span>Log level（R33）</span>
            <select :value="store.state.selectedLogLevel ?? ''" :disabled="extLocked" @change="onLogLevelChange">
              <option v-for="o in LOG_LEVEL_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </label>
        </div>
        <div v-if="store.state.selectedProcessName" class="hint ext-proc" :title="`目标子进程（§21.6）：${store.state.selectedProcessName}`">
          目标子进程：{{ store.state.selectedProcessName }}
          <button class="mini" style="margin-left: 4px" @click="store.selectProcess('')">回到主进程</button>
        </div>

        <!-- M6 会话回显（§21.5-21.7 可选回显字段） -->
        <div v-if="store.state.session" class="ext-echo">
          <span class="chip gray" title="§21.5 回显 fpsWindow">窗口:{{ store.state.session.fpsWindow?.type ?? 'auto' }}</span>
          <span v-if="store.state.session.processName" class="chip gray" :title="`§21.6 回显 processName：${store.state.session.processName}`">
            进程:{{ store.state.session.processName.split(':').pop() }}
          </span>
          <span v-if="store.state.session.logLevel" class="chip gray" title="§21.7 回显 logLevel">log:{{ store.state.session.logLevel }}</span>
        </div>

        <!-- M7 R38：深度分析定时截图（§22.3 real-only，仅 recording 可启动；synthetic 不可用态明示） -->
        <div v-if="store.state.session" class="ts-block">
          <div class="ts-row">
            <span class="ts-name" title="R38（§22.3）：深度分析定时截图——超低消耗定时截图（2s 一次），截图进 artifacts 索引，深度分析页按帧时间配比对齐">
              定时截图（R38）
            </span>
            <select v-model.number="tsInterval" :disabled="tsActive" title="截图间隔（契约 §22.3 允许 500–60000ms，缺省 2000）">
              <option v-for="i in TS_INTERVALS" :key="i" :value="i">{{ i / 1000 }}s</option>
            </select>
            <button
              class="mini"
              :disabled="!tsAvailable || tsActive || tsBusy"
              :title="tsActive ? '已开启（随停止记录自动终止）' : !tsAvailable ? '仅「记录中」可启动（契约 §22.3）' : '启动定时截图循环'"
              @click="toggleTimedScreenshot"
            >
              {{ tsActive ? '已开启' : tsBusy ? '启动中…' : '开启' }}
            </button>
          </div>
          <div v-if="tsMsg" class="hint ts-msg">{{ tsMsg }}</div>
          <div v-if="tsActive" class="hint ts-msg">随停止记录自动终止（契约 §22.3 无独立停止端点，不伪装可停止）。</div>
        </div>

        <div class="hint" style="margin-top: 8px">
          开始监控 = 接入即实时显示（不落盘）；开始记录 = 落盘；停止后选择保存或放弃。
        </div>
      </div>

      <button v-if="!replaying" style="width: 100%" @click="panelOpen = true">＋ 指标面板</button>
    </aside>

    <main class="main">
      <!-- 刷新恢复：后端仍存活但本页未接管的会话（接管 / 停止放弃） -->
      <SessionRecoveryBar />
      <nav class="tabs">
        <span class="tab" :class="{ active: store.state.activeTab === 'monitor' }" @click="setActiveTab('monitor')">
          实时监控
        </span>
        <span class="tab" :class="{ active: store.state.activeTab === 'records' }" @click="setActiveTab('records')">
          测试记录
          <span v-if="store.state.runs.length > 0" style="color: var(--text-faint)">{{ store.state.runs.length }}</span>
        </span>
        <span class="tab" :class="{ active: store.state.activeTab === 'cases' }" @click="setActiveTab('cases')">
          我的数据
        </span>
        <span class="tab" :class="{ active: store.state.activeTab === 'compare' }" @click="setActiveTab('compare')">
          对比报告
          <span v-if="store.state.comparePool.length > 0" style="color: #79b8ff">({{ store.state.comparePool.length }})</span>
        </span>
        <span class="tab" :class="{ active: store.state.activeTab === 'review' }" @click="setActiveTab('review')">
          回归评审
        </span>
        <span
          v-if="store.state.report"
          class="tab"
          :class="{ active: store.state.activeTab === 'report' }"
          @click="setActiveTab('report')"
        >
          报告 · {{ store.state.report.kind === 'run' ? store.state.report.caseName ?? store.state.report.runId : '统计预览' }}
        </span>
        <span
          v-if="store.state.replay"
          class="tab"
          :class="{ active: store.state.activeTab === 'replay' }"
          @click="setActiveTab('replay')"
        >
          回放 · {{ store.state.replay.caseName ?? store.state.replay.runId }}
        </span>
        <span
          v-if="store.state.deep"
          class="tab"
          :class="{ active: store.state.activeTab === 'deep' }"
          @click="setActiveTab('deep')"
        >
          深度分析 · {{ store.state.deep.caseName ?? store.state.deep.runId }}
        </span>
      </nav>
      <div class="tab-content">
        <MonitorView v-show="store.state.activeTab === 'monitor'" />
        <RecordsView v-if="store.state.activeTab === 'records'" />
        <CasesView v-if="store.state.activeTab === 'cases'" />
        <CompareView v-if="store.state.activeTab === 'compare'" />
        <ReviewView v-if="store.state.activeTab === 'review'" />
        <ReportView v-if="store.state.activeTab === 'report' && store.state.report" />
        <ReplayView v-if="store.state.activeTab === 'replay' && store.state.replay" />
        <DeepAnalysisView v-if="store.state.activeTab === 'deep' && store.state.deep" />
      </div>
      <LogPanel />
    </main>
  </div>

  <MetricPanel v-if="panelOpen" @close="panelOpen = false" />
  <SaveDialog />

  <div v-if="store.state.errorMsg" class="toast">
    <span>{{ store.state.errorMsg }}</span>
    <button class="close" @click="store.clearError()">✕</button>
  </div>
</template>

<style scoped>
/* M6 会话扩展选择（R19/R33）：两列小字段 */
.ext-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 8px;
}

.ext-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 10.5px;
  color: var(--text-dim);
}

.ext-field select {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  font-size: 11px;
  padding: 3px 4px;
  width: 100%;
}

.ext-proc {
  margin-top: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ext-echo {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 6px;
}

.ext-echo .chip {
  font-size: 10px;
}

/* M7 R38：定时截图开关（会话操作区；synthetic 不可用态在 ts-msg 明示） */
.ts-block {
  margin-top: 10px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}

.ts-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ts-name {
  font-size: 10.5px;
  color: var(--text-dim);
  flex: 1;
  min-width: 0;
}

.ts-row select {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  font-size: 11px;
  padding: 2px 3px;
}

.ts-msg {
  margin-top: 4px;
  font-size: 10.5px;
  line-height: 1.5;
}
</style>
