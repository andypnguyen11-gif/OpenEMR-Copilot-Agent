"""Vision-LLM document extractor (W2-03 + W2-04 demo cut).

Calls the Anthropic API with vision content blocks for each rendered
PDF page and a forced `tool_choice` set to one of two schema tools
(`lab_pdf_extraction` or `intake_form_extraction`). The VLM's
tool_use input is validated against a flat "raw" Pydantic model, then
converted to the canonical `LabPdfFacts` / `IntakeFormFacts` shape.

**Demo simplifications** vs. the full W2-03/W2-04 plan:

  * One citation per row/section instead of per field (the VLM emits
    one bbox covering the whole row; all sub-fields share it).
  * No persistent queue — the caller drives extraction synchronously.
  * Citation is validated by confidence threshold only; the OCR check
    (W2-05) is not wired in here yet.

These are documented at the boundary; downstream code (`merge`,
`tools/extracted_facts`) sees the same `ExtractedField` shape it would
in production, so the demo path is forward-compatible with the real
extractor.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, cast

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clinical_copilot.documents.fetcher import (
    RenderedPage,
    encode_jpeg_bytes,
    render_document,
)
from clinical_copilot.documents.schemas.citation import ExtractedField, SourceCitation
from clinical_copilot.documents.schemas.intake_form import (
    ActiveProblem,
    FamilyHistoryEntry,
    IntakeFormFacts,
    ReportedAllergy,
    ReportedMedication,
    SexAssignedAtBirth,
    TobaccoStatus,
)
from clinical_copilot.documents.schemas.lab_pdf import LabObservation, LabPdfFacts
from clinical_copilot.schemas.abstain import RuntimeAbstainReason

DocumentType = Literal[
    "lab_pdf",
    "intake_form",
    "referral_docx",
    "fax_tiff",
    "workbook_xlsx",
    "hl7_oru",
    "hl7_adt",
]
"""All document types the registry knows about. New types land here first
so the worker / eval runner / case manifests can reference them before
their extractor implementation is wired in. Unimplemented types raise
``UnsupportedDocumentTypeError`` (a subclass of ``ExtractorError``)."""

# Confidence below this threshold flips a value to LOW_CONFIDENCE per
# PRD2 §6. Single source so eval and runtime see the same number.
CONFIDENCE_THRESHOLD: float = 0.7

# VLM call settings. Sonnet handles vision + structured output reliably
# at this max_tokens; Haiku occasionally truncates a multi-row lab
# panel mid-tool-use, which costs more retries than the savings buy.
VLM_MAX_TOKENS: int = 4096


# ---------------------------------------------------------------------------
# Raw VLM-side schemas (flat, JSON-Schema-friendly)
# ---------------------------------------------------------------------------


class RawCitation(BaseModel):
    """One bbox + raw text the VLM claims it read.

    Per the demo cut, this citation is shared by every field of the
    parent row/section. The bbox is normalized 0..1, top-left origin,
    matching `SourceCitation`.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    bbox: list[float] = Field(min_length=4, max_length=4)
    raw_text: str


class RawLabObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display: str
    code: str | None = None
    value: float | None = None
    unit: str | None = None
    effective_date: str | None = None  # ISO YYYY-MM-DD
    reference_low: float | None = None
    reference_high: float | None = None
    flag: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    citation: RawCitation


class RawLabExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list[RawLabObservation] = Field(default_factory=list)


class RawReportedMedication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    dose: str | None = None
    frequency: str | None = None
    rxnorm: str | None = None
    started_year: int | None = None
    indication: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    citation: RawCitation


class RawReportedAllergy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    substance: str
    reaction: str | None = None
    severity: str | None = None
    rxnorm: str | None = None
    snomed: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    citation: RawCitation


class RawActiveProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: str
    icd10: str | None = None
    snomed: str | None = None
    onset_year: int | None = None
    status: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    citation: RawCitation


class RawFamilyHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relation: str
    condition: str
    onset_age: int | None = None
    status: str | None = None
    snomed: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    citation: RawCitation


class RawIntakeExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ----- Patient demographics (optional — only present if the form
    #       prints them in the personal-details section).
    legal_first_name: str | None = None
    legal_first_name_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    legal_first_name_citation: RawCitation | None = None

    legal_last_name: str | None = None
    legal_last_name_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    legal_last_name_citation: RawCitation | None = None

    date_of_birth: str | None = None  # ISO YYYY-MM-DD
    date_of_birth_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    date_of_birth_citation: RawCitation | None = None

    sex_assigned_at_birth: SexAssignedAtBirth | None = None
    sex_assigned_at_birth_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    sex_assigned_at_birth_citation: RawCitation | None = None

    medical_record_number: str | None = None
    medical_record_number_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    medical_record_number_citation: RawCitation | None = None

    phone: str | None = None
    phone_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    phone_citation: RawCitation | None = None

    email: str | None = None
    email_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    email_citation: RawCitation | None = None

    chief_complaint: str | None = None
    chief_complaint_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    chief_complaint_citation: RawCitation | None = None

    current_medications: list[RawReportedMedication] = Field(default_factory=list)
    reported_allergies: list[RawReportedAllergy] = Field(default_factory=list)
    active_problems: list[RawActiveProblem] = Field(default_factory=list)
    family_history: list[RawFamilyHistoryEntry] = Field(default_factory=list)

    pain_scale: int | None = Field(default=None, ge=0, le=10)
    pain_scale_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    pain_scale_citation: RawCitation | None = None

    tobacco_status: TobaccoStatus | None = None
    tobacco_status_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    tobacco_status_citation: RawCitation | None = None

    tobacco_pack_years: float | None = None
    tobacco_pack_years_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    tobacco_pack_years_citation: RawCitation | None = None


# ---------------------------------------------------------------------------
# Tool definitions (sent to Anthropic)
# ---------------------------------------------------------------------------


def _tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Strip Pydantic's `$defs` indirection — Anthropic accepts the
    JSON Schema as-is, but we want the inner schema flat for
    readability in eval failures."""

    return model.model_json_schema()


_LAB_TOOL: dict[str, Any] = {
    "name": "lab_pdf_extraction",
    "description": (
        "Return every observation row visible on the lab report. "
        "Each observation MUST include a citation with page (1-indexed) "
        "and a bbox normalized to 0..1 in (x0, y0, x1, y1) order with "
        "top-left origin, plus the raw_text exactly as printed inside "
        "that bbox. Confidence reflects how sure you are of the value "
        "and unit together — drop below 0.7 if the print is unclear."
    ),
    "input_schema": _tool_schema(RawLabExtraction),
}

_INTAKE_TOOL: dict[str, Any] = {
    "name": "intake_form_extraction",
    "description": (
        "Return the patient's intake-form responses. Important rules:\n"
        "  * 'NKDA' / 'no known drug allergies' / 'denies allergies' is a "
        "SINGLE reported_allergies entry with substance='NKDA' — do NOT "
        "emit an empty list.\n"
        "  * Capture every visible row of the medication, allergy, "
        "active-problem (PMH / problem list), and family-history sections. "
        "Include RxNorm / ICD-10 / SNOMED codes when the form prints them.\n"
        "  * tobacco_status is one of: 'never' / 'former' / 'current'. If "
        "the form says 'former smoker (quit YYYY, ~N pack-years)', set "
        "tobacco_status='former' AND tobacco_pack_years=N.\n"
        "  * Each row's citation has page (1-indexed), bbox (normalized "
        "0..1, top-left origin), and the raw_text exactly as printed."
    ),
    "input_schema": _tool_schema(RawIntakeExtraction),
}


# ---------------------------------------------------------------------------
# Extractor entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    document_id: str
    document_type: DocumentType
    facts: LabPdfFacts | IntakeFormFacts
    raw_tool_input: dict[str, Any]


class ExtractorError(RuntimeError):
    """Raised when the VLM call or response cannot be turned into a
    valid `*Facts` model — caller decides whether to retry or surface
    a TOOL_FAILURE abstention."""


class UnsupportedDocumentTypeError(ExtractorError):
    """Raised when the registry has no implementation for a known
    ``DocumentType``. Distinct from a generic ExtractorError so callers
    (the worker, eval runner) can map it to ``UNSUPPORTED_DOCUMENT_TYPE``
    abstention rather than a transient TOOL_FAILURE retry."""


# Per-type extractor signature. All extractors take the same kwargs even
# when they don't need a VLM client (text-based extractors like docx /
# xlsx / hl7 just ignore ``client`` and ``model``). This keeps the
# dispatch point uniform.
_ExtractorFn = Callable[..., "BaseModel"]


def extract(
    *,
    client: Anthropic,
    model: str,
    document_id: str,
    document_type: DocumentType,
    pdf_path: Path,
) -> ExtractionResult:
    """Dispatch ``document_type`` to its registered extractor.

    ``pdf_path`` is the on-disk path to the document — it is named for
    historical reasons (the first two extractors took PDFs) but accepts
    any path the dispatched extractor knows how to read (.tiff, .docx,
    .xlsx, .hl7, etc.).
    """

    extractor_fn = _EXTRACTORS.get(document_type)
    if extractor_fn is None:
        raise UnsupportedDocumentTypeError(
            f"document_type {document_type!r} is not implemented yet. "
            f"Implemented: {sorted(_EXTRACTORS)}"
        )

    facts = extractor_fn(
        client=client,
        model=model,
        document_id=document_id,
        document_path=pdf_path,
    )

    return ExtractionResult(
        document_id=document_id,
        document_type=document_type,
        facts=facts,
        raw_tool_input={"document_path": str(pdf_path)},
    )


# ---------------------------------------------------------------------------
# Per-document-type extraction
# ---------------------------------------------------------------------------


def _extract_lab_dispatch(
    *,
    client: Anthropic,
    model: str,
    document_id: str,
    document_path: Path,
) -> LabPdfFacts:
    pages = render_document(document_path)
    if not pages:
        raise ExtractorError(f"No pages rendered from {document_path}")
    return _extract_lab(
        client=client, model=model, document_id=document_id, pages=pages
    )


def _extract_intake_dispatch(
    *,
    client: Anthropic,
    model: str,
    document_id: str,
    document_path: Path,
) -> IntakeFormFacts:
    pages = render_document(document_path)
    if not pages:
        raise ExtractorError(f"No pages rendered from {document_path}")
    return _extract_intake(
        client=client, model=model, document_id=document_id, pages=pages
    )


def _stub_extractor(document_type: DocumentType, task_ref: str) -> _ExtractorFn:
    """Build a not-yet-implemented stub extractor.

    Each Week 2 multimodal expansion step replaces its stub with the
    real implementation. Until then, calling the stub raises
    ``UnsupportedDocumentTypeError`` so the eval runner / supervisor
    can map to ``UNSUPPORTED_DOCUMENT_TYPE`` abstention rather than
    crash on a malformed call.
    """

    def _impl(**_: object) -> BaseModel:
        raise UnsupportedDocumentTypeError(
            f"{document_type!r} extractor is not implemented yet ({task_ref})."
        )

    return _impl


_EXTRACTORS: dict[DocumentType, _ExtractorFn] = {
    "lab_pdf": _extract_lab_dispatch,
    "intake_form": _extract_intake_dispatch,
    "referral_docx": _stub_extractor("referral_docx", "Week 2 Step 3 (DOCX referral)"),
    "fax_tiff": _stub_extractor("fax_tiff", "Week 2 Step 2 (TIFF fax packet)"),
    "workbook_xlsx": _stub_extractor("workbook_xlsx", "Week 2 Step 5 (XLSX workbook)"),
    "hl7_oru": _stub_extractor("hl7_oru", "Week 2 Step 6 (HL7 ORU-R01)"),
    "hl7_adt": _stub_extractor("hl7_adt", "Week 2 Step 7 (HL7 ADT-A08)"),
}


def _extract_lab(
    *, client: Anthropic, model: str, document_id: str, pages: list[RenderedPage]
) -> LabPdfFacts:
    all_obs: list[LabObservation] = []
    for page in pages:
        raw = _call_vlm(
            client=client,
            model=model,
            tool=_LAB_TOOL,
            tool_model=RawLabExtraction,
            page=page,
            system_prompt=_LAB_SYSTEM_PROMPT,
        )
        for raw_obs in raw.observations:
            all_obs.append(
                _to_lab_observation(document_id=document_id, raw=raw_obs)
            )
    # Demo merge: simple concat. Dedup by (code, effective_date, value, page)
    # is in the production W2-03 merge module; this demo path expects
    # one row per (page, observation) which is what the prompt asks for.
    return LabPdfFacts(document_id=document_id, observations=all_obs)


def _extract_intake(
    *, client: Anthropic, model: str, document_id: str, pages: list[RenderedPage]
) -> IntakeFormFacts:
    # Call the VLM on every page and merge. The full W2-04 plan uses a
    # stateful page-2 fallback (only invoke page 2 when page 1 left
    # required fields empty) for cost reasons; the demo cut runs
    # every page eagerly because intake forms are short and the
    # extra call cost is bounded.
    per_page: list[RawIntakeExtraction] = []
    for page in pages:
        per_page.append(
            _call_vlm(
                client=client,
                model=model,
                tool=_INTAKE_TOOL,
                tool_model=RawIntakeExtraction,
                page=page,
                system_prompt=_INTAKE_SYSTEM_PROMPT,
            )
        )
    merged = _merge_intake_pages(per_page)
    return _to_intake_facts(document_id=document_id, raw=merged)


def _merge_intake_pages(pages: list[RawIntakeExtraction]) -> RawIntakeExtraction:
    """Merge per-page extractions into a single RawIntakeExtraction.

    Scalars: prefer the first page that has a non-None value with a
    matching citation; on ties, prefer the higher-confidence reading.
    Lists (medications, allergies): concat across pages, preserving
    page order. We do not dedupe here — the same row appearing on two
    pages is unusual on real intake forms (the form layout chunks
    sections), and over-aggressive dedup risks dropping a legitimate
    second entry that happens to share a name.
    """

    if not pages:
        return RawIntakeExtraction()
    if len(pages) == 1:
        return pages[0]

    def pick_best[T](
        candidates: list[tuple[T | None, float | None, RawCitation | None]],
    ) -> tuple[T | None, float | None, RawCitation | None]:
        best: tuple[T | None, float | None, RawCitation | None] = (None, None, None)
        for value, conf, cite in candidates:
            if value is None or cite is None:
                continue
            if best[0] is None:
                best = (value, conf, cite)
                continue
            if conf is not None and best[1] is not None and conf > best[1]:
                best = (value, conf, cite)
        return best

    cc_v, cc_c, cc_cite = pick_best(
        [
            (p.chief_complaint, p.chief_complaint_confidence, p.chief_complaint_citation)
            for p in pages
        ]
    )
    fname_v, fname_c, fname_cite = pick_best(
        [
            (p.legal_first_name, p.legal_first_name_confidence, p.legal_first_name_citation)
            for p in pages
        ]
    )
    lname_v, lname_c, lname_cite = pick_best(
        [
            (p.legal_last_name, p.legal_last_name_confidence, p.legal_last_name_citation)
            for p in pages
        ]
    )
    dob_v, dob_c, dob_cite = pick_best(
        [(p.date_of_birth, p.date_of_birth_confidence, p.date_of_birth_citation) for p in pages]
    )
    sex_v, sex_c, sex_cite = pick_best(
        [
            (
                p.sex_assigned_at_birth,
                p.sex_assigned_at_birth_confidence,
                p.sex_assigned_at_birth_citation,
            )
            for p in pages
        ]
    )
    mrn_v, mrn_c, mrn_cite = pick_best(
        [
            (
                p.medical_record_number,
                p.medical_record_number_confidence,
                p.medical_record_number_citation,
            )
            for p in pages
        ]
    )
    phone_v, phone_c, phone_cite = pick_best(
        [(p.phone, p.phone_confidence, p.phone_citation) for p in pages]
    )
    email_v, email_c, email_cite = pick_best(
        [(p.email, p.email_confidence, p.email_citation) for p in pages]
    )
    pain_v, pain_c, pain_cite = pick_best(
        [(p.pain_scale, p.pain_scale_confidence, p.pain_scale_citation) for p in pages]
    )
    tobacco_v, tobacco_c, tobacco_cite = pick_best(
        [
            (p.tobacco_status, p.tobacco_status_confidence, p.tobacco_status_citation)
            for p in pages
        ]
    )
    pack_v, pack_c, pack_cite = pick_best(
        [
            (
                p.tobacco_pack_years,
                p.tobacco_pack_years_confidence,
                p.tobacco_pack_years_citation,
            )
            for p in pages
        ]
    )

    medications: list[RawReportedMedication] = []
    allergies: list[RawReportedAllergy] = []
    problems: list[RawActiveProblem] = []
    family: list[RawFamilyHistoryEntry] = []
    for p in pages:
        medications.extend(p.current_medications)
        allergies.extend(p.reported_allergies)
        problems.extend(p.active_problems)
        family.extend(p.family_history)

    return RawIntakeExtraction(
        legal_first_name=fname_v,
        legal_first_name_confidence=fname_c,
        legal_first_name_citation=fname_cite,
        legal_last_name=lname_v,
        legal_last_name_confidence=lname_c,
        legal_last_name_citation=lname_cite,
        date_of_birth=dob_v,
        date_of_birth_confidence=dob_c,
        date_of_birth_citation=dob_cite,
        sex_assigned_at_birth=sex_v,
        sex_assigned_at_birth_confidence=sex_c,
        sex_assigned_at_birth_citation=sex_cite,
        medical_record_number=mrn_v,
        medical_record_number_confidence=mrn_c,
        medical_record_number_citation=mrn_cite,
        phone=phone_v,
        phone_confidence=phone_c,
        phone_citation=phone_cite,
        email=email_v,
        email_confidence=email_c,
        email_citation=email_cite,
        chief_complaint=cc_v,
        chief_complaint_confidence=cc_c,
        chief_complaint_citation=cc_cite,
        current_medications=medications,
        reported_allergies=allergies,
        active_problems=problems,
        family_history=family,
        pain_scale=pain_v,
        pain_scale_confidence=pain_c,
        pain_scale_citation=pain_cite,
        tobacco_status=tobacco_v,
        tobacco_status_confidence=tobacco_c,
        tobacco_status_citation=tobacco_cite,
        tobacco_pack_years=pack_v,
        tobacco_pack_years_confidence=pack_c,
        tobacco_pack_years_citation=pack_cite,
    )


# ---------------------------------------------------------------------------
# Anthropic call
# ---------------------------------------------------------------------------


def _call_vlm[T: BaseModel](
    *,
    client: Anthropic,
    model: str,
    tool: dict[str, Any],
    tool_model: type[T],
    page: RenderedPage,
    system_prompt: str,
) -> T:
    image_bytes = encode_jpeg_bytes(page.image)
    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"This is page {page.page_number} of the document. "
                        "Call the extraction tool with everything you can read."
                    ),
                },
            ],
        }
    ]

    response = client.messages.create(
        model=model,
        max_tokens=VLM_MAX_TOKENS,
        system=system_prompt,
        tools=cast(Any, [tool]),
        tool_choice=cast(Any, {"type": "tool", "name": tool["name"]}),
        messages=cast(Any, messages),
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            input_dict = dict(getattr(block, "input", {}) or {})
            try:
                return tool_model.model_validate(input_dict)
            except ValidationError as exc:
                raise ExtractorError(
                    f"VLM emitted tool_use input that failed schema validation: {exc}"
                ) from exc

    raise ExtractorError(
        "VLM response contained no tool_use block — cannot extract structured facts."
    )


# ---------------------------------------------------------------------------
# Raw → ExtractedField conversion
# ---------------------------------------------------------------------------


def _to_lab_observation(*, document_id: str, raw: RawLabObservation) -> LabObservation:
    cite = _to_source_citation(document_id=document_id, raw=raw.citation)
    low_conf = raw.confidence < CONFIDENCE_THRESHOLD

    parsed_date: date | None
    try:
        parsed_date = (
            datetime.strptime(raw.effective_date, "%Y-%m-%d").date()
            if raw.effective_date
            else None
        )
    except ValueError:
        parsed_date = None

    return LabObservation(
        code=_field(raw.code, cite, low_conf),
        display=_field(raw.display, cite, low_conf),
        value=_field(raw.value, cite, low_conf),
        unit=_field(raw.unit, cite, low_conf),
        effective_date=_field(parsed_date, cite, low_conf),
        reference_low=_optional_field(raw.reference_low, cite, low_conf),
        reference_high=_optional_field(raw.reference_high, cite, low_conf),
        flag=_optional_field(raw.flag, cite, low_conf),
    )


def _to_intake_facts(*, document_id: str, raw: RawIntakeExtraction) -> IntakeFormFacts:
    chief_complaint = _build_field(
        document_id=document_id,
        value=raw.chief_complaint,
        confidence=raw.chief_complaint_confidence,
        citation=raw.chief_complaint_citation,
    )

    pain_scale = _build_optional_field(
        document_id=document_id,
        value=raw.pain_scale,
        confidence=raw.pain_scale_confidence,
        citation=raw.pain_scale_citation,
    )
    tobacco_status = _build_optional_field(
        document_id=document_id,
        value=raw.tobacco_status,
        confidence=raw.tobacco_status_confidence,
        citation=raw.tobacco_status_citation,
    )
    tobacco_pack_years = _build_optional_field(
        document_id=document_id,
        value=raw.tobacco_pack_years,
        confidence=raw.tobacco_pack_years_confidence,
        citation=raw.tobacco_pack_years_citation,
    )

    medications: list[ReportedMedication] = []
    for raw_med in raw.current_medications:
        cite = _to_source_citation(document_id=document_id, raw=raw_med.citation)
        low_conf = raw_med.confidence < CONFIDENCE_THRESHOLD
        medications.append(
            ReportedMedication(
                name=_field(raw_med.name, cite, low_conf),
                dose=_optional_field(raw_med.dose, cite, low_conf),
                frequency=_optional_field(raw_med.frequency, cite, low_conf),
                rxnorm=_optional_field(raw_med.rxnorm, cite, low_conf),
                started_year=_optional_field(raw_med.started_year, cite, low_conf),
                indication=_optional_field(raw_med.indication, cite, low_conf),
            )
        )

    allergies: list[ReportedAllergy] = []
    for raw_alg in raw.reported_allergies:
        cite = _to_source_citation(document_id=document_id, raw=raw_alg.citation)
        low_conf = raw_alg.confidence < CONFIDENCE_THRESHOLD
        allergies.append(
            ReportedAllergy(
                substance=_field(raw_alg.substance, cite, low_conf),
                reaction=_optional_field(raw_alg.reaction, cite, low_conf),
                severity=_optional_field(raw_alg.severity, cite, low_conf),
                rxnorm=_optional_field(raw_alg.rxnorm, cite, low_conf),
                snomed=_optional_field(raw_alg.snomed, cite, low_conf),
            )
        )

    problems: list[ActiveProblem] = []
    for raw_prob in raw.active_problems:
        cite = _to_source_citation(document_id=document_id, raw=raw_prob.citation)
        low_conf = raw_prob.confidence < CONFIDENCE_THRESHOLD
        problems.append(
            ActiveProblem(
                condition=_field(raw_prob.condition, cite, low_conf),
                icd10=_optional_field(raw_prob.icd10, cite, low_conf),
                snomed=_optional_field(raw_prob.snomed, cite, low_conf),
                onset_year=_optional_field(raw_prob.onset_year, cite, low_conf),
                status=_optional_field(raw_prob.status, cite, low_conf),
            )
        )

    family: list[FamilyHistoryEntry] = []
    for raw_fh in raw.family_history:
        cite = _to_source_citation(document_id=document_id, raw=raw_fh.citation)
        low_conf = raw_fh.confidence < CONFIDENCE_THRESHOLD
        family.append(
            FamilyHistoryEntry(
                relation=_field(raw_fh.relation, cite, low_conf),
                condition=_field(raw_fh.condition, cite, low_conf),
                onset_age=_optional_field(raw_fh.onset_age, cite, low_conf),
                status=_optional_field(raw_fh.status, cite, low_conf),
                snomed=_optional_field(raw_fh.snomed, cite, low_conf),
            )
        )

    legal_first_name = _build_optional_field(
        document_id=document_id,
        value=raw.legal_first_name,
        confidence=raw.legal_first_name_confidence,
        citation=raw.legal_first_name_citation,
    )
    legal_last_name = _build_optional_field(
        document_id=document_id,
        value=raw.legal_last_name,
        confidence=raw.legal_last_name_confidence,
        citation=raw.legal_last_name_citation,
    )

    parsed_dob: date | None
    if raw.date_of_birth:
        try:
            parsed_dob = datetime.strptime(raw.date_of_birth, "%Y-%m-%d").date()
        except ValueError:
            parsed_dob = None
    else:
        parsed_dob = None
    date_of_birth_field = _build_optional_field(
        document_id=document_id,
        value=parsed_dob,
        confidence=raw.date_of_birth_confidence,
        citation=raw.date_of_birth_citation,
    )

    sex_field = _build_optional_field(
        document_id=document_id,
        value=raw.sex_assigned_at_birth,
        confidence=raw.sex_assigned_at_birth_confidence,
        citation=raw.sex_assigned_at_birth_citation,
    )
    mrn_field = _build_optional_field(
        document_id=document_id,
        value=raw.medical_record_number,
        confidence=raw.medical_record_number_confidence,
        citation=raw.medical_record_number_citation,
    )
    phone_field = _build_optional_field(
        document_id=document_id,
        value=raw.phone,
        confidence=raw.phone_confidence,
        citation=raw.phone_citation,
    )
    email_field = _build_optional_field(
        document_id=document_id,
        value=raw.email,
        confidence=raw.email_confidence,
        citation=raw.email_citation,
    )

    return IntakeFormFacts(
        document_id=document_id,
        legal_first_name=legal_first_name,
        legal_last_name=legal_last_name,
        date_of_birth=date_of_birth_field,
        sex_assigned_at_birth=sex_field,
        medical_record_number=mrn_field,
        phone=phone_field,
        email=email_field,
        chief_complaint=chief_complaint,
        current_medications=medications,
        reported_allergies=allergies,
        active_problems=problems,
        family_history=family,
        pain_scale=pain_scale,
        tobacco_status=tobacco_status,
        tobacco_pack_years=tobacco_pack_years,
    )


# ---------------------------------------------------------------------------
# Field-construction helpers
# ---------------------------------------------------------------------------


def _to_source_citation(
    *, document_id: str, raw: RawCitation
) -> SourceCitation | None:
    """Build a SourceCitation, returning None on degenerate VLM bboxes.

    The VLM occasionally returns a zero-area bbox or a bbox with
    out-of-range coords for fields it didn't actually see (e.g. when
    asked about tobacco status on a form that doesn't have a tobacco
    section). Catching the SourceCitation validator here lets the
    caller drop to a CITATION_INVALID abstain rather than crash the
    whole extraction.
    """

    try:
        return SourceCitation(
            document_id=document_id,
            page=raw.page,
            bbox=cast(tuple[float, float, float, float], tuple(raw.bbox)),
            confidence=1.0,  # Per-citation confidence isn't surfaced in the demo;
            # the per-field confidence threshold runs upstream.
            raw_text=raw.raw_text,
        )
    except ValidationError:
        return None


def _field[T](
    value: T | None, cite: SourceCitation | None, low_conf: bool
) -> ExtractedField[T]:
    """Required field: missing, low-confidence, or invalid-citation → abstain."""

    if value is None:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.NO_DATA)
    if cite is None:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.CITATION_INVALID)
    if low_conf:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.LOW_CONFIDENCE)
    return ExtractedField[T](value=value, citation=cite)


def _optional_field[T](
    value: T | None, cite: SourceCitation | None, low_conf: bool
) -> ExtractedField[T] | None:
    """Optional field: missing → None; low-conf or invalid-citation → abstain."""

    if value is None:
        return None
    if cite is None:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.CITATION_INVALID)
    if low_conf:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.LOW_CONFIDENCE)
    return ExtractedField[T](value=value, citation=cite)


def _build_field[T](
    *,
    document_id: str,
    value: T | None,
    confidence: float | None,
    citation: RawCitation | None,
) -> ExtractedField[T]:
    if value is None or citation is None or confidence is None:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.NO_DATA)
    cite = _to_source_citation(document_id=document_id, raw=citation)
    if cite is None:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.CITATION_INVALID)
    if confidence < CONFIDENCE_THRESHOLD:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.LOW_CONFIDENCE)
    return ExtractedField[T](value=value, citation=cite)


def _build_optional_field[T](
    *,
    document_id: str,
    value: T | None,
    confidence: float | None,
    citation: RawCitation | None,
) -> ExtractedField[T] | None:
    if value is None or citation is None or confidence is None:
        return None
    cite = _to_source_citation(document_id=document_id, raw=citation)
    if cite is None:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.CITATION_INVALID)
    if confidence < CONFIDENCE_THRESHOLD:
        return ExtractedField[T](abstain_reason=RuntimeAbstainReason.LOW_CONFIDENCE)
    return ExtractedField[T](value=value, citation=cite)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_LAB_SYSTEM_PROMPT = (
    "You are a medical document extractor. The image is one page of a "
    "lab result PDF. Read every observation row and return them via "
    "the lab_pdf_extraction tool. Be exact with units and decimal "
    "places. If a value is illegible or ambiguous, lower the confidence "
    "field for that row (below 0.7 will mark the row as low-confidence "
    "and drop it from rendered facts). Bounding boxes are normalized to "
    "0..1 in (x0, y0, x1, y1) with top-left origin. Each observation's "
    "bbox should cover the entire row (analyte name through flag column)."
)

_INTAKE_SYSTEM_PROMPT = (
    "You are a medical document extractor. The image is one page of a "
    "patient intake form. Return the patient's responses via the "
    "intake_form_extraction tool. Capture every visible row of every "
    "section — patient demographics, chief complaint, current "
    "medications, allergies, active problems / past medical history, "
    "family history. If a section is not present on this page, leave "
    "its list empty or its scalar field null; do not invent rows.\n\n"
    "Demographics: when the personal-details section prints them, "
    "capture legal_first_name, legal_last_name, date_of_birth (ISO "
    "YYYY-MM-DD), sex_assigned_at_birth (one of 'Female' / 'Male' / "
    "'Other' / 'Unknown'), medical_record_number, phone, email. "
    "If the form prints a single 'Full Name' field, split it into "
    "first/last using natural English convention.\n\n"
    "Codes: include rxnorm on each medication when the form prints it; "
    "include icd10 / snomed on each active_problem when printed; "
    "include snomed on family_history rows when printed.\n\n"
    "Allergies: 'NKDA' / 'no known drug allergies' / 'denies allergies' "
    "is a SINGLE reported_allergies entry with substance='NKDA' — never "
    "emit an empty allergies list in that case (an empty list means the "
    "page is silent on the question, which is different).\n\n"
    "Tobacco: tobacco_status is one of 'never' / 'former' / 'current'. "
    "If the form says e.g. 'former smoker (quit 2008, ~12 pack-years)', "
    "set tobacco_status='former' AND tobacco_pack_years=12. If only "
    "current/former/never is checked without pack-years, leave "
    "tobacco_pack_years null.\n\n"
    "Bounding boxes: normalized 0..1 in (x0, y0, x1, y1) with top-left "
    "origin; each row's bbox should cover the printed row including its "
    "label cell and value cell."
)
