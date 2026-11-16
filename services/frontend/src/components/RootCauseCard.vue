<script setup lang="ts">
/**
 * 根因诊断卡（M7 R51，契约 §22.2，对标 [F§9] analyze_root_cause）：
 * - 下降区间列表：起止时间 / 持续 / 降幅 / baseline→trough；点击区间 → 回放定位
 *   （复用 M4 openReplay + replaySeekTs，ReplayView 游标对齐该时刻 [I§2.5] 时间定位）；
 * - 各指标相关系数条形：正负着色（正=橙红、负=蓝），宽度=|pearson|，附样本对数
 *   （<3 对的指标服务端不出行，UI 不补行）；
 * - 候选原因 Top3 排序卡：rank + metricId + score + evidence（服务端规则文本）；
 * - AI 叙述：本部署无 LLM key / 无 LLM 端点（契约 §22.2 注：LLM 叙述化不在契约，
 *   LLM 只生成叙述不改变判定 [P§2.2]）→ 恒显示「AI 叙述不可用」占位，不伪造输出；
 * - 数据缺失：note 原样显示（锚指标无数据/无下降区间是常态，不填假区间）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useStore } from '../store'
import { metricMeta } from '../metricMeta'
import type { DiagnosticsResponse } from '../api-client'

const props = withDefaults(
  defineProps<{
    runId: string
    caseName?: string
    /** 锚指标候选（诊断请求 anchor 下拉；缺省仅 FPS 均值——契约默认锚） */
    anchors?: string[]
    /** 相对时间轴原点（epoch ms，与回放/曲线首样本一致；缺省显示原始 epoch） */
    originTs?: number | null
  }>(),
  { anchors: () => ['frame.fps.mean'], caseName: undefined, originTs: null },
)

const store = useStore()

const anchor = ref(props.anchors[0] ?? 'frame.fps.mean')
const dropThresholdPct = ref(15)
const minDurationMs = ref(1000)
const data = ref<DiagnosticsResponse | null>(null)
const loading = ref(false)
const errorMsg = ref('')
let loadSeq = 0

async function load(): Promise<void> {
  const seq = ++loadSeq
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await store.getRunDiagnostics(props.runId, {
      anchor: anchor.value,
      dropThresholdPct: dropThresholdPct.value,
      minDurationMs: minDurationMs.value,
    })
    if (seq === loadSeq) data.value = res
  } catch (err) {
    if (seq === loadSeq) {
      errorMsg.value = err instanceof Error ? err.message : String(err)
      data.value = null
    }
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

watch(() => props.runId, load, { immediate: false })
onMounted(load)

function labelOf(metricId: string): string {
  return metricMeta(metricId).label || metricId
}

/** 相对时间（mm:ss.s，原点 originTs；无原点回退 epoch 秒） */
function fmtRel(ts: number): string {
  if (props.originTs !== null && props.originTs !== undefined && ts >= props.originTs) {
    const rel = ts - props.originTs
    const total = Math.floor(rel / 1000)
    const ms = Math.floor(rel % 1000)
    return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms).padStart(1, '0')}`
  }
  return `epoch ${Math.round(ts)}`
}

function fmtDur(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

/** 点击下降区间 → 回放打开并定位区间起点（M4 openReplay + replaySeekTs） */
function jumpToReplay(fromMs: number): void {
  store.openReplay(props.runId, props.caseName, fromMs)
}

/** |pearson| 上限的条宽（pearson ∈ [-1,1]；>1 视为脏数据截断到 100%） */
function barPct(r: number): string {
  return `${Math.min(100, Math.abs(r) * 100).toFixed(1)}%`
}

const hasAnyData = computed(
  () => (data.value?.dropIntervals.length ?? 0) + (data.value?.correlations.length ?? 0) > 0,
)
</script>

<template>
  <div class="rcc">
    <!-- 参数行：锚指标 + 下降阈值 + 最小持续 + 重新分析（§22.2 查询参数直通） -->
    <div class="rcc-params">
      <label title="锚指标 metricId（下降检测在该指标序列上执行；未注册 → 400 validation_error）">
        锚指标
        <select v-model="anchor" @change="load">
          <option v-for="a in anchors" :key="a" :value="a">{{ labelOf(a) }}</option>
        </select>
      </label>
      <label title="相邻点下降百分比阈值（缺省 15，正数）">
        下降阈值 %
        <input v-model.number="dropThresholdPct" type="number" min="1" step="1" />
      </label>
      <label title="最小持续时长 ms（缺省 1000，正整数）">
        最小持续 ms
        <input v-model.number="minDurationMs" type="number" min="1" step="100" />
      </label>
      <button class="mini" :disabled="loading" @click="load">{{ loading ? '分析中…' : '重新分析' }}</button>
      <span v-if="data" class="chip gray" title="契约 §22.2 响应回显 params">
        anchor={{ data.anchor }} · {{ data.params.dropThresholdPct }}% · ≥{{ data.params.minDurationMs }}ms
      </span>
    </div>

    <div v-if="loading && !data" class="empty-hint">根因诊断计算中…（/diagnostics 现算）</div>
    <div v-else-if="errorMsg" class="empty-hint">
      诊断不可用：{{ errorMsg }}
      <template>（live 后端可能未实现契约 §22.2 /diagnostics 端点）</template>
    </div>

    <template v-else-if="data">
      <!-- 下降区间列表（点击 → 回放定位该区间起点，复用 M4 时间定位） -->
      <div class="rcc-section">
        <div class="rcc-title">
          下降区间 ×{{ data.dropIntervals.length }}
          <span class="hint">（点击区间 → 回放定位；起止 = epoch ms 相对数据起点）</span>
        </div>
        <table v-if="data.dropIntervals.length > 0" class="rcc-table">
          <thead>
            <tr>
              <th>#</th>
              <th>起点</th>
              <th>终点</th>
              <th>持续</th>
              <th>降幅</th>
              <th>baseline → trough</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(iv, i) in data.dropIntervals" :key="`${iv.fromMs}-${iv.toMs}`" class="rcc-row" @click="jumpToReplay(iv.fromMs)">
              <td>{{ i + 1 }}</td>
              <td class="mono">{{ fmtRel(iv.fromMs) }}</td>
              <td class="mono">{{ fmtRel(iv.toMs) }}</td>
              <td>{{ fmtDur(iv.durationMs) }}</td>
              <td class="drop">−{{ iv.dropPct }}%</td>
              <td class="mono">{{ iv.baseline }} → {{ iv.trough }}</td>
              <td><button class="mini" title="跳转回放并定位该区间（复用 M4 /replay 与游标时间定位）">回放 ▸</button></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="stat-note">※ 无下降区间（区间为常态空，不填假区间）。</div>
      </div>

      <!-- 相关系数条形（正负着色；样本对 <3 的指标服务端不出行） -->
      <div class="rcc-section">
        <div class="rcc-title">
          指标相关系数 ×{{ data.correlations.length }}
          <span class="hint">（皮尔逊，下降区间内采样点对齐；<span class="pos">正</span>/<span class="neg">负</span> 着色）</span>
        </div>
        <div v-if="data.correlations.length > 0" class="rcc-corr">
          <div v-for="c in data.correlations" :key="c.metricId" class="corr-row" :title="`${labelOf(c.metricId)}：pearson=${c.pearson}（样本对 ${c.sampleCount}）`">
            <span class="corr-name" :title="c.metricId">{{ labelOf(c.metricId) }}</span>
            <div class="corr-track">
              <div
                class="corr-bar"
                :class="c.pearson >= 0 ? 'pos' : 'neg'"
                :style="{ width: barPct(c.pearson) }"
              ></div>
            </div>
            <span class="corr-val mono" :class="c.pearson >= 0 ? 'pos' : 'neg'">{{ c.pearson > 0 ? '+' : '' }}{{ c.pearson }}</span>
            <span class="corr-n">n={{ c.sampleCount }}</span>
          </div>
        </div>
        <div v-else class="stat-note">※ 无相关系数行（锚指标无下降区间，或各指标区间内样本对 &lt;3 —— 不填假相关）。</div>
      </div>

      <!-- 候选原因 Top3（score 主导项 |pearson|，rank 按 score 排序） -->
      <div class="rcc-section">
        <div class="rcc-title">候选原因 Top{{ Math.min(3, Math.max(data.candidates.length, 1)) }}（score 排序）</div>
        <div v-if="data.candidates.length > 0" class="rcc-cands">
          <div v-for="c in data.candidates" :key="c.rank" class="cand-card">
            <div class="cand-head">
              <span class="cand-rank">#{{ c.rank }}</span>
              <span class="cand-name">{{ labelOf(c.metricId) }}</span>
              <span class="cand-score mono" :class="c.score >= 0 ? 'pos' : 'neg'">score {{ c.score > 0 ? '+' : '' }}{{ c.score }}</span>
            </div>
            <div class="cand-evidence">{{ c.evidence }}</div>
          </div>
        </div>
        <div v-else class="stat-note">※ 无候选原因（无相关系数行即无候选，不填假候选）。</div>
      </div>

      <!-- AI 叙述：本地无 LLM key / 契约无 LLM 端点 → 占位，不伪造（R51 要求） -->
      <div class="rcc-section">
        <div class="rcc-title">AI 叙述</div>
        <div class="ai-placeholder">
          <span class="ai-icon">✦</span>
          <div>
            <div class="ai-title">AI 叙述不可用</div>
            <div class="ai-desc">
              本部署未配置 LLM key，且契约 §22.2 不含 LLM 端点（LLM 只生成叙述不改变判定
              [P§2.2]）；上方 evidence 为服务端规则生成的确定性文本。不伪造 AI 输出。
            </div>
          </div>
        </div>
      </div>

      <div v-if="data.note" class="stat-note" style="margin-top: 8px">※ {{ data.note }}</div>
      <div v-else-if="!hasAnyData" class="stat-note">※ 该记录诊断数据面为空。</div>
    </template>
  </div>
</template>

<style scoped>
.rcc {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  padding: 10px 12px;
}

.rcc-params {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.rcc-params label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-dim);
}

.rcc-params select,
.rcc-params input {
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  font-size: 11px;
  padding: 2px 4px;
}

.rcc-params input {
  width: 64px;
  -moz-appearance: textfield;
}

.rcc-params input::-webkit-outer-spin-button,
.rcc-params input::-webkit-inner-spin-button {
  -webkit-appearance: none;
}

.rcc-section {
  margin-top: 10px;
}

.rcc-title {
  font-size: 11px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  margin-bottom: 6px;
}

.rcc-title .hint {
  text-transform: none;
  letter-spacing: 0;
}

.rcc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11.5px;
}

.rcc-table th {
  text-align: left;
  color: var(--text-faint);
  font-weight: 400;
  padding: 3px 6px;
  border-bottom: 1px solid var(--border);
}

.rcc-table td {
  padding: 3px 6px;
  border-bottom: 1px solid #1c2531;
}

.rcc-row {
  cursor: pointer;
}

.rcc-row:hover {
  background: #1a222d;
}

.rcc-table .drop {
  color: #e5534b;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.rcc-corr {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.corr-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
}

.corr-name {
  width: 170px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-dim);
}

.corr-track {
  flex: 1;
  height: 10px;
  background: #10161d;
  border-radius: 3px;
  overflow: hidden;
}

.corr-bar {
  height: 100%;
  border-radius: 3px;
}

.corr-bar.pos {
  background: #d29922;
}

.corr-bar.neg {
  background: #2f81f7;
}

.corr-val {
  width: 48px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.corr-val.pos {
  color: #d29922;
}

.corr-val.neg {
  color: #79b8ff;
}

.corr-n {
  width: 52px;
  color: var(--text-faint);
  font-size: 10.5px;
  text-align: right;
}

.rcc-cands {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.cand-card {
  flex: 1;
  min-width: 220px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  background: var(--bg-panel-2);
}

.cand-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 3px;
}

.cand-rank {
  color: #d29922;
  font-weight: 700;
  font-size: 12px;
}

.cand-name {
  font-size: 12px;
  font-weight: 600;
}

.cand-score {
  margin-left: auto;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.cand-score.pos {
  color: #d29922;
}

.cand-score.neg {
  color: #79b8ff;
}

.cand-evidence {
  font-size: 11px;
  color: var(--text-dim);
  line-height: 1.6;
}

/* AI 叙述占位（不伪造） */
.ai-placeholder {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  border: 1px dashed var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  color: var(--text-faint);
}

.ai-icon {
  font-size: 16px;
  line-height: 1.2;
}

.ai-title {
  font-size: 12px;
  color: var(--text-dim);
  margin-bottom: 2px;
}

.ai-desc {
  font-size: 11px;
  line-height: 1.6;
}
</style>
