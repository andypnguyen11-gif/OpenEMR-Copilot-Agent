import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { MedicationRequest } from 'fhir/r4'
import { PrescriptionsCard } from './PrescriptionsCard'
import * as useFhirClientModule from '../fhir/useFhirClient'
import { buildMedicationRequest } from '../__fixtures__/fhir/medicationRequest'

function stubSearch(entries: MedicationRequest[]) {
  const search = vi.fn().mockResolvedValue(entries)
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    search,
    read: vi.fn(),
  })
  return search
}

describe('<PrescriptionsCard />', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('searches with intent=order so the server-side filter does the work', async () => {
    const search = stubSearch([])

    render(<PrescriptionsCard patientId="patient-uuid" />)

    await screen.findByText('Nothing Recorded')
    expect(search).toHaveBeenCalledWith('MedicationRequest', {
      patient: 'patient-uuid',
      intent: 'order',
    })
  })

  it('renders drug, dose, frequency, route, refills, and quantity', async () => {
    stubSearch([
      buildMedicationRequest({
        id: 'rx1',
        intent: 'order',
        display: 'Lisinopril 10 MG Oral Tablet',
        dose: { value: 1, unit: 'tablet' },
        frequencyText: 'Once daily',
        routeText: 'Oral',
        refills: 3,
        quantity: { value: 30, unit: 'tablet' },
      }),
    ])

    render(<PrescriptionsCard patientId="p1" />)

    await screen.findByTestId('prescriptions-list')
    expect(
      screen.getByText('Lisinopril 10 MG Oral Tablet'),
    ).toBeInTheDocument()
    expect(screen.getByText('1 tablet')).toBeInTheDocument()
    expect(screen.getByText('Once daily')).toBeInTheDocument()
    expect(screen.getByText('Oral')).toBeInTheDocument()
    expect(screen.getByTestId('rx-refills')).toHaveTextContent('3')
    expect(screen.getByTestId('rx-quantity')).toHaveTextContent('30 tablet')
  })

  it('does not crash when dispenseRequest is missing', async () => {
    stubSearch([
      buildMedicationRequest({
        id: 'rx1',
        intent: 'order',
        display: 'Amoxicillin 500 MG',
        omitDispense: true,
      }),
    ])

    render(<PrescriptionsCard patientId="p1" />)

    expect(await screen.findByText('Amoxicillin 500 MG')).toBeInTheDocument()
    expect(screen.queryByTestId('rx-refills')).toBeNull()
    expect(screen.queryByTestId('rx-quantity')).toBeNull()
  })

  it('shows "Nothing Recorded" when the bundle is empty', async () => {
    stubSearch([])

    render(<PrescriptionsCard patientId="p1" />)

    expect(await screen.findByText('Nothing Recorded')).toBeInTheDocument()
  })

  it('renders an error message when the FHIR search rejects', async () => {
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      search: vi.fn().mockRejectedValue(new Error('boom')),
      read: vi.fn(),
    })

    render(<PrescriptionsCard patientId="p1" />)

    expect(
      await screen.findByText(/could not load prescriptions/i),
    ).toBeInTheDocument()
  })
})
