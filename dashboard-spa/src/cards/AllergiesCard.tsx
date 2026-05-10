import { useEffect, useState } from 'react'
import type { AllergyIntolerance } from 'fhir/r4'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { Loading } from '../components/Loading'
import { useFhirClient } from '../fhir/useFhirClient'

// SNOMED CT 716186003 ("no known allergy") is the coded sentinel OpenEMR
// emits when a patient has been screened and reports no allergies. Treated
// as the "screened, none" signal — never rendered as a row.
const NKDA_CODE = '716186003'

interface Props {
  patientId: string
}

export function AllergiesCard({ patientId }: Props) {
  const fhir = useFhirClient()
  const [data, setData] = useState<AllergyIntolerance[] | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    fhir
      .search<AllergyIntolerance>('AllergyIntolerance', { patient: patientId })
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
    <CardBase title="Allergies">
      <Body data={data} error={error} />
    </CardBase>
  )
}

function Body({
  data,
  error,
}: {
  data: AllergyIntolerance[] | null
  error: Error | null
}) {
  if (error) {
    return <p className="text-danger mb-0 ml-2">Could not load allergies.</p>
  }
  if (data === null) {
    return <Loading label="Loading allergies" />
  }
  if (data.length === 0) {
    return <EmptyState variant="nothing-recorded" />
  }
  // The clinical-status filter is intentionally client-side: OpenEMR's FHIR
  // search-param surface for AllergyIntolerance does not honor a server-side
  // `clinical-status` query, so filtering here is the only option that
  // reliably hides resolved/inactive entries.
  const nkda = data.some(isNkda)
  const active = data.filter((r) => !isNkda(r) && isActive(r))
  if (active.length === 0) {
    return (
      <EmptyState variant={nkda ? 'no-known-allergies' : 'nothing-recorded'} />
    )
  }
  return (
    <ul
      className="list-group list-group-flush pami-list"
      data-testid="allergies-list"
    >
      {active.map((a) => (
        <AllergyRow key={a.id ?? title(a)} allergy={a} />
      ))}
    </ul>
  )
}

function AllergyRow({ allergy }: { allergy: AllergyIntolerance }) {
  return (
    <li className="list-group-item p-1">
      <div className="d-flex w-100 justify-content-between align-items-center">
        <div>
          <span>{title(allergy)}</span>
          {manifestation(allergy) !== '' && (
            <small className="text-muted ml-2">
              {manifestation(allergy)}
            </small>
          )}
        </div>
        {allergy.criticality === 'high' && (
          <span
            className="badge badge-warning"
            data-testid="severity-badge"
          >
            High
          </span>
        )}
      </div>
    </li>
  )
}

function isNkda(r: AllergyIntolerance): boolean {
  return r.code?.coding?.some((c) => c.code === NKDA_CODE) === true
}

function isActive(r: AllergyIntolerance): boolean {
  return r.clinicalStatus?.coding?.[0]?.code === 'active'
}

function title(r: AllergyIntolerance): string {
  return (
    r.code?.text ??
    r.code?.coding?.[0]?.display ??
    'Unknown allergen'
  )
}

function manifestation(r: AllergyIntolerance): string {
  const all = r.reaction
    ?.flatMap((rx) => rx.manifestation ?? [])
    .map((m) => m.text ?? m.coding?.[0]?.display ?? '')
    .filter((s) => s.length > 0) ?? []
  return all.join(', ')
}
