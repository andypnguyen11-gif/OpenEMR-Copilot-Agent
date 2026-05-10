import { useEffect, useState } from 'react'
import { startAuthorizeRedirect } from '../auth/flow'
import { readEnv } from '../auth/env'
import type { DiscoveryConfig } from '../auth/oauth'

interface LoginProps {
  config: DiscoveryConfig
}

export function Login({ config }: LoginProps) {
  const [error, setError] = useState<Error | null>(null)
  useEffect(() => {
    startAuthorizeRedirect(readEnv(), config).catch(setError)
  }, [config])
  if (error) {
    return (
      <div className="container py-5">
        <h1 className="h3">Login failed</h1>
        <p className="text-danger">{error.message}</p>
      </div>
    )
  }
  return (
    <div className="container py-5">
      <p>Redirecting to OpenEMR…</p>
    </div>
  )
}
