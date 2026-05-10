import { useParams } from 'react-router-dom'
import type { ReactNode } from 'react'
import { AllergiesCard } from '../cards/AllergiesCard'
import { CareTeamCard } from '../cards/CareTeamCard'
import { LabResultsCard } from '../cards/LabResultsCard'
import { MedicationsCard } from '../cards/MedicationsCard'
import { PrescriptionsCard } from '../cards/PrescriptionsCard'
import { ProblemsCard } from '../cards/ProblemsCard'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { PatientHeader } from '../components/PatientHeader'
import { SwitchPatientButton } from '../components/SwitchPatientButton'

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
      <CardSlot title="Allergies">
        <AllergiesCard patientId={id} />
      </CardSlot>
      <CardSlot title="Problem List">
        <ProblemsCard patientId={id} />
      </CardSlot>
      <CardSlot title="Medications">
        <MedicationsCard patientId={id} />
      </CardSlot>
      <CardSlot title="Prescriptions">
        <PrescriptionsCard patientId={id} />
      </CardSlot>
      <CardSlot title="Care Team">
        <CareTeamCard patientId={id} />
      </CardSlot>
      <CardSlot title="Lab Results">
        <LabResultsCard patientId={id} />
      </CardSlot>
    </div>
  )
}

// One ErrorBoundary per card so a single render exception only blanks the
// failing card. React's contract requires a class boundary, so we use the
// existing ErrorBoundary primitive — title is forwarded to the fallback so
// the failure surface tells the clinician which section they lost.
function CardSlot({
  title,
  children,
}: {
  title: string
  children: ReactNode
}) {
  return (
    <ErrorBoundary
      cardTitle={title}
      fallback={
        <div
          className="alert alert-warning mb-3"
          role="alert"
          data-testid={`card-error-${title.toLowerCase().replace(/\s+/g, '-')}`}
        >
          {`The "${title}" card failed to load.`}
        </div>
      }
    >
      {children}
    </ErrorBoundary>
  )
}
