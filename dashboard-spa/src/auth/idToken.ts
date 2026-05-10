// Minimal id_token sub extractor. We don't validate the signature here —
// AuthContext already trusts the token end-to-end (it came back over TLS
// from the OAuth token endpoint), so the only thing we need is the `sub`
// claim to namespace per-user UI preferences. JWT signature validation is
// the IdP's job; us re-doing it on the client buys nothing.
//
// Returns `undefined` for any malformed input so callers can fall back to
// an "anonymous" namespace without try/catching at the call site.

interface JwtPayload {
  sub?: unknown
}

export function decodeIdTokenSub(idToken: string): string | undefined {
  const segments = idToken.split('.')
  if (segments.length < 2) return undefined
  const payload = segments[1]
  if (payload === undefined || payload === '') return undefined
  try {
    // base64url → base64. atob doesn't accept the URL-safe alphabet.
    const padded = payload
      .replace(/-/g, '+')
      .replace(/_/g, '/')
      .padEnd(payload.length + ((4 - (payload.length % 4)) % 4), '=')
    const json = JSON.parse(atob(padded)) as JwtPayload
    return typeof json.sub === 'string' && json.sub !== ''
      ? json.sub
      : undefined
  } catch {
    return undefined
  }
}
