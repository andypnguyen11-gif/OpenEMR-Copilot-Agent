import { useParams } from 'react-router-dom'
import { SwitchPatientButton } from '../components/SwitchPatientButton'

// Placeholder dashboard shell. PRs 4-8 fill in the patient header, six
// clinical cards, and the lab-results section. PR 9 wires per-card error
// boundaries + collapse persistence + parity-matrix screenshots.
//
// This file deliberately stays thin — the real layout work lives in the
// `<PatientHeader>` and `<CardBase>` primitives that arrive in PR 4.
export function Dashboard() {
  const { id } = useParams<{ id: string }>()
  return (
    <div className="container py-4">
      <div className="d-flex justify-content-between align-items-start mb-4">
        <div>
          <h1 className="h3 mb-1">Patient dashboard</h1>
          <p className="text-muted mb-0">
            Patient ID: <code>{id}</code>
          </p>
        </div>
        <SwitchPatientButton />
      </div>
      <div className="alert alert-secondary" role="status">
        Patient header and clinical cards land in PRs 4–8.
      </div>
    </div>
  )
}
