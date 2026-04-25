import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Match the iris-qa / leanspecs convention so this codebase pulls
      // shared components (Sidebar, HealthCard, AILogCard, etc.) from
      // shared/frontend/src instead of duplicating them.
      '@ai-agents/shared-ui': path.resolve(__dirname, '../../shared/frontend/src'),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // Proxy API calls to the hub during dev mode so the browser can treat
    // everything as same-origin and cookies / bearer headers flow cleanly.
    proxy: {
      '/api': 'http://localhost:8004',
      '/tubemail': 'http://localhost:8004',
      '/health': 'http://localhost:8004',
      '/mcp': 'http://localhost:8004',
    },
  },
})
