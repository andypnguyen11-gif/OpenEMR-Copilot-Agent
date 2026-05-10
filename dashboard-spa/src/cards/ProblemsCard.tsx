import { useEffect, useState } from 'react'
import type { Condition } from 'fhir/r4'
import { CardBase } from '../components/CardBase'
import { EmptyState } from '../components/EmptyState'
import { Loading } from '../components/Loading'
import { useFhirClient } from '../fhir/useFhirClient'

interface Props {
  patientId: string
}

export function ProblemsCard({ patientId }: Props) {
  const fhir = useFhirClient()
  const [data, setData] = useState<Condition[] | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    fhir
      .search<Condition>('Condition', {
        patient: patientId,
        category: 'problem-list-item',
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
    <CardBase title="Problem List">
      <Body data={data} error={error} />
    </CardBase>
  )
}

function Body({
  data,
  error,
}: {
  data: Condition[] | null
  error: Error | null
}) {
  if (error) {
    return <p className="text-danger mb-0 ml-2">Could not load problems.</p>
  }
  if (data === null) {
    return <Loading label="Loading problems" />
  }
  if (data.length === 0) {
    return <EmptyState variant="nothing-recorded" />
  }
  return (
    <ul
      className="list-group list-group-flush pami-list"
      data-testid="problems-list"
    >
      {data.map((c) => (
        <li key={c.id ?? title(c)} className="list-group-item py-1 px-1">
          {title(c)}
        </li>
      ))}
    </ul>
  )
}

function title(c: Condition): string {
  return c.code?.text ?? c.code?.coding?.[0]?.display ?? 'Unknown problem'
}
