import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const BACKEND_PORT = process.env.VITE_BACKEND_PORT
  ? Number(process.env.VITE_BACKEND_PORT)
  : 8080  // 与 app/core/config.py 的 web_port 默认值一致

/**
 * 开发代理。
 *
 * **两个业务域的端点都要代理**：
 *
 * - `/api`  —— 辅种域（`/api/v1/*`）与媒体库域（`/api/*`）共用一个前缀，
 *   一个规则覆盖两者
 * - `/302`  —— 中转端点（本地调试播放时用）
 * - `/dav`  —— WebDAV 入口（本地调试 Infuse 连接时用）
 * - `/healthz` `/readyz` `/metrics` —— 运维端点
 */
const proxy = {
  '/api': `http://127.0.0.1:${BACKEND_PORT}`,
  '/302': `http://127.0.0.1:${BACKEND_PORT}`,
  '/dav': `http://127.0.0.1:${BACKEND_PORT}`,
  '/healthz': `http://127.0.0.1:${BACKEND_PORT}`,
  '/readyz': `http://127.0.0.1:${BACKEND_PORT}`,
  '/metrics': `http://127.0.0.1:${BACKEND_PORT}`,
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    extensions: ['.vue', '.ts', '.js', '.json'],
  },
  server: {
    proxy,
  },
  preview: {
    proxy,
  },
})
