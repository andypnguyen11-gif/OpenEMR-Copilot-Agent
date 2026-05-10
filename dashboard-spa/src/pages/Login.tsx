import { useState } from 'react'
import { startAuthorizeRedirect } from '../auth/flow'
import { readEnv } from '../auth/env'
import type { DiscoveryConfig } from '../auth/oauth'

interface LoginProps {
  config: DiscoveryConfig
}

// Mirrors the upstream login screen at interface/login/login.php — the
// OpenEMR wordmark over the "most popular open-source EHR…" tagline,
// centered card on a light surface, single primary action. We replace the
// username/password/language form with a single Sign in button because the
// SPA delegates auth to OpenEMR's OAuth provider screen, which is the
// upstream credentials surface.
export function Login({ config }: LoginProps) {
  const [error, setError] = useState<Error | null>(null)
  const [redirecting, setRedirecting] = useState(false)

  const handleSignIn = () => {
    setError(null)
    setRedirecting(true)
    startAuthorizeRedirect(readEnv(), config).catch((e: unknown) => {
      setRedirecting(false)
      setError(e instanceof Error ? e : new Error(String(e)))
    })
  }

  return (
    <div className="login-shell">
      <main className="login-card card" role="main">
        <div className="card-body p-4">
          <div className="text-center mb-4">
            <img
              src="/openemr-logo.svg"
              alt="OpenEMR"
              className="login-logo"
              width="320"
              height="80"
            />
          </div>
          <p className="text-center text-muted mb-4 login-tagline">
            The most popular open-source Electronic Health Record and
            Medical Practice Management solution.
          </p>

          {error && (
            <div className="alert alert-danger" role="alert">
              <strong>Login failed.</strong> {error.message}
            </div>
          )}

          <button
            type="button"
            className="btn btn-primary btn-block btn-lg"
            onClick={handleSignIn}
            disabled={redirecting}
          >
            {redirecting ? 'Redirecting to OpenEMR…' : 'Sign in with OpenEMR'}
          </button>

          <p className="text-center text-muted small mt-4 mb-0">
            You will be redirected to OpenEMR to authenticate.
          </p>
        </div>
      </main>
    </div>
  )
}
