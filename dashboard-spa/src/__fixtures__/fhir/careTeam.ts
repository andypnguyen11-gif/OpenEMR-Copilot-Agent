import type { CareTeam, Practitioner } from 'fhir/r4'

interface ParticipantOpts {
  practitionerUuid?: string
  // For organization or related-person participants, reference is set
  // verbatim and `member.display` is the only name we can show (no
  // Practitioner.read fan-out for those rows).
  reference?: string
  display?: string
  role?: string
  periodStart?: string
  onBehalfOf?: string
}

interface CareTeamOpts {
  id?: string
  status?: CareTeam['status']
  participants?: ParticipantOpts[]
  note?: string
}

export function buildCareTeam(opts: CareTeamOpts = {}): CareTeam {
  const { id = 'team-1', status = 'active', participants = [], note } = opts
  const resource: CareTeam = {
    resourceType: 'CareTeam',
    id,
    status,
    subject: { reference: 'Patient/test-patient' },
    participant: participants.map((p) => ({
      ...(p.practitionerUuid !== undefined && {
        member: {
          reference: `Practitioner/${p.practitionerUuid}`,
          ...(p.display !== undefined && { display: p.display }),
        },
      }),
      ...(p.reference !== undefined && {
        member: {
          reference: p.reference,
          ...(p.display !== undefined && { display: p.display }),
        },
      }),
      ...(p.role !== undefined && {
        role: [{ text: p.role }],
      }),
      ...(p.periodStart !== undefined && {
        period: { start: p.periodStart },
      }),
      ...(p.onBehalfOf !== undefined && {
        onBehalfOf: { display: p.onBehalfOf },
      }),
    })),
  }
  if (note !== undefined) {
    resource.note = [{ text: note }]
  }
  return resource
}

interface PractitionerOpts {
  id: string
  text?: string
  family?: string
  given?: string[]
}

export function buildPractitioner(opts: PractitionerOpts): Practitioner {
  return {
    resourceType: 'Practitioner',
    id: opts.id,
    name: [
      {
        ...(opts.text !== undefined && { text: opts.text }),
        ...(opts.family !== undefined && { family: opts.family }),
        ...(opts.given !== undefined && { given: opts.given }),
      },
    ],
  }
}
