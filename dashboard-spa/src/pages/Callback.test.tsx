import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { Callback } from './Callback'
import { AuthProvider } from '../auth/AuthContext'
import type { DiscoveryConfig } from '../auth/oauth'

const CONFIG: DiscoveryConfig = {
  authorization_endpoint: 'https://localhost:9300/oauth2/default/authorize',
  token_endpoint: 'https://localhost:9300/oauth2/default/token',
  end_session_endpoint: 'https://localhost:9300/oauth2/default/logout',
}

beforeEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
  vi.stubEnv('VITE_OPENEMR_BASE_URL', 'https://localhost:9300')
  vi.stubEnv('VITE_OAUTH_CLIENT_ID', 'test-client-id')
})

function renderCallbackAt(initialUrl: string) {
  return render(
    <MemoryRouter initialEntries={[initialUrl]}>
      <AuthProvider config={CONFIG} clientId="test-client-id">
        <Routes>
          <Route path="/callback" element={<Callback config={CONFIG} />} />
          <Route path="/" element={<div>landed at home</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('Callback', () => {
  it('shows an error when code or state is missing', async () => {
    renderCallbackAt('/callback')
    await waitFor(() =>
      expect(screen.getByText(/missing code or state/i)).toBeInTheDocument(),
    )
  })

  it('rejects unknown state (CSRF / stale callback guard)', async () => {
    renderCallbackAt('/callback?code=abc&state=never-stashed')
    await waitFor(() =>
      expect(
        screen.getByText(/unknown oauth state.*csrf|stale/i),
      ).toBeInTheDocument(),
    )
  })

  it('surfaces ?error from the authorize redirect', async () => {
    renderCallbackAt(
      '/callback?error=access_denied&error_description=user%20cancelled',
    )
    await waitFor(() =>
      expect(screen.getByText(/access_denied/i)).toBeInTheDocument(),
    )
  })
})
