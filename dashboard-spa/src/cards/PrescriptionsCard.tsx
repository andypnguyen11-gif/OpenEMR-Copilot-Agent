import { useEffect, useState } from 'react'
import type { MedicationRequest } from 'fhir/r4'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { Loading } from '../components/Loading'
import { useFhirClient } from '../fhir/useFhirClient'

// Prescriptions are MedicationRequest rows whose intent === 'order' — the
// concrete dispensing event the legacy dashboard's Smarty rx fragment
// displayed. We rely on the server-side `intent=order` token search here
// (unlike Medications, which has to filter client-side because excluding
// `order` would also drop valid `original-order` / `instance-order` rows).
//
// Layout reconstructed from the legacy Smarty fragment, since rx.html.twig
// only wraps it: drug, dose, frequency, route, refills, quantity per row.

interface Props {
  patientId: string
}

export function PrescriptionsCard({ patientId }: Props) {
  const fhir = useFhirClient()
  const [data, setData] = useState<MedicationRequest[] | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    fhir
      .search<MedicationRequest>('MedicationRequest', {
        patient: patientId,
        intent: 'order',
      })
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
    <CardBase title="Prescriptions" cardId="prescriptions">
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
    return <p className="text-danger mb-0 ml-2">Could not load prescriptions.</p>
  }
  if (data === null) {
    return <Loading label="Loading prescriptions" />
  }
  if (data.length === 0) {
    return <EmptyState variant="nothing-recorded" />
  }
  return (
    <ul
      className="list-group list-group-flush pami-list"
      data-testid="prescriptions-list"
    >
      {data.map((m) => (
        <PrescriptionRow key={m.id ?? title(m)} rx={m} />
      ))}
    </ul>
  )
}

function PrescriptionRow({ rx }: { rx: MedicationRequest }) {
  return (
    <li className="list-group-item p-1">
      <div className="font-weight-normal">{title(rx)}</div>
      <div className="small text-muted d-flex flex-wrap">
        <Field label="Dose" value={dose(rx)} />
        <Field label="Frequency" value={frequency(rx)} />
        <Field label="Route" value={route(rx)} />
        <Field label="Refills" value={refills(rx)} testId="rx-refills" />
        <Field label="Quantity" value={quantity(rx)} testId="rx-quantity" />
      </div>
    </li>
  )
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
  if (value === '') return null
  return (
    <span className="mr-3" data-testid={testId}>
      <span className="font-weight-bold">{label}:</span> {value}
    </span>
  )
}

function title(m: MedicationRequest): string {
  return (
    m.medicationReference?.display ??
    m.medicationCodeableConcept?.text ??
    m.medicationCodeableConcept?.coding?.[0]?.display ??
    'Unknown medication'
  )
}

function dose(m: MedicationRequest): string {
  const dq = m.dosageInstruction?.[0]?.doseAndRate?.[0]?.doseQuantity
  if (!dq) return ''
  const value = dq.value
  const unit = dq.unit ?? ''
  if (value === undefined || value === null) return ''
  return unit !== '' ? `${value} ${unit}` : String(value)
}

function frequency(m: MedicationRequest): string {
  return m.dosageInstruction?.[0]?.timing?.code?.text ?? ''
}

function route(m: MedicationRequest): string {
  return m.dosageInstruction?.[0]?.route?.text ?? ''
}

// numberOfRepeatsAllowed is a 0..1 unsignedInt. OpenEMR's service writes 0 by
// default when the source row has no value, so we render "0" rather than
// hiding the field — matches the legacy "0 refills" surface.
function refills(m: MedicationRequest): string {
  const v = m.dispenseRequest?.numberOfRepeatsAllowed
  return v === undefined ? '' : String(v)
}

function quantity(m: MedicationRequest): string {
  const q = m.dispenseRequest?.quantity
  if (!q) return ''
  const value = q.value
  const unit = q.unit ?? ''
  if (value === undefined || value === null) return ''
  return unit !== '' ? `${value} ${unit}` : String(value)
}
