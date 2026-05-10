// RFC 7636 PKCE helpers for OAuth2 public clients.
//
// `code_verifier` is a 43-128 character random string from the URL-safe set.
// `code_challenge` is the base64url-no-pad encoding of SHA-256(code_verifier).
// We generate 32 random bytes (256 bits of entropy) which encode to 43 chars
// of base64url, comfortably above the 43-char floor.

const VERIFIER_BYTES = 32

export function generateCodeVerifier(): string {
  const bytes = new Uint8Array(VERIFIER_BYTES)
  crypto.getRandomValues(bytes)
  return base64UrlEncode(bytes)
}

export async function generateCodeChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier)
  const digest = await crypto.subtle.digest('SHA-256', data)
  return base64UrlEncode(new Uint8Array(digest))
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  return btoa(binary)
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replaceAll('=', '')
}
