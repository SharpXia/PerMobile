<script setup lang="ts">
import { computed } from 'vue'
import type { Session } from '../api-client'
import { useStore } from '../store'

/**
 * 后台会话接管条（页面刷新/换标签页后的恢复入口）。
 *
 * 会话是后端内存态：刷新页面只丢掉前端 store，后端 monitoring/recording 仍在
 * 采集。旧行为下这些会话既看不见也停不掉（变孤儿）。本组件把「后端存活但本页
 * 未接管」的会话列出来，提供两条出口：
 * - 接管：回填设备/应用/指标选择 + 重连 SSE，曲线与标记经既有端点校准（不造数）；
 * - 停止并放弃：收口后端采集（recording 先 record/stop 再 discard），不留孤儿进程。
 */
const store = useStore()

const sessions = computed<Session[]>(() => store.state.orphanSessions)

const STATE_TEXT: Record<string, string> = {
  monitoring: '监控中',
  recording: '记录中',
  stopped: '已停止未收口',
}

const STATE_CLS: Record<string, string> = {
  monitoring: 'blue',
  recording: 'red',
  stopped: 'orange',
}

function stateText(s: Session): string {
  return STATE_TEXT[s.state] ?? s.state
}

function busy(s: Session): boolean {
  return store.state.recoveryBusyId === s.id
}

function fmtAge(createdAt: number): string {
  const sec = Math.max(0, Math.floor((store.state.now - createdAt) / 1000))
  const m = Math.floor(sec / 60)
  const ss = sec % 60
  return m > 0 ? `${m}m${String(ss).padStart(2, '0')}s` : `${ss}s`
}

function closeHint(s: Session): string {
  if (s.state === 'stopped') {
    return '放弃后 staging run 目录一并删除（要保留数据请先「接管」再保存）'
  }
  return '停止后端采集并放弃本次数据（不会写入 runs）'
}
</script>

<template>
  <div v-if="sessions.length > 0 && !store.state.replay" class="recovery-bar">
    <div class="recovery-head">
      <span class="recovery-title">检测到 {{ sessions.length }} 个后台会话</span>
      <span class="hint">
        刷新/关闭页面不会结束后端采集；可接管继续查看，或停止放弃以收口（不留孤儿采集）。
      </span>
      <span style="flex: 1"></span>
      <button class="mini" @click="store.refreshBackendSessions()">刷新列表</button>
    </div>
    <div v-for="s in sessions" :key="s.id" class="recovery-row">
      <span class="chip blue mono">{{ s.id }}</span>
      <span class="chip" :class="STATE_CLS[s.state] ?? 'gray'">{{ stateText(s) }}</span>
      <span class="mono recovery-dev">{{ s.deviceId }}</span>
      <span class="mono recovery-pkg">{{ s.packageName }}</span>
      <span class="hint">已运行 {{ fmtAge(s.createdAt) }}</span>
      <span style="flex: 1"></span>
      <button
        class="mini primary"
        :disabled="busy(s) || store.state.session !== null"
        :title="store.state.session ? '当前已有接管中的会话，先停止并收口后再接管' : '接管该会话：恢复曲线、标记与记录时长'"
        @click="store.adoptSession(s.id)"
      >
        {{ busy(s) ? '处理中…' : '接管' }}
      </button>
      <button
        class="mini stop"
        :disabled="busy(s)"
        :title="closeHint(s)"
        @click="store.closeBackendSession(s.id)"
      >
        停止并放弃
      </button>
    </div>
  </div>
</template>
