import type { AllergyIntolerance } from 'fhir/r4'

// SNOMED CT code carried by an explicit "no known allergies" record.
// US-Core / OpenEMR encode NKDA this way, so card-empty logic distinguishes
// "patient screened, none reported" from "no record at all."
export const NKDA_SNOMED_CODE = '716186003'

interface BuildOpts {
  id?: string
  status?: 'active' | 'inactive' | 'resolved'
  code?: string
  display?: string
  criticality?: AllergyIntolerance['criticality']
  manifestation?: string
}

export function buildAllergyIntolerance(opts: BuildOpts = {}): AllergyIntolerance {
  const {
    id = 'allergy-1',
    status = 'active',
    display = 'Penicillin',
    code,
    criticality,
    manifestation,
  } = opts
  const resource: AllergyIntolerance = {
    resourceType: 'AllergyIntolerance',
    id,
    clinicalStatus: {
      coding: [
        {
          system:
            'http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical',
          code: status,
        },
      ],
    },
    code: {
      text: display,
      ...(code !== undefined && {
        coding: [{ system: 'http://snomed.info/sct', code, display }],
      }),
    },
    patient: { reference: 'Patient/test-patient' },
  }
  if (criticality !== undefined) {
    resource.criticality = criticality
  }
  if (manifestation !== undefined) {
    resource.reaction = [
      { manifestation: [{ text: manifestation }] },
    ]
  }
  return resource
}

export function buildNkdaRecord(): AllergyIntolerance {
  return {
    resourceType: 'AllergyIntolerance',
    id: 'nkda',
    code: {
      coding: [
        {
          system: 'http://snomed.info/sct',
          code: NKDA_SNOMED_CODE,
          display: 'No known allergy',
        },
      ],
      text: 'No known allergies',
    },
    patient: { reference: 'Patient/test-patient' },
  }
}
