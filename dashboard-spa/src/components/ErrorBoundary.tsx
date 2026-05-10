import { Component, type ErrorInfo, type ReactNode } from 'react'

// Per-card error boundary. PR 9 wraps every clinical card in one of these
// so a single render exception (bad FHIR shape, unexpected null, etc.)
// only blanks the failing card — the other five keep mounting.
//
// React's error boundary contract requires a class component; everything
// else in this codebase is hooks. Keep this file tiny and don't reuse it
// for non-card error fallbacks.

interface Props {
  children: ReactNode
  cardTitle?: string
  fallback?: ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface to the dev console — in prod we'd ship to a logging endpoint,
    // but the SPA's no-BFF stance means there is no server to ship to. The
    // migration doc §3 records this tradeoff.
    console.error(
      `Card "${this.props.cardTitle ?? 'unknown'}" crashed:`,
      error,
      info.componentStack,
    )
  }

  render(): ReactNode {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback
      return (
        <div className="alert alert-warning mb-0" role="alert">
          This section couldn’t load.
        </div>
      )
    }
    return this.props.children
  }
}
