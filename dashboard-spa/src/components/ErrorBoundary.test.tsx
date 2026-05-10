import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ErrorBoundary } from './ErrorBoundary'

function Thrower(): never {
  throw new Error('boom')
}

describe('<ErrorBoundary />', () => {
  // React logs caught errors to the console; silence so the test output
  // stays clean while still asserting on the render fallback.
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    consoleErrorSpy.mockRestore()
  })

  it('renders children when no error is thrown', () => {
    render(
      <ErrorBoundary cardTitle="Allergies">
        <p>healthy child</p>
      </ErrorBoundary>,
    )
    expect(screen.getByText('healthy child')).toBeInTheDocument()
  })

  it('renders the default fallback when a child throws', () => {
    render(
      <ErrorBoundary cardTitle="Allergies">
        <Thrower />
      </ErrorBoundary>,
    )
    expect(
      screen.getByText(/this section couldn’t load/i),
    ).toBeInTheDocument()
  })

  it('renders a custom fallback when one is supplied', () => {
    render(
      <ErrorBoundary
        cardTitle="Allergies"
        fallback={<p>custom fallback</p>}
      >
        <Thrower />
      </ErrorBoundary>,
    )
    expect(screen.getByText('custom fallback')).toBeInTheDocument()
  })

  it('does not unmount siblings rendered outside the boundary', () => {
    render(
      <div>
        <ErrorBoundary cardTitle="Allergies">
          <Thrower />
        </ErrorBoundary>
        <p>sibling outside</p>
      </div>,
    )
    expect(screen.getByText('sibling outside')).toBeInTheDocument()
    expect(
      screen.getByText(/this section couldn’t load/i),
    ).toBeInTheDocument()
  })
})
