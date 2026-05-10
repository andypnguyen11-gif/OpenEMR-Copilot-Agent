/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
//
// The SPA runs at http://localhost:5173/ and talks to OpenEMR at
// https://localhost:9300/ via direct cross-origin fetch (no proxy). The
// browser will need to accept OpenEMR's self-signed cert once.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
