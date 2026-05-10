import { useMemo } from 'react'
import { useAuth } from '../auth/AuthContext'
import { readEnv } from '../auth/env'
import { createFhirClient, type FhirClient } from './client'

// Wires the hand-rolled FhirClient to AuthContext so cards never have to
// thread tokens or base URLs themselves. Memoized so a render churn on the
// AuthProvider doesn't tear down in-flight requests inside child cards.
export function useFhirClient(): FhirClient {
  const { getAccessToken } = useAuth()
  // readEnv is a pure read of import.meta.env — fine to call on every render,
  // but caching avoids re-allocating the env object that flows into useMemo
  // dependency comparison below.
  const env = useMemo(() => readEnv(), [])
  return useMemo(
    () => createFhirClient({ baseUrl: env.fhirBaseUrl, getAccessToken }),
    [env.fhirBaseUrl, getAccessToken],
  )
}
