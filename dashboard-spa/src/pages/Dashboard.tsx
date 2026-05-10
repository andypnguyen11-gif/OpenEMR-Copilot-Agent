import { useParams } from 'react-router-dom'
import { AllergiesCard } from '../cards/AllergiesCard'
import { CareTeamCard } from '../cards/CareTeamCard'
import { MedicationsCard } from '../cards/MedicationsCard'
import { PrescriptionsCard } from '../cards/PrescriptionsCard'
import { ProblemsCard } from '../cards/ProblemsCard'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { PatientHeader } from '../components/PatientHeader'
import { SwitchPatientButton } from '../components/SwitchPatientButton'

// PR 8 swaps the last placeholder (Lab Results) for a real card.
const PLACEHOLDER_TITLES = ['Lab Results'] as const

export function Dashboard() {
  const { id } = useParams<{ id: string }>()
  if (!id) {
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
      <AllergiesCard patientId={id} />
      <ProblemsCard patientId={id} />
      <MedicationsCard patientId={id} />
      <PrescriptionsCard patientId={id} />
      <CareTeamCard patientId={id} />
      {PLACEHOLDER_TITLES.map((title) => (
        <CardBase key={title} title={title}>
          <EmptyState variant="nothing-recorded" />
        </CardBase>
      ))}
    </div>
  )
}
