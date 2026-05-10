import { Navigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

// Auto-route the root URL to the patient bound by OpenEMR's SMART launch.
// See PATIENT_DASHBOARD_MIGRATION.md §0 — patient context comes from the
// SMART flow, not a SPA-side picker.
export function HomeRedirect() {
  const { state } = useAuth()
  if (!state?.patient) {
    return <Navigate to="/login" replace />
  }
  return <Navigate to={`/patients/${state.patient}`} replace />
}
