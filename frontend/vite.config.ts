import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // Proxy API calls to the hub during dev mode so the browser can treat
    // everything as same-origin and cookies / bearer headers flow cleanly.
    proxy: {
      '/api': 'http://localhost:8001',
      '/tubemail': 'http://localhost:8001',
      '/health': 'http://localhost:8001',
      '/mcp': 'http://localhost:8001',
    },
  },
})
