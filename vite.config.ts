import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// 开发时前端跑在 5173，API 请求转发到本地后端
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
})
