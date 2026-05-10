import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  buildAuthorizeUrl,
  exchangeCode,
  refreshTokens,
  buildLogoutUrl,
  fetchDiscovery,
} from './oauth'

const TOKEN_ENDPOINT = 'https://localhost:9300/oauth2/default/token'
const AUTHORIZE_ENDPOINT = 'https://localhost:9300/oauth2/default/authorize'
const DISCOVERY_URL = 'https://localhost:9300/.well-known/openid-configuration'
const END_SESSION = 'https://localhost:9300/oauth2/default/logout'
const CLIENT_ID = 'test-client-id'
const REDIRECT_URI = 'http://localhost:5173/callback'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('buildAuthorizeUrl', () => {
  it('includes every PKCE+OIDC param the spec requires', () => {
    const url = new URL(
      buildAuthorizeUrl({
        authorizeEndpoint: AUTHORIZE_ENDPOINT,
        clientId: CLIENT_ID,
        redirectUri: REDIRECT_URI,
        scope: 'openid offline_access patient/Patient.read',
        state: 'state-123',
        codeChallenge: 'challenge-abc',
        audience: 'https://localhost:9300/apis/default/fhir/',
      }),
    )
    expect(url.searchParams.get('response_type')).toBe('code')
    expect(url.searchParams.get('client_id')).toBe(CLIENT_ID)
    expect(url.searchParams.get('redirect_uri')).toBe(REDIRECT_URI)
    expect(url.searchParams.get('scope')).toBe(
      'openid offline_access patient/Patient.read',
    )
    expect(url.searchParams.get('state')).toBe('state-123')
    expect(url.searchParams.get('code_challenge')).toBe('challenge-abc')
    expect(url.searchParams.get('code_challenge_method')).toBe('S256')
    expect(url.searchParams.get('aud')).toBe(
      'https://localhost:9300/apis/default/fhir/',
    )
  })
})

describe('buildLogoutUrl', () => {
  it('includes id_token_hint and post_logout_redirect_uri', () => {
    const url = new URL(
      buildLogoutUrl({
        endSessionEndpoint: END_SESSION,
        idTokenHint: 'id-token-xyz',
        postLogoutRedirectUri: 'http://localhost:5173/',
      }),
    )
    expect(url.searchParams.get('id_token_hint')).toBe('id-token-xyz')
    expect(url.searchParams.get('post_logout_redirect_uri')).toBe(
      'http://localhost:5173/',
    )
  })
})

describe('fetchDiscovery', () => {
  it('GETs the discovery document and returns the parsed JSON', async () => {
    const mockConfig = {
      authorization_endpoint: AUTHORIZE_ENDPOINT,
      token_endpoint: TOKEN_ENDPOINT,
      end_session_endpoint: END_SESSION,
    }
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(mockConfig), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const config = await fetchDiscovery(DISCOVERY_URL)
    expect(config.token_endpoint).toBe(TOKEN_ENDPOINT)
    expect(fetchMock).toHaveBeenCalledWith(DISCOVERY_URL, expect.any(Object))
  })
})

describe('exchangeCode', () => {
  it('POSTs grant_type=authorization_code with code, verifier, client_id', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            access_token: 'access-1',
            refresh_token: 'refresh-1',
            expires_in: 3600,
            scope: 'openid offline_access',
            patient: 'patient-uuid-1',
            id_token: 'id-token-1',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const before = Date.now()
    const state = await exchangeCode({
      tokenEndpoint: TOKEN_ENDPOINT,
      clientId: CLIENT_ID,
      redirectUri: REDIRECT_URI,
      code: 'auth-code-1',
      codeVerifier: 'verifier-1',
    })
    const after = Date.now()

    const call = fetchMock.mock.calls[0] as [string, RequestInit] | undefined
    expect(call?.[0]).toBe(TOKEN_ENDPOINT)
    const body = new URLSearchParams(String(call?.[1]?.body ?? ''))
    expect(body.get('grant_type')).toBe('authorization_code')
    expect(body.get('code')).toBe('auth-code-1')
    expect(body.get('redirect_uri')).toBe(REDIRECT_URI)
    expect(body.get('client_id')).toBe(CLIENT_ID)
    expect(body.get('code_verifier')).toBe('verifier-1')

    expect(state.accessToken).toBe('access-1')
    expect(state.refreshToken).toBe('refresh-1')
    expect(state.patient).toBe('patient-uuid-1')
    expect(state.idToken).toBe('id-token-1')
    expect(state.expiresAt).toBeGreaterThanOrEqual(before + 3600 * 1000)
    expect(state.expiresAt).toBeLessThanOrEqual(after + 3600 * 1000)
  })

  it('throws on non-2xx response and surfaces the OAuth error code', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: 'invalid_grant',
              error_description: 'Authorization code expired',
            }),
            { status: 400 },
          ),
      ),
    )
    await expect(
      exchangeCode({
        tokenEndpoint: TOKEN_ENDPOINT,
        clientId: CLIENT_ID,
        redirectUri: REDIRECT_URI,
        code: 'expired-code',
        codeVerifier: 'verifier',
      }),
    ).rejects.toThrowError(/invalid_grant/)
  })
})

describe('refreshTokens', () => {
  it('POSTs grant_type=refresh_token with refresh_token + client_id', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            access_token: 'access-2',
            refresh_token: 'refresh-2',
            expires_in: 3600,
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const state = await refreshTokens({
      tokenEndpoint: TOKEN_ENDPOINT,
      clientId: CLIENT_ID,
      refreshToken: 'refresh-1',
    })

    const call = fetchMock.mock.calls[0] as [string, RequestInit] | undefined
    const body = new URLSearchParams(String(call?.[1]?.body ?? ''))
    expect(body.get('grant_type')).toBe('refresh_token')
    expect(body.get('refresh_token')).toBe('refresh-1')
    expect(body.get('client_id')).toBe(CLIENT_ID)

    expect(state.accessToken).toBe('access-2')
    expect(state.refreshToken).toBe('refresh-2')
  })

  it('persists rotated refresh_token (OpenEMR rotates on refresh)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              access_token: 'a',
              refresh_token: 'rotated-refresh',
              expires_in: 3600,
            }),
            { status: 200 },
          ),
      ),
    )
    const state = await refreshTokens({
      tokenEndpoint: TOKEN_ENDPOINT,
      clientId: CLIENT_ID,
      refreshToken: 'old-refresh',
    })
    expect(state.refreshToken).toBe('rotated-refresh')
    expect(state.refreshToken).not.toBe('old-refresh')
  })
})
