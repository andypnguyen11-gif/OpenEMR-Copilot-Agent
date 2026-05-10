import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SwitchPatientButton } from './SwitchPatientButton'
import * as AuthContext from '../auth/AuthContext'

describe('SwitchPatientButton', () => {
  it('calls clearSession when clicked', async () => {
    const clearSession = vi.fn()
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
      state: null,
      setSession: vi.fn(),
      clearSession,
      getAccessToken: vi.fn(),
    })
    render(<SwitchPatientButton />)
    await userEvent.click(screen.getByRole('button', { name: /switch patient/i }))
    expect(clearSession).toHaveBeenCalledTimes(1)
  })
})
