// Hand-rolled FHIR client over fetch. Stays small on purpose — every quirk
// has a comment so a future reader can audit the assumptions against
// OpenEMR's behavior.
//
// Why no Medplum SDK or fhirclient.js: the migration doc §4 argues for a
// typed-but-minimal data layer. Everything below is ~100 LOC and exercises
// only the FHIR operations the dashboard actually uses (search + read).

const DEFAULT_COUNT = '200'

interface MinimalBundle {
  resourceType: 'Bundle'
  entry?: Array<{ resource?: unknown }>
}

export interface FhirClientConfig {
  baseUrl: string
  // Pulls a usable access token from AuthContext. `force=true` bypasses the
  // freshness check, which the 401-retry path uses to invalidate stale
  // tokens after server-side expiry.
  getAccessToken: (force?: boolean) => Promise<string>
}

export interface FhirClient {
  // Generic over the result resource type; resourceType is passed as a
  // string at the wire so callers parameterize via `client.search<AllergyIntolerance>(...)`.
  search<T>(resourceType: string, params: Record<string, string>): Promise<T[]>
  read<T>(resourceType: string, id: string): Promise<T>
}

export function createFhirClient(config: FhirClientConfig): FhirClient {
  return {
    async search<T>(
      resourceType: string,
      params: Record<string, string>,
    ): Promise<T[]> {
      const url = buildSearchUrl(config.baseUrl, resourceType, params)
      const response = await requestWith401Retry(config, url)
      if (response.status === 403) {
        // Out-of-scope is documented as empty for the dashboard surface —
        // a 403 on (e.g.) CareTeam should not blank the whole page.
        // §7 Known parity gaps logs the surface where this matters.
        console.warn(`FHIR ${resourceType} returned 403; treating as empty.`)
        return []
      }
      if (!response.ok) {
        throw new Error(
          `FHIR ${resourceType} search failed: HTTP ${response.status}`,
        )
      }
      const bundle = (await response.json()) as MinimalBundle
      // OpenEMR returns Bundle.type='collection' instead of 'searchset' —
      // don't assert on the discriminator. Just flatten.
      return (bundle.entry ?? [])
        .map((e) => e.resource as T | undefined)
        .filter((r): r is T => r !== undefined)
    },

    async read<T>(resourceType: string, id: string): Promise<T> {
      const url = `${config.baseUrl}/${resourceType}/${id}`
      const response = await requestWith401Retry(config, url)
      if (!response.ok) {
        throw new Error(
          `FHIR ${resourceType}.read(${id}) failed: HTTP ${response.status}`,
        )
      }
      return (await response.json()) as T
    },
  }
}

function buildSearchUrl(
  baseUrl: string,
  resourceType: string,
  params: Record<string, string>,
): string {
  // URLSearchParams (not new URL) — the dev build uses a relative
  // baseUrl ("/apis/default/fhir" via Vite's proxy), and `new URL` rejects
  // those. fetch() itself accepts relative URLs fine.
  const search = new URLSearchParams(params)
  if (!search.has('_count')) {
    search.set('_count', DEFAULT_COUNT)
  }
  return `${baseUrl}/${resourceType}?${search.toString()}`
}

async function requestWith401Retry(
  config: FhirClientConfig,
  url: string,
): Promise<Response> {
  const first = await fhirFetch(config, url, false)
  if (first.status !== 401) return first
  // Token rejected — force a refresh through AuthContext (single-flight) and
  // retry exactly once. AuthContext deduplicates parallel refresh attempts,
  // so 6 cards each hitting 401 fan in to one /token call.
  const second = await fhirFetch(config, url, true)
  return second
}

async function fhirFetch(
  config: FhirClientConfig,
  url: string,
  forceRefresh: boolean,
): Promise<Response> {
  const token = await config.getAccessToken(forceRefresh)
  return fetch(url, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/fhir+json',
    },
  })
}
