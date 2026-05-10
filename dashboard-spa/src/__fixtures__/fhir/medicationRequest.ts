import type { MedicationRequest } from 'fhir/r4'

interface BuildOpts {
  id?: string
  intent?: MedicationRequest['intent']
  // Drug name. When `useReference` is true the fixture sets
  // medicationReference.display instead of medicationCodeableConcept.text —
  // OpenEMR's FHIR service only emits CodeableConcept, but the card must
  // still render reference-shaped data correctly for forward-compat.
  display?: string
  useReference?: boolean
  dosageText?: string
  dose?: { value: number; unit: string }
  frequencyText?: string
  routeText?: string
  refills?: number
  quantity?: { value: number; unit: string }
  // Set to true to omit dispenseRequest entirely (prescriptions test asserts
  // the row doesn't crash when the dispense block is missing).
  omitDispense?: boolean
}

export function buildMedicationRequest(
  opts: BuildOpts = {},
): MedicationRequest {
  const {
    id = 'medreq-1',
    intent = 'order',
    display = 'Lisinopril 10 MG Oral Tablet',
    useReference = false,
    dosageText,
    dose,
    frequencyText,
    routeText,
    refills,
    quantity,
    omitDispense = false,
  } = opts
  const resource: MedicationRequest = {
    resourceType: 'MedicationRequest',
    id,
    status: 'active',
    intent,
    subject: { reference: 'Patient/test-patient' },
    ...(useReference
      ? {
          medicationReference: {
            reference: 'Medication/med-1',
            display,
          },
        }
      : {
          medicationCodeableConcept: { text: display },
        }),
  }
  if (
    dosageText !== undefined ||
    dose !== undefined ||
    frequencyText !== undefined ||
    routeText !== undefined
  ) {
    resource.dosageInstruction = [
      {
        ...(dosageText !== undefined && { text: dosageText }),
        ...(dose !== undefined && {
          doseAndRate: [
            {
              doseQuantity: { value: dose.value, unit: dose.unit },
            },
          ],
        }),
        ...(frequencyText !== undefined && {
          timing: { code: { text: frequencyText } },
        }),
        ...(routeText !== undefined && {
          route: { text: routeText },
        }),
      },
    ]
  }
  if (!omitDispense) {
    resource.dispenseRequest = {
      ...(refills !== undefined && { numberOfRepeatsAllowed: refills }),
      ...(quantity !== undefined && {
        quantity: { value: quantity.value, unit: quantity.unit },
      }),
    }
  }
  return resource
}
