import { describe, it, expect } from 'vitest'
import { decodeIdTokenSub } from './idToken'

// Hand-rolled token builder — keeps the test independent of any JWT lib.
function makeToken(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: 'none', typ: 'JWT' }))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  const body = btoa(JSON.stringify(payload))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return `${header}.${body}.signature`
}

describe('decodeIdTokenSub', () => {
  it('extracts a string sub claim', () => {
    expect(decodeIdTokenSub(makeToken({ sub: 'user-42' }))).toBe('user-42')
  })

  it('returns undefined when sub is absent', () => {
    expect(decodeIdTokenSub(makeToken({ aud: 'spa' }))).toBeUndefined()
  })

  it('returns undefined when sub is non-string', () => {
    expect(decodeIdTokenSub(makeToken({ sub: 42 }))).toBeUndefined()
    expect(decodeIdTokenSub(makeToken({ sub: null }))).toBeUndefined()
  })

  it('returns undefined for malformed JWT input', () => {
    expect(decodeIdTokenSub('not-a-jwt')).toBeUndefined()
    expect(decodeIdTokenSub('only.two')).toBe(undefined) // missing payload section is fine
    expect(decodeIdTokenSub('aaa.@@@.bbb')).toBeUndefined()
    expect(decodeIdTokenSub('')).toBeUndefined()
  })

  it('handles base64url-encoded payloads with stripped padding', () => {
    // Manually build a payload whose base64 representation needs padding.
    const sub = 'a-stable-uuid-with-trailing-chars'
    const payloadJson = JSON.stringify({ sub })
    const url = btoa(payloadJson)
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '')
    const token = `header.${url}.signature`
    expect(decodeIdTokenSub(token)).toBe(sub)
  })
})
