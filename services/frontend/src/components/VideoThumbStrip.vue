<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ArtifactEntry } from '../api-client'

/**
 * 录屏缩略图时间条（R37，[I§2.5] S2-img9「截图缩略图时间条」）：
 * - 有 video 工件时显示横向滚动的缩略图序列 —— 客户端用 <video> + canvas 逐段截图生成
 *   （零后端依赖：隐藏 video 元素按时间点 seek 后 drawImage 到小 canvas，toDataURL 落图）；
 * - 每帧下方标注帧下标时间（段内序号 + 视频相对时间 m:ss，[I§2.5]「每帧下方标注时间」）；
 * - 点击缩略图 → emit seek(该段视频时刻)，父组件对齐曲线到该时刻（映射依赖 clockAnchors，
 *   不可映射时仅走视频 seek，由父组件声明）；
 * - 生成失败（编解码器不支持/Range 不可用等）→ 退化为时间刻度条并 emit error，不伪造截图。
 */
const props = withDefaults(
  defineProps<{
    /** kind=video 的工件列表（分段 run 有多条，?name= 选择，契约 §17） */
    videos: ArtifactEntry[]
    /** 工件名 → 取流 URL（api-client getRunVideoUrl 提供） */
    urlFor: (name?: string) => string
    /** 当前选中分段名（undefined = 缺省首个） */
    selectedName?: string
    /** 主视频当前播放时刻（s），用于高亮活动缩略图 */
    currentTime?: number
    /** 最多生成多少帧（默认 24；时长短则自动减少，逐段间隔≈时长/N） */
    maxThumbs?: number
  }>(),
  { currentTime: undefined, maxThumbs: 24 },
)

const emit = defineEmits<{ seek: [videoTimeSec: number]; duration: [seconds: number]; error: [message: string] }>()

interface Thumb {
  index: number
  t: number
  dataUrl: string | null
}

const thumbs = ref<Thumb[]>([])
const generating = ref(false)
const genError = ref('')

const videoName = computed(() => props.selectedName ?? props.videos[0]?.name)
const videoUrl = computed(() => (videoName.value ? props.urlFor(videoName.value) : ''))

let genSeq = 0

/** 段内帧下标时间标注：m:ss（[I§2.5] 每帧下方时间标注形态） */
function fmtTime(t: number): string {
  const sec = Math.max(0, Math.floor(t))
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

const activeIndex = computed(() => {
  const cur = props.currentTime
  if (cur === undefined || thumbs.value.length === 0) return -1
  let best = -1
  for (let i = 0; i < thumbs.value.length; i++) {
    const th = thumbs.value[i]
    if (th && cur >= th.t - 0.5) best = i
  }
  return best
})

function onThumbClick(th: Thumb): void {
  if (th.dataUrl === null && generating.value) return
  emit('seek', th.t)
}

watch(
  videoUrl,
  (url) => {
    thumbs.value = []
    genError.value = ''
    if (url) void generate(url)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  genSeq += 1 // 使在途生成任务失效
})

async function generate(url: string): Promise<void> {
  const seq = ++genSeq
  generating.value = true
  genError.value = ''
  const v = document.createElement('video')
  v.muted = true
  v.playsInline = true
  v.preload = 'auto'
  try {
    await onceEvent(v, 'loadedmetadata', 15_000, () => {
      v.src = url
    })
    if (seq !== genSeq) return
    const duration = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : 0
    if (duration <= 0) throw new Error('视频时长不可读（metadata 无 duration）')
    emit('duration', duration)
    // 逐段帧数：≈每 2s 一帧，最少 4 帧（时长短自适应），上限 maxThumbs
    const n = Math.max(4, Math.min(props.maxThumbs, Math.round(duration / 2)))
    const list: Thumb[] = []
    for (let i = 0; i < n; i++) list.push({ index: i, t: (duration * i) / n, dataUrl: null })
    thumbs.value = list
    const W = 96
    const H = 54
    for (const th of list) {
      if (seq !== genSeq) return
      try {
        await seekTo(v, th.t)
        if (seq !== genSeq) return
        const canvas = document.createElement('canvas')
        canvas.width = W
        canvas.height = H
        const ctx = canvas.getContext('2d')
        if (!ctx) throw new Error('canvas 2d 上下文不可用')
        ctx.drawImage(v, 0, 0, W, H)
        th.dataUrl = canvas.toDataURL('image/jpeg', 0.6)
        thumbs.value = [...thumbs.value] // 触发响应式更新
      } catch {
        th.dataUrl = null // 单帧失败不中断整条（该帧退化为刻度）
      }
    }
    const failed = list.filter((t) => t.dataUrl === null).length
    if (failed > 0 && failed === list.length) {
      genError.value = '缩略图截图全部失败（编解码器不支持或取流不可用），显示为时间刻度条'
      emit('error', genError.value)
    }
  } catch (err) {
    if (seq !== genSeq) return
    genError.value = `缩略图生成失败：${err instanceof Error ? err.message : String(err)}（显示为时间刻度条）`
    emit('error', genError.value)
  } finally {
    if (seq === genSeq) generating.value = false
    v.removeAttribute('src')
    v.load()
  }
}

function onceEvent(v: HTMLVideoElement, ev: string, timeoutMs: number, start: () => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const onOk = (): void => {
      cleanup()
      resolve()
    }
    const onErr = (): void => {
      cleanup()
      reject(new Error('视频加载失败（取流不可用或编解码器不支持）'))
    }
    const timer = setTimeout(() => {
      cleanup()
      reject(new Error(`加载超时（${timeoutMs}ms）`))
    }, timeoutMs)
    const cleanup = (): void => {
      clearTimeout(timer)
      v.removeEventListener(ev, onOk)
      v.removeEventListener('error', onErr)
    }
    v.addEventListener(ev, onOk, { once: true })
    v.addEventListener('error', onErr, { once: true })
    start()
  })
}

function seekTo(v: HTMLVideoElement, t: number): Promise<void> {
  return new Promise((resolve, reject) => {
    if (Math.abs(v.currentTime - t) < 0.01) {
      resolve()
      return
    }
    const onSeeked = (): void => {
      cleanup()
      resolve()
    }
    const onErr = (): void => {
      cleanup()
      reject(new Error('seek 失败'))
    }
    const timer = setTimeout(() => {
      cleanup()
      reject(new Error('seek 超时'))
    }, 8_000)
    const cleanup = (): void => {
      clearTimeout(timer)
      v.removeEventListener('seeked', onSeeked)
      v.removeEventListener('error', onErr)
    }
    v.addEventListener('seeked', onSeeked, { once: true })
    v.addEventListener('error', onErr, { once: true })
    v.currentTime = Math.min(Math.max(0, t), Math.max(0, (v.duration || 0) - 0.05))
  })
}
</script>

<template>
  <div class="thumb-strip-wrap">
    <div class="thumb-strip" :class="{ generating }">
      <button
        v-for="th in thumbs"
        :key="th.index"
        class="thumb"
        :class="{ active: th.index === activeIndex }"
        :title="`点击对齐到 ${fmtTime(th.t)}`"
        @click="onThumbClick(th)"
      >
        <span class="thumb-img">
          <img v-if="th.dataUrl" :src="th.dataUrl" alt="" draggable="false" />
          <span v-else class="thumb-blank" :style="{ animationDelay: `${th.index * 60}ms` }"></span>
        </span>
        <span class="thumb-time">{{ fmtTime(th.t) }}</span>
      </button>
    </div>
    <div v-if="generating" class="thumb-hint">缩略图生成中（video + canvas 逐段截图）…</div>
    <div v-else-if="genError" class="thumb-hint warn">※ {{ genError }}</div>
  </div>
</template>

<style scoped>
.thumb-strip-wrap {
  width: 100%;
  min-width: 0;
}

.thumb-strip {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  padding: 4px 2px;
}

.thumb {
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 2px;
}

.thumb:hover:not(:disabled) {
  background: var(--bg-hover);
}

.thumb.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.thumb-img {
  width: 96px;
  height: 54px;
  border-radius: 4px;
  overflow: hidden;
  background: #0a0e13;
  display: flex;
  align-items: center;
  justify-content: center;
}

.thumb-img img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.thumb-blank {
  width: 100%;
  height: 100%;
  background: linear-gradient(90deg, #131a22 0%, #1c2531 50%, #131a22 100%);
  background-size: 200% 100%;
  animation: thumbPulse 1.2s linear infinite;
}

@keyframes thumbPulse {
  from {
    background-position: 200% 0;
  }
  to {
    background-position: 0 0;
  }
}

.thumb-time {
  font-size: 9.5px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
  line-height: 1.4;
}

.thumb.active .thumb-time {
  color: #79b8ff;
}

.thumb-hint {
  font-size: 11px;
  color: var(--text-faint);
  padding: 2px 4px 0;
}

.thumb-hint.warn {
  color: #e3b341;
}
</style>
