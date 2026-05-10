import { describe, it, expect } from 'vitest'
import { generateCodeVerifier, generateCodeChallenge } from './pkce'

describe('generateCodeVerifier', () => {
  it('returns a verifier within RFC 7636 length bounds (43-128 chars)', () => {
    const v = generateCodeVerifier()
    expect(v.length).toBeGreaterThanOrEqual(43)
    expect(v.length).toBeLessThanOrEqual(128)
  })

  it('returns a URL-safe character set (RFC 7636 §4.1: ALPHA / DIGIT / "-" / "." / "_" / "~")', () => {
    const v = generateCodeVerifier()
    expect(v).toMatch(/^[A-Za-z0-9._~-]+$/)
  })

  it('returns different values across calls (entropy check)', () => {
    const a = generateCodeVerifier()
    const b = generateCodeVerifier()
    const c = generateCodeVerifier()
    expect(new Set([a, b, c]).size).toBe(3)
  })
})

describe('generateCodeChallenge', () => {
  // RFC 7636 §B.1 worked example. Anchors the SHA-256 + base64url-no-pad chain.
  const RFC_VERIFIER = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
  const RFC_CHALLENGE = 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'

  it('matches the RFC 7636 §B.1 worked example', async () => {
    expect(await generateCodeChallenge(RFC_VERIFIER)).toBe(RFC_CHALLENGE)
  })

  it('returns base64url with no padding (no "=", no "+", no "/")', async () => {
    const challenge = await generateCodeChallenge(generateCodeVerifier())
    expect(challenge).not.toContain('=')
    expect(challenge).not.toContain('+')
    expect(challenge).not.toContain('/')
    expect(challenge).toMatch(/^[A-Za-z0-9_-]+$/)
  })

  it('is deterministic for a given verifier', async () => {
    const v = generateCodeVerifier()
    const a = await generateCodeChallenge(v)
    const b = await generateCodeChallenge(v)
    expect(a).toBe(b)
  })

  it('returns a 43-character SHA-256 base64url-no-pad digest', async () => {
    // SHA-256 = 32 bytes → base64 = 44 chars with one "=" pad → base64url-no-pad = 43 chars
    const challenge = await generateCodeChallenge(generateCodeVerifier())
    expect(challenge.length).toBe(43)
  })
})
