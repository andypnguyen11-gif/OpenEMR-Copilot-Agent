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
    <div className="container dashboard-shell py-4">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h1 className="h5 mb-0 text-muted font-weight-normal">
          Medical Record Dashboard
        </h1>
        <SwitchPatientButton />
      </div>

      <PatientHeader id={id} />

      {/* Top row mirrors the legacy 3-column layout: Allergies, Problem List,
          Medications. col-md-6 keeps two-up on tablets; col-xl-4 splits to
          three-up on desktop. */}
      <div className="row">
        <div className="col-12 col-md-6 col-xl-4">
          <CardSlot title="Allergies">
            <AllergiesCard patientId={id} />
          </CardSlot>
        </div>
        <div className="col-12 col-md-6 col-xl-4">
          <CardSlot title="Problem List">
            <ProblemsCard patientId={id} />
          </CardSlot>
        </div>
        <div className="col-12 col-md-12 col-xl-4">
          <CardSlot title="Medications">
            <MedicationsCard patientId={id} />
          </CardSlot>
        </div>
      </div>

      {/* Prescriptions is full-width per the legacy — it's the densest card,
          with refills/quantity columns that benefit from horizontal room. */}
      <div className="row">
        <div className="col-12">
          <CardSlot title="Prescriptions">
            <PrescriptionsCard patientId={id} />
          </CardSlot>
        </div>
      </div>

      {/* Care Team + Lab Results paired on lg+. */}
      <div className="row">
        <div className="col-12 col-lg-6">
          <CardSlot title="Care Team">
            <CareTeamCard patientId={id} />
          </CardSlot>
        </div>
        <div className="col-12 col-lg-6">
          <CardSlot title="Lab Results">
            <LabResultsCard patientId={id} />
          </CardSlot>
        </div>
      </div>
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
