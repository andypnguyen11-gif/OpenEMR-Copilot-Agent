import { useEffect, useState } from 'react'
import type { MedicationRequest } from 'fhir/r4'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { Loading } from '../components/Loading'
import { useFhirClient } from '../fhir/useFhirClient'

// "Medications" is the upstream Twig's `medication.html.twig` — the issue-list
// view of medications a patient is on. In FHIR that maps to MedicationRequest
// rows whose intent is `plan` or `proposal` (the prescriber's intent for
// ongoing therapy). Anything with intent=order/original-order/instance-order
// is a prescription event — covered by PrescriptionsCard.
//
// The intent filter has to run client-side: if we filtered with `intent != order`
// server-side we'd also drop `original-order` / `instance-order`, which is wrong
// per the upstream surface.
const ONGOING_INTENTS: MedicationRequest['intent'][] = ['plan', 'proposal']

interface Props {
  patientId: string
}

export function MedicationsCard({ patientId }: Props) {
  const fhir = useFhirClient()
  const [data, setData] = useState<MedicationRequest[] | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    fhir
      .search<MedicationRequest>('MedicationRequest', { patient: patientId })
      .then((entries) => {
        if (!cancelled) setData(entries)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e : new Error(String(e)))
        }
      })
    return () => {
      cancelled = true
    }
  }, [fhir, patientId])

  return (
    <CardBase title="Medications">
      <Body data={data} error={error} />
    </CardBase>
  )
}

function Body({
  data,
  error,
}: {
  data: MedicationRequest[] | null
  error: Error | null
}) {
  if (error) {
    return <p className="text-danger mb-0 ml-2">Could not load medications.</p>
  }
  if (data === null) {
    return <Loading label="Loading medications" />
  }
  const ongoing = data.filter((m) =>
    ONGOING_INTENTS.includes(m.intent),
  )
  if (ongoing.length === 0) {
    return <EmptyState variant="nothing-recorded" />
  }
  return (
    <ul
      className="list-group list-group-flush pami-list"
      data-testid="medications-list"
    >
      {ongoing.map((m) => (
        <li key={m.id ?? title(m)} className="list-group-item p-1">
          <span className="font-weight-normal">{title(m)}</span>
          {dosage(m) !== '' && (
            <span className="text-muted ml-2">{dosage(m)}</span>
          )}
        </li>
      ))}
    </ul>
  )
}

// Drug-name extraction. Per the FHIR spec the field is one-of —
// medicationReference XOR medicationCodeableConcept. OpenEMR currently only
// emits CodeableConcept, but the spec allows either, so we fall back through
// both so a future Reference-emitting build doesn't render rows as "Unknown".
export function title(m: MedicationRequest): string {
  return (
    m.medicationReference?.display ??
    m.medicationCodeableConcept?.text ??
    m.medicationCodeableConcept?.coding?.[0]?.display ??
    'Unknown medication'
  )
}

function dosage(m: MedicationRequest): string {
  return m.dosageInstruction?.[0]?.text ?? ''
}
