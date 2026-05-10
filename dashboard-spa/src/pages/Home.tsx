import { useAuth } from '../auth/AuthContext'

// Minimal post-auth landing page. PR 3 replaces this with /patients picker.
export function Home() {
  const { state, clearSession, getAccessToken } = useAuth()
  const expiresIn = state ? Math.max(0, Math.round((state.expiresAt - Date.now()) / 1000)) : 0
  return (
    <div className="container py-5">
      <h1 className="display-4">OpenEMR Dashboard SPA</h1>
      <p className="lead text-muted">PR 2 — OAuth2 PKCE login wired.</p>
      {state ? (
        <div className="card mt-4">
          <div className="card-body">
            <h2 className="h5">Authenticated</h2>
            <dl className="row mb-0">
              <dt className="col-sm-3">Patient context</dt>
              <dd className="col-sm-9">
                <code>{state.patient ?? '— (no SMART launch context)'}</code>
              </dd>
              <dt className="col-sm-3">Access token</dt>
              <dd className="col-sm-9">
                <code>{state.accessToken.slice(0, 20)}…</code>
              </dd>
              <dt className="col-sm-3">Expires in</dt>
              <dd className="col-sm-9">{expiresIn}s</dd>
              <dt className="col-sm-3">Scope</dt>
              <dd className="col-sm-9">
                <code className="small">{state.scope ?? '—'}</code>
              </dd>
            </dl>
            <div className="mt-3 d-flex gap-2">
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                onClick={() => {
                  void getAccessToken(true).then((t) =>
                    alert(
                      `Forced refresh hit /oauth2/default/token.\nNew access token (first 20 chars): ${t.slice(0, 20)}…`,
                    ),
                  )
                }}
              >
                Force refresh
              </button>
              <button
                type="button"
                className="btn btn-outline-danger btn-sm"
                onClick={clearSession}
              >
                Sign out (local)
              </button>
            </div>
          </div>
        </div>
      ) : (
        <p>Not signed in.</p>
      )}
    </div>
  )
}
