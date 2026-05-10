import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createFhirClient, type FhirClient } from './client'

const FHIR_BASE = 'https://localhost:9300/apis/default/fhir'

type FetchCall = [url: string, init?: RequestInit]

function buildBundle<T extends { resourceType: string }>(entries: T[]) {
  return {
    resourceType: 'Bundle' as const,
    type: 'collection' as const,
    entry: entries.map((resource) => ({ resource })),
  }
}

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('fhirSearch', () => {
  it('attaches a Bearer token from getAccessToken', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(buildBundle([])))
    vi.stubGlobal('fetch', fetchMock)
    const getAccessToken = vi.fn(async (_force?: boolean) => 'access-1')
    const client: FhirClient = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken,
    })
    await client.search('AllergyIntolerance', { patient: 'p-1' })
    const call = fetchMock.mock.calls[0] as FetchCall | undefined
    const headers = new Headers(call?.[1]?.headers)
    expect(headers.get('Authorization')).toBe('Bearer access-1')
    expect(headers.get('Accept')).toBe('application/fhir+json')
  })

  it('flattens a 200 Bundle into an array of resources', async () => {
    const allergies = [
      { resourceType: 'AllergyIntolerance', id: 'a1' },
      { resourceType: 'AllergyIntolerance', id: 'a2' },
    ]
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(buildBundle(allergies))),
    )
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access-1',
    })
    const result = await client.search<{ resourceType: string; id: string }>(
      'AllergyIntolerance',
      { patient: 'p-1' },
    )
    expect(result).toHaveLength(2)
    expect(result[0]?.id).toBe('a1')
    expect(result[1]?.id).toBe('a2')
  })

  it('treats missing entry as an empty array', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          jsonResponse({ resourceType: 'Bundle', type: 'collection' }),
      ),
    )
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access-1',
    })
    const result = await client.search('Condition', { patient: 'p-1' })
    expect(result).toEqual([])
  })

  it('applies _count=200 when the caller omits it', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(buildBundle([])))
    vi.stubGlobal('fetch', fetchMock)
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access-1',
    })
    await client.search('Observation', { patient: 'p-1' })
    const call = fetchMock.mock.calls[0] as FetchCall | undefined
    expect(new URL(call?.[0] ?? '').searchParams.get('_count')).toBe('200')
  })

  it('builds search URLs against a relative baseUrl (Vite-proxy dev mode)', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(buildBundle([])))
    vi.stubGlobal('fetch', fetchMock)
    const client = createFhirClient({
      baseUrl: '/apis/default/fhir',
      getAccessToken: async () => 'access',
    })
    await client.search('AllergyIntolerance', { patient: 'p-1' })
    const call = fetchMock.mock.calls[0] as FetchCall | undefined
    expect(call?.[0]).toBe(
      '/apis/default/fhir/AllergyIntolerance?patient=p-1&_count=200',
    )
  })

  it('honors a caller-provided _count', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(buildBundle([])))
    vi.stubGlobal('fetch', fetchMock)
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access-1',
    })
    await client.search('Observation', { patient: 'p-1', _count: '50' })
    const call = fetchMock.mock.calls[0] as FetchCall | undefined
    expect(new URL(call?.[0] ?? '').searchParams.get('_count')).toBe('50')
  })

  it('on 401, calls getAccessToken with force=true and retries once with the new token', async () => {
    let call = 0
    const fetchMock = vi.fn(async () => {
      call += 1
      if (call === 1) return new Response('', { status: 401 })
      return jsonResponse(buildBundle([{ resourceType: 'Patient', id: 'p-1' }]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const getAccessToken = vi.fn(async (force?: boolean) =>
      force ? 'access-rotated' : 'access-stale',
    )
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken,
    })
    const result = await client.search('Patient', {})
    expect(result).toHaveLength(1)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(getAccessToken).toHaveBeenNthCalledWith(1, false)
    expect(getAccessToken).toHaveBeenNthCalledWith(2, true)
    const retry = fetchMock.mock.calls[1] as FetchCall | undefined
    expect(new Headers(retry?.[1]?.headers).get('Authorization')).toBe(
      'Bearer access-rotated',
    )
  })

  it('on 401-after-retry, throws — no infinite loop', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: vi.fn(async () => 'access'),
    })
    await expect(client.search('Patient', {})).rejects.toThrow(/401/)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('on 403, returns [] and does not throw (out-of-scope ≠ error)', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 403 }))
    vi.stubGlobal('fetch', fetchMock)
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access',
    })
    const result = await client.search('CareTeam', { patient: 'p-1' })
    expect(result).toEqual([])
  })

  it('on other 5xx, throws with status code', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('Internal Error', { status: 500 })),
    )
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access',
    })
    await expect(client.search('Patient', {})).rejects.toThrow(/500/)
  })
})

describe('fhirRead', () => {
  it('builds the URL as ${baseUrl}/{resourceType}/{id}', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ resourceType: 'Patient', id: 'p-1' }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access',
    })
    await client.read('Patient', 'p-1')
    const call = fetchMock.mock.calls[0] as FetchCall | undefined
    expect(call?.[0]).toBe(`${FHIR_BASE}/Patient/p-1`)
  })

  it('returns the resource directly (not wrapped in a Bundle)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ resourceType: 'Patient', id: 'p-1', active: true }),
      ),
    )
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access',
    })
    const result = await client.read<{
      resourceType: 'Patient'
      id: string
      active: boolean
    }>('Patient', 'p-1')
    expect(result.id).toBe('p-1')
    expect(result.active).toBe(true)
  })

  it('on 401, retries once with a forced-refresh token', async () => {
    let call = 0
    const fetchMock = vi.fn(async () => {
      call += 1
      if (call === 1) return new Response('', { status: 401 })
      return jsonResponse({ resourceType: 'Patient', id: 'p-1' })
    })
    vi.stubGlobal('fetch', fetchMock)
    const getAccessToken = vi.fn(async (force?: boolean) =>
      force ? 'rotated' : 'stale',
    )
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken,
    })
    const result = await client.read<{ resourceType: 'Patient'; id: string }>(
      'Patient',
      'p-1',
    )
    expect(result.id).toBe('p-1')
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(getAccessToken).toHaveBeenNthCalledWith(2, true)
  })

  it('on 404, throws — read missing resources should not be silently empty', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('', { status: 404 })),
    )
    const client = createFhirClient({
      baseUrl: FHIR_BASE,
      getAccessToken: async () => 'access',
    })
    await expect(client.read('Patient', 'missing-id')).rejects.toThrow(/404/)
  })
})
