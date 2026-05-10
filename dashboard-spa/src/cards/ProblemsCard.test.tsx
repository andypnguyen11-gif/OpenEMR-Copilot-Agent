import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Condition } from 'fhir/r4'
import { ProblemsCard } from './ProblemsCard'
import * as useFhirClientModule from '../fhir/useFhirClient'
import { buildCondition } from '../__fixtures__/fhir/condition'

function stubSearch(entries: Condition[]) {
  const search = vi.fn().mockResolvedValue(entries)
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    search,
    read: vi.fn(),
  })
  return search
}

describe('<ProblemsCard />', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders code.text for each Condition in the bundle', async () => {
    stubSearch([
      buildCondition({ id: 'c1', display: 'Hypertension' }),
      buildCondition({ id: 'c2', display: 'Type 2 diabetes' }),
      buildCondition({ id: 'c3', display: 'Asthma' }),
    ])

    render(<ProblemsCard patientId="p1" />)

    const list = await screen.findByTestId('problems-list')
    expect(list).toHaveTextContent('Hypertension')
    expect(list).toHaveTextContent('Type 2 diabetes')
    expect(list).toHaveTextContent('Asthma')
  })

  it('searches with category=problem-list-item', async () => {
    const search = stubSearch([])

    render(<ProblemsCard patientId="patient-uuid" />)

    await screen.findByText('Nothing Recorded')
    expect(search).toHaveBeenCalledWith('Condition', {
      patient: 'patient-uuid',
      category: 'problem-list-item',
    })
  })

  it('shows "Nothing Recorded" when the bundle is empty', async () => {
    stubSearch([])

    render(<ProblemsCard patientId="p1" />)

    expect(await screen.findByText('Nothing Recorded')).toBeInTheDocument()
  })

  it('renders an error message when the FHIR search rejects', async () => {
    vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
      search: vi.fn().mockRejectedValue(new Error('boom')),
      read: vi.fn(),
    })

    render(<ProblemsCard patientId="p1" />)

    expect(
      await screen.findByText(/could not load problems/i),
    ).toBeInTheDocument()
  })
})
