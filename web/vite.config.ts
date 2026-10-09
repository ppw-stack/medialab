import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const BACKEND_PORT = 18180

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
