import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, proxy the API to the local backend so the browser only ever talks to
// one origin (same as the nginx setup in production). Override the target with
// CADENCE_DEV_API if the backend runs elsewhere.
const apiTarget = process.env.CADENCE_DEV_API ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
})
