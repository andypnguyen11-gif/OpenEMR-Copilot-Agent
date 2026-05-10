// OAuth2 + OIDC stateless helpers. The React layer (AuthContext) wires these
// into a session — single-flight refresh, token storage, route gating — but
// these functions are pure I/O wrappers and pure URL builders so the auth
// surface is deterministically testable.

export interface DiscoveryConfig {
  authorization_endpoint: string
  token_endpoint: string
  end_session_endpoint?: string
  registration_endpoint?: string
  issuer?: string
}

export interface AuthState {
  accessToken: string
  refreshToken: string
  expiresAt: number
  patient?: string
  idToken?: string
  scope?: string
}

export interface BuildAuthorizeUrlInput {
  authorizeEndpoint: string
  clientId: string
  redirectUri: string
  scope: string
  state: string
  codeChallenge: string
  audience: string
}

export interface ExchangeCodeInput {
  tokenEndpoint: string
  clientId: string
  redirectUri: string
  code: string
  codeVerifier: string
}

export interface RefreshTokensInput {
  tokenEndpoint: string
  clientId: string
  refreshToken: string
}

export interface BuildLogoutUrlInput {
  endSessionEndpoint: string
  idTokenHint: string
  postLogoutRedirectUri: string
}

interface TokenResponse {
  access_token: string
  refresh_token: string
  expires_in: number
  scope?: string
  patient?: string
  id_token?: string
}

interface OAuthErrorResponse {
  error: string
  error_description?: string
}

export async function fetchDiscovery(
  discoveryUrl: string,
): Promise<DiscoveryConfig> {
  const response = await fetch(discoveryUrl, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new Error(`Discovery failed: HTTP ${response.status}`)
  }
  return (await response.json()) as DiscoveryConfig
}

export function buildAuthorizeUrl(input: BuildAuthorizeUrlInput): string {
  const url = new URL(input.authorizeEndpoint)
  url.searchParams.set('response_type', 'code')
  url.searchParams.set('client_id', input.clientId)
  url.searchParams.set('redirect_uri', input.redirectUri)
  url.searchParams.set('scope', input.scope)
  url.searchParams.set('state', input.state)
  url.searchParams.set('code_challenge', input.codeChallenge)
  url.searchParams.set('code_challenge_method', 'S256')
  url.searchParams.set('aud', input.audience)
  return url.toString()
}

export function buildLogoutUrl(input: BuildLogoutUrlInput): string {
  const url = new URL(input.endSessionEndpoint)
  url.searchParams.set('id_token_hint', input.idTokenHint)
  url.searchParams.set('post_logout_redirect_uri', input.postLogoutRedirectUri)
  return url.toString()
}

export async function exchangeCode(input: ExchangeCodeInput): Promise<AuthState> {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code: input.code,
    redirect_uri: input.redirectUri,
    client_id: input.clientId,
    code_verifier: input.codeVerifier,
  })
  return tokenRequest(input.tokenEndpoint, body)
}

export async function refreshTokens(input: RefreshTokensInput): Promise<AuthState> {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: input.refreshToken,
    client_id: input.clientId,
  })
  return tokenRequest(input.tokenEndpoint, body)
}

async function tokenRequest(
  endpoint: string,
  body: URLSearchParams,
): Promise<AuthState> {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Accept: 'application/json',
    },
    body: body.toString(),
  })

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new Error(`Token endpoint returned non-JSON HTTP ${response.status}`)
  }

  if (!response.ok) {
    const err = payload as OAuthErrorResponse
    throw new Error(
      `OAuth ${err.error ?? 'unknown_error'}: ${err.error_description ?? `HTTP ${response.status}`}`,
    )
  }

  const tokens = payload as TokenResponse
  const state: AuthState = {
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    expiresAt: Date.now() + tokens.expires_in * 1000,
  }
  if (tokens.scope !== undefined) state.scope = tokens.scope
  if (tokens.patient !== undefined) state.patient = tokens.patient
  if (tokens.id_token !== undefined) state.idToken = tokens.id_token
  return state
}
