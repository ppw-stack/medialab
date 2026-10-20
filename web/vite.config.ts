import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const BACKEND_PORT = process.env.VITE_BACKEND_PORT
  ? Number(process.env.VITE_BACKEND_PORT)
  : 8080  // 与 app/core/config.py 的 web_port 默认值一致

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    extensions: ['.vue', '.ts', '.js', '.json'],
  },
  server: {
    proxy: {
      '/api': `http://127.0.0.1:${BACKEND_PORT}`,
      '/healthz': `http://127.0.0.1:${BACKEND_PORT}`,
      '/metrics': `http://127.0.0.1:${BACKEND_PORT}`,
    },
  },
  preview: {
    proxy: {
      '/api': `http://127.0.0.1:${BACKEND_PORT}`,
      '/healthz': `http://127.0.0.1:${BACKEND_PORT}`,
      '/metrics': `http://127.0.0.1:${BACKEND_PORT}`,
    },
  },
})
