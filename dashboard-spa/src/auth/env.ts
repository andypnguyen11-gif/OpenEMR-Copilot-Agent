// Centralized read of build-time env. Keeps `import.meta.env` access in one
// spot so tests and components can both depend on the same config shape.

export interface AppEnv {
  baseUrl: string
  clientId: string
  redirectUri: string
  postLogoutRedirectUri: string
  scope: string
  audience: string
}

const SCOPE = [
  'openid',
  'offline_access',
  'launch/patient',
  'patient/Patient.read',
  'patient/AllergyIntolerance.read',
  'patient/Condition.read',
  'patient/MedicationRequest.read',
  'patient/CareTeam.read',
  'patient/Practitioner.read',
  'patient/Observation.read',
].join(' ')

export function readEnv(): AppEnv {
  const baseUrl = import.meta.env.VITE_OPENEMR_BASE_URL
  const clientId = import.meta.env.VITE_OAUTH_CLIENT_ID
  if (!baseUrl) {
    throw new Error(
      'VITE_OPENEMR_BASE_URL is not set. Copy .env.example to .env.local and fill it in.',
    )
  }
  if (!clientId) {
    throw new Error(
      'VITE_OAUTH_CLIENT_ID is not set. Register a public client in OpenEMR and paste the ID into .env.local.',
    )
  }
  const redirectUri = `${window.location.origin}/callback`
  const postLogoutRedirectUri = `${window.location.origin}/`
  const audience = `${baseUrl}/apis/default/fhir/`
  return { baseUrl, clientId, redirectUri, postLogoutRedirectUri, scope: SCOPE, audience }
}

export function discoveryUrl(baseUrl: string): string {
  return `${baseUrl}/oauth2/default/.well-known/openid-configuration`
}
