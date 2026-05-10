import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { HomeRedirect } from './HomeRedirect'
import * as AuthContext from '../auth/AuthContext'

function renderAtRoot() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<HomeRedirect />} />
        <Route path="/login" element={<div>login screen</div>} />
        <Route path="/patients/:id" element={<div>dashboard for {':id'}</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('HomeRedirect', () => {
  it('redirects to /patients/{id} when state.patient is set', () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
      state: {
        accessToken: 'access',
        refreshToken: 'refresh',
        expiresAt: Date.now() + 60_000,
        patient: 'patient-uuid-123',
      },
      setSession: vi.fn(),
      clearSession: vi.fn(),
      getAccessToken: vi.fn(),
    })
    renderAtRoot()
    expect(screen.getByText(/dashboard for/i)).toBeInTheDocument()
  })

  it('redirects to /login when state.patient is missing', () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
      state: {
        accessToken: 'access',
        refreshToken: 'refresh',
        expiresAt: Date.now() + 60_000,
      },
      setSession: vi.fn(),
      clearSession: vi.fn(),
      getAccessToken: vi.fn(),
    })
    renderAtRoot()
    expect(screen.getByText(/login screen/i)).toBeInTheDocument()
  })

  it('redirects to /login when there is no session', () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
      state: null,
      setSession: vi.fn(),
      clearSession: vi.fn(),
      getAccessToken: vi.fn(),
    })
    renderAtRoot()
    expect(screen.getByText(/login screen/i)).toBeInTheDocument()
  })
})
