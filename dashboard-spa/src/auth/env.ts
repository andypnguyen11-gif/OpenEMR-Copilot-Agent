// Centralized read of build-time env. Keeps `import.meta.env` access in one
// spot so tests and components can both depend on the same config shape.

export interface AppEnv {
  baseUrl: string
  clientId: string
  redirectUri: string
  postLogoutRedirectUri: string
  scope: string
  // OAuth `aud` parameter — the absolute FHIR base OpenEMR validates against
  // CustomAuthCodeGrant. Always points at OpenEMR directly, never a proxy.
  audience: string
  // Base URL useFhirClient uses to construct request URLs. In dev this is a
  // relative path served via Vite's proxy so cross-origin preflights don't
  // hit OpenEMR's CORSListener (which 404s on OPTIONS for FHIR resource
  // routes). In prod it can be set absolute via VITE_FHIR_BASE_URL when the
  // SPA host has CORS configured at OpenEMR.
  fhirBaseUrl: string
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
  // OpenEMR's expected `aud` is the FHIR base WITHOUT a trailing slash —
  // see CustomAuthCodeGrant.php and ServerConfig::getFhirUrl(). A trailing
  // slash gets a 400 invalid_request "Aud parameter did not match".
  const audience = `${baseUrl}/apis/default/fhir`
  // Default to a relative path so dev requests flow through Vite's `/apis`
  // proxy and avoid the cross-origin preflight that OpenEMR's CORSListener
  // currently fails (returns 404 on OPTIONS for FHIR resource routes). Prod
  // can override with an absolute URL when the OpenEMR origin grants CORS.
  const fhirBaseUrl =
    import.meta.env.VITE_FHIR_BASE_URL || '/apis/default/fhir'
  return {
    baseUrl,
    clientId,
    redirectUri,
    postLogoutRedirectUri,
    scope: SCOPE,
    audience,
    fhirBaseUrl,
  }
}

export function discoveryUrl(baseUrl: string): string {
  return `${baseUrl}/oauth2/default/.well-known/openid-configuration`
}
