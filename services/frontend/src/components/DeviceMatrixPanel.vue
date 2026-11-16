<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { Device } from '../api-client'
import {
  connIcon,
  connTitle,
  menuItemsFor,
  miniReadoutFor,
  platformIcon,
  type DeviceMenuItem,
} from '../capability'
import { useStore } from '../store'

/**
 * 设备矩阵面板（M6，R22，对标 [I§2.2] 设备列表 + 官网多设备矩阵演示口径 [F§4]）：
 * - 多设备卡片按平台分组（平台图标按 d.platform 切换 + 分组标题）；
 * - 每卡：连接模式图标区分（回形针=USB / 扇形=WiFi / 🖥️=Simulator 宿主行 §23.0，
 *   同一物理设备两行并存时按设备 id 独立判定，§20.4）+ 在线状态 + 电量/刷新率；
 * - 每设备可独立开会话（monitor-only 接入即显示，不落盘；synthetic 两台同时监控演示）
 *   —— 卡片迷你读数按设备分流（capability.miniReadoutFor：Android FPS/CPU/MEM、
 *   iOS 模拟器宿主双指标 §23.2、iOS 真机 xctrace 双指标 §26；FPS 行 iOS 不渲染）；
 * - 操作菜单（⋮）按设备分流（capability.menuItemsFor）：Android usb/wifi → WiFi 模式
 *   两项（R15，§21.1 real-only；synthetic 返回 501 + 诊断反馈）；iOS 模拟器 offline →
 *   「启动模拟器」（§23.1 sim-boot，成功后设备行转在线）；iOS 真机与其余 → 空
 *   （WiFi 为 adb 专属，隐藏 real-only duck-call 入口）。
 * 交互面判定全部收敛为 capability.ts 纯函数（npm run check:matrix 直测），本组件只绑定。
 */
const store = useStore()

/** 平台分组（[I§2.2] Mobile/Windows/Switch 分组形态） */
const PLATFORM_LABEL: Record<string, string> = { android: 'Android（Mobile）', ios: 'iOS（Mobile）', windows: 'Windows' }

const groups = computed<Array<{ platform: string; label: string; devices: Device[]; idleSims: Device[] }>>(() => {
  const byPlatform = new Map<string, Device[]>()
  for (const d of store.state.devices) {
    const cur = byPlatform.get(d.platform)
    if (cur) cur.push(d)
    else byPlatform.set(d.platform, [d])
  }
  return [...byPlatform.entries()].map(([platform, devices]) => ({
    platform,
    label: PLATFORM_LABEL[platform] ?? platform,
    // 未启动模拟器（offline + simulator 连接）沉底折叠（UI 反馈 #1：关机模拟器
    // 常占 ~30 行淹没在线设备）；其余设备维持原序（offline 真机/安卓仍直接可见）
    devices: devices.filter((d) => !(d.state !== 'online' && d.connection === 'simulator')),
    idleSims: devices.filter((d) => d.state !== 'online' && d.connection === 'simulator'),
  }))
})

/** 未启动模拟器分区展开态（默认收起；按平台各自记忆） */
const idleSimsOpen = ref<Set<string>>(new Set())

function toggleIdleSims(platform: string): void {
  const next = new Set(idleSimsOpen.value)
  if (next.has(platform)) next.delete(platform)
  else next.add(platform)
  idleSimsOpen.value = next
}

function stateText(s: string): string {
  if (s === 'online') return '在线'
  if (s === 'unauthorized') return '未授权'
  return '离线'
}

function select(d: Device): void {
  if (d.state !== 'online') return
  void store.selectDevice(d.id)
}

const rt = (deviceId: string) => store.state.matrixSessions[deviceId]

/** 卡片迷你读数（行集合按设备分流自 capability.miniReadoutFor；per-core/thread 标签序列取首条） */
function readOf(deviceId: string, metricId: string): string {
  const latest = rt(deviceId)?.latest ?? {}
  const direct = latest[metricId]
  if (direct !== undefined) return fmtVal(direct)
  const prefix = `${metricId}@`
  for (const [k, v] of Object.entries(latest)) {
    if (k.startsWith(prefix)) return fmtVal(v)
  }
  return '—'
}
function fmtVal(v: number | null): string {
  return v === null || !Number.isFinite(v) ? '—' : Math.round(v * 10) / 10 === Math.round(v) ? String(Math.round(v)) : (Math.round(v * 10) / 10).toFixed(1)
}

/* ---------------- ⋮ 操作菜单（条目按设备分流自 capability.menuItemsFor） ---------------- */

const menuFor = ref<string | null>(null)
const hostInput = ref('')
const hostFor = ref<string | null>(null)
const rootEl = ref<HTMLDivElement>()

function toggleMenu(deviceId: string): void {
  menuFor.value = menuFor.value === deviceId ? null : deviceId
  hostFor.value = null
  hostInput.value = ''
}

/** 菜单项 → 动作分发（kind 由纯函数给定；组件不做二次设备判定） */
function onMenuItem(d: Device, item: DeviceMenuItem): void {
  if (item.kind === 'wifi-enable') {
    void wifiEnable(d.id)
    return
  }
  if (item.kind === 'wifi-connect') {
    wifiConnectStart(d.id)
    return
  }
  // sim-boot：bootstatus 阻塞最长 ~180s（§23.1），收起菜单靠状态行反馈进度
  menuFor.value = null
  hostFor.value = null
  void store.simBoot(d.id)
}

async function wifiEnable(deviceId: string): Promise<void> {
  menuFor.value = null
  await store.setWifiMode(deviceId, { action: 'enable' })
}

function wifiConnectStart(deviceId: string): void {
  hostFor.value = deviceId
}

async function wifiConnectCommit(deviceId: string): Promise<void> {
  const host = hostInput.value.trim()
  if (!host) {
    store.setError('WiFi 连接需要设备 IP（host 必填，契约 §21.1 validation_error）')
    return
  }
  menuFor.value = null
  hostFor.value = null
  hostInput.value = ''
  await store.setWifiMode(deviceId, { action: 'connect', host })
}

function onDocClick(e: MouseEvent): void {
  if (rootEl.value && !rootEl.value.contains(e.target as Node)) {
    menuFor.value = null
    hostFor.value = null
  }
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="rootEl" class="side-block">
    <div class="side-title">设备矩阵<span class="side-tag">R22 · 每设备独立会话</span></div>

    <div v-for="g in groups" :key="g.platform" class="dm-group">
      <div class="dm-group-head">
        <span>{{ platformIcon(g.platform) }}</span>
        <span>{{ g.label }}</span>
        <span class="dm-count">{{ g.devices.length + g.idleSims.length }}</span>
      </div>

      <!-- 未启动模拟器（UI 反馈 #1）：默认收起沉底；展开后卡片照常渲染（⋮ 可 sim-boot） -->
      <button
        v-if="g.idleSims.length > 0"
        class="dm-idle-toggle"
        :title="`展开/收起未启动模拟器（${g.idleSims.length} 台，⋮ → 启动模拟器）`"
        @click="toggleIdleSims(g.platform)"
      >
        {{ idleSimsOpen.has(g.platform) ? '▾' : '▸' }} 未启动模拟器（{{ g.idleSims.length }}）
      </button>
      <div
        v-for="d in (idleSimsOpen.has(g.platform) ? [...g.devices, ...g.idleSims] : g.devices)"
        :key="d.id"
        class="dm-card"
        :class="{ dim: d.state !== 'online', selected: d.id === store.state.selectedDeviceId }"
        @click="select(d)"
      >
        <div class="dm-row">
          <span class="dm-platform">{{ platformIcon(d.platform) }}</span>
          <span class="dm-model" :title="`${d.model}（serial ${d.serial}）`">{{ d.model }}</span>
          <span class="dm-conn" :class="d.connection" :title="connTitle(d)">{{ connIcon(d) }}</span>
          <span class="dot" :class="d.state" :title="stateText(d.state)"></span>
        </div>

        <div class="dm-row dm-sub">
          <span class="chip gray" :title="`连接模式 ${d.connection.toUpperCase()}（[I§2.2] USB/WiFi 各一行；simulator=§23.0）`">
            {{ d.connection.toUpperCase() }}
          </span>
          <span class="chip gray">{{ stateText(d.state) }}</span>
          <span v-if="d.info?.batteryLevel != null" class="chip gray">🔋{{ d.info.batteryLevel }}%</span>
          <span v-if="d.profile?.refreshRateHz != null" class="chip gray" title="设备 profile 刷新率（§20.2）">
            {{ d.profile.refreshRateHz }}Hz
          </span>
          <span v-if="d.profile?.gpuSource" class="chip gray" title="GPU 源（profile.gpuSource，§20.2；kgsl 驱动 GPU 利用率可用）">
            {{ d.profile.gpuSource }}
          </span>
        </div>

        <!-- 矩阵卡会话运行时（monitor-only；两台同时监控演示 R22；读数行按设备分流 S3） -->
        <template v-if="rt(d.id)">
          <div class="dm-live">
            <span class="chip" :class="rt(d.id)!.state === 'recording' ? 'red' : 'blue'">
              {{ rt(d.id)!.state === 'recording' ? '记录中' : '监控中' }}
            </span>
            <span v-for="row in miniReadoutFor(d)" :key="row.key" class="dm-read" :title="row.title">
              {{ row.key }} {{ readOf(d.id, row.metricId) }}{{ row.suffix }}
            </span>
            <span class="dm-samples">{{ rt(d.id)!.samples }} pts</span>
          </div>
          <div class="dm-actions">
            <button class="mini stop" @click.stop="store.stopMatrixSession(d.id)">■ 停止</button>
            <span class="hint">monitor-only：接入即显示不落盘（记录/保存走主监控区）</span>
          </div>
        </template>

        <!-- 操作行：开会话 + ⋮ 菜单（条目按设备分流；无条目的设备不渲染 ⋮，S3） -->
        <div v-else class="dm-actions">
          <button class="mini primary" :disabled="d.state !== 'online'" title="该设备独立开会话（monitor-only，缺省指标按平台分流 §20.3）" @click.stop="store.startMatrixSession(d.id)">
            ▶ 开会话
          </button>
          <button v-if="menuItemsFor(d).length > 0" class="mini" :title="`${d.model} 操作菜单`" @click.stop="toggleMenu(d.id)">⋮</button>
          <span
            v-if="store.state.wifiStatus[d.id]"
            class="dm-op-status"
            :class="{ fail: store.state.wifiStatus[d.id]!.includes('不可用') || store.state.wifiStatus[d.id]!.includes('失败') }"
          >
            {{ store.state.wifiStatus[d.id] }}
          </span>
          <span
            v-else-if="store.state.simBootStatus[d.id]"
            class="dm-op-status"
            :class="{ fail: store.state.simBootStatus[d.id]!.includes('不可用') || store.state.simBootStatus[d.id]!.includes('失败') }"
          >
            {{ store.state.simBootStatus[d.id] }}
          </span>
        </div>

        <!-- ⋮ 菜单：条目由 capability.menuItemsFor 给定（WiFi §21.1 / sim-boot §23.1） -->
        <div v-if="menuFor === d.id && menuItemsFor(d).length > 0" class="dm-menu" @click.stop>
          <template v-for="item in menuItemsFor(d)" :key="item.kind">
            <button class="menu-item" :title="item.title" @click="onMenuItem(d, item)">
              {{ item.icon }} {{ item.label }}
            </button>
            <div v-if="item.kind === 'wifi-connect' && hostFor === d.id" class="dm-host-row">
              <input
                v-model="hostInput"
                class="dm-host-input"
                type="text"
                placeholder="设备 IP，如 192.168.1.23"
                @keydown.enter="wifiConnectCommit(d.id)"
                @keydown.esc="hostFor = null"
              />
              <button class="mini primary" @click="wifiConnectCommit(d.id)">连接</button>
            </div>
          </template>
          <div class="menu-note">{{ menuItemsFor(d)[0]?.note }}</div>
        </div>
      </div>
    </div>

    <div v-if="store.state.devices.length === 0" class="empty-hint">No device ?</div>
  </div>
</template>

<style scoped>
.side-tag {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-faint);
  font-weight: 400;
}

.dm-group {
  margin-bottom: 6px;
}

/* 未启动模拟器折叠开关（UI 反馈 #1） */
.dm-idle-toggle {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--text-faint);
  font-size: 10.5px;
  padding: 4px 8px;
  margin-bottom: 6px;
  cursor: pointer;
}

.dm-idle-toggle:hover {
  color: var(--text-dim);
  border-color: var(--text-faint);
}

.dm-group-head {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  margin: 6px 0 4px;
}

.dm-count {
  margin-left: auto;
  color: var(--text-faint);
}

.dm-card {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  padding: 7px 8px;
  margin-bottom: 6px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.dm-card:hover {
  background: var(--bg-hover);
}

.dm-card.selected {
  border-color: var(--accent);
}

.dm-card.dim {
  opacity: 0.55;
}

.dm-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.dm-platform {
  font-size: 13px;
}

.dm-model {
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dm-conn {
  font-size: 12px;
}

.dm-sub {
  margin-top: 5px;
  flex-wrap: wrap;
  gap: 4px;
}

.dm-sub .chip {
  font-size: 10px;
}

.dm-live {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 6px;
}

.dm-read {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--text);
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
}

.dm-samples {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.dm-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}

.dm-actions .hint {
  font-size: 10px;
}

.dm-op-status {
  font-size: 10px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1;
}

.dm-op-status.fail {
  color: var(--danger);
}

.dm-menu {
  position: absolute;
  right: 6px;
  top: calc(100% - 4px);
  z-index: 12;
  width: 250px;
  background: #1a222dee;
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
  padding: 6px;
}

.menu-item {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  padding: 5px 6px;
  border-radius: 5px;
  font-size: 11.5px;
}

.menu-item:hover {
  background: var(--bg-hover);
}

.menu-note {
  font-size: 10px;
  color: var(--text-faint);
  padding: 4px 6px 2px;
  border-top: 1px solid var(--border);
  margin-top: 4px;
}

.dm-host-row {
  display: flex;
  gap: 4px;
  padding: 4px 6px;
}

.dm-host-input {
  flex: 1;
  min-width: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  font-size: 11px;
  padding: 3px 6px;
}
</style>
