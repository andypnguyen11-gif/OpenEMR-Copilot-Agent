import type { Condition } from 'fhir/r4'

interface BuildOpts {
  id?: string
  display?: string
  category?: 'problem-list-item' | 'encounter-diagnosis'
}

export function buildCondition(opts: BuildOpts = {}): Condition {
  const {
    id = 'condition-1',
    display = 'Hypertension',
    category = 'problem-list-item',
  } = opts
  return {
    resourceType: 'Condition',
    id,
    category: [
      {
        coding: [
          {
            system:
              'http://terminology.hl7.org/CodeSystem/condition-category',
            code: category,
          },
        ],
      },
    ],
    code: { text: display },
    subject: { reference: 'Patient/test-patient' },
  }
}
