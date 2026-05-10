// Browser-side glue between PKCE generation and OAuth helpers. Owns the
// `sessionStorage` stash that lets the /callback page recover the
// code_verifier the redirect started with. Kept separate from oauth.ts so
// that file remains pure-function testable.

import { generateCodeChallenge, generateCodeVerifier } from './pkce'
import { buildAuthorizeUrl, type DiscoveryConfig } from './oauth'
import type { AppEnv } from './env'

const STASH_PREFIX = 'pkce:'

interface StashedFlow {
  codeVerifier: string
  // ISO timestamp so a stale stash from a prior tab can be detected if needed.
  startedAt: string
}

export async function startAuthorizeRedirect(
  env: AppEnv,
  config: DiscoveryConfig,
): Promise<void> {
  const codeVerifier = generateCodeVerifier()
  const codeChallenge = await generateCodeChallenge(codeVerifier)
  const state = generateCodeVerifier()
  const stash: StashedFlow = { codeVerifier, startedAt: new Date().toISOString() }
  sessionStorage.setItem(`${STASH_PREFIX}${state}`, JSON.stringify(stash))
  const url = buildAuthorizeUrl({
    authorizeEndpoint: config.authorization_endpoint,
    clientId: env.clientId,
    redirectUri: env.redirectUri,
    scope: env.scope,
    state,
    codeChallenge,
    audience: env.audience,
  })
  window.location.assign(url)
}

export function consumeStashedFlow(state: string): StashedFlow | null {
  const key = `${STASH_PREFIX}${state}`
  const raw = sessionStorage.getItem(key)
  if (!raw) return null
  sessionStorage.removeItem(key)
  try {
    return JSON.parse(raw) as StashedFlow
  } catch {
    return null
  }
}
