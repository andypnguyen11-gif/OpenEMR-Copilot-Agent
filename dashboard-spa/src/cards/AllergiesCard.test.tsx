import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { AllergyIntolerance } from 'fhir/r4'
import { AllergiesCard } from './AllergiesCard'
import * as useFhirClientModule from '../fhir/useFhirClient'
import {
  buildAllergyIntolerance,
  buildNkdaRecord,
} from '../__fixtures__/fhir/allergyIntolerance'

function stubSearch(entries: AllergyIntolerance[]) {
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    search: vi.fn().mockResolvedValue(entries),
    read: vi.fn(),
  })
}

describe('<AllergiesCard />', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders only entries whose clinicalStatus.coding[0].code is "active"', async () => {
    stubSearch([
      buildAllergyIntolerance({
        id: 'a1',
        status: 'active',
        display: 'Penicillin',
      }),
      buildAllergyIntolerance({
        id: 'a2',
        status: 'inactive',
        display: 'Latex',
      }),
      buildAllergyIntolerance({
        id: 'a3',
        status: 'resolved',
        display: 'Peanut',
      }),
    ])

    render(<AllergiesCard patientId="p1" />)

    const list = await screen.findByTestId('allergies-list')
    expect(list).toHaveTextContent('Penicillin')
    expect(list).not.toHaveTextContent('Latex')
    expect(list).not.toHaveTextContent('Peanut')
  })

  it('shows "No Known Allergies" when an NKDA-coded record is present', async () => {
    stubSearch([buildNkdaRecord()])

    render(<AllergiesCard patientId="p1" />)

    expect(await screen.findByText('No Known Allergies')).toBeInTheDocument()
  })

  it('shows "Nothing Recorded" when the bundle is empty', async () => {
    stubSearch([])

    render(<AllergiesCard patientId="p1" />)

    expect(await screen.findByText('Nothing Recorded')).toBeInTheDocument()
  })

  it('renders the severity badge when criticality is "high"', async () => {
    stubSearch([
      buildAllergyIntolerance({
        id: 'a1',
        status: 'active',
        display: 'Bee venom',
        criticality: 'high',
      }),
      buildAllergyIntolerance({
        id: 'a2',
        status: 'active',
        display: 'Strawberry',
        criticality: 'low',
      }),
    ])

    render(<AllergiesCard patientId="p1" />)

    await screen.findByTestId('allergies-list')
    const badges = screen.getAllByTestId('severity-badge')
    expect(badges).toHaveLength(1)
    expect(badges[0]).toHaveTextContent('High')
  })

  it('renders the reaction manifestation text alongside the allergen', async () => {
    stubSearch([
      buildAllergyIntolerance({
        id: 'a1',
        status: 'active',
        display: 'Penicillin',
        manifestation: 'Hives',
      }),
    ])

    render(<AllergiesCard patientId="p1" />)

    expect(await screen.findByText('Penicillin')).toBeInTheDocument()
    expect(screen.getByText('Hives')).toBeInTheDocument()
  })

  it('renders an error message when the FHIR search rejects', async () => {
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      search: vi.fn().mockRejectedValue(new Error('boom')),
      read: vi.fn(),
    })

    render(<AllergiesCard patientId="p1" />)

    expect(
      await screen.findByText(/could not load allergies/i),
    ).toBeInTheDocument()
  })
})
