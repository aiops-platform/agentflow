import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/workflows': 'http://localhost:8000',
      '/run': 'http://localhost:8000',
      '/runs': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
