import { useId, useState, type ReactNode } from 'react'
import { useOptionalAuth } from '../auth/AuthContext'
import { decodeIdTokenSub } from '../auth/idToken'
import {
  DEFAULT_COLLAPSED,
  getCollapsed,
  setCollapsed,
} from '../prefs/collapseStore'

// Mirrors templates/patient/card/card_base.html.twig — Bootstrap 4.6 card
// with a clickable header that toggles the body's collapsed state. Upstream
// drives the collapse via Bootstrap's data-toggle JS; we drive it from React
// state so the optional `cardId` prop can persist preferences via
// collapseStore. Tests without an AuthProvider see persistence as a no-op.
//
// Edit/Add buttons from the Twig version are deliberately omitted: this
// port is read-only (PATIENT_DASHBOARD_MIGRATION.md §0).

interface Props {
  title: string
  children: ReactNode
  initiallyCollapsed?: boolean
  // Optional — useful for tests / aria. Defaults to a stable React id.
  id?: string
  // When set, collapse state persists via collapseStore namespaced by
  // user.sub from the id_token. Falls back to "anonymous" when no id_token
  // is present (e.g., scope omitted) so the SPA still works without
  // OpenID. Omit cardId entirely to keep CardBase uncontrolled.
  cardId?: string
}

export function CardBase({
  title,
  children,
  initiallyCollapsed = DEFAULT_COLLAPSED,
  id,
  cardId,
}: Props) {
  const reactId = useId()
  const bodyId = id ?? `card-body-${reactId}`
  const auth = useOptionalAuth()
  const userKey = userKeyFromIdToken(auth?.state?.idToken)
  const persistenceEnabled = cardId !== undefined && userKey !== null

  const [collapsed, setCollapsedState] = useState(() =>
    persistenceEnabled
      ? getCollapsed(userKey, cardId)
      : initiallyCollapsed,
  )
  const expanded = !collapsed

  const handleToggle = () => {
    setCollapsedState((prev) => {
      const next = !prev
      if (persistenceEnabled) {
        setCollapsed(userKey, cardId, next)
      }
      return next
    })
  }

  return (
    <section className="card dashboard-card mb-3">
      <div className="card-body p-0">
        <h6 className="card-title mb-0 d-flex px-3 py-2 justify-content-between">
          <button
            type="button"
            className="btn btn-link text-left font-weight-bolder p-0"
            aria-expanded={expanded}
            aria-controls={bodyId}
            onClick={handleToggle}
          >
            {title}
            <Chevron expanded={expanded} />
          </button>
        </h6>
        <div
          id={bodyId}
          className={`card-text collapse${expanded ? ' show' : ''}`}
        >
          <div className="clearfix pt-2">{children}</div>
        </div>
      </div>
    </section>
  )
}

// "anonymous" when there's no id_token (login pre-OpenID or SMART without
// `openid` scope). Returning the literal string keeps collapseStore happy
// with a single key shape.
function userKeyFromIdToken(idToken: string | undefined): string {
  if (idToken === undefined) return 'anonymous'
  return decodeIdTokenSub(idToken) ?? 'anonymous'
}

// Inline SVG chevron so we don't depend on font-awesome. Rotates 180° when
// collapsed via CSS transform — keeps the icon a single DOM node so the
// chevron-flip test can target it without a re-render.
function Chevron({ expanded }: { expanded: boolean }) {
  return (
    <svg
      data-testid="card-chevron"
      data-expanded={expanded}
      className="ml-1"
      width="12"
      height="12"
      viewBox="0 0 12 12"
      aria-hidden="true"
      style={{
        transition: 'transform 120ms',
        transform: expanded ? 'rotate(0deg)' : 'rotate(-90deg)',
      }}
    >
      <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="2" fill="none" />
    </svg>
  )
}
