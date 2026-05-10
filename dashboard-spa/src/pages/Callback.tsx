import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { exchangeCode, type DiscoveryConfig } from '../auth/oauth'
import { useAuth } from '../auth/AuthContext'
import { consumeStashedFlow } from '../auth/flow'
import { readEnv } from '../auth/env'

interface CallbackProps {
  config: DiscoveryConfig
}

export function Callback({ config }: CallbackProps) {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const { setSession } = useAuth()
  const [error, setError] = useState<string | null>(null)
  // React 19 StrictMode runs effects twice in dev. Guard so we only consume
  // the sessionStorage stash + token-exchange once per real callback.
  const consumedRef = useRef(false)

  useEffect(() => {
    if (consumedRef.current) return
    consumedRef.current = true

    const oauthError = params.get('error')
    if (oauthError) {
      setError(`OAuth ${oauthError}: ${params.get('error_description') ?? ''}`)
      return
    }
    const code = params.get('code')
    const state = params.get('state')
    if (!code || !state) {
      setError('Missing code or state in callback URL.')
      return
    }
    const stash = consumeStashedFlow(state)
    if (!stash) {
      setError(
        'Unknown OAuth state — the callback may be stale or this could be a CSRF attempt.',
      )
      return
    }

    const env = readEnv()
    exchangeCode({
      tokenEndpoint: config.token_endpoint,
      clientId: env.clientId,
      redirectUri: env.redirectUri,
      code,
      codeVerifier: stash.codeVerifier,
    })
      .then((authState) => {
        setSession(authState)
        navigate('/', { replace: true })
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : 'Token exchange failed.')
      })
  }, [params, navigate, setSession, config.token_endpoint])

  if (error) {
    return (
      <div className="container py-5">
        <h1 className="h3">Sign-in failed</h1>
        <p className="text-danger">{error}</p>
        <a href="/login" className="btn btn-primary mt-3">
          Try again
        </a>
      </div>
    )
  }
  return (
    <div className="container py-5">
      <p>Completing sign-in…</p>
    </div>
  )
}
