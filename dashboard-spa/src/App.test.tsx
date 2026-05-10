import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import App from './App'

describe('App', () => {
  it('renders the dashboard heading', () => {
    render(<App />)
    expect(
      screen.getByRole('heading', { name: /OpenEMR Dashboard SPA/i, level: 1 }),
    ).toBeInTheDocument()
  })
})
