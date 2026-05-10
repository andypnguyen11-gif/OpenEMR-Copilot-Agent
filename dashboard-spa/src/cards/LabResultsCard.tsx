import { useEffect, useState } from 'react'
import type { Observation } from 'fhir/r4'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { Loading } from '../components/Loading'
import { useFhirClient } from '../fhir/useFhirClient'

const LOINC_SYSTEM = 'http://loinc.org'

// V3 ObservationInterpretation codes the dashboard surfaces as colored
// badges. Anything outside this list (e.g. `N` normal) renders the row
// without a badge — empty surface area is the right default.
const BADGE_VARIANT: Record<string, 'warning' | 'danger' | 'info'> = {
  H: 'warning',
  L: 'warning',
  HH: 'danger',
  LL: 'danger',
  A: 'info',
}

const BADGE_LABEL: Record<string, string> = {
  H: 'High',
  L: 'Low',
  HH: 'Critical High',
  LL: 'Critical Low',
  A: 'Abnormal',
}

interface Props {
  patientId: string
}

interface Group {
  key: string
  label: string
  rows: Observation[]
}

export function LabResultsCard({ patientId }: Props) {
  const fhir = useFhirClient()
  const [data, setData] = useState<Observation[] | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    fhir
      // _count=200 caps how many lab rows the dashboard pulls. The
      // migration doc §7 records this as a known parity gap — the legacy
      // labdata fragment paginates server-side, this view doesn't.
      // _sort=-date relies on the server applying the sort; we do a
      // tiebreaker sort in-memory so missing dates don't shuffle groups.
      .search<Observation>('Observation', {
        patient: patientId,
        category: 'laboratory',
        _sort: '-date',
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
    <CardBase title="Lab Results">
      <Body data={data} error={error} />
    </CardBase>
  )
}

function Body({
  data,
  error,
}: {
  data: Observation[] | null
  error: Error | null
}) {
  if (error) {
    return (
      <p className="text-danger mb-0 ml-2">Could not load lab results.</p>
    )
  }
  if (data === null) {
    return <Loading label="Loading lab results" />
  }
  if (data.length === 0) {
    return <EmptyState variant="nothing-recorded" />
  }
  const groups = groupByLoinc(data)
  return (
    <div data-testid="lab-results">
      {groups.map((g) => (
        <LabGroup key={g.key} group={g} />
      ))}
    </div>
  )
}

function LabGroup({ group }: { group: Group }) {
  return (
    <section
      className="mb-2"
      data-testid="lab-group"
      data-group-key={group.key}
    >
      <h6 className="font-weight-bold mb-1 ml-2 small">{group.label}</h6>
      <ul className="list-group list-group-flush pami-list">
        {group.rows.map((obs, i) => (
          <LabRow key={obs.id ?? `${group.key}-${i}`} obs={obs} />
        ))}
      </ul>
    </section>
  )
}

function LabRow({ obs }: { obs: Observation }) {
  const code = interpretationCode(obs)
  const variant = code !== '' ? BADGE_VARIANT[code] : undefined
  return (
    <li className="list-group-item p-1">
      <div className="d-flex justify-content-between align-items-baseline">
        <span>
          <span className="font-weight-normal">{value(obs)}</span>
          {referenceRange(obs) !== '' && (
            <small className="text-muted ml-2">
              ref: {referenceRange(obs)}
            </small>
          )}
        </span>
        <span className="d-flex align-items-baseline">
          {variant !== undefined && (
            <span
              className={`badge badge-${variant} mr-2`}
              data-testid="lab-badge"
              data-interpretation={code}
            >
              {BADGE_LABEL[code]}
            </span>
          )}
          <small className="text-muted">{obs.effectiveDateTime ?? ''}</small>
        </span>
      </div>
    </li>
  )
}

// Group key falls through LOINC code → code.text → "Unknown". The label
// shown in the section header prefers a human-readable display from the
// LOINC coding so groups don't all read as bare LOINC numbers.
function groupByLoinc(observations: Observation[]): Group[] {
  const map = new Map<string, Group>()
  for (const obs of observations) {
    const key = groupKey(obs)
    const label = groupLabel(obs)
    let g = map.get(key)
    if (g === undefined) {
      g = { key, label, rows: [] }
      map.set(key, g)
    }
    g.rows.push(obs)
  }
  for (const g of map.values()) {
    // Newest-first within each group. Server `_sort=-date` mostly does
    // this; the in-memory sort is a tiebreaker so a missing or malformed
    // effectiveDateTime never shuffles the group order.
    g.rows.sort((a, b) => {
      const da = a.effectiveDateTime ?? ''
      const db = b.effectiveDateTime ?? ''
      return db.localeCompare(da)
    })
  }
  return [...map.values()]
}

function groupKey(obs: Observation): string {
  const loinc = obs.code?.coding?.find((c) => c.system === LOINC_SYSTEM)?.code
  if (loinc !== undefined && loinc !== '') return `loinc:${loinc}`
  if (obs.code?.text !== undefined && obs.code.text !== '') {
    return `text:${obs.code.text}`
  }
  return 'unknown'
}

function groupLabel(obs: Observation): string {
  const loinc = obs.code?.coding?.find((c) => c.system === LOINC_SYSTEM)
  if (loinc?.display !== undefined && loinc.display !== '') return loinc.display
  if (obs.code?.text !== undefined && obs.code.text !== '') return obs.code.text
  if (loinc?.code !== undefined && loinc.code !== '') return `LOINC ${loinc.code}`
  return 'Unknown'
}

function value(obs: Observation): string {
  if (obs.valueQuantity !== undefined) {
    const v = obs.valueQuantity.value
    const u = obs.valueQuantity.unit ?? ''
    if (v === undefined || v === null) return ''
    return u !== '' ? `${v} ${u}` : String(v)
  }
  if (obs.valueString !== undefined) return obs.valueString
  return ''
}

function referenceRange(obs: Observation): string {
  const r = obs.referenceRange?.[0]
  if (r === undefined) return ''
  if (r.text !== undefined && r.text !== '') return r.text
  const lo = r.low?.value
  const hi = r.high?.value
  const unit = r.high?.unit ?? r.low?.unit ?? ''
  if (lo !== undefined && hi !== undefined) {
    return unit !== '' ? `${lo}–${hi} ${unit}` : `${lo}–${hi}`
  }
  if (hi !== undefined) return unit !== '' ? `< ${hi} ${unit}` : `< ${hi}`
  if (lo !== undefined) return unit !== '' ? `> ${lo} ${unit}` : `> ${lo}`
  return ''
}

function interpretationCode(obs: Observation): string {
  return obs.interpretation?.[0]?.coding?.[0]?.code ?? ''
}
