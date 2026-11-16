import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'
import { createApiClient } from './api-client'
import { STORE_KEY, createMonitorStore } from './store'

/**
 * 运行时配置：UI 层不读环境变量（ARCH §5.2 规则 4），此处为唯一读取点。
 * mock 模式已于 2026-11-12 移除（用户决策：桌面 app 语境下假数据是干扰源）——
 * 永远直连后端；后端不可达由 store.init 的健康检查如实报错。
 * baseUrl：URL 参数 `pdbase` > 构建期 VITE_API_BASE_URL > ''（同源 /api，契约 §1）。
 */
const params = new URLSearchParams(window.location.search)
const baseUrl = params.get('pdbase') ?? import.meta.env.VITE_API_BASE_URL ?? ''
const api = createApiClient({ baseUrl })
const store = createMonitorStore(api, baseUrl)
void store.init()

createApp(App).provide(STORE_KEY, store).mount('#app')
