import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发形态默认 Vite proxy 到后端（ARCHITECTURE.md §4.6：Vite proxy 为默认，而非后端 CORS）。
// 后端入口：python services/backend/run.py --port 8737（ARCHITECTURE.md H2）。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8737',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2020',
    // echarts 整包引入（M1 取稳定优先，见 services/frontend/README.md 的取舍说明）
    chunkSizeWarningLimit: 1600,
  },
})
