import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { CareTeam, Practitioner } from 'fhir/r4'
import { CareTeamCard } from './CareTeamCard'
import * as useFhirClientModule from '../fhir/useFhirClient'
import type { FhirClient } from '../fhir/client'
import {
  buildCareTeam,
  buildPractitioner,
} from '../__fixtures__/fhir/careTeam'

// Wires fhir.search to return CareTeam fixtures + fhir.read to return
// Practitioners by id, while exposing both mocks for assertion.
function stubClient(
  teams: CareTeam[],
  practitionersById: Map<string, Practitioner>,
) {
  const search = vi.fn().mockResolvedValue(teams)
  const read = vi.fn(async (resourceType: string, id: string) => {
    if (resourceType !== 'Practitioner') {
      throw new Error(`unexpected read for ${resourceType}`)
    }
    const p = practitionersById.get(id)
    if (p === undefined) {
      throw new Error(`no fixture for Practitioner/${id}`)
    }
    return p
  })
  // Cast through unknown — FhirClient.read is generic <T>(...) => Promise<T>,
  // which a concrete async function can't satisfy structurally. The mock
  // methods (`.mock.calls`, etc.) stay on `read` for the assertions below.
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    search,
    read: read as unknown as FhirClient['read'],
  })
  return { search, read }
}

describe('<CareTeamCard />', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('fires exactly one Practitioner.read per unique participant UUID (4 participants → 3 reads)', async () => {
    const team = buildCareTeam({
      participants: [
        { practitionerUuid: 'pr-1', role: 'Primary' },
        { practitionerUuid: 'pr-2', role: 'Cardiologist' },
        // Duplicate of pr-1 with a different role — must not double-bill the read.
        { practitionerUuid: 'pr-1', role: 'Care Manager' },
        { practitionerUuid: 'pr-3', role: 'Pharmacist' },
      ],
    })
    const practitioners = new Map([
      ['pr-1', buildPractitioner({ id: 'pr-1', text: 'Dr. Alice Adams' })],
      ['pr-2', buildPractitioner({ id: 'pr-2', text: 'Dr. Bob Brown' })],
      ['pr-3', buildPractitioner({ id: 'pr-3', text: 'Dr. Carol Chen' })],
    ])
    const { read } = stubClient([team], practitioners)

    render(<CareTeamCard patientId="p1" />)

    await screen.findByTestId('care-team-list')
    expect(read).toHaveBeenCalledTimes(3)
    expect(read).toHaveBeenCalledWith('Practitioner', 'pr-1')
    expect(read).toHaveBeenCalledWith('Practitioner', 'pr-2')
    expect(read).toHaveBeenCalledWith('Practitioner', 'pr-3')
  })

  it('renders resolved practitioner names for every participant row', async () => {
    const team = buildCareTeam({
      participants: [
        { practitionerUuid: 'pr-1' },
        { practitionerUuid: 'pr-2' },
        { practitionerUuid: 'pr-1' },
      ],
    })
    const practitioners = new Map([
      ['pr-1', buildPractitioner({ id: 'pr-1', text: 'Dr. Alice Adams' })],
      ['pr-2', buildPractitioner({ id: 'pr-2', text: 'Dr. Bob Brown' })],
    ])
    stubClient([team], practitioners)

    render(<CareTeamCard patientId="p1" />)

    await screen.findByTestId('care-team-list')
    // Two participant rows reference pr-1 → name appears twice.
    expect(screen.getAllByText('Dr. Alice Adams')).toHaveLength(2)
    expect(screen.getByText('Dr. Bob Brown')).toBeInTheDocument()
  })

  it('falls back to given+family when Practitioner.name[0].text is absent', async () => {
    const team = buildCareTeam({
      participants: [{ practitionerUuid: 'pr-1' }],
    })
    const practitioners = new Map([
      [
        'pr-1',
        buildPractitioner({ id: 'pr-1', family: 'Adams', given: ['Alice'] }),
      ],
    ])
    stubClient([team], practitioners)

    render(<CareTeamCard patientId="p1" />)

    expect(await screen.findByText('Alice Adams')).toBeInTheDocument()
  })

  it('renders participant.role[0].text and period.start when present', async () => {
    const team = buildCareTeam({
      participants: [
        {
          practitionerUuid: 'pr-1',
          role: 'Primary care physician',
          periodStart: '2024-01-15',
        },
      ],
    })
    const practitioners = new Map([
      ['pr-1', buildPractitioner({ id: 'pr-1', text: 'Dr. Alice Adams' })],
    ])
    stubClient([team], practitioners)

    render(<CareTeamCard patientId="p1" />)

    await screen.findByTestId('care-team-list')
    expect(screen.getByTestId('ct-role')).toHaveTextContent(
      'Primary care physician',
    )
    expect(screen.getByTestId('ct-since')).toHaveTextContent('2024-01-15')
  })

  it('renders rows missing role/period without crashing', async () => {
    const team = buildCareTeam({
      participants: [
        { practitionerUuid: 'pr-1' },
        { practitionerUuid: 'pr-2', role: 'Cardiologist' },
      ],
    })
    const practitioners = new Map([
      ['pr-1', buildPractitioner({ id: 'pr-1', text: 'Dr. Alice Adams' })],
      ['pr-2', buildPractitioner({ id: 'pr-2', text: 'Dr. Bob Brown' })],
    ])
    stubClient([team], practitioners)

    render(<CareTeamCard patientId="p1" />)

    await screen.findByTestId('care-team-list')
    expect(screen.getByText('Dr. Alice Adams')).toBeInTheDocument()
    expect(screen.getByText('Dr. Bob Brown')).toBeInTheDocument()
    // Only one row carries a role — assert the bare-row case doesn't render
    // a stray "Role:" label.
    expect(screen.getAllByTestId('ct-role')).toHaveLength(1)
  })

  it('shows "Nothing Recorded" when no CareTeam resources exist', async () => {
    stubClient([], new Map())

    render(<CareTeamCard patientId="p1" />)

    expect(await screen.findByText('Nothing Recorded')).toBeInTheDocument()
  })

  it('renders an error message when the FHIR search rejects', async () => {
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      search: vi.fn().mockRejectedValue(new Error('boom')),
      read: vi.fn(),
    })

    render(<CareTeamCard patientId="p1" />)

    expect(
      await screen.findByText(/could not load care team/i),
    ).toBeInTheDocument()
  })

  it('keeps the card alive when one Practitioner.read 404s (allSettled)', async () => {
    const team = buildCareTeam({
      participants: [
        { practitionerUuid: 'pr-1', display: 'Dr. Admin (member.display)' },
        { practitionerUuid: 'pr-2' },
      ],
    })
    const search = vi.fn().mockResolvedValue([team])
    // pr-1 read rejects (mirrors OpenEMR returning 404 for the admin user
    // who doesn't qualify as a clinician); pr-2 read succeeds.
    const read = vi.fn(async (resourceType: string, id: string) => {
      if (resourceType === 'Practitioner' && id === 'pr-1') {
        throw new Error('FHIR Practitioner.read(pr-1) failed: HTTP 404')
      }
      if (resourceType === 'Practitioner' && id === 'pr-2') {
        return buildPractitioner({ id: 'pr-2', text: 'Dr. Bob Brown' })
      }
      throw new Error(`unexpected read for ${resourceType}/${id}`)
    })
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      search,
      read: read as unknown as FhirClient['read'],
    })

    render(<CareTeamCard patientId="p1" />)

    await screen.findByTestId('care-team-list')
    // pr-1 row falls back to participant.member.display
    expect(
      screen.getByText('Dr. Admin (member.display)'),
    ).toBeInTheDocument()
    // pr-2 row resolves normally
    expect(screen.getByText('Dr. Bob Brown')).toBeInTheDocument()
  })

  it('falls back to member.display for non-Practitioner participants (no read fired)', async () => {
    const team = buildCareTeam({
      participants: [
        // An Organization participant — only its display is rendered, no read.
        {
          reference: 'Organization/org-1',
          display: 'Acme Clinic',
          role: 'Facility',
        },
        { practitionerUuid: 'pr-1', role: 'Primary' },
      ],
    })
    const practitioners = new Map([
      ['pr-1', buildPractitioner({ id: 'pr-1', text: 'Dr. Alice Adams' })],
    ])
    const { read } = stubClient([team], practitioners)

    render(<CareTeamCard patientId="p1" />)

    await screen.findByTestId('care-team-list')
    expect(screen.getByText('Acme Clinic')).toBeInTheDocument()
    expect(screen.getByText('Dr. Alice Adams')).toBeInTheDocument()
    expect(read).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenCalledWith('Practitioner', 'pr-1')
  })
})
