import { useId, useState, type ReactNode } from 'react'

// Mirrors templates/patient/card/card_base.html.twig — Bootstrap 4.6 card
// with a clickable header that toggles the body's collapsed state. Upstream
// drives the collapse via Bootstrap's data-toggle JS; we drive it from React
// state so PR 9 can layer localStorage persistence on top via collapseStore.
//
// Edit/Add buttons from the Twig version are deliberately omitted: this
// port is read-only (PATIENT_DASHBOARD_MIGRATION.md §0).

interface Props {
  title: string
  children: ReactNode
  initiallyCollapsed?: boolean
  // Optional — useful for tests / aria. Defaults to a stable React id.
  id?: string
}

export function CardBase({
  title,
  children,
  initiallyCollapsed = false,
  id,
}: Props) {
  const reactId = useId()
  const bodyId = id ?? `card-body-${reactId}`
  const [collapsed, setCollapsed] = useState(initiallyCollapsed)
  const expanded = !collapsed

  return (
    <section className="card mb-3">
      <div className="card-body p-1">
        <h6 className="card-title mb-0 d-flex p-1 justify-content-between">
          <button
            type="button"
            className="btn btn-link text-left font-weight-bolder p-0"
            aria-expanded={expanded}
            aria-controls={bodyId}
            onClick={() => setCollapsed((c) => !c)}
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
