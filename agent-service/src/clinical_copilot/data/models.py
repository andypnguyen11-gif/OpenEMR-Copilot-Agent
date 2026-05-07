"""Minimal Pydantic models for the FHIR R4 resources the agent reads.

Models intentionally cover only the fields the tool layer projects into
:mod:`clinical_copilot.tools.records`. A full FHIR R4 schema would be an
order of magnitude larger and would force every parse to walk fields the
agent never inspects, increasing the surface for "silently mis-displayed
clinical data" bugs at no benefit.

Design notes:

- Every model uses ``extra="ignore"`` because FHIR servers attach their own
  bookkeeping (``meta``, ``text``, ``contained``, etc.) and those fields
  changing must not break parsing. ``populate_by_name=True`` so we can
  alias the FHIR camelCase names to snake_case Python attributes — the
  alias is the wire name, the attribute is what the rest of the codebase
  reads.
- Models are ``frozen=True`` so the orchestrator and tool layer can pass
  them through layers without worrying about hidden mutation.
- Choice types (e.g. ``onset[x]`` on ``Condition``) are flattened into
  separate optional attributes; downstream code picks whichever is set.
- Reference fields are stored as the raw ``"ResourceType/id"`` string —
  the agent currently never resolves them, so a typed ``Reference`` model
  would be dead weight.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _FhirModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        populate_by_name=True,
    )


# ---------- shared primitive shapes ----------


class Coding(_FhirModel):
    """One ``code + system + display`` triple inside a CodeableConcept.

    All three are optional in FHIR; the agent surfaces ``display`` to
    clinicians and uses ``code`` for matching, so parsing must tolerate a
    missing ``display`` (e.g. ICD-10 entries imported without text).
    """

    system: str | None = None
    code: str | None = None
    display: str | None = None


class CodeableConcept(_FhirModel):
    """A FHIR CodeableConcept — one or more ``coding`` plus a free-text fallback.

    Per FHIR R4 §2.43: at least one of ``coding`` or ``text`` should be
    populated, but neither is strictly required, so both are optional.
    The ``preferred_display()`` helper centralizes the "what should the
    UI show" decision so every call site agrees.
    """

    coding: list[Coding] = Field(default_factory=list)
    text: str | None = None

    def preferred_display(self) -> str | None:
        """Best human-readable label, or ``None`` if neither text nor a
        coding ``display`` is present.
        """
        if self.text:
            return self.text
        for c in self.coding:
            if c.display:
                return c.display
        return None

    def primary_code(self) -> str | None:
        for c in self.coding:
            if c.code:
                return c.code
        return None


class HumanName(_FhirModel):
    family: str | None = None
    given: list[str] = Field(default_factory=list)
    text: str | None = None


class Quantity(_FhirModel):
    """An Observation value-as-quantity (e.g. ``5.6 mg/dL``).

    All four fields optional per FHIR; we surface ``value`` and ``unit``
    in lab outputs so the test display reads naturally.
    """

    value: float | None = None
    unit: str | None = None
    system: str | None = None
    code: str | None = None


class Period(_FhirModel):
    start: str | None = None
    end: str | None = None


class Range(_FhirModel):
    low: Quantity | None = None
    high: Quantity | None = None


class ObservationReferenceRange(_FhirModel):
    low: Quantity | None = None
    high: Quantity | None = None
    text: str | None = None


class Annotation(_FhirModel):
    text: str | None = None


class AllergyIntoleranceReaction(_FhirModel):
    """One reaction event nested under an AllergyIntolerance.

    A single allergy resource can carry multiple reactions; the tool layer
    flattens the most clinically-relevant manifestation into the record.
    """

    manifestation: list[CodeableConcept] = Field(default_factory=list)
    severity: str | None = None
    description: str | None = None


class Attachment(_FhirModel):
    """One DocumentReference content payload.

    OpenEMR may inline data as ``data`` (base64) or by ``url``; the agent
    only reads inline notes today, so ``url`` is captured but unused.
    """

    content_type: str | None = Field(default=None, alias="contentType")
    data: str | None = None
    url: str | None = None
    title: str | None = None


class DocumentReferenceContent(_FhirModel):
    attachment: Attachment | None = None


class Reference(_FhirModel):
    """A FHIR Reference rendered as a string.

    Pydantic handles either ``{"reference": "Patient/123"}`` or a bare
    string; we keep it as a string so the rest of the codebase isn't
    forced to walk a one-field object.
    """

    reference: str | None = None
    display: str | None = None


# ---------- resource shapes ----------


class Patient(_FhirModel):
    """Subset of FHIR R4 Patient — just the demographics the agent surfaces."""

    id: str
    name: list[HumanName] = Field(default_factory=list)
    gender: str | None = None
    birth_date: str | None = Field(default=None, alias="birthDate")


class Condition(_FhirModel):
    """Subset of FHIR R4 Condition (problems list).

    OpenEMR populates ``onsetDateTime`` for most conditions; ``onsetPeriod``
    appears on date-range entries. Both flatten to a single ``onset_date``
    in the tool projection — start date wins when a period is present.
    """

    id: str
    code: CodeableConcept | None = None
    clinical_status: CodeableConcept | None = Field(default=None, alias="clinicalStatus")
    onset_date_time: str | None = Field(default=None, alias="onsetDateTime")
    onset_period: Period | None = Field(default=None, alias="onsetPeriod")


class Dosage(_FhirModel):
    text: str | None = None


class MedicationRequest(_FhirModel):
    """Subset of FHIR R4 MedicationRequest (active and recent prescriptions).

    OpenEMR uses ``medicationCodeableConcept`` for inline drug names and
    ``medicationReference`` for catalog-linked entries. The tool projection
    prefers the inline display because reference resolution would require
    a second FHIR call per medication.
    """

    id: str
    status: str | None = None
    medication_codeable_concept: CodeableConcept | None = Field(
        default=None, alias="medicationCodeableConcept"
    )
    medication_reference: Reference | None = Field(default=None, alias="medicationReference")
    authored_on: str | None = Field(default=None, alias="authoredOn")
    dosage_instruction: list[Dosage] = Field(default_factory=list, alias="dosageInstruction")

    @field_validator("dosage_instruction", mode="before")
    @classmethod
    def _drop_malformed_dosage_entries(cls, value: object) -> object:
        # OpenEMR's FHIR projection emits ``dosageInstruction: [[]]`` for
        # prescriptions with no dosage detail (Synthea-imported records hit
        # this on nearly every med). Treat non-dict entries as absent rather
        # than raising — the alternative is a TOOL_FAILURE on every meds call.
        # Already-typed ``Dosage`` instances pass through unchanged so direct
        # construction (tests, in-process callers) works the same as JSON parsing.
        if isinstance(value, list):
            return [item for item in value if isinstance(item, (dict, Dosage))]
        return value


class AllergyIntolerance(_FhirModel):
    """Subset of FHIR R4 AllergyIntolerance.

    ``criticality`` and the per-reaction ``severity`` overlap; the tool
    layer prefers ``criticality`` because it's the resource-level summary
    OpenEMR populates more reliably than per-reaction severity.
    """

    id: str
    code: CodeableConcept | None = None
    criticality: str | None = None
    clinical_status: CodeableConcept | None = Field(default=None, alias="clinicalStatus")
    reaction: list[AllergyIntoleranceReaction] = Field(default_factory=list)


class Observation(_FhirModel):
    """Subset of FHIR R4 Observation, scoped to lab-category usage.

    The agent searches ``Observation?category=laboratory`` to keep vitals
    and other categories out of the lab tool's surface; the parser
    doesn't enforce that — the search filter does. ``valueQuantity`` is
    the common case; ``valueString`` covers free-text results
    (e.g. "Negative") and ``valueCodeableConcept`` covers coded
    qualitative results.
    """

    id: str
    status: str | None = None
    code: CodeableConcept | None = None
    effective_date_time: str | None = Field(default=None, alias="effectiveDateTime")
    value_quantity: Quantity | None = Field(default=None, alias="valueQuantity")
    value_string: str | None = Field(default=None, alias="valueString")
    value_codeable_concept: CodeableConcept | None = Field(
        default=None, alias="valueCodeableConcept"
    )
    reference_range: list[ObservationReferenceRange] = Field(
        default_factory=list, alias="referenceRange"
    )


class EncounterReason(_FhirModel):
    use: list[CodeableConcept] = Field(default_factory=list)
    value: list[Reference] = Field(default_factory=list)


class Encounter(_FhirModel):
    """Subset of FHIR R4 Encounter (visit history).

    ``type`` is a list per FHIR (a visit can carry multiple type tags);
    the projection picks the first with a display. ``period.start`` is
    the visit date; entries without a period are dropped by the tool
    layer because a dateless visit can't be cited reliably.
    """

    id: str
    status: str | None = None
    type: list[CodeableConcept] = Field(default_factory=list)
    period: Period | None = None
    reason_code: list[CodeableConcept] = Field(default_factory=list, alias="reasonCode")


class DocumentReference(_FhirModel):
    """Subset of FHIR R4 DocumentReference (clinical notes).

    Note bodies arrive as base64 ``Attachment.data``; the tool layer
    decodes and surfaces them as delimited tool output (data, not
    instructions, per the prompt-injection hardening in PR 26). Tools
    skip entries where the inline attachment is missing or empty.
    """

    id: str
    status: str | None = None
    type: CodeableConcept | None = None
    date: str | None = None
    author: list[Reference] = Field(default_factory=list)
    content: list[DocumentReferenceContent] = Field(default_factory=list)


# ---------- bundle wrapper ----------


class BundleEntry(_FhirModel):
    """One entry in a FHIR Bundle response.

    The wrapped resource is kept as a raw dict because Bundles can carry
    heterogeneous resource types in a single response (e.g. ``$everything``
    operations); the FHIR client narrows to the requested resource type
    before validation, which means errors surface as a typed parse failure
    against the expected model rather than an opaque shape mismatch.
    """

    resource: dict[str, object] | None = None


class Bundle(_FhirModel):
    resource_type: str | None = Field(default=None, alias="resourceType")
    total: int | None = None
    entry: list[BundleEntry] = Field(default_factory=list)
