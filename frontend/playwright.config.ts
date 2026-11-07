import { defineConfig, devices } from '@playwright/test'
import { loadEnv } from 'vite'
const env = loadEnv('development', '.', 'CASTLE_')
const baseURL = `http://127.0.0.1:${env.CASTLE_FRONTEND_PORT || 5173}`
export default defineConfig({
  testDir: './tests', testMatch: '*.e2e.ts',
  use: { ...devices['Desktop Chrome'], channel: 'chrome', viewport: { width: 1280, height: 920 }, baseURL },
  webServer: { command: 'npm run dev', url: baseURL, reuseExistingServer: true, timeout: 30000 }
})
