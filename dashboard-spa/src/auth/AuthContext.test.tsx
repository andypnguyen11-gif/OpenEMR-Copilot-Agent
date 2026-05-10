import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { AuthProvider, useAuth } from './AuthContext'
import type { AuthState, DiscoveryConfig } from './oauth'

const CONFIG: DiscoveryConfig = {
  authorization_endpoint: 'https://localhost:9300/oauth2/default/authorize',
  token_endpoint: 'https://localhost:9300/oauth2/default/token',
  end_session_endpoint: 'https://localhost:9300/oauth2/default/logout',
}
const CLIENT_ID = 'test-client-id'

function wrapper({ children }: { children: ReactNode }) {
  return (
    <AuthProvider config={CONFIG} clientId={CLIENT_ID}>
      {children}
    </AuthProvider>
  )
}

const FRESH: AuthState = {
  accessToken: 'access-fresh',
  refreshToken: 'refresh-1',
  expiresAt: Date.now() + 60 * 60 * 1000,
}

const EXPIRED: AuthState = {
  accessToken: 'access-expired',
  refreshToken: 'refresh-1',
  expiresAt: Date.now() - 1000,
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('AuthProvider', () => {
  it('initial state is unauthenticated', () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.state).toBeNull()
  })

  it('setSession stores the AuthState; clearSession resets it', () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => result.current.setSession(FRESH))
    expect(result.current.state).toEqual(FRESH)
    act(() => result.current.clearSession())
    expect(result.current.state).toBeNull()
  })
})

describe('getAccessToken', () => {
  it('throws when no session is set', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    await expect(result.current.getAccessToken()).rejects.toThrow(
      /not authenticated/i,
    )
  })

  it('returns the current access token without a network call when fresh', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => result.current.setSession(FRESH))
    const token = await result.current.getAccessToken()
    expect(token).toBe('access-fresh')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('refreshes when expired and returns the new access token', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            access_token: 'access-refreshed',
            refresh_token: 'refresh-rotated',
            expires_in: 3600,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => result.current.setSession(EXPIRED))
    const token = await result.current.getAccessToken()
    expect(token).toBe('access-refreshed')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(result.current.state?.refreshToken).toBe('refresh-rotated'),
    )
  })

  it(
    'single-flight: 6 parallel callers with an expired token fire exactly one ' +
      'token-endpoint request',
    async () => {
      let resolveFetch: (value: Response) => void = () => {}
      const fetchMock = vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve
          }),
      )
      vi.stubGlobal('fetch', fetchMock)
      const { result } = renderHook(() => useAuth(), { wrapper })
      act(() => result.current.setSession(EXPIRED))

      // Fire 6 parallel calls before the single in-flight promise resolves.
      const tokens = Promise.all(
        Array.from({ length: 6 }, () => result.current.getAccessToken()),
      )
      // Allow microtasks so all 6 call sites enter and dedup on the same promise.
      await Promise.resolve()
      await Promise.resolve()

      // Exactly one fetch should have fired.
      expect(fetchMock).toHaveBeenCalledTimes(1)

      // Resolve the in-flight refresh.
      resolveFetch(
        new Response(
          JSON.stringify({
            access_token: 'access-refreshed',
            refresh_token: 'refresh-rotated',
            expires_in: 3600,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )

      const resolved = await tokens
      expect(resolved).toEqual(Array.from({ length: 6 }, () => 'access-refreshed'))
      expect(fetchMock).toHaveBeenCalledTimes(1)
    },
  )

  it('after refresh resolves, a fresh subsequent call does not refire fetch', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            access_token: 'access-refreshed',
            refresh_token: 'refresh-rotated',
            expires_in: 3600,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => result.current.setSession(EXPIRED))
    await result.current.getAccessToken()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const second = await result.current.getAccessToken()
    expect(second).toBe('access-refreshed')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
