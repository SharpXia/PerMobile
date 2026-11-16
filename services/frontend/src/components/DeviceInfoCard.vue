<script setup lang="ts">
import { computed } from 'vue'
import type { Device } from '../api-client'

/**
 * 设备信息卡（R13 深化，对标 [I§2.1] 设备页 Info/Value 两列表）：
 * - 环境块：Device/OS/CPU/GPU/OpenGL/Resolution/Ram/刷新率（契约 §4 info 仅下发
 *   os/cpu/resolution/batteryLevel/refreshRateHz —— GPU/Ram/OpenGL 无下发字段，显示
 *   「—」并注明来源缺失，不填 0 [R§8.6]）；
 * - profile 能力行（§20.2）：连接模式（usb/wifi 可用性）、GPU 源（kgsl/null，驱动
 *   §20.3 能力门禁）、CPU 核数、刷新率。缺值显示 — 不写 0。
 */
const props = defineProps<{ device: Device | null }>()

interface InfoRow {
  k: string
  v: string
  tip?: string
}

const infoRows = computed<InfoRow[]>(() => {
  const d = props.device
  if (!d) return []
  return [
    { k: 'Device', v: d.model },
    { k: 'OS', v: d.info?.os ?? '—', tip: d.info?.os ? undefined : '契约 §4 info 未下发' },
    { k: 'CPU', v: d.info?.cpu ?? '—' },
    {
      k: 'GPU',
      v: d.profile?.gpuSource ? String(d.profile.gpuSource) : '—',
      tip: d.profile?.gpuSource
        ? `GPU 数据源 ${d.profile.gpuSource}（profile，§20.2；GPU 利用率可用）`
        : '契约未下发 GPU 型号；profile.gpuSource=null 表示无利用率数据源（GPU 指标禁勾）',
    },
    { k: 'OpenGL', v: '—', tip: '契约未下发（PerfDog 设备页含 OpenGL ES 版本，本实现无对应字段）' },
    { k: 'Resolution', v: d.info?.resolution ?? '—' },
    { k: 'Ram', v: '—', tip: '契约未下发 Ram Size（PerfDog 设备页含 Ram/LMK/Swap，本实现无对应字段）' },
    {
      k: '刷新率',
      v: d.info?.refreshRateHz != null ? `${d.info.refreshRateHz}Hz` : '—',
    },
    { k: '电池', v: d.info?.batteryLevel != null ? `${d.info.batteryLevel}%` : '—' },
  ]
})

const profileRows = computed<InfoRow[]>(() => {
  const d = props.device
  if (!d) return []
  const p = d.profile
  return [
    {
      k: '连接模式',
      v: `${p?.usb ? 'USB ✓' : 'USB ✗'} / ${p?.wifi ? 'WiFi ✓' : 'WiFi ✗'}`,
      tip: 'profile.usb/wifi：该设备当前可用的连接模式（§20.2；勾选面板可用性 = capabilities ∩ profile）',
    },
    {
      k: 'GPU 源',
      v: p?.gpuSource === undefined ? '未下发' : (p.gpuSource ?? '无（null）'),
      tip: 'profile.gpuSource ∈ kgsl | null；null → GPU 利用率不可用（reason=kgsl_missing）',
    },
    { k: 'CPU 核数', v: p?.cpuCores != null ? String(p.cpuCores) : '—' },
    { k: '平台', v: d.platform },
  ]
})
</script>

<template>
  <div class="side-block">
    <div class="side-title">设备信息<span class="dic-tag">R13 · Info/Value + profile</span></div>
    <template v-if="device">
      <table class="info-table">
        <tbody>
          <tr v-for="r in infoRows" :key="r.k" :title="r.tip">
            <td class="k">{{ r.k }}</td>
            <td class="v">{{ r.v }}</td>
          </tr>
        </tbody>
      </table>
      <div class="dic-profile-head">profile 能力行（§20.2）</div>
      <table class="info-table">
        <tbody>
          <tr v-for="r in profileRows" :key="r.k" :title="r.tip">
            <td class="k">{{ r.k }}</td>
            <td class="v">{{ r.v }}</td>
          </tr>
        </tbody>
      </table>
    </template>
    <div v-else class="empty-hint">未选择设备</div>
  </div>
</template>

<style scoped>
.dic-tag {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-faint);
  font-weight: 400;
}

.dic-profile-head {
  font-size: 10px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  margin: 8px 0 2px;
}
</style>
