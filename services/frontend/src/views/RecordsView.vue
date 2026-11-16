<script setup lang="ts">
import { ref } from 'vue'
import { useStore } from '../store'
import type { RunDetail } from '../api-client'

/** 测试记录列表（对标 [I§2.9] 用例表的最小 M1 形态：runs 索引 + 摘要展开） */
const store = useStore()

const expandedRunId = ref('')
const loadingRun = ref(false)
const runDetail = ref<RunDetail | null>(null)
const runError = ref('')

async function toggleRun(runId: string): Promise<void> {
  if (expandedRunId.value === runId) {
    expandedRunId.value = ''
    runDetail.value = null
    return
  }
  expandedRunId.value = runId
  runDetail.value = null
  runError.value = ''
  loadingRun.value = true
  try {
    runDetail.value = await store.getRunDetail(runId)
  } catch (err) {
    runError.value = String(err)
  } finally {
    loadingRun.value = false
  }
}

function fmtTime(ts: number): string {
  const d = new Date(ts)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
</script>

<template>
  <div class="records-wrap">
    <div class="records-head">
      <strong>测试记录</strong>
      <span class="hint">saved 会话的运行记录（runs 索引）</span>
      <span style="flex: 1"></span>
      <button :disabled="store.state.runsLoading" @click="store.loadRuns()">
        {{ store.state.runsLoading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <table class="records-table" v-if="store.state.runs.length > 0">
      <thead>
        <tr>
          <th>用例名</th>
          <th>设备</th>
          <th>应用</th>
          <th>创建时间</th>
          <th>汇总</th>
          <th>路径</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="run in store.state.runs" :key="run.runId">
          <tr class="clickable" @click="toggleRun(run.runId)">
            <td class="mono">{{ run.caseName }}</td>
            <td>{{ run.device ?? '—' }}</td>
            <td class="mono">{{ run.packageName ?? '—' }}</td>
            <td>{{ fmtTime(run.createdAt) }}</td>
            <td>
              <span class="chip" :class="run.summaryAvailable ? 'green' : 'gray'">
                {{ run.summaryAvailable ? '可用' : '无' }}
              </span>
            </td>
            <td class="mono">{{ run.path }}</td>
            <td>
              <span class="row-actions" @click.stop>
                <button class="mini" @click="store.openRunReport(run.runId, run.caseName)">报告</button>
                <button class="mini" @click="store.exportRun(run.runId)">导出</button>
              </span>
            </td>
          </tr>
          <tr v-if="expandedRunId === run.runId">
            <td colspan="7" style="padding: 0">
              <div class="run-summary">
                <div v-if="loadingRun" class="hint">加载中…</div>
                <div v-else-if="runError" class="hint">{{ runError }}</div>
                <template v-else-if="runDetail?.summary?.metrics && runDetail.summary.metrics.length > 0">
                  <table>
                    <thead>
                      <tr>
                        <th>指标</th>
                        <th>样本数</th>
                        <th>均值</th>
                        <th>最小</th>
                        <th>最大</th>
                        <th>最新</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="m in runDetail.summary.metrics" :key="m.metricId">
                        <td>{{ m.metricId }}</td>
                        <td>{{ m.count ?? '—' }}</td>
                        <td>{{ m.avg ?? '—' }} {{ m.unit ?? '' }}</td>
                        <td>{{ m.min ?? '—' }}</td>
                        <td>{{ m.max ?? '—' }}</td>
                        <td>{{ m.latest ?? '—' }}</td>
                      </tr>
                    </tbody>
                  </table>
                </template>
                <div v-else class="hint">该记录无汇总数据</div>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    <div v-else class="empty-hint">
      暂无测试记录 —— 开始监控 → 开始记录 → 停止并「保存」后，运行记录会出现在这里。
    </div>
  </div>
</template>
