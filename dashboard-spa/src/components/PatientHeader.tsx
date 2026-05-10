import { useEffect, useState } from 'react'
import type { Patient } from 'fhir/r4'
import { useFhirClient } from '../fhir/useFhirClient'
import { Loading } from './Loading'

// Persistent identity bar above the six clinical cards. Lifts the visual
// shape from upstream's dashboard_header.php output (Bootstrap 4.6 card,
// inline label/value pairs) but drops the page-heading + nav glue —
// react-router-dom owns navigation here.

interface Props {
  id: string
}

export function PatientHeader({ id }: Props) {
  const fhir = useFhirClient()
  const [patient, setPatient] = useState<Patient | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    setPatient(null)
    setError(null)
    fhir
      .read<Patient>('Patient', id)
      .then((p) => {
        if (!cancelled) setPatient(p)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e : new Error(String(e)))
        }
      })
    return () => {
      cancelled = true
    }
  }, [fhir, id])

  if (error) {
    return (
      <header className="card mb-3" role="banner">
        <div className="card-body p-3">
          <p className="text-danger mb-0">
            Could not load patient details.
          </p>
        </div>
      </header>
    )
  }

  if (!patient) {
    return (
      <header className="card mb-3" role="banner">
        <div className="card-body p-3">
          <Loading label="Loading patient" />
        </div>
      </header>
    )
  }

  return (
    <header className="card patient-header mb-3" role="banner">
      <div
        className="card-body p-3 d-flex flex-wrap align-items-center"
        data-testid="patient-header"
      >
        <h1
          className="h4 mb-0 mr-4 patient-name"
          data-testid="patient-name"
        >
          {displayName(patient)}
        </h1>
        <Field label="DOB" value={patient.birthDate ?? '—'} />
        <Field label="Sex" value={patient.gender ?? '—'} />
        <Field
          label="Status"
          value={patient.active === false ? 'Inactive' : 'Active'}
        />
        <Field label="MRN" value={mrn(patient)} testId="patient-mrn" />
      </div>
    </header>
  )
}

// Exported for unit testing — name/MRN extraction is tightly coupled to the
// FHIR shape OpenEMR returns, so the contract deserves direct coverage.

export function displayName(p: Patient): string {
  const n = p.name?.[0]
  if (n?.text) return n.text
  const family = n?.family ?? ''
  const given = n?.given?.join(' ') ?? ''
  const joined = `${given} ${family}`.trim()
  return joined.length > 0 ? joined : 'Unknown patient'
}

// Plan §PR4: filter `identifier` by `type.coding[].code === 'PT'`. Anything
// else (driver's licence, SSN) is intentionally ignored — only the medical
// record number belongs in the header.
export function mrn(p: Patient): string {
  const match = p.identifier?.find((i) =>
    i.type?.coding?.some((c) => c.code === 'PT'),
  )
  return match?.value ?? '—'
}

function Field({
  label,
  value,
  testId,
}: {
  label: string
  value: string
  testId?: string
}) {
  return (
    <div className="mr-4 mb-1">
      <small className="d-block field-label">{label}</small>
      <span className="field-value" data-testid={testId}>
        {value}
      </span>
    </div>
  )
}
