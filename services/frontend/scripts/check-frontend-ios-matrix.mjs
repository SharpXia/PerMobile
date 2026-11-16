#!/usr/bin/env node
// S3 设备矩阵 iOS/模拟器交互面断言门禁（check:matrix）。
//
// 被测行为全部收敛为 src/capability.ts 的无 Vue 依赖纯函数
//   （connIcon / connTitle / menuItemsFor / miniReadoutFor / platformDefaultMetrics），
// 本脚本经 Node 原生 TS 类型剥离（node ≥22.6 --experimental-strip-types，
// v23.6+ 默认开启；本机 v24.21.0 实测）直接导入被测模块，零测试框架依赖。
//
// 分级声明（不混充）：
//   - 本脚本承载 = 纯函数行为断言 + api-client/store/组件的静态接线断言（源文本级）；
//   - 实际 DOM 渲染绑定 = vue-tsc 类型覆盖（npm run build）+ 人工目检，本脚本不冒充。
//
// 退出码：0=通过；1=断言失败；2=结构性缺失（被测模块不存在/导入失败）。
// 仅用 Node 内置模块，路径相对脚本自身解析，与调用方 CWD 无关。

import { strict as assert } from 'node:assert'
import { existsSync, readFileSync } from 'node:fs'
import { join, relative, resolve as pathResolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const FRONTEND_ROOT = pathResolve(fileURLToPath(new URL('..', import.meta.url)))

/** 相对 FRONTEND_ROOT 解析（保持脚本与调用方 CWD 无关） */
function fromRoot(p) {
  return join(FRONTEND_ROOT, p)
}

const CAPABILITY_TS = fromRoot('src/capability.ts')
const METRIC_META_TS = fromRoot('src/metricMeta.ts')
const API_INDEX = fromRoot('src/api-client/index.ts')
const API_TYPES = fromRoot('src/api-client/types.ts')
const STORE = fromRoot('src/store.ts')
const PANEL = fromRoot('src/components/DeviceMatrixPanel.vue')

for (const p of [CAPABILITY_TS, METRIC_META_TS, API_INDEX, API_TYPES, STORE, PANEL]) {
  if (!existsSync(p)) {
    console.error(`[check:matrix] 结构性缺失：${relative(FRONTEND_ROOT, p)} 不存在（按失败处理）`)
    process.exit(2)
  }
}

/* ---------- 纯函数导入（Node 原生 TS 类型剥离） ---------- */

let capability, metricMetaModule
try {
  capability = await import(pathToFileURL(CAPABILITY_TS).href)
  metricMetaModule = await import(pathToFileURL(METRIC_META_TS).href)
} catch (err) {
  console.error(`[check:matrix] 被测模块导入失败（需 node ≥22.6 原生 TS 类型剥离；本仓基线 v24.21.0）：${err.message}`)
  process.exit(2)
}

const { connIcon, connTitle, menuItemsFor, miniReadoutFor, platformDefaultMetrics, platformIcon, METRIC_GROUPS } =
  capability
const { METRIC_META } = metricMetaModule

let pass = 0
const failures = []

/** 单条断言：名字 + 闭包；失败记入清单不中断（一次性给全量失败面） */
function check(name, fn) {
  try {
    fn()
    pass += 1
    console.log(`  ✓ ${name}`)
  } catch (err) {
    failures.push({ name, err })
    console.error(`  ✗ ${name}`)
    console.error(`      ${err.message.split('\n').join('\n      ')}`)
  }
}

/* ---------- S3 契约冻结的指标 id 常量（本流只引用，S2 为权威） ---------- */

const HOST_CPU = 'cpu.proc.host.pct' // §23.2 模拟器宿主 CPU
const HOST_RSS = 'memory.rss.mb' // §23.2 模拟器宿主 RSS
const XCTRACE_CPU = 'cpu.proc.xctrace.pct' // §26 真机 xctrace CPU
const FOOTPRINT = 'memory.footprint.mb' // §26 真机 physical footprint
const ANDROID_THREE = ['frame.fps.mean', 'cpu.proc.pct', 'memory.pss.peak_mb'] // 既有矩阵卡三指标

console.log('[check:matrix] menuItemsFor（⋮ 菜单按设备分流；500 穿透预防 UI 侧）')
check('Android usb → WiFi 两项（enable + connect）', () => {
  const items = menuItemsFor({ platform: 'android', connection: 'usb', state: 'online' })
  assert.deepEqual(
    items.map((i) => i.kind),
    ['wifi-enable', 'wifi-connect'],
  )
})
check('Android wifi → WiFi 两项', () => {
  const items = menuItemsFor({ platform: 'android', connection: 'wifi', state: 'online' })
  assert.deepEqual(
    items.map((i) => i.kind),
    ['wifi-enable', 'wifi-connect'],
  )
})
check('simulator + offline → 仅「启动模拟器」（sim-boot）', () => {
  const items = menuItemsFor({ platform: 'ios', connection: 'simulator', state: 'offline' })
  assert.equal(items.length, 1, `期望恰好 1 项，实际 ${items.length}`)
  assert.equal(items[0].kind, 'sim-boot')
  assert.ok(items[0].label.includes('启动模拟器'), `label 应含「启动模拟器」，实际「${items[0].label}」`)
})
check('simulator + online → 空（已 Booted 无操作）', () => {
  assert.deepEqual(menuItemsFor({ platform: 'ios', connection: 'simulator', state: 'online' }), [])
})
check('iOS 真机 usb → 空（WiFi 入口隐藏，§21.1 real-only duck-call 不可达）', () => {
  assert.deepEqual(menuItemsFor({ platform: 'ios', connection: 'usb', state: 'online' }), [])
})
check('iOS 真机 wifi → 空（同上）', () => {
  assert.deepEqual(menuItemsFor({ platform: 'ios', connection: 'wifi', state: 'online' }), [])
})

console.log('[check:matrix] miniReadoutFor（矩阵卡迷你读数按设备分流）')
check('simulator → 宿主口径 CPU/MEM（cpu.proc.host.pct / memory.rss.mb）', () => {
  const rows = miniReadoutFor({ platform: 'ios', connection: 'simulator', state: 'online' })
  assert.deepEqual(
    rows.map((r) => r.metricId),
    [HOST_CPU, HOST_RSS],
  )
})
check('iOS 真机 → xctrace 口径 CPU/MEM（cpu.proc.xctrace.pct / memory.footprint.mb）', () => {
  for (const connection of ['usb', 'wifi']) {
    const rows = miniReadoutFor({ platform: 'ios', connection, state: 'online' })
    assert.deepEqual(rows.map((r) => r.metricId), [XCTRACE_CPU, FOOTPRINT], `connection=${connection}`)
  }
})
check('FPS 行 iOS 不出（模拟器无逐帧 API §23.2 / 真机本期不接线——恒无数据不出行）', () => {
  for (const connection of ['simulator', 'usb', 'wifi']) {
    const rows = miniReadoutFor({ platform: 'ios', connection, state: 'online' })
    assert.ok(
      rows.every((r) => r.key !== 'FPS' && r.metricId !== 'frame.fps.mean'),
      `connection=${connection} 不应出现 FPS 行`,
    )
  }
})
check('Android → 既有 FPS/CPU/MEM 三行（行为不变）', () => {
  const rows = miniReadoutFor({ platform: 'android', connection: 'usb', state: 'online' })
  assert.deepEqual(rows.map((r) => r.metricId), ANDROID_THREE)
  assert.equal(rows[0].key, 'FPS')
})

console.log('[check:matrix] connIcon / connTitle（simulator 行不再落 USB else 分支）')
check('simulator 图标非 📎（回形针=USB 行专属）', () => {
  const d = { connection: 'simulator' }
  assert.notEqual(connIcon(d), '📎')
  assert.notEqual(connIcon(d), '📶')
})
check('simulator tooltip 非「USB 模式设备行」', () => {
  const title = connTitle({ connection: 'simulator' })
  assert.ok(!title.includes('USB 模式设备行'), `不应出现 USB 文案，实际「${title}」`)
  assert.ok(title.includes('Simulator'), `应声明 Simulator 身份，实际「${title}」`)
})
check('usb/wifi 既有图标与文案不变', () => {
  assert.equal(connIcon({ connection: 'usb' }), '📎')
  assert.equal(connIcon({ connection: 'wifi' }), '📶')
  assert.ok(connTitle({ connection: 'usb' }).includes('USB 模式设备行'))
  assert.ok(connTitle({ connection: 'wifi' }).includes('WiFi 模式设备行'))
})

console.log('[check:matrix] platformDefaultMetrics（平台 → 缺省指标集）')
check('ios（缺省/模拟器）→ §23.2 宿主双指标', () => {
  assert.deepEqual(platformDefaultMetrics('ios'), [HOST_CPU, HOST_RSS])
  assert.deepEqual(platformDefaultMetrics('ios', 'simulator'), [HOST_CPU, HOST_RSS])
})
check('ios + 真机连接（usb/wifi）→ §26 xctrace 双指标', () => {
  assert.deepEqual(platformDefaultMetrics('ios', 'usb'), [XCTRACE_CPU, FOOTPRINT])
  assert.deepEqual(platformDefaultMetrics('ios', 'wifi'), [XCTRACE_CPU, FOOTPRINT])
})
check('android → 既有三指标（MATRIX_DEFAULT_METRICS 原样迁移）', () => {
  assert.deepEqual(platformDefaultMetrics('android'), ANDROID_THREE)
  assert.deepEqual(platformDefaultMetrics('android', 'usb'), ANDROID_THREE)
  assert.deepEqual(platformDefaultMetrics('android', 'wifi'), ANDROID_THREE)
})

console.log('[check:matrix] metricMeta（§23.2/§26 四条目，desc 携口径声明）')
const IOS_METRIC_IDS = [HOST_CPU, HOST_RSS, XCTRACE_CPU, FOOTPRINT]
check('四个 iOS metricId 均已注册 label/desc/color', () => {
  for (const id of IOS_METRIC_IDS) {
    const meta = METRIC_META[id]
    assert.ok(meta, `${id} 缺 METRIC_META 条目`)
    assert.ok(meta.label && meta.desc && meta.color, `${id} label/desc/color 不完整`)
  }
})
check('desc 携口径声明（宿主/xctrace ≠ PerfDog 口径，数值不可直接对比）', () => {
  assert.ok(METRIC_META[HOST_CPU].desc.includes('不可直接对比'), HOST_CPU)
  assert.ok(METRIC_META[HOST_CPU].desc.includes('宿主口径'), HOST_CPU)
  assert.ok(METRIC_META[HOST_RSS].desc.includes('不可直接对比'), HOST_RSS)
  assert.ok(METRIC_META[XCTRACE_CPU].desc.includes('xctrace'), XCTRACE_CPU)
  assert.ok(METRIC_META[XCTRACE_CPU].desc.includes('不可直接对比'), XCTRACE_CPU)
  assert.ok(METRIC_META[FOOTPRINT].desc.includes('不可直接对比'), FOOTPRINT)
})
check('四条目配色齐全且不落入缺省灰（图例可分辨）', () => {
  // DEFAULT_COLOR（metricMeta.ts 内部常量）= '#90a4ae'；未导出，此处按字面值对拍
  const DEFAULT_COLOR = '#90a4ae'
  const colors = IOS_METRIC_IDS.map((id) => METRIC_META[id].color)
  assert.equal(new Set(colors).size, colors.length, `配色重复：${colors.join(',')}`)
  for (const c of colors) assert.notEqual(c, DEFAULT_COLOR, '配色不应等于 DEFAULT_COLOR')
})

console.log('[check:matrix] 勾选组与平台图标')
check('METRIC_GROUPS 含 ios-simulator / ios-real 两组，metricIds 与冻结常量同构', () => {
  const byId = new Map(METRIC_GROUPS.map((g) => [g.id, g]))
  assert.deepEqual(byId.get('ios-simulator')?.metricIds, [HOST_CPU, HOST_RSS], 'ios-simulator 组')
  assert.deepEqual(byId.get('ios-real')?.metricIds, [XCTRACE_CPU, FOOTPRINT], 'ios-real 组（modes usb/wifi 同构 §26）')
})
check('platformIcon：ios=🍎 / android=🤖（平台图标不再恒 🤖）', () => {
  assert.equal(platformIcon('ios'), '🍎')
  assert.equal(platformIcon('android'), '🤖')
  assert.notEqual(platformIcon('ios'), platformIcon('android'))
})

/* ---------- 静态接线断言（源文本级；DOM 渲染归 vue-tsc + 人工目检，不在此冒充） ---------- */

const apiIndexSrc = readFileSync(API_INDEX, 'utf8')
const apiTypesSrc = readFileSync(API_TYPES, 'utf8')
const storeSrc = readFileSync(STORE, 'utf8')
const panelSrc = readFileSync(PANEL, 'utf8')

console.log('[check:matrix] sim-boot 接线（§23.1；统一错误体 + 类型面对齐后端响应形状）')
check('api-client：simBoot → POST /api/devices/{id}/sim-boot，经 requestJson 统一错误体', () => {
  assert.match(apiIndexSrc, /simBoot\(deviceId: string\): Promise<SimBootResult>/, '接口方法签名')
  assert.match(
    apiIndexSrc,
    /path: `\/api\/devices\/\$\{encodeURIComponent\(deviceId\)\}\/sim-boot`/,
    '端点路径（§23.1）',
  )
  assert.match(apiIndexSrc, /simBoot\(deviceId: string\): Promise<SimBootResult> \{\s*return requestJson/, '走 requestJson（§3 错误体统一解析）')
})
check('api-client：simBoot 超时上限覆盖 bootstatus ~180s（不早于后端先超时）', () => {
  const m = /simBoot[\s\S]{0,600}?timeoutMs: ([\d_]+)/.exec(apiIndexSrc)
  assert.ok(m, '未找到 simBoot 的 timeoutMs')
  const timeoutMs = Number(m[1].replace(/_/g, ''))
  assert.ok(timeoutMs >= 180_000, `timeoutMs=${timeoutMs} 应 ≥ 180000（bootstatus 上限 ~180s）`)
})
check('types.ts：SimBootResult 与后端 200 形状一致（deviceId/action=boot/state）', () => {
  assert.match(apiTypesSrc, /export interface SimBootResult \{[\s\S]*?deviceId: string[\s\S]*?action: 'boot'[\s\S]*?state: DeviceState/, '契约 §23.1 golden 字段')
})
check('types.ts：ApiErrorCode 含 simctl_unavailable（§3 错误表 M-iOS 行，503 透出）', () => {
  assert.match(apiTypesSrc, /'simctl_unavailable'/)
})
check('store：simBoot 失败经 errText 透出错误码+message（404/503 不吞不掉）', () => {
  const fn = /async function simBoot\(deviceId: string\): Promise<boolean> \{([\s\S]*?)\n  \}/.exec(storeSrc)
  assert.ok(fn, 'store.simBoot 未找到')
  assert.ok(fn[1].includes('errText(err)'), '失败分支应透出 errText（ApiError.toString = [code] message）')
  assert.ok(fn[1].includes('await refreshDevices()'), '成功分支应刷新设备行（offline→online）')
})
check('store：矩阵卡缺省采集集按设备 platform/connection 分流（platformDefaultMetrics）', () => {
  assert.ok(storeSrc.includes('platformDefaultMetrics'), '应引用 capability.platformDefaultMetrics')
  assert.ok(!storeSrc.includes('MATRIX_DEFAULT_METRICS'), '硬编码 Android 三指标常量应已迁移')
  assert.match(
    storeSrc,
    /platformDefaultMetrics\((\w|\?|\.| )+'', (\w|\?|\.| )+\)/,
    '未找到设备时应取 platformDefaultMetrics 分流结果',
  )
})
check('DeviceMatrixPanel：迷你读数/菜单/图标/标题全部绑定 capability 纯函数', () => {
  for (const fn of ['connIcon(d)', 'connTitle(d)', 'menuItemsFor(d)', 'miniReadoutFor(d)', 'platformIcon(d.platform)', 'platformIcon(g.platform)']) {
    assert.ok(panelSrc.includes(fn), `模板应绑定 ${fn}`)
  }
  assert.match(panelSrc, /v-for="row in miniReadoutFor\(d\)"/, '迷你读数应 v-for 纯函数行')
  assert.match(panelSrc, /from '\.\.\/capability'/, '应从 capability 导入纯函数')
  assert.ok(!panelSrc.includes("readOf(d.id, 'frame.fps.mean')"), 'FPS 读数不应再硬编码（iOS 设备恒不出 FPS 行）')
})

/* ---------- 汇总 ---------- */

if (failures.length > 0) {
  console.error(`\n[check:matrix] 失败 ${failures.length}/${pass + failures.length} 条断言（清单见上）`)
  process.exit(1)
}
console.log(`\n[check:matrix] 通过：${pass} 条断言全绿（纯函数行为 + 静态接线；DOM 渲染归 vue-tsc/人工目检）。`)
process.exit(0)
