import { useAuth } from '../auth/AuthContext'

// Clears the in-memory session. RequireAuth then redirects to /login,
// which kicks off a fresh OAuth round-trip — OpenEMR's SMART picker is the
// patient picker. See PATIENT_DASHBOARD_MIGRATION.md §0/§5/§7.
export function SwitchPatientButton() {
  const { clearSession } = useAuth()
  return (
    <button
      type="button"
      className="btn btn-outline-secondary btn-sm"
      onClick={clearSession}
      title="Sign out and pick a different patient"
    >
      Switch patient
    </button>
  )
}
