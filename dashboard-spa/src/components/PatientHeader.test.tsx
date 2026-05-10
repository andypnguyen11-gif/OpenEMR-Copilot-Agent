import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Patient } from 'fhir/r4'
import { PatientHeader, displayName, mrn } from './PatientHeader'
import * as useFhirClientModule from '../fhir/useFhirClient'

function stubFhirClient(patient: Patient) {
  const read = vi.fn().mockResolvedValue(patient)
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    read,
    search: vi.fn(),
  })
  return read
}

describe('PatientHeader extractors', () => {
  describe('displayName', () => {
    it('prefers name[0].text', () => {
      expect(
        displayName({
          resourceType: 'Patient',
          name: [{ text: 'Jane Doe', family: 'Doe', given: ['Jane'] }],
        } as Patient),
      ).toBe('Jane Doe')
    })

    it('falls back to given + family when text is absent', () => {
      expect(
        displayName({
          resourceType: 'Patient',
          name: [{ family: 'Doe', given: ['Jane', 'M'] }],
        } as Patient),
      ).toBe('Jane M Doe')
    })

    it('returns "Unknown patient" when name is missing entirely', () => {
      expect(
        displayName({ resourceType: 'Patient' } as Patient),
      ).toBe('Unknown patient')
    })
  })

  describe('mrn', () => {
    it('returns the value of the identifier whose type.coding.code === "PT"', () => {
      const p = {
        resourceType: 'Patient',
        identifier: [
          { value: '999-99-9999', type: { coding: [{ code: 'SS' }] } },
          { value: 'MRN-42', type: { coding: [{ code: 'PT' }] } },
        ],
      } as Patient
      expect(mrn(p)).toBe('MRN-42')
    })

    it('returns "—" when no PT identifier exists', () => {
      const p = {
        resourceType: 'Patient',
        identifier: [
          { value: 'XYZ', type: { coding: [{ code: 'DL' }] } },
        ],
      } as Patient
      expect(mrn(p)).toBe('—')
    })

    it('returns "—" when identifier array is missing', () => {
      expect(mrn({ resourceType: 'Patient' } as Patient)).toBe('—')
    })
  })
})

describe('<PatientHeader />', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders MRN, DOB, sex, and status from a Patient with PT identifier', async () => {
    stubFhirClient({
      resourceType: 'Patient',
      name: [{ text: 'Jane Doe' }],
      birthDate: '1980-05-12',
      gender: 'female',
      active: true,
      identifier: [{ value: 'MRN-42', type: { coding: [{ code: 'PT' }] } }],
    } as Patient)

    render(<PatientHeader id="patient-uuid" />)

    expect(await screen.findByTestId('patient-name')).toHaveTextContent('Jane Doe')
    expect(screen.getByTestId('patient-mrn')).toHaveTextContent('MRN-42')
    expect(screen.getByText('1980-05-12')).toBeInTheDocument()
    expect(screen.getByText('female')).toBeInTheDocument()
    expect(screen.getByText('Active')).toBeInTheDocument()
  })

  it('renders MRN as "—" when no PT identifier is present', async () => {
    stubFhirClient({
      resourceType: 'Patient',
      name: [{ text: 'Jane Doe' }],
      birthDate: '1980-05-12',
      gender: 'female',
      identifier: [{ value: 'XYZ', type: { coding: [{ code: 'DL' }] } }],
    } as Patient)

    render(<PatientHeader id="patient-uuid" />)

    await screen.findByTestId('patient-name')
    expect(screen.getByTestId('patient-mrn')).toHaveTextContent('—')
  })

  it('falls back to a graceful name when name[0].text is missing', async () => {
    stubFhirClient({
      resourceType: 'Patient',
      name: [{ family: 'Smith', given: ['John'] }],
    } as Patient)

    render(<PatientHeader id="patient-uuid" />)

    expect(await screen.findByTestId('patient-name')).toHaveTextContent('John Smith')
  })

  it('shows "Inactive" when patient.active is explicitly false', async () => {
    stubFhirClient({
      resourceType: 'Patient',
      name: [{ text: 'Jane Doe' }],
      active: false,
    } as Patient)

    render(<PatientHeader id="patient-uuid" />)

    await screen.findByTestId('patient-name')
    expect(screen.getByText('Inactive')).toBeInTheDocument()
  })

  it('renders an error message when Patient.read rejects', async () => {
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      read: vi.fn().mockRejectedValue(new Error('boom')),
      search: vi.fn(),
    })

    render(<PatientHeader id="patient-uuid" />)

    expect(
      await screen.findByText(/could not load patient details/i),
    ).toBeInTheDocument()
  })
})
