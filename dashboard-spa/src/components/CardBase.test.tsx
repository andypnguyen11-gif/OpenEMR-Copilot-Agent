import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CardBase } from './CardBase'

describe('<CardBase />', () => {
  it('renders the title and is expanded by default', () => {
    render(
      <CardBase title="Allergies">
        <p data-testid="content">child</p>
      </CardBase>,
    )
    const toggle = screen.getByRole('button', { name: /allergies/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('content')).toBeInTheDocument()
    expect(screen.getByTestId('card-chevron')).toHaveAttribute(
      'data-expanded',
      'true',
    )
  })

  it('toggles collapsed state when the header is clicked', async () => {
    render(
      <CardBase title="Allergies">
        <p>child</p>
      </CardBase>,
    )
    const toggle = screen.getByRole('button', { name: /allergies/i })

    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('card-chevron')).toHaveAttribute(
      'data-expanded',
      'false',
    )

    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('card-chevron')).toHaveAttribute(
      'data-expanded',
      'true',
    )
  })

  it('honors initiallyCollapsed', () => {
    render(
      <CardBase title="Allergies" initiallyCollapsed>
        <p>child</p>
      </CardBase>,
    )
    expect(
      screen.getByRole('button', { name: /allergies/i }),
    ).toHaveAttribute('aria-expanded', 'false')
  })
})
