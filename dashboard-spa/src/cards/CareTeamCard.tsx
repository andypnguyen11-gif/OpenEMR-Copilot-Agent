import { useEffect, useState } from 'react'
import type { CareTeam, Practitioner } from 'fhir/r4'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { Loading } from '../components/Loading'
import { useFhirClient } from '../fhir/useFhirClient'

// CareTeam.participant.member can be Practitioner, Organization, or
// RelatedPerson. We only fan out a Practitioner.read for the first; org
// and related-person rows surface whatever member.display the team
// resource carried (US Core requires it).
const PRACTITIONER_REF = /^Practitioner\/(.+)$/

interface Props {
  patientId: string
}

interface Row {
  key: string
  name: string
  role: string
  since: string
  facility: string
  teamStatus: CareTeam['status']
}

interface Loaded {
  rows: Row[]
  notes: string[]
}

export function CareTeamCard({ patientId }: Props) {
  const fhir = useFhirClient()
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)

    async function load() {
      const teams = await fhir.search<CareTeam>('CareTeam', {
        patient: patientId,
      })

      // Collect unique practitioner UUIDs across all teams. Memoizing here
      // (rather than per-row) is what makes the test's "4 participants → 3
      // reads" guarantee load-bearing: a duplicate reference shouldn't
      // double-bill Practitioner.read.
      const uuids = new Set<string>()
      for (const team of teams) {
        for (const p of team.participant ?? []) {
          const uuid = practitionerUuid(p.member?.reference)
          if (uuid !== '') uuids.add(uuid)
        }
      }

      // One read per unique UUID, all in flight at once. allSettled
      // (not all) — OpenEMR will emit a Practitioner reference for any user
      // listed on the team, but FhirPractitionerService filters out users
      // who don't qualify as clinicians (e.g. the bare admin account, no
      // NPI / physician_type), so ~one read per real-world team can 404.
      // A single 404 should not blank the card; the row falls back to
      // member.display below.
      const settled = await Promise.allSettled(
        [...uuids].map(async (uuid) => {
          const p = await fhir.read<Practitioner>('Practitioner', uuid)
          return [uuid, p] as const
        }),
      )
      const practitionersById = new Map<string, Practitioner>()
      for (const result of settled) {
        if (result.status === 'fulfilled') {
          practitionersById.set(result.value[0], result.value[1])
        } else {
          console.warn('CareTeam: Practitioner.read failed', result.reason)
        }
      }

      const rows: Row[] = []
      const notes: string[] = []
      for (const team of teams) {
        for (const note of team.note ?? []) {
          if (note.text !== undefined) notes.push(note.text)
        }
        for (const [i, p] of (team.participant ?? []).entries()) {
          const ref = p.member?.reference ?? ''
          const uuid = practitionerUuid(ref)
          const practitioner = uuid !== '' ? practitionersById.get(uuid) : undefined
          rows.push({
            key: `${team.id ?? 'team'}-${i}`,
            name:
              practitioner !== undefined
                ? practitionerName(practitioner)
                : (p.member?.display ?? memberFallback(ref)),
            role: p.role?.[0]?.text ?? p.role?.[0]?.coding?.[0]?.display ?? '',
            since: p.period?.start ?? '',
            facility: p.onBehalfOf?.display ?? '',
            teamStatus: team.status,
          })
        }
      }

      if (!cancelled) setData({ rows, notes })
    }

    load().catch((e: unknown) => {
      if (!cancelled) {
        setError(e instanceof Error ? e : new Error(String(e)))
      }
    })

    return () => {
      cancelled = true
    }
  }, [fhir, patientId])

  return (
    <CardBase title="Care Team">
      <Body data={data} error={error} />
    </CardBase>
  )
}

function Body({ data, error }: { data: Loaded | null; error: Error | null }) {
  if (error) {
    return <p className="text-danger mb-0 ml-2">Could not load care team.</p>
  }
  if (data === null) {
    return <Loading label="Loading care team" />
  }
  if (data.rows.length === 0) {
    return <EmptyState variant="nothing-recorded" />
  }
  return (
    <>
      {data.notes.length > 0 && (
        <div className="mb-2 ml-2 small text-muted">
          {data.notes.map((n, i) => (
            <p key={i} className="mb-0">
              {n}
            </p>
          ))}
        </div>
      )}
      <ul
        className="list-group list-group-flush pami-list"
        data-testid="care-team-list"
      >
        {data.rows.map((row) => (
          <CareTeamRow key={row.key} row={row} />
        ))}
      </ul>
    </>
  )
}

function CareTeamRow({ row }: { row: Row }) {
  return (
    <li className="list-group-item p-1">
      <div className="d-flex justify-content-between align-items-baseline">
        <span className="font-weight-normal">{row.name}</span>
        {row.teamStatus !== undefined && (
          <small className="text-muted">{row.teamStatus}</small>
        )}
      </div>
      <div className="small text-muted d-flex flex-wrap">
        <Field label="Role" value={row.role} testId="ct-role" />
        <Field label="Since" value={row.since} testId="ct-since" />
        <Field label="Facility" value={row.facility} testId="ct-facility" />
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

function practitionerUuid(reference: string | undefined): string {
  if (reference === undefined) return ''
  const m = PRACTITIONER_REF.exec(reference)
  return m?.[1] ?? ''
}

function practitionerName(p: Practitioner): string {
  const n = p.name?.[0]
  if (n?.text !== undefined && n.text !== '') return n.text
  const family = n?.family ?? ''
  const given = n?.given?.join(' ') ?? ''
  const joined = `${given} ${family}`.trim()
  return joined !== '' ? joined : 'Unknown practitioner'
}

// Used when the participant references a non-Practitioner resource (org or
// related person) and the resource didn't carry member.display. Keep the
// short type — better than an empty row.
function memberFallback(reference: string): string {
  if (reference === '') return 'Unknown member'
  const type = reference.split('/')[0] ?? ''
  return type !== '' ? type : 'Unknown member'
}
