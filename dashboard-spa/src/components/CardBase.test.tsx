import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect as useReactEffect, type ReactNode } from 'react'
import { CardBase } from './CardBase'
import { AuthProvider, useAuth } from '../auth/AuthContext'
import type { AuthState, DiscoveryConfig } from '../auth/oauth'
import { setCollapsed } from '../prefs/collapseStore'

// Hand-rolled JWT whose payload encodes `sub: "user-42"`. Matches the
// minimal-payload format decodeIdTokenSub already accepts in idToken.test.
function makeIdToken(sub: string): string {
  const body = btoa(JSON.stringify({ sub }))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return `header.${body}.sig`
}

const STUB_CONFIG: DiscoveryConfig = {
  authorization_endpoint: 'https://example/authorize',
  token_endpoint: 'https://example/token',
  end_session_endpoint: 'https://example/logout',
}

// Mounts AuthProvider with the given AuthState already populated. Children
// render only after setSession has fired, so CardBase reads the populated
// id_token on its first useState init.
function ProviderWith({
  state,
  children,
}: {
  state: AuthState
  children: ReactNode
}) {
  return (
    <AuthProvider config={STUB_CONFIG} clientId="spa">
      <Hydrate state={state}>{children}</Hydrate>
    </AuthProvider>
  )
}

function Hydrate({
  state,
  children,
}: {
  state: AuthState
  children: ReactNode
}) {
  const { state: current, setSession } = useAuth()
  useReactEffect(() => {
    setSession(state)
  }, [state, setSession])
  if (current === null) return null
  return <>{children}</>
}

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

describe('<CardBase /> persistence', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  function buildState(idToken: string): AuthState {
    return {
      accessToken: 'access',
      refreshToken: 'refresh',
      expiresAt: Date.now() + 60_000,
      idToken,
    }
  }

  it('hydrates collapsed=true from collapseStore for the current user', () => {
    setCollapsed('user-42', 'allergies', true)

    render(
      <ProviderWith state={buildState(makeIdToken('user-42'))}>
        <CardBase title="Allergies" cardId="allergies">
          <p>child</p>
        </CardBase>
      </ProviderWith>,
    )

    expect(
      screen.getByRole('button', { name: /allergies/i }),
    ).toHaveAttribute('aria-expanded', 'false')
  })

  it('writes the new state to collapseStore on toggle', async () => {
    render(
      <ProviderWith state={buildState(makeIdToken('user-42'))}>
        <CardBase title="Allergies" cardId="allergies">
          <p>child</p>
        </CardBase>
      </ProviderWith>,
    )

    await userEvent.click(
      screen.getByRole('button', { name: /allergies/i }),
    )

    expect(
      localStorage.getItem('dashboard-spa:collapse:user-42:allergies'),
    ).toBe('1')
  })

  it("does not pick up another user's stored preference", () => {
    setCollapsed('user-other', 'allergies', true)

    render(
      <ProviderWith state={buildState(makeIdToken('user-42'))}>
        <CardBase title="Allergies" cardId="allergies">
          <p>child</p>
        </CardBase>
      </ProviderWith>,
    )

    expect(
      screen.getByRole('button', { name: /allergies/i }),
    ).toHaveAttribute('aria-expanded', 'true')
  })

  it('falls back to userKey="anonymous" when id_token is absent', () => {
    setCollapsed('anonymous', 'allergies', true)

    render(
      <ProviderWith
        state={{
          accessToken: 'access',
          refreshToken: 'refresh',
          expiresAt: Date.now() + 60_000,
          // no idToken — happens when openid scope wasn't granted
        }}
      >
        <CardBase title="Allergies" cardId="allergies">
          <p>child</p>
        </CardBase>
      </ProviderWith>,
    )

    expect(
      screen.getByRole('button', { name: /allergies/i }),
    ).toHaveAttribute('aria-expanded', 'false')
  })
})
