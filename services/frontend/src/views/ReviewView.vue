<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useStore } from '../store'
import MetricChart from '../components/MetricChart.vue'
import type { ChartSeries } from '../components/MetricChart.vue'
import type { CaseRecord, CompareMode, CompareResponse, MetricPoint } from '../api-client'

/**
 * 回归评审页（R44，[F§8] 大版本对比 + [I§2.9] 可比前提）：
 * - baseline / candidate 选择：baseline 为多选（compare 引擎按 run 取中位数 + bootstrap，
 *   perfeval.compare.v1 `baselineRunIds` 数组语义），candidate 单选；
 * - 对比模式（契约 §9 mode）：same_app_devices（缺省，同应用跨设备）/ same_device_versions
 *   （同设备换版本）随请求透传；模式前提由引擎 errors 承载（如同设备走缺省模式 →
 *   device_not_changed invalid），结果区展示实际 mode 与错误码中文释义；
 * - POST /api/reports/compare（契约 §9）→ 预算门禁结论展示：总体 status + 可靠性 +
 *   逐指标 judgement/reason/Δ/CI 表；invalid 时展示前提校验错误码；
 * - 退化点图上高亮：candidate 曲线中偏离基线中位数超过 5%（方向感知）的点以红色菱形
 *   标记。该 5% 为本页可视化辅助阈值（在 UI 明示），非引擎判定；单击退化点 →
 *   store.openReplay（R37 回放页，携带退化点时刻 seek 定位游标，[I§4.6] 时间定位）。
 *
 * 曲线数据边界（如实）：run 级时序端点契约未定义（§9），曲线仅在来源会话缓冲可用时
 * 可得（mock=本次页面会话内）；不可得时曲线区降级为说明，门禁结论表不受影响。
 */
const store = useStore()

const FPS_METRIC = 'frame.fps.mean'
const DEGRADE_RATIO = 0.05 // 本页可视化辅助阈值（非引擎判定；UI 明示）

const baselineIds = ref<string[]>([])
const candidateId = ref('')
/** 对比模式（契约 §9）：缺省 same_app_devices = 服务端缺省（compare_service.DEFAULT_COMPARE_MODE） */
const compareMode = ref<CompareMode>('same_app_devices')
const running = ref(false)
const result = ref<CompareResponse | null>(null)
const errorMsg = ref('')

/* 评审曲线数据（baseline 组 + candidate 的 FPS 序列与来源会话） */
const curvePoints = ref<Record<string, MetricPoint[]>>({})
const curveNote = ref('')

onMounted(() => {
  void store.loadReviewCandidates()
})

const cases = computed<CaseRecord[]>(() => store.state.reviewCases)
const caseById = computed<Record<string, CaseRecord>>(() => {
  const map: Record<string, CaseRecord> = {}
  for (const c of cases.value) map[c.runId] = c
  return map
})

function nameOf(runId: string): string {
  return caseById.value[runId]?.caseName ?? runId
}

function toggleBaseline(runId: string): void {
  const i = baselineIds.value.indexOf(runId)
  if (i >= 0) baselineIds.value.splice(i, 1)
  else baselineIds.value.push(runId)
  if (candidateId.value === runId) candidateId.value = ''
}

function selectCandidate(runId: string): void {
  candidateId.value = runId
  const i = baselineIds.value.indexOf(runId)
  if (i >= 0) baselineIds.value.splice(i, 1)
}

const canRun = computed(() => baselineIds.value.length > 0 && candidateId.value !== '')

/* ---- 前提自检（同设备/同应用；场景与初始状态无元数据 → 人工确认） ---- */

const premiseIssues = computed<string[]>(() => {
  const issues: string[] = []
  const all = [...baselineIds.value, candidateId.value]
  if (all.length >= 2) {
    const devices = new Set(all.map((id) => caseById.value[id]?.device))
    if (devices.size > 1) issues.push('设备不一致')
    const pkgs = new Set(all.map((id) => caseById.value[id]?.packageName ?? ''))
    if (pkgs.size > 1) issues.push('应用（包名）不一致')
  }
  return issues
})

/* ---- 运行评审 ---- */

async function runReview(): Promise<void> {
  if (!canRun.value || running.value) return
  running.value = true
  errorMsg.value = ''
  result.value = null
  try {
    result.value = await store.runCompareReview([...baselineIds.value], candidateId.value, compareMode.value)
    await loadCurves()
  } catch (err) {
    errorMsg.value = errText(err)
  } finally {
    running.value = false
  }
}

async function loadCurves(): Promise<void> {
  curvePoints.value = {}
  curveNote.value = ''
  const runIds = [...baselineIds.value, candidateId.value]
  const next: Record<string, MetricPoint[]> = {}
  const notes: string[] = []
  for (const id of runIds) {
    try {
      const detail = await store.getRunDetail(id)
      if (!detail.sessionId) throw new Error('无来源会话（run 级时序端点未定义 §9）')
      const win = await store.getMetricsWindow(detail.sessionId, { metricIds: [FPS_METRIC], maxPoints: 600 })
      const pts = win.series[0]?.points ?? []
      if (pts.length === 0) throw new Error('会话窗口内无 FPS 点列')
      next[id] = pts
    } catch (err) {
      notes.push(`${nameOf(id)}：${errText(err)}`)
    }
  }
  curvePoints.value = next
  if (notes.length === runIds.length && runIds.length > 0) {
    curveNote.value = `曲线不可用 —— ${notes.join('；')}。曲线仅在来源会话缓冲可用时可得（mock=本次页面会话内；§9 无 run 级时序端点）。门禁结论表不受影响。`
  } else if (notes.length > 0) {
    curveNote.value = `部分曲线不可用：${notes.join('；')}`
  }
}

/* ---- 结论展示 ---- */

const STATUS_TEXT: Record<string, { label: string; cls: string; desc: string }> = {
  pass: { label: '通过', cls: 'green', desc: '全部指标在预算内（或未配置回归阈值）' },
  warn: { label: '警告', cls: 'orange', desc: '存在超 warn 阈值的退化' },
  fail: { label: '不通过', cls: 'red', desc: '存在超 fail 阈值的退化 / 崩溃 / 绝对阈值击穿' },
  unknown: { label: '无法判定', cls: 'gray', desc: '样本量不足或指标缺失（引擎判定序）' },
  invalid: { label: '前提不成立', cls: 'red', desc: '前提校验失败（见错误码）' },
}

const ERROR_TEXT: Record<string, string> = {
  baseline_or_candidate_missing: 'baseline 或 candidate 缺失',
  device_mismatch: '两组设备不一致（same_device_versions 要求同设备）',
  app_mismatch: '两组应用不一致（same_app_devices 要求同应用）',
  device_not_changed: '设备未变化（same_app_devices 模式要求换设备）',
  app_version_not_changed: '应用版本未变化（same_device_versions 模式要求换版本）',
}

function errorText(code: string): string {
  return ERROR_TEXT[code] ?? code
}

function isMockResult(): boolean {
  return result.value?.schemaVersion.endsWith('-mock') ?? false
}

interface MetricRow {
  metricId: string
  direction: string
  unit: string
  baseMedian: number | null
  candMedian: number | null
  deltaAbs: number | null
  deltaPct: number | null
  ci: [number, number] | null
  judgement: string
  reason: string | null
}

const metricRows = computed<MetricRow[]>(() => {
  const r = result.value
  if (!r) return []
  return Object.entries(r.metrics)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([metricId, m]) => ({
      metricId,
      direction: m.direction ?? '',
      unit: m.unit ?? '',
      baseMedian: m.baseline?.median ?? null,
      candMedian: m.candidate?.median ?? null,
      deltaAbs: m.delta?.abs ?? null,
      deltaPct: m.delta?.pct ?? null,
      ci: m.delta?.bootstrapCi95 ?? null,
      judgement: m.judgement,
      reason: m.reason ?? null,
    }))
})

const judgementCls = (j: string): string =>
  j === 'pass' ? 'green' : j === 'fail' ? 'red' : j === 'warn' ? 'orange' : 'gray'

function fmtNum(v: number | null): string {
  return v === null || v === undefined ? '—' : String(Math.round(v * 1000) / 1000)
}

/* ---- 退化点高亮（图内）+ 单击进回放联动（R37 回放页 seek 定位） ---- */

const fpsCompare = computed(() => result.value?.metrics?.[FPS_METRIC] ?? null)

/** 基线中位数（引擎 baseline.median）；无 compare 结果或该指标缺失时不画参考线 */
const baselineMedian = computed<number | null>(() => fpsCompare.value?.baseline?.median ?? null)

const markPoints = computed<Array<{ ts: number; value: number }>>(() => {
  const candId = candidateId.value
  const med = baselineMedian.value
  if (!candId || med === null || med === 0) return []
  const direction = fpsCompare.value?.direction ?? 'lower_better'
  const pts = curvePoints.value[candId] ?? []
  const out: Array<{ ts: number; value: number }> = []
  for (const p of pts) {
    if (p.value === null) continue
    const degraded =
      direction === 'higher_better' ? p.value < med * (1 - DEGRADE_RATIO) : p.value > med * (1 + DEGRADE_RATIO)
    if (degraded) out.push({ ts: p.ts, value: p.value })
  }
  return out
})

const reviewCharts = computed<ChartSeries[]>(() => {
  const list: ChartSeries[] = []
  const palette = ['#5c6873', '#7a8694', '#98a4b3']
  baselineIds.value.forEach((id, i) => {
    const pts = curvePoints.value[id] ?? []
    if (pts.length > 0) {
      list.push({ metricId: FPS_METRIC, label: `基线·${nameOf(id)}`, unit: 'fps', color: palette[i % palette.length]!, points: pts })
    }
  })
  const candPts = curvePoints.value[candidateId.value] ?? []
  if (candPts.length > 0) {
    list.push({ metricId: FPS_METRIC, label: `候选·${nameOf(candidateId.value)}`, unit: 'fps', color: '#4fc3f7', points: candPts })
  }
  if (baselineMedian.value !== null && list.length > 0) {
    // 基线中位数参考线（来自 compare 结果 baseline.median，引擎口径）
    const ts0 = Math.min(...list.flatMap((s) => s.points.map((p) => p.ts)))
    const ts1 = Math.max(...list.flatMap((s) => s.points.map((p) => p.ts)))
    list.push({
      metricId: FPS_METRIC,
      label: '基线中位数',
      unit: 'fps',
      color: '#3fb950',
      points: [
        { ts: ts0, value: baselineMedian.value },
        { ts: ts1, value: baselineMedian.value },
      ],
    })
  }
  return list
})

/** 图内相对时间原点（mm:ss 轴）：取全部曲线最早点 */
const reviewOrigin = computed<number | undefined>(() => {
  let min = Infinity
  for (const s of reviewCharts.value) for (const p of s.points) if (p.ts < min) min = p.ts
  return Number.isFinite(min) ? min : undefined
})

/** 退化点单击 → 回放联动（[I§4.6] 时间定位）：openReplay 携带退化点时刻 seek，
 *  回放页打开后游标定位该时刻（store.consumeReplaySeek 取走即清，M7 诊断卡同款用法） */
function onDegradedPointClick(point: { ts: number; value: number }): void {
  const candId = candidateId.value
  if (!candId) return
  store.openReplay(candId, nameOf(candId), point.ts)
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}
</script>

<template>
  <div class="review-wrap">
    <div class="records-head">
      <strong>回归评审</strong>
      <span class="hint">baseline/candidate 对比 + 预算门禁结论（POST /api/reports/compare，perfeval.compare.v1）</span>
      <span style="flex: 1"></span>
    </div>

    <div v-if="cases.length === 0" class="empty-hint">
      暂无用例可选 —— 先在实时监控保存记录（保存后自动登记 case 索引，契约 §14），或到「我的数据」查看。
    </div>

    <template v-else>
      <!-- baseline / candidate 选择 -->
      <div class="select-area">
        <div class="select-col">
          <div class="select-title">baseline（可多选；引擎按 run 取中位数 + bootstrap CI）</div>
          <div class="case-list">
            <label
              v-for="c in cases"
              :key="c.runId"
              class="case-pick"
              :class="{ picked: baselineIds.includes(c.runId), forbidden: candidateId === c.runId }"
            >
              <input
                type="checkbox"
                :checked="baselineIds.includes(c.runId)"
                :disabled="candidateId === c.runId"
                @change="toggleBaseline(c.runId)"
              />
              <span class="case-pick-name" :title="c.runId">{{ c.caseName }}</span>
              <span class="case-pick-meta">{{ c.device }}</span>
            </label>
          </div>
        </div>
        <div class="select-col">
          <div class="select-title">candidate（单选）</div>
          <div class="case-list">
            <label
              v-for="c in cases"
              :key="c.runId"
              class="case-pick"
              :class="{ picked: candidateId === c.runId, forbidden: baselineIds.includes(c.runId) }"
            >
              <input
                type="radio"
                name="review-candidate"
                :checked="candidateId === c.runId"
                :disabled="baselineIds.includes(c.runId)"
                @change="selectCandidate(c.runId)"
              />
              <span class="case-pick-name" :title="c.runId">{{ c.caseName }}</span>
              <span class="case-pick-meta">{{ c.device }}</span>
            </label>
          </div>
        </div>
      </div>

      <div class="review-bar">
        <span v-if="premiseIssues.length > 0" class="premise-warn">可比前提：⚠ {{ premiseIssues.join('；') }}（[F§8] 相同设备/场景/时长/初始状态，场景与初始状态请人工确认）</span>
        <span v-else class="hint">可比前提（[F§8]）：相同设备/场景/时长/初始状态 —— 设备/应用已核对一致；场景与初始状态无元数据，请人工确认。</span>
        <span style="flex: 1"></span>
        <!-- 对比模式出口（契约 §9 mode）：前提不符由引擎 invalid/errors 承载，此处可换模式重跑 -->
        <label
          class="mode-pick"
          title="same_app_devices：同应用跨设备，要求设备变化（同设备 → device_not_changed）；same_device_versions：同设备换版本，要求同设备且应用版本变化（mock 无版本元数据，版本前提不校验）。选错模式会被引擎判 invalid，换模式重跑即可。"
        >
          对比模式
          <select v-model="compareMode">
            <option value="same_app_devices">same_app_devices（同应用跨设备）</option>
            <option value="same_device_versions">same_device_versions（同设备换版本）</option>
          </select>
        </label>
        <button class="primary" :disabled="!canRun || running" @click="runReview">
          {{ running ? '评审中…' : '运行评审（compare）' }}
        </button>
      </div>

      <div v-if="errorMsg" class="empty-hint">compare 请求失败：{{ errorMsg }}</div>

      <template v-if="result">
        <!-- 门禁结论（总体 status + 可靠性） -->
        <div class="verdict-bar">
          <span class="chip verdict-chip" :class="STATUS_TEXT[result.status]?.cls ?? 'gray'">
            门禁结论：{{ STATUS_TEXT[result.status]?.label ?? result.status }}
          </span>
          <span class="verdict-desc">{{ STATUS_TEXT[result.status]?.desc ?? '' }}</span>
          <span v-if="result.reliability" class="chip gray" title="reliability.score（engine.py _reliability：run 数/离群率/CI 宽度/证据等级扣分）">
            可靠性 {{ result.reliability.level }} ({{ result.reliability.score.toFixed(2) }})
          </span>
          <span v-if="result.mode" class="chip gray" title="本次 compare 实际使用的对比模式（契约 §9 mode，引擎前提见模式选择器说明）">
            mode: {{ result.mode }}
          </span>
          <span v-if="result.summary" class="chip gray">基线 {{ result.summary.baselineRuns }} run · 候选 {{ result.summary.candidateRuns }} run · {{ result.summary.metricCount }} 指标</span>
          <span v-if="isMockResult()" class="chip orange" :title="typeof result.note === 'string' ? result.note : ''">mock 镜像</span>
        </div>
        <div v-if="result.errors && result.errors.length > 0" class="stat-note" style="margin-bottom: 8px">
          ※ 前提校验错误（引擎 errors）：{{ result.errors.map(errorText).join('；') }}
        </div>
        <div v-if="typeof result.meta?.preconditionNote === 'string'" class="stat-note" style="margin-bottom: 8px">
          ※ {{ result.meta.preconditionNote }}
        </div>

        <!-- 逐指标门禁表 -->
        <div class="report-sub">逐指标门禁（judgement 按 engine 判定序：绝对阈值 → 崩溃 → 样本量 → 中性 → 回归阈值）</div>
        <table v-if="metricRows.length > 0" class="records-table compare-table">
          <thead>
            <tr>
              <th>指标</th>
              <th>方向</th>
              <th>baseline 中位数</th>
              <th>candidate 中位数</th>
              <th>Δabs</th>
              <th>Δpct</th>
              <th>CI95（bootstrap）</th>
              <th>judgement</th>
              <th>reason</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in metricRows" :key="row.metricId">
              <td class="mono">{{ row.metricId }}<span v-if="row.unit" class="dim"> [{{ row.unit }}]</span></td>
              <td>{{ row.direction }}</td>
              <td class="num">{{ fmtNum(row.baseMedian) }}</td>
              <td class="num">{{ fmtNum(row.candMedian) }}</td>
              <td class="num">{{ fmtNum(row.deltaAbs) }}</td>
              <td class="num">{{ row.deltaPct === null ? '—' : `${(row.deltaPct * 100).toFixed(1)}%` }}</td>
              <td class="num">{{ row.ci ? `[${fmtNum(row.ci[0])}, ${fmtNum(row.ci[1])}]` : '—' }}</td>
              <td><span class="chip" :class="judgementCls(row.judgement)">{{ row.judgement }}</span></td>
              <td class="mono dim">{{ row.reason ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-hint">compare 结果无指标数据。</div>

        <!-- 退化点图上高亮（图内）+ 单击进回放联动（R37） -->
        <div class="report-section">
          <div class="report-sub">FPS 曲线与退化点（图内高亮，单击进入回放）</div>
          <div class="stat-note" style="margin-bottom: 8px">
            ※ 高亮规则：candidate 曲线中偏离基线中位数超过 {{ (DEGRADE_RATIO * 100).toFixed(0) }}%（方向感知：FPS 为 higher_better，向下偏离视为退化）的点。
            该比例为评审页可视化辅助阈值，非引擎预算判定；单击退化点 → 回放页打开并定位该时刻（store.openReplay seek，[I§4.6]）。
          </div>
          <div v-if="curveNote" class="stat-note" style="margin-bottom: 8px">※ {{ curveNote }}</div>
          <MetricChart
            v-if="reviewCharts.length > 0"
            title="FPS（基线组 / 候选 / 基线中位数参考线）"
            :series="reviewCharts"
            :start-ts="reviewOrigin"
            :window-ms="Number.MAX_SAFE_INTEGER"
            :height="240"
            :mark-points="markPoints.length > 0 ? markPoints : undefined"
            mark-name="退化点"
            mark-click-enabled
            @mark-point-click="onDegradedPointClick"
          />
          <div v-else class="empty-hint">无曲线数据。</div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.review-wrap {
  padding: 14px;
}

.select-area {
  display: flex;
  gap: 12px;
  margin-bottom: 10px;
}

.select-col {
  flex: 1;
  min-width: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px;
}

.select-title {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

.case-list {
  max-height: 240px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.case-pick {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}

.case-pick:hover {
  background: var(--bg-hover);
}

.case-pick.picked {
  border-color: rgba(47, 129, 247, 0.55);
  background: var(--bg-panel-2);
}

.case-pick.forbidden {
  opacity: 0.45;
}

.case-pick input {
  accent-color: var(--accent);
  flex: none;
}

.case-pick-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.case-pick-meta {
  color: var(--text-faint);
  font-size: 11px;
  flex: none;
}

.review-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.mode-pick {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-dim);
  cursor: pointer;
}

.mode-pick select {
  font-size: 12px;
  padding: 3px 6px;
  background: var(--bg-panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.premise-warn {
  color: #e3b341;
  font-size: 12px;
}

.verdict-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.verdict-chip {
  font-size: 13px;
  padding: 4px 12px;
}

.verdict-desc {
  color: var(--text-dim);
  font-size: 12px;
}

.dim {
  color: var(--text-faint);
}

.num {
  font-variant-numeric: tabular-nums;
  text-align: right;
}
</style>
