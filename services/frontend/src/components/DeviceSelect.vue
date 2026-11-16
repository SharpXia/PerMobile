<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { Device } from '../api-client'
import { useStore } from '../store'

/**
 * 设备选择器（对标 [I§2.2]）：一行式下拉 + 设备列表；
 * 离线设备灰显但仍可选择（启动会话时由后端返回 device_not_online，如实呈现）。
 */
const store = useStore()
const open = ref(false)
const rootEl = ref<HTMLDivElement>()

defineProps<{ disabled?: boolean; placeholder?: string }>()

const selected = computed(
  () => store.state.devices.find((d) => d.id === store.state.selectedDeviceId) ?? null,
)

function stateText(s: string): string {
  if (s === 'online') return '在线'
  if (s === 'unauthorized') return '未授权'
  return '离线'
}

function select(d: Device): void {
  void store.selectDevice(d.id)
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
    <div class="side-title">设备选择器</div>
    <button class="selector-btn" :disabled="disabled" @click="open = !open">
      <span class="dot" :class="selected?.state ?? 'offline'"></span>
      <span class="label">{{ selected ? selected.model : placeholder ?? '请选择设备' }}</span>
      <span v-if="selected" class="chip gray">{{ selected.connection.toUpperCase() }}</span>
      <span class="caret">▼</span>
    </button>
    <div v-if="open" class="dropdown-panel">
      <div
        v-for="d in store.state.devices"
        :key="d.id"
        class="dropdown-row"
        :class="{ dim: d.state !== 'online' }"
        @click="select(d)"
      >
        <span class="dot" :class="d.state"></span>
        <span class="name" :title="`${d.model}（serial ${d.serial}）`">{{ d.model }}</span>
        <span class="chip gray">{{ d.connection.toUpperCase() }}</span>
        <span class="chip" :class="d.state === 'online' ? 'green' : 'gray'">{{ stateText(d.state) }}</span>
      </div>
      <div v-if="store.state.devices.length === 0" class="empty-hint">No device ?</div>
    </div>
  </div>
</template>
