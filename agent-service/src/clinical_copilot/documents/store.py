"""JSON-on-disk persistence for the W2 demo cut.

The full W2-03 plan persists `extracted_facts` to agent-db (one row
per field, alembic-managed table). For tonight's local-ingest demo we
write the whole `*Facts` model to a JSON file under `data/extracted/`.

Same keying (`document_id`) and same Pydantic shape, so swapping the
backend in the production MR is a write-site-only change — read sites
already use Pydantic round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from clinical_copilot.documents.schemas.fax_tiff import FaxTiffFacts
from clinical_copilot.documents.schemas.intake_form import IntakeFormFacts
from clinical_copilot.documents.schemas.lab_pdf import LabPdfFacts

# `data/` is gitignored at the repo level; demo runs persist here.
_DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[3] / "data" / "extracted"

# Each multimodal-expansion step adds its facts class to this union as
# its extractor lands. The TypeAdapter validates round-trips, so unknown
# top-level shapes raise on read rather than silently re-typing as the
# wrong model — preventing a fax-packet write from being read back as
# an empty LabPdfFacts.
_FactsUnion = LabPdfFacts | IntakeFormFacts | FaxTiffFacts
_ADAPTER: TypeAdapter[_FactsUnion] = TypeAdapter(_FactsUnion)


def store_root() -> Path:
    return _DEFAULT_STORE_ROOT


def write(facts: _FactsUnion, *, root: Path | None = None) -> Path:
    """Write `facts` as JSON, return the path. Overwrites prior version."""

    target_root = root or _DEFAULT_STORE_ROOT
    target_root.mkdir(parents=True, exist_ok=True)
    path = target_root / f"{facts.document_id}.json"
    path.write_text(facts.model_dump_json(indent=2), encoding="utf-8")
    return path


def read(document_id: str, *, root: Path | None = None) -> _FactsUnion | None:
    """Read previously written facts, or None if no row exists."""

    target_root = root or _DEFAULT_STORE_ROOT
    path = target_root / f"{document_id}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _ADAPTER.validate_python(raw)
