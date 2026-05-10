import { useEffect, useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import { fetchDiscovery, type DiscoveryConfig } from './auth/oauth'
import { discoveryUrl, readEnv } from './auth/env'
import { RequireAuth } from './auth/routes'
import { Login } from './pages/Login'
import { Callback } from './pages/Callback'
import { Home } from './pages/Home'

function App() {
  const [config, setConfig] = useState<DiscoveryConfig | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    try {
      const env = readEnv()
      fetchDiscovery(discoveryUrl(env.baseUrl))
        .then((c) => {
          if (!cancelled) setConfig(c)
        })
        .catch((e: unknown) => {
          if (!cancelled) setError(e instanceof Error ? e : new Error(String(e)))
        })
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)))
    }
    return () => {
      cancelled = true
    }
  }, [])

  if (error) {
    return (
      <div className="container py-5">
        <h1 className="h3">Cannot reach OpenEMR</h1>
        <p className="text-danger">{error.message}</p>
        <p className="text-muted small">
          Confirm the OpenEMR dev stack is running, that you've accepted the
          self-signed cert at <code>https://localhost:9300/</code>, and that
          <code> .env.local</code> points at the right base URL and client ID.
        </p>
      </div>
    )
  }

  if (!config) {
    return (
      <div className="container py-5">
        <p>Loading OpenEMR configuration…</p>
      </div>
    )
  }

  const env = readEnv()
  return (
    <BrowserRouter>
      <AuthProvider config={config} clientId={env.clientId}>
        <Routes>
          <Route path="/login" element={<Login config={config} />} />
          <Route path="/callback" element={<Callback config={config} />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <Home />
              </RequireAuth>
            }
          />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App
