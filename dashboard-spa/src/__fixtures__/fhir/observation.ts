import type { Observation } from 'fhir/r4'

export const LOINC_SYSTEM = 'http://loinc.org'

interface BuildOpts {
  id?: string
  loinc?: string
  loincDisplay?: string
  // When provided, overrides the LOINC-derived `code.text` so tests can
  // exercise the "missing LOINC, fall back to code.text" path.
  text?: string
  // Drop the LOINC coding entirely. Combined with `text`, exercises the
  // text-fallback group key; combined with neither, falls all the way to
  // the "Unknown" group.
  noLoinc?: boolean
  effectiveDateTime?: string
  valueQuantity?: { value: number; unit: string }
  valueString?: string
  interpretation?: 'H' | 'L' | 'HH' | 'LL' | 'A' | 'N'
  referenceRange?: {
    low?: { value: number; unit: string }
    high?: { value: number; unit: string }
    text?: string
  }
}

export function buildLabObservation(opts: BuildOpts = {}): Observation {
  const {
    id = 'obs-1',
    loinc = '2339-0',
    loincDisplay = 'Glucose [Mass/volume] in Blood',
    text,
    noLoinc = false,
    effectiveDateTime = '2024-06-01',
    valueQuantity,
    valueString,
    interpretation,
    referenceRange,
  } = opts
  const codeCoding = noLoinc
    ? []
    : [{ system: LOINC_SYSTEM, code: loinc, display: loincDisplay }]
  const resource: Observation = {
    resourceType: 'Observation',
    id,
    status: 'final',
    category: [
      {
        coding: [
          {
            system:
              'http://terminology.hl7.org/CodeSystem/observation-category',
            code: 'laboratory',
          },
        ],
      },
    ],
    code: {
      ...(codeCoding.length > 0 && { coding: codeCoding }),
      ...(text !== undefined && { text }),
    },
    subject: { reference: 'Patient/test-patient' },
    effectiveDateTime,
  }
  if (valueQuantity !== undefined) {
    resource.valueQuantity = {
      value: valueQuantity.value,
      unit: valueQuantity.unit,
    }
  }
  if (valueString !== undefined) {
    resource.valueString = valueString
  }
  if (interpretation !== undefined) {
    resource.interpretation = [
      {
        coding: [
          {
            system:
              'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation',
            code: interpretation,
          },
        ],
      },
    ]
  }
  if (referenceRange !== undefined) {
    resource.referenceRange = [
      {
        ...(referenceRange.low !== undefined && {
          low: { value: referenceRange.low.value, unit: referenceRange.low.unit },
        }),
        ...(referenceRange.high !== undefined && {
          high: {
            value: referenceRange.high.value,
            unit: referenceRange.high.unit,
          },
        }),
        ...(referenceRange.text !== undefined && {
          text: referenceRange.text,
        }),
      },
    ]
  }
  return resource
}
