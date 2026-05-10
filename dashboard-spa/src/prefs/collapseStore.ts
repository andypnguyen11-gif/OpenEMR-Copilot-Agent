// localStorage-backed wrapper for the dashboard's collapse preferences.
// Pure functions on top of `globalThis.localStorage` — every call is wrapped
// in try/catch because the API throws under SecurityError (private mode in
// some browsers) and QuotaExceededError, neither of which should ever blank
// the dashboard.
//
// Keys are namespaced as `${PREFIX}:${userKey}:${cardId}` so two clinicians
// sharing a browser session keep their own collapse preferences.
// PATIENT_DASHBOARD_MIGRATION.md §7 documents this as the intentional
// per-browser-via-localStorage trade-off vs upstream's per-user-via-AJAX.

const PREFIX = 'dashboard-spa:collapse'
const COLLAPSED = '1'
const EXPANDED = '0'

export const DEFAULT_COLLAPSED = false

function buildKey(userKey: string, cardId: string): string {
  return `${PREFIX}:${userKey}:${cardId}`
}

export function getCollapsed(userKey: string, cardId: string): boolean {
  try {
    const raw = globalThis.localStorage?.getItem(buildKey(userKey, cardId))
    if (raw === COLLAPSED) return true
    if (raw === EXPANDED) return false
    // null (missing) or any other value (corrupt) → documented default.
    return DEFAULT_COLLAPSED
  } catch {
    return DEFAULT_COLLAPSED
  }
}

export function setCollapsed(
  userKey: string,
  cardId: string,
  collapsed: boolean,
): void {
  try {
    globalThis.localStorage?.setItem(
      buildKey(userKey, cardId),
      collapsed ? COLLAPSED : EXPANDED,
    )
  } catch {
    // Best-effort persistence — quota errors and SecurityErrors must not
    // break the toggle. The next render reads the in-memory state anyway.
  }
}
