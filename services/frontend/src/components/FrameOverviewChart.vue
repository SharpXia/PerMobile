<script setup lang="ts">
/**
 * 帧时间线 Overview 图（M7 R50，[I§2.7]）：
 * - X 轴 = 帧序号（0→N，value 轴），Y 轴 = FTime ms；右侧 30/60 FPS 参考线
 *   （33.33ms / 16.67ms，对应 [I§2.7] 截图的 30FPS/60FPS 参考线）；
 * - jank 帧红色散点高亮；选中帧蓝色游标、悬停帧灰色游标（M 键定位的视觉锚）；
 * - 交互：悬停 emit hover-frame（供 M 键「选定对应帧」）、单击 emit select-frame、
 *   右键 emit context-frame（右键菜单：Max FrameTime / Detail，[I§2.7] S4 原文）。
 * - 不复用 MetricChart（其为时间轴 type:'time'，本图为帧序号 value 轴），echarts 直连
 *   属组件内渲染库用法，网络访问仍为零（H6 不受限， echarts 为既有依赖）。
 */
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { FrameRecord } from '../api-client'

const props = defineProps<{
  frames: FrameRecord[]
  selectedIndex?: number | null
  hoverIndex?: number | null
  height?: number
}>()

const emit = defineEmits<{
  'hover-frame': [frameIndex: number | null]
  'select-frame': [frameIndex: number]
  'context-frame': [payload: { frameIndex: number | null; x: number; y: number }]
}>()

const canvasEl = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null
let resizeObserver: ResizeObserver | null = null

/** 帧序号 → FTime（本图数据面） */
function frameAt(index: number): FrameRecord | undefined {
  return props.frames.find((f) => f.frameIndex === index)
}

function buildOption(): echarts.EChartsOption {
  const data = props.frames.map((f) => [f.frameIndex, f.durationMs] as [number, number])
  const jankData = props.frames.filter((f) => f.jank).map((f) => [f.frameIndex, f.durationMs] as [number, number])
  const idxMax = props.frames.length > 0 ? (props.frames[props.frames.length - 1]?.frameIndex ?? 0) : 0
  const overlay: Array<Record<string, unknown>> = []
  if (props.selectedIndex !== undefined && props.selectedIndex !== null) {
    overlay.push({ xAxis: props.selectedIndex, lineStyle: { color: '#2f81f7', width: 1.4, type: 'solid' } })
  }
  if (props.hoverIndex !== undefined && props.hoverIndex !== null && props.hoverIndex !== props.selectedIndex) {
    overlay.push({ xAxis: props.hoverIndex, lineStyle: { color: '#8b98a5', width: 1, type: 'dashed' } })
  }
  return {
    animation: false,
    grid: { left: 48, right: 18, top: 20, bottom: 40 },
    xAxis: {
      type: 'value',
      name: '帧序号',
      nameTextStyle: { color: '#8b98a5', fontSize: 10 },
      min: 0,
      max: idxMax || 1,
      axisLine: { lineStyle: { color: '#2b3644' } },
      axisTick: { show: false },
      axisLabel: { color: '#8b98a5', fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      name: 'FTime (ms)',
      nameTextStyle: { color: '#8b98a5', fontSize: 10 },
      scale: true,
      axisLine: { show: true, lineStyle: { color: '#2b3644' } },
      axisLabel: { color: '#8b98a5', fontSize: 10 },
      splitLine: { lineStyle: { color: '#1c2531' } },
    },
    series: [
      {
        name: 'FTime',
        type: 'line',
        showSymbol: false,
        sampling: 'lttb',
        lineStyle: { width: 1.2, color: '#58a6ff' },
        itemStyle: { color: '#58a6ff' },
        data,
        ...(overlay.length > 0
          ? {
              markLine: {
                silent: true,
                symbol: 'none',
                animation: false,
                label: { show: false },
                data: overlay,
              },
            }
          : {}),
      },
      {
        name: 'Jank 帧',
        type: 'scatter',
        symbolSize: 5,
        itemStyle: { color: '#e5534b' },
        z: 6,
        data: jankData,
      },
      {
        // 参考线挂在独立透明 series 上（不随 scale 抖动，[I§2.7] 30FPS/60FPS 横线）
        name: '参考线',
        type: 'line',
        data: [],
        silent: true,
        markLine: {
          silent: true,
          symbol: 'none',
          animation: false,
          label: {
            show: true,
            position: 'insideEndTop',
            formatter: '{b}',
            color: '#d29922',
            fontSize: 9,
          },
          data: [
            { name: '60FPS', yAxis: 16.67, lineStyle: { color: '#3fb95088', type: 'dashed', width: 1 } },
            { name: '30FPS', yAxis: 33.33, lineStyle: { color: '#d2992288', type: 'dashed', width: 1 } },
          ],
        },
      },
    ],
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a222d',
      borderColor: '#2b3644',
      textStyle: { color: '#d7e0ea', fontSize: 11 },
      formatter: (params: unknown) => {
        const arr = Array.isArray(params) ? params : [params]
        const head = arr[0] as { value?: unknown } | undefined
        const idx = Array.isArray(head?.value) ? (head!.value as [number, number])[0] : null
        const f = typeof idx === 'number' ? frameAt(idx) : undefined
        if (!f) return ''
        return (
          `<div style="color:#8b98a5">Frame ${f.frameIndex}</div>` +
          `FTime&nbsp;<b>${f.durationMs.toFixed(2)}ms</b>` +
          (f.jank ? `<br/><span style="color:#e5534b">Jank：${f.jankType ?? ''}</span>` : '')
        )
      },
    },
  }
}

function update(): void {
  chart?.setOption(buildOption(), { replaceMerge: ['series'] })
}

onMounted(() => {
  if (!canvasEl.value) return
  chart = echarts.init(canvasEl.value)
  // 悬停 → hover-frame（M 键「选定对应帧」的悬停锚，[I§2.7] 帧时间线联动）
  chart.on('updateAxisPointer', (event) => {
    const info = (event as { axesInfo?: Array<{ value?: unknown }> }).axesInfo?.[0]
    emit('hover-frame', typeof info?.value === 'number' ? Math.round(info.value) : null)
  })
  chart.getZr().on('globalout', () => emit('hover-frame', null))
  // 单击 → 选定帧（上下联动：Overview 卡 + CPU 调度 + 列表滚动）
  chart.on('click', (params) => {
    const idx = Array.isArray(params.value) ? (params.value as [number, number])[0] : null
    if (typeof idx === 'number') emit('select-frame', Math.round(idx))
  })
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(canvasEl.value)
  update()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.dispose()
  chart = null
})

watch(() => [props.frames, props.selectedIndex, props.hoverIndex], update, { deep: false })

/** 右键 → context-frame（携带光标像素位置供父组件定位菜单；命中帧序号换算同 MetricChart） */
function onContextmenu(e: MouseEvent): void {
  e.preventDefault()
  let frameIndex: number | null = null
  if (chart && canvasEl.value) {
    try {
      const rect = canvasEl.value.getBoundingClientRect()
      const v = chart.convertFromPixel({ xAxisIndex: 0 }, [e.clientX - rect.left, 0])
      if (Array.isArray(v) && typeof v[0] === 'number') frameIndex = Math.round(v[0]!)
    } catch {
      frameIndex = null
    }
  }
  emit('context-frame', { frameIndex, x: e.clientX, y: e.clientY })
}
</script>

<template>
  <div class="foc-wrap">
    <div
      ref="canvasEl"
      class="foc-canvas"
      :style="{ height: (height ?? 220) + 'px' }"
      @contextmenu="onContextmenu"
    ></div>
  </div>
</template>

<style scoped>
.foc-wrap {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  overflow: hidden;
}

.foc-canvas {
  width: 100%;
}
</style>
