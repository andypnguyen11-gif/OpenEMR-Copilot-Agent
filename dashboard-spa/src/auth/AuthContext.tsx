import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import { refreshTokens, type AuthState, type DiscoveryConfig } from './oauth'

// Refresh `expiresAt` skew: treat the token as expired if it would expire
// within this many ms. Avoids the race where a request leaves the SPA with a
// just-valid token and arrives at OpenEMR a moment too late.
const EXPIRY_SKEW_MS = 30_000

interface AuthContextValue {
  state: AuthState | null
  setSession: (next: AuthState) => void
  clearSession: () => void
  // Returns a usable access token, refreshing single-flight if expired.
  // Pass `force=true` to bypass the freshness check (for diagnostics or the
  // demo "Force refresh" button — production callers leave it false).
  getAccessToken: (force?: boolean) => Promise<string>
}

const AuthContext = createContext<AuthContextValue | null>(null)

interface AuthProviderProps {
  config: DiscoveryConfig
  clientId: string
  children: ReactNode
}

export function AuthProvider({ config, clientId, children }: AuthProviderProps) {
  const [state, setState] = useState<AuthState | null>(null)

  // stateRef mirrors `state` so getAccessToken always reads the latest value
  // without becoming a new function on every render. The single-flight Promise
  // is also kept in a ref — module-level state would deadlock test isolation.
  const stateRef = useRef<AuthState | null>(state)
  stateRef.current = state
  const refreshInFlightRef = useRef<Promise<AuthState> | null>(null)

  const setSession = useCallback((next: AuthState) => setState(next), [])
  const clearSession = useCallback(() => setState(null), [])

  const getAccessToken = useCallback(async (force = false): Promise<string> => {
    const current = stateRef.current
    if (!current) {
      throw new Error('Not authenticated')
    }
    if (!force && current.expiresAt > Date.now() + EXPIRY_SKEW_MS) {
      return current.accessToken
    }
    if (!refreshInFlightRef.current) {
      refreshInFlightRef.current = refreshTokens({
        tokenEndpoint: config.token_endpoint,
        clientId,
        refreshToken: current.refreshToken,
      })
        .then((next) => {
          // Update the ref synchronously inside the resolution so a follow-up
          // getAccessToken() doesn't re-enter the refresh path while waiting
          // for React to flush its re-render.
          stateRef.current = next
          setState(next)
          return next
        })
        .finally(() => {
          refreshInFlightRef.current = null
        })
    }
    const next = await refreshInFlightRef.current
    return next.accessToken
  }, [config.token_endpoint, clientId])

  const value: AuthContextValue = {
    state,
    setSession,
    clearSession,
    getAccessToken,
  }
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return ctx
}

// Non-throwing variant — returns null when no AuthProvider is in scope.
// CardBase uses this so its existing standalone unit tests (which don't
// mount an AuthProvider) keep working while production renders inside the
// provider tree get full persistence behavior.
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext)
}
