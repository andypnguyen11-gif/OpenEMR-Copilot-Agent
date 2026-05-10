import { useParams } from 'react-router-dom'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { PatientHeader } from '../components/PatientHeader'
import { SwitchPatientButton } from '../components/SwitchPatientButton'

// Six placeholder cards seed the layout PRs 5–8 fill in (one card per PR
// section). They render here so the parity-matrix screenshots in PR 9 can
// capture the full grid even before any card has real data.
const CARD_TITLES = [
  'Allergies',
  'Problem List',
  'Medications',
  'Prescriptions',
  'Care Team',
  'Lab Results',
] as const

export function Dashboard() {
  const { id } = useParams<{ id: string }>()
  if (!id) {
    // Reached only via a misconfigured route; useParams returns undefined.
    // Surface a clear message rather than silently rendering an empty header.
    return (
      <div className="container py-4">
        <p className="text-danger">Missing patient ID in route.</p>
      </div>
    )
  }
  return (
    <div className="container py-4">
      <div className="d-flex justify-content-end mb-2">
        <SwitchPatientButton />
      </div>
      <PatientHeader id={id} />
      {CARD_TITLES.map((title) => (
        <CardBase key={title} title={title}>
          <EmptyState variant="nothing-recorded" />
        </CardBase>
      ))}
    </div>
  )
}
