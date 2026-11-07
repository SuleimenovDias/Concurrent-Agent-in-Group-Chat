import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
export default defineConfig(({mode}) => {
  const env = loadEnv(mode, '.', 'CASTLE_')
  return {
    plugins: [react()],
    server: { port: Number(env.CASTLE_FRONTEND_PORT || 5173), strictPort: true,
      proxy: { '/api': { target: env.CASTLE_API_TARGET || 'http://127.0.0.1:8000', rewrite: path => path.replace(/^\/api/, '') } } },
    test: { environment: 'jsdom', setupFiles: './tests/setup.ts', css: true }
  }
})
