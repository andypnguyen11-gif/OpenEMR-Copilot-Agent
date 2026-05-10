import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import type { Observation } from 'fhir/r4'
import { LabResultsCard } from './LabResultsCard'
import * as useFhirClientModule from '../fhir/useFhirClient'
import { buildLabObservation } from '../__fixtures__/fhir/observation'

function stubSearch(entries: Observation[]) {
  const search = vi.fn().mockResolvedValue(entries)
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    search,
    read: vi.fn(),
  })
  return search
}

describe('<LabResultsCard />', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('searches with category=laboratory and _sort=-date', async () => {
    const search = stubSearch([])

    render(<LabResultsCard patientId="patient-uuid" />)

    await screen.findByText('Nothing Recorded')
    expect(search).toHaveBeenCalledWith('Observation', {
      patient: 'patient-uuid',
      category: 'laboratory',
      _sort: '-date',
    })
  })

  it('groups observations by LOINC code, falling back to code.text then "Unknown"', async () => {
    stubSearch([
      buildLabObservation({
        id: 'g1a',
        loinc: '2339-0',
        loincDisplay: 'Glucose [Mass/volume] in Blood',
        valueQuantity: { value: 98, unit: 'mg/dL' },
      }),
      buildLabObservation({
        id: 'g1b',
        loinc: '2339-0',
        loincDisplay: 'Glucose [Mass/volume] in Blood',
        valueQuantity: { value: 120, unit: 'mg/dL' },
        effectiveDateTime: '2024-08-01',
      }),
      buildLabObservation({
        id: 'g2',
        loinc: '718-7',
        loincDisplay: 'Hemoglobin',
        valueQuantity: { value: 14.2, unit: 'g/dL' },
      }),
      buildLabObservation({
        id: 'g3',
        noLoinc: true,
        text: 'Custom Panel',
        valueString: 'See report',
      }),
      buildLabObservation({
        id: 'g4',
        noLoinc: true,
        valueString: 'No code',
      }),
    ])

    render(<LabResultsCard patientId="p1" />)

    await screen.findByTestId('lab-results')
    const groups = screen.getAllByTestId('lab-group')
    const keys = groups.map((g) => g.getAttribute('data-group-key'))
    expect(keys).toEqual([
      'loinc:2339-0',
      'loinc:718-7',
      'text:Custom Panel',
      'unknown',
    ])
    // The Glucose group should contain both rows.
    const glucose = groups[0] as HTMLElement
    expect(within(glucose).getByText(/98 mg\/dL/)).toBeInTheDocument()
    expect(within(glucose).getByText(/120 mg\/dL/)).toBeInTheDocument()
  })

  it('sorts rows within a group newest-first by effectiveDateTime', async () => {
    stubSearch([
      buildLabObservation({
        id: 'old',
        loinc: '2339-0',
        loincDisplay: 'Glucose',
        valueQuantity: { value: 98, unit: 'mg/dL' },
        effectiveDateTime: '2024-01-15',
      }),
      buildLabObservation({
        id: 'new',
        loinc: '2339-0',
        loincDisplay: 'Glucose',
        valueQuantity: { value: 140, unit: 'mg/dL' },
        effectiveDateTime: '2024-09-01',
      }),
      buildLabObservation({
        id: 'mid',
        loinc: '2339-0',
        loincDisplay: 'Glucose',
        valueQuantity: { value: 110, unit: 'mg/dL' },
        effectiveDateTime: '2024-05-15',
      }),
    ])

    render(<LabResultsCard patientId="p1" />)

    await screen.findByTestId('lab-results')
    const group = screen.getByTestId('lab-group')
    const rows = within(group).getAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('140')
    expect(rows[1]).toHaveTextContent('110')
    expect(rows[2]).toHaveTextContent('98')
  })

  it.each([
    ['H', 'warning', 'High'],
    ['L', 'warning', 'Low'],
    ['HH', 'danger', 'Critical High'],
    ['LL', 'danger', 'Critical Low'],
    ['A', 'info', 'Abnormal'],
  ] as const)(
    'renders %s interpretation with the %s badge',
    async (code, variant, label) => {
      stubSearch([
        buildLabObservation({
          id: 'b1',
          interpretation: code,
          valueQuantity: { value: 250, unit: 'mg/dL' },
        }),
      ])

      render(<LabResultsCard patientId="p1" />)

      const badge = await screen.findByTestId('lab-badge')
      expect(badge).toHaveAttribute('data-interpretation', code)
      expect(badge).toHaveTextContent(label)
      expect(badge.className).toContain(`badge-${variant}`)
    },
  )

  it('renders no badge for "N" (normal) interpretations', async () => {
    stubSearch([
      buildLabObservation({
        id: 'n1',
        interpretation: 'N',
        valueQuantity: { value: 95, unit: 'mg/dL' },
      }),
    ])

    render(<LabResultsCard patientId="p1" />)

    await screen.findByTestId('lab-results')
    expect(screen.queryByTestId('lab-badge')).toBeNull()
  })

  it('renders valueString rows alongside valueQuantity rows', async () => {
    stubSearch([
      buildLabObservation({
        id: 'q1',
        loinc: '2339-0',
        loincDisplay: 'Glucose',
        valueQuantity: { value: 120, unit: 'mg/dL' },
      }),
      buildLabObservation({
        id: 's1',
        loinc: '11502-2',
        loincDisplay: 'Laboratory report',
        valueString: 'Negative for occult blood',
      }),
    ])

    render(<LabResultsCard patientId="p1" />)

    await screen.findByTestId('lab-results')
    expect(screen.getByText('120 mg/dL')).toBeInTheDocument()
    expect(
      screen.getByText('Negative for occult blood'),
    ).toBeInTheDocument()
  })

  it('renders the reference range when present', async () => {
    stubSearch([
      buildLabObservation({
        id: 'r1',
        valueQuantity: { value: 14.2, unit: 'g/dL' },
        referenceRange: {
          low: { value: 13.5, unit: 'g/dL' },
          high: { value: 17.5, unit: 'g/dL' },
        },
      }),
    ])

    render(<LabResultsCard patientId="p1" />)

    await screen.findByTestId('lab-results')
    expect(screen.getByText(/13.5–17.5 g\/dL/)).toBeInTheDocument()
  })

  it('shows "Nothing Recorded" when the bundle is empty', async () => {
    stubSearch([])

    render(<LabResultsCard patientId="p1" />)

    expect(await screen.findByText('Nothing Recorded')).toBeInTheDocument()
  })

  it('renders an error message when the FHIR search rejects', async () => {
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      search: vi.fn().mockRejectedValue(new Error('boom')),
      read: vi.fn(),
    })

    render(<LabResultsCard patientId="p1" />)

    expect(
      await screen.findByText(/could not load lab results/i),
    ).toBeInTheDocument()
  })
})
