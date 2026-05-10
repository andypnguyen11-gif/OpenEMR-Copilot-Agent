import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { MedicationRequest } from 'fhir/r4'
import { MedicationsCard } from './MedicationsCard'
import * as useFhirClientModule from '../fhir/useFhirClient'
import { buildMedicationRequest } from '../__fixtures__/fhir/medicationRequest'

function stubSearch(entries: MedicationRequest[]) {
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    search: vi.fn().mockResolvedValue(entries),
    read: vi.fn(),
  })
}

describe('<MedicationsCard />', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders only entries whose intent is "plan" or "proposal"', async () => {
    stubSearch([
      buildMedicationRequest({
        id: 'm1',
        intent: 'plan',
        display: 'Lisinopril 10 MG',
      }),
      buildMedicationRequest({
        id: 'm2',
        intent: 'proposal',
        display: 'Metformin 500 MG',
      }),
      buildMedicationRequest({
        id: 'm3',
        intent: 'order',
        display: 'Amoxicillin 250 MG',
      }),
      buildMedicationRequest({
        id: 'm4',
        intent: 'original-order',
        display: 'Albuterol 90 MCG',
      }),
    ])

    render(<MedicationsCard patientId="p1" />)

    const list = await screen.findByTestId('medications-list')
    expect(list).toHaveTextContent('Lisinopril 10 MG')
    expect(list).toHaveTextContent('Metformin 500 MG')
    expect(list).not.toHaveTextContent('Amoxicillin 250 MG')
    expect(list).not.toHaveTextContent('Albuterol 90 MCG')
  })

  it('displays dosageInstruction[0].text alongside the drug name', async () => {
    stubSearch([
      buildMedicationRequest({
        id: 'm1',
        intent: 'plan',
        display: 'Lisinopril 10 MG',
        dosageText: 'Take 1 tablet by mouth daily',
      }),
    ])

    render(<MedicationsCard patientId="p1" />)

    await screen.findByTestId('medications-list')
    expect(
      screen.getByText('Take 1 tablet by mouth daily'),
    ).toBeInTheDocument()
  })

  it('falls back to medicationCodeableConcept.text when medicationReference is absent', async () => {
    stubSearch([
      buildMedicationRequest({
        id: 'm1',
        intent: 'plan',
        display: 'Atorvastatin 20 MG',
        useReference: false,
      }),
    ])

    render(<MedicationsCard patientId="p1" />)

    expect(await screen.findByText('Atorvastatin 20 MG')).toBeInTheDocument()
  })

  it('uses medicationReference.display when present (forward-compat)', async () => {
    stubSearch([
      buildMedicationRequest({
        id: 'm1',
        intent: 'plan',
        display: 'Future Med',
        useReference: true,
      }),
    ])

    render(<MedicationsCard patientId="p1" />)

    expect(await screen.findByText('Future Med')).toBeInTheDocument()
  })

  it('shows "Nothing Recorded" when no entries match the intent filter', async () => {
    stubSearch([
      buildMedicationRequest({ id: 'm1', intent: 'order' }),
      buildMedicationRequest({ id: 'm2', intent: 'instance-order' }),
    ])

    render(<MedicationsCard patientId="p1" />)

    expect(await screen.findByText('Nothing Recorded')).toBeInTheDocument()
  })

  it('shows "Nothing Recorded" when the bundle is empty', async () => {
    stubSearch([])

    render(<MedicationsCard patientId="p1" />)

    expect(await screen.findByText('Nothing Recorded')).toBeInTheDocument()
  })

  it('renders an error message when the FHIR search rejects', async () => {
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      search: vi.fn().mockRejectedValue(new Error('boom')),
      read: vi.fn(),
    })

    render(<MedicationsCard patientId="p1" />)

    expect(
      await screen.findByText(/could not load medications/i),
    ).toBeInTheDocument()
  })
})
