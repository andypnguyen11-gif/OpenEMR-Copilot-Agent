/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
//
// The SPA runs at http://localhost:5173/. OAuth top-level redirects go
// straight to OpenEMR (cross-origin is fine for navigations and form-encoded
// POSTs). FHIR fetches add `Authorization` and `Accept`, which trigger CORS
// preflights — and OpenEMR's CORSListener currently 404s on OPTIONS for
// resource routes. Until that's fixed upstream, we proxy `/apis` through Vite
// so the browser sees same-origin calls.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const proxyTarget = env.VITE_OPENEMR_BASE_URL || 'https://localhost:9300'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/apis': {
          target: proxyTarget,
          changeOrigin: true,
          // OpenEMR dev runs on a self-signed cert; Vite needs to accept it
          // to forward requests. Production deploys should not need this.
          secure: false,
        },
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      css: false,
    },
  }
})
