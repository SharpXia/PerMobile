<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useStore } from '../store'
import type { CaseRecord, CaseSortKey } from '../api-client'

/**
 * 我的数据页（R42，[I§2.9] Web 用例表 + [F§5] 用例管理）：
 * - 筛选条：用例名（query 子串）+ 项目下拉（packageName）+ 设备下拉 + 重置；
 * - 列：名称 / 项目 / 设备 / Avg(FPS) / Smooth(稳帧指数) / Jank(/10min) / Peak(Memory)[MB] /
 *   Avg(AppCPU)[%] / Avg(Power)[mA] / 上传时间（可排序）+ 行菜单 ⋮；
 * - 行菜单（[I§2.9] S2-img29 的本地可用子集，R42 口径：回放 / 报告 / 导出 / 改名 /
 *   加入对比池 / 删除）：导出走 §13.1 GET /api/runs/{id}/export.xlsx（store.exportRun
 *   Blob 下载）；PerfDog 行菜单另有 归档/分享/删除图片 —— 依赖任务/分享体系（M8+ R34/R48），
 *   不伪装入口。
 * - 对接契约 §14：GET /api/cases（query/device/sort/order/limit/offset）+
 *   PATCH（改名）+ DELETE（删除）+ POST /api/cases/rebuild（索引修复入口）。
 * - 边界（如实）：契约 §14 服务端筛选参数仅 query/device，「项目」筛选在取回窗口内
 *   前端执行（store.loadCases 窗口化取 200 条），此时隐藏分页控件并提示。
 */
const store = useStore()

const menuRunId = ref('')
const renameTarget = ref<CaseRecord | null>(null)
const renameName = ref('')
const renameBusy = ref(false)
const deleteTarget = ref<CaseRecord | null>(null)
const deleteBusy = ref(false)
const searchInput = ref('')

const filter = computed(() => store.state.caseFilter)
const windowed = computed(() => Boolean(filter.value.packageName))

/** 设备下拉：已知设备 ∪ 用例表出现过的设备标识 */
const deviceOptions = computed<string[]>(() => {
  const set = new Set<string>()
  for (const d of store.state.devices) set.add(d.id)
  for (const c of store.state.cases) set.add(c.device)
  return [...set].sort()
})

/** 项目下拉：用例表出现过的 packageName（当前窗口内） */
const pkgOptions = computed<string[]>(() => {
  const set = new Set<string>()
  for (const c of store.state.cases) if (c.packageName) set.add(c.packageName)
  return [...set].sort()
})

onMounted(() => {
  searchInput.value = filter.value.text
  void store.loadCases()
})

function applyTextFilter(): void {
  store.setCaseFilter({ text: searchInput.value })
  void store.loadCases()
}

function applySelectFilter(): void {
  store.setCaseFilter({ device: filter.value.device, packageName: filter.value.packageName })
  void store.loadCases()
}

function resetFilter(): void {
  searchInput.value = ''
  store.setCaseFilter({ text: '', device: '', packageName: '' })
  void store.loadCases()
}

/** 列定义与表体单元格同序；key=null 的列（项目/设备）不可排序（契约 §14 排序键不含它们） */
const COLUMNS: Array<{ key: CaseSortKey | 'caseName' | null; label: string; tip?: string }> = [
  { key: 'caseName', label: '名称' },
  { key: null, label: '项目' },
  { key: null, label: '设备' },
  { key: 'avgFps', label: 'Avg(FPS)', tip: 'fps 点列均值 [F§3.1]（§12 统计层）' },
  { key: 'smooth', label: 'Smooth(稳帧指数)', tip: '近似口径：官方未公开算法公式 [F§3.5][F§10.1]' },
  { key: 'jankPer10min', label: 'Jank(/10min)', tip: 'Jank 次数 × 600000 / durationMs [F§3.3]' },
  { key: 'peakMemoryMb', label: 'Peak(Memory)[MB]', tip: 'memory.pss.peak_mb 采样序列最大值' },
  { key: 'avgAppCpuPct', label: 'Avg(AppCPU)[%]', tip: 'cpu.proc.pct 采样序列均值（进程级口径）' },
  { key: 'avgPowerMa', label: 'Avg(Power)[mA]', tip: 'power.current.ma 采样序列均值（USB 电流口径，取绝对值；原始符号见 tags.currentRawMa）' },
  { key: 'createdAt', label: '上传时间', tip: '按最新上传时间排列（缺省，[I§2.9]）' },
]

function isSortable(key: string): key is CaseSortKey {
  return key !== 'caseName'
}

function onSort(key: CaseSortKey | 'caseName'): void {
  if (!isSortable(key)) return // 名称列本地不排序（保持服务端排序口径一致）
  store.toggleCaseSort(key)
}

function sortMark(key: CaseSortKey | 'caseName'): string {
  if (!isSortable(key) || filter.value.sort !== key) return ''
  return filter.value.order === 'asc' ? '▲' : '▼'
}

function fmtNum(v: number | null): string {
  return v === null ? '—' : String(Math.round(v * 10) / 10)
}

function fmtTime(ts: number): string {
  const d = new Date(ts)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/* ---- 行菜单（⋮） ---- */

function toggleMenu(runId: string): void {
  menuRunId.value = menuRunId.value === runId ? '' : runId
}

function closeMenu(): void {
  menuRunId.value = ''
}

function poolDisabled(rec: CaseRecord): { disabled: boolean; title: string } {
  if (store.inComparePool(rec.runId)) return { disabled: true, title: '已在对比池中' }
  if (store.comparePoolFull()) return { disabled: true, title: '对比池最多 5 条用例（[I§2.9]）' }
  return { disabled: false, title: '加入对比池（最多 5 条同时对比）' }
}

function addToPool(rec: CaseRecord): void {
  closeMenu()
  store.addToComparePool(rec)
}

function openRename(rec: CaseRecord): void {
  closeMenu()
  renameTarget.value = rec
  renameName.value = rec.caseName
}

async function confirmRename(): Promise<void> {
  const target = renameTarget.value
  if (!target || !renameName.value.trim() || renameBusy.value) return
  renameBusy.value = true
  const ok = await store.renameCase(target.runId, renameName.value.trim())
  renameBusy.value = false
  if (ok) {
    renameTarget.value = null
    renameName.value = ''
  }
}

function openDelete(rec: CaseRecord): void {
  closeMenu()
  deleteTarget.value = rec
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target || deleteBusy.value) return
  deleteBusy.value = true
  const ok = await store.deleteCase(target.runId)
  deleteBusy.value = false
  if (ok) deleteTarget.value = null
}

/* ---- 分页（契约 §14 limit/offset + Total） ---- */

const page = computed(() => Math.floor(filter.value.offset / filter.value.limit))
const totalPages = computed(() => Math.max(1, Math.ceil(store.state.casesTotal / filter.value.limit)))

function goPage(delta: number): void {
  const next = Math.min(Math.max(0, page.value + delta), totalPages.value - 1)
  if (next === page.value) return
  store.setCaseFilter({ offset: next * filter.value.limit })
  void store.loadCases()
}

function openReport(rec: CaseRecord): void {
  store.openRunReport(rec.runId, rec.caseName)
}

/** 行菜单「回放」（M4 R37，[I§2.5] 回放入口；主窗口复用，切换数据源到该 run） */
function openReplay(rec: CaseRecord): void {
  closeMenu()
  store.openReplay(rec.runId, rec.caseName)
}

/** 行菜单「导出」（§13.1，[I§4.2] Excel 导出）：store.exportRun → GET /api/runs/{id}/export.xlsx
 *  Blob 落地为浏览器下载（文件名 = run 目录名.xlsx，Content-Disposition 权威） */
function exportRun(rec: CaseRecord): void {
  closeMenu()
  void store.exportRun(rec.runId)
}
</script>

<template>
  <div class="cases-wrap" @click="closeMenu">
    <div class="records-head">
      <strong>我的数据</strong>
      <span class="hint">用例库（runs 事实源 + 索引快照，契约 §14）</span>
      <span style="flex: 1"></span>
      <button
        class="primary"
        :disabled="store.comparePoolFull()"
        title="已加入对比池的用例可在「对比报告」页对比"
        @click="store.state.activeTab = 'compare'"
      >
        对比池({{ store.state.comparePool.length }}) ▸ 开始对比
      </button>
      <button title="runs 目录被手动改动后，用事实源重建索引（幂等）" @click="store.rebuildCases()">重建索引</button>
    </div>

    <!-- 筛选条（[I§2.9]：用例名搜索 + 项目下拉 + 设备下拉 + 重置） -->
    <div class="cases-filter">
      <input
        v-model="searchInput"
        type="text"
        placeholder="用例名搜索（子串）"
        style="width: 220px"
        @keyup.enter="applyTextFilter"
      />
      <button class="mini" @click="applyTextFilter">搜索</button>
      <label class="cases-filter-label">项目</label>
      <select v-model="filter.packageName" @change="applySelectFilter">
        <option value="">全部</option>
        <option v-for="p in pkgOptions" :key="p" :value="p">{{ p }}</option>
      </select>
      <label class="cases-filter-label">设备</label>
      <select v-model="filter.device" @change="applySelectFilter">
        <option value="">全部</option>
        <option v-for="d in deviceOptions" :key="d" :value="d">{{ d }}</option>
      </select>
      <button class="mini" @click="resetFilter">重置</button>
      <span style="flex: 1"></span>
      <span v-if="windowed" class="chip blue" title="契约 §14 服务端仅提供用例名/设备筛选参数；项目筛选在取回窗口内执行">
        项目筛选=窗口内过滤
      </span>
    </div>

    <div v-if="store.state.casesLoading" class="empty-hint">加载中…</div>
    <table v-else-if="store.state.cases.length > 0" class="records-table cases-table">
      <thead>
        <tr>
          <th
            v-for="col in COLUMNS"
            :key="col.key ?? col.label"
            :class="{ sortable: col.key !== null }"
            :title="col.tip"
            @click="col.key && onSort(col.key)"
          >
            {{ col.label }} <span class="sort-mark">{{ col.key ? sortMark(col.key) : '' }}</span>
          </th>
          <th style="width: 34px"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="rec in store.state.cases" :key="rec.runId">
          <td class="mono case-name" :title="`${rec.caseName}\n${rec.path}`" @click="openReport(rec)">{{ rec.caseName }}</td>
          <td class="mono dim">{{ rec.packageName ?? '—' }}</td>
          <td>{{ rec.device }}</td>
          <td class="num">{{ fmtNum(rec.stats.avgFps) }}</td>
          <td class="num" :title="rec.stats.smooth === null ? '近似口径：官方未公开算法公式 [F§3.5]' : '近似口径（Smooth）[F§3.5]'">
            {{ fmtNum(rec.stats.smooth) }}
          </td>
          <td class="num">{{ fmtNum(rec.stats.jankPer10min) }}</td>
          <td class="num">{{ fmtNum(rec.stats.peakMemoryMb) }}</td>
          <td class="num">{{ fmtNum(rec.stats.avgAppCpuPct) }}</td>
          <td class="num">{{ fmtNum(rec.stats.avgPowerMa) }}</td>
          <td>{{ fmtTime(rec.createdAt) }}</td>
          <td style="position: relative" @click.stop>
            <button class="mini menu-btn" @click="toggleMenu(rec.runId)">⋮</button>
            <div v-if="menuRunId === rec.runId" class="row-menu">
              <button class="row-menu-item" @click="openReplay(rec)">回放</button>
              <button class="row-menu-item" @click="openReport(rec)">报告</button>
              <button class="row-menu-item" @click="exportRun(rec)">导出</button>
              <button class="row-menu-item" @click="openRename(rec)">改名</button>
              <button
                class="row-menu-item"
                :disabled="poolDisabled(rec).disabled"
                :title="poolDisabled(rec).title"
                @click="addToPool(rec)"
              >
                加入对比池
              </button>
              <button class="row-menu-item danger" @click="openDelete(rec)">删除</button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty-hint">
      暂无用例 —— PerfDog 空态提示「使用客户端测试上传用例」[I§2.9]；本端：实时监控 → 开始记录 → 停止并保存后，用例会登记到这里（契约 §14）。
    </div>

    <div v-if="!windowed && store.state.casesTotal > 0" class="cases-pager">
      <span>Total {{ store.state.casesTotal }}</span>
      <button class="mini" :disabled="page === 0" @click="goPage(-1)">上一页</button>
      <span>{{ page + 1 }} / {{ totalPages }}</span>
      <button class="mini" :disabled="page >= totalPages - 1" @click="goPage(1)">下一页</button>
    </div>
    <div v-else-if="windowed" class="hint" style="padding: 8px 0">窗口内命中 {{ store.state.cases.length }} 条（项目筛选作用域）</div>

    <!-- 改名对话框（[I§2.9] 行菜单「改名」；契约 §14：只改显示名，runId 不变） -->
    <div v-if="renameTarget" class="modal-mask" @click.self="renameTarget = null">
      <div class="modal">
        <h3>改名</h3>
        <div class="modal-sub">{{ renameTarget.runId }}（runId 保持不变）</div>
        <div class="field">
          <label>Case name</label>
          <input v-model="renameName" type="text" @keyup.enter="confirmRename" />
        </div>
        <div class="modal-actions">
          <button @click="renameTarget = null">取消</button>
          <button class="primary" :disabled="!renameName.trim() || renameBusy" @click="confirmRename">
            {{ renameBusy ? '保存中…' : '确认' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除确认（契约 §14：删事实源 run 目录 + 索引行 + 同名 .xlsx，不可恢复） -->
    <div v-if="deleteTarget" class="modal-mask" @click.self="deleteTarget = null">
      <div class="modal">
        <h3>删除用例</h3>
        <div class="modal-sub">
          将删除 <b>{{ deleteTarget.caseName }}</b> 的事实源（run 目录）、索引行与同名 .xlsx（契约 §14），不可恢复。
        </div>
        <div class="modal-actions">
          <button @click="deleteTarget = null">取消</button>
          <button class="stop" :disabled="deleteBusy" @click="confirmDelete">{{ deleteBusy ? '删除中…' : '删除' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cases-wrap {
  padding: 14px;
}

.cases-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.cases-filter input {
  width: 220px;
}

.cases-filter-label {
  color: var(--text-dim);
  font-size: 12px;
}

.cases-table th.sortable {
  cursor: pointer;
  user-select: none;
}

.cases-table th.sortable:hover {
  color: var(--text);
}

.sort-mark {
  font-size: 9px;
  color: var(--accent);
}

.case-name {
  cursor: pointer;
}

.case-name:hover {
  color: #79b8ff;
}

.dim {
  color: var(--text-dim);
}

.num {
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.menu-btn {
  padding: 1px 8px;
  font-size: 13px;
}

.row-menu {
  position: absolute;
  right: 4px;
  top: 26px;
  z-index: 20;
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  min-width: 128px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
  display: flex;
  flex-direction: column;
  padding: 3px;
}

.row-menu-item {
  border: none;
  background: transparent;
  text-align: left;
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 12px;
}

.row-menu-item:hover:not(:disabled) {
  background: var(--bg-hover);
}

.row-menu-item.danger {
  color: #ff7b72;
}

.cases-pager {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  font-size: 12px;
  color: var(--text-dim);
}
</style>
