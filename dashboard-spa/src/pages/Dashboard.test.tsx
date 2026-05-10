import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import * as useFhirClientModule from '../fhir/useFhirClient'
import { Dashboard } from './Dashboard'

// AllergiesCard is the only card we deliberately replace with a thrower.
// The other five render through their real components but with a stubbed
// fhir client so they sit in their loading→empty state without making
// network calls — that's enough to assert "they still mounted".
vi.mock('../cards/AllergiesCard', () => ({
  AllergiesCard: () => {
    throw new Error('Allergies card blew up')
  },
}))

// SwitchPatientButton calls the throwing useAuth — we don't mount an
// AuthProvider here, and the button isn't what this suite is testing.
vi.mock('../components/SwitchPatientButton', () => ({
  SwitchPatientButton: () => null,
}))

// Patient header reads via fhir.read; stub it to a benign Patient so the
// header component finishes mounting and doesn't throw on its own.
const PATIENT_FIXTURE = {
  resourceType: 'Patient' as const,
  name: [{ text: 'Test Patient' }],
}

beforeEach(() => {
  // Suppress the React error-boundary noise — getDerivedStateFromError
  // logs the thrown error to console.error during test render.
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.spyOn(useFhirClientModule, 'useFhirClient').mockReturnValue({
    search: vi.fn().mockResolvedValue([]),
    read: vi.fn().mockResolvedValue(PATIENT_FIXTURE),
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('<Dashboard />', () => {
  function renderRoute() {
    return render(
      <MemoryRouter initialEntries={['/patients/p1']}>
        <Routes>
          <Route path="/patients/:id" element={<Dashboard />} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('renders the boundary fallback for the failing card and keeps the other five mounted', () => {
    renderRoute()

    // The Allergies card threw — its boundary fallback shows.
    expect(screen.getByTestId('card-error-allergies')).toBeInTheDocument()

    // The other five cards mounted: each rendered its own CardBase header
    // button (the title from CardBase).
    expect(
      screen.getByRole('button', { name: /problem list/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /medications/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /prescriptions/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /care team/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /lab results/i }),
    ).toBeInTheDocument()

    // Exactly one boundary fallback — the others are healthy.
    const errors = screen.getAllByRole('alert')
    expect(errors).toHaveLength(1)
  })

  it('shows a missing-patient message when the route has no :id', () => {
    render(
      <MemoryRouter initialEntries={['/patients/']}>
        <Routes>
          <Route path="/patients" element={<Dashboard />} />
          <Route path="/patients/:id" element={<Dashboard />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(screen.getByText(/missing patient id/i)).toBeInTheDocument()
  })
})
