import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './tests', testMatch: '*.e2e.ts',
  use: { ...devices['Desktop Chrome'], channel: 'chrome', viewport: { width: 1280, height: 920 }, baseURL: 'http://127.0.0.1:5173' },
  webServer: { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: true, timeout: 30000 }
})
