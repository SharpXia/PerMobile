<script setup lang="ts">
import { ref, watch } from 'vue'
import { useStore } from '../store'

/**
 * 收口保存对话框（R39，对标 [I§2.6] 停止后的显式决策点）：
 * - Case name 默认 PD_<时间戳>（可改）；
 * - ☑ Save excel：契约 §13.2 请求可选字段 exportExcel（[I§2.6] 保存对话框 Save(excel) 复
 *   选，[I§3 要点4]「Save 与其他方式可同时勾选」）；勾选后保存成功即随 §13.1 下载同名
 *   .xlsx —— 本地优先=默认本地导出（R39），故默认勾选；
 * - 确认=保存（生成运行记录）/ 丢弃=放弃本次数据。
 * PerfDog 的 ☑Upload（云端）与 ☑Task+Archive 属 Web 云侧能力（M3+，R43/R44），不在此对话框。
 */
const store = useStore()
const caseName = ref('')
const exportExcel = ref(true)
const busy = ref(false)

watch(
  () => store.state.pendingDecision,
  (open) => {
    if (open) caseName.value = defaultCaseName()
  },
  { immediate: true },
)

function defaultCaseName(): string {
  const d = new Date()
  const p = (n: number): string => String(n).padStart(2, '0')
  return `PD_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

async function onSave(): Promise<void> {
  busy.value = true
  try {
    await store.confirmSave(caseName.value, exportExcel.value)
  } finally {
    busy.value = false
  }
}

function onDiscard(): void {
  void store.confirmDiscard()
}
</script>

<template>
  <div v-if="store.state.pendingDecision" class="modal-mask">
    <div class="modal">
      <h3>测试收口</h3>
      <div class="modal-sub">
        停止后进入显式决策点：保存生成运行记录（写入 runs），或放弃本次数据。
        <template v-if="store.state.session">
          当前会话 {{ store.state.session.id }} · {{ store.state.session.packageName }} ·
          监控 {{ Math.floor(store.sessionDurationSec() / 60) }}m{{ store.sessionDurationSec() % 60 }}s
        </template>
      </div>
      <div class="field">
        <label for="case-name">Case name</label>
        <input id="case-name" v-model="caseName" type="text" placeholder="PD_yyyyMMdd_HHMMSS" />
      </div>
      <label class="field-check" for="save-excel">
        <input id="save-excel" v-model="exportExcel" type="checkbox" />
        <span>Save excel（随保存生成并下载同名 .xlsx）</span>
      </label>
      <div v-if="exportExcel" class="modal-sub">
        勾选后按契约 §13.2 携带 exportExcel 保存；xlsx 下载走 GET /api/runs/{id}/export.xlsx。
        导出失败不影响保存本身（run 已落盘，可从记录/报告页重试）。
      </div>
      <div class="modal-actions">
        <button class="ghost" @click="store.keepDecisionPending()">暂不决定</button>
        <button class="stop" @click="onDiscard">放弃数据</button>
        <button class="primary" :disabled="busy" @click="onSave">
          {{ busy ? '保存中…' : '确认' }}
        </button>
      </div>
    </div>
  </div>
</template>
