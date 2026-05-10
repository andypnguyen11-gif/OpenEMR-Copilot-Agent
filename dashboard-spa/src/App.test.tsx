import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import App from './App'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.stubEnv('VITE_OPENEMR_BASE_URL', 'https://localhost:9300')
  vi.stubEnv('VITE_OAUTH_CLIENT_ID', 'test-client-id')
  // Discovery fetch hangs forever in this test — we only care that the App
  // mounts and shows the loading screen while it waits.
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
})

describe('App', () => {
  it('mounts and shows the discovery-loading screen', () => {
    render(<App />)
    expect(screen.getByText(/loading openemr configuration/i)).toBeInTheDocument()
  })
})
