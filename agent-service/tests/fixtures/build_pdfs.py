"""One-shot builder for the Week 2 demo PDF fixtures.

Run from the `agent-service/` directory::

    uv run python -m tests.fixtures.build_pdfs

Produces under ``tests/fixtures/lab_pdf/`` and
``tests/fixtures/intake_form/`` the small set of synthetic-but-realistic
PDFs the demo + initial eval cases run against. Synthetic content only —
no PHI; the demographics / values are deliberately implausible-but-typed
so a reviewer immediately knows these are not real patient documents.

The builder is committed so a grader can rebuild fixtures locally if a
font / metric change shifts a bounding box; the resulting PDFs are also
committed so the demo runs without invoking the builder first.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

FIXTURE_ROOT = Path(__file__).resolve().parent
LAB_DIR = FIXTURE_ROOT / "lab_pdf"
INTAKE_DIR = FIXTURE_ROOT / "intake_form"


# ---------------------------------------------------------------------------
# Lab PDFs
# ---------------------------------------------------------------------------


def _draw_lab_header(c: canvas.Canvas, *, title: str, accession: str) -> None:
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1 * inch, 10.2 * inch, "DEMO LABORATORIES")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.0 * inch, "1234 Synthetic Way   Phantom City, ST 99999")
    c.line(1 * inch, 9.85 * inch, 7.5 * inch, 9.85 * inch)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(1 * inch, 9.6 * inch, title)
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.4 * inch, f"Accession: {accession}")
    c.drawString(1 * inch, 9.25 * inch, "Patient: DEMO, PATIENT (synthetic)")
    c.drawString(1 * inch, 9.10 * inch, "Collected: 2025-11-12   Reported: 2025-11-12")


def _draw_lab_table(
    c: canvas.Canvas,
    rows: list[tuple[str, str, str, str, str]],
    *,
    y_start: float = 8.6 * inch,
) -> None:
    """rows: (analyte, value, unit, range, flag)."""

    headers = ("Analyte", "Result", "Units", "Reference", "Flag")
    cols_x = [1.0, 3.2, 4.5, 5.5, 7.0]

    c.setFont("Helvetica-Bold", 10)
    for header, col_x in zip(headers, cols_x, strict=True):
        c.drawString(col_x * inch, y_start, header)
    c.line(1 * inch, y_start - 0.05 * inch, 7.5 * inch, y_start - 0.05 * inch)

    c.setFont("Helvetica", 10)
    y = y_start - 0.3 * inch
    for row in rows:
        for value, col_x in zip(row, cols_x, strict=True):
            c.drawString(col_x * inch, y, value)
        y -= 0.3 * inch


def build_glucose_panel() -> Path:
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    out = LAB_DIR / "glucose_panel.pdf"
    c = canvas.Canvas(str(out), pagesize=LETTER)
    _draw_lab_header(c, title="Basic Metabolic Panel", accession="A-2025-001")
    _draw_lab_table(
        c,
        rows=[
            ("Glucose", "142", "mg/dL", "70-99", "H"),
            ("Sodium", "139", "mmol/L", "135-145", ""),
            ("Potassium", "4.1", "mmol/L", "3.5-5.0", ""),
            ("Chloride", "101", "mmol/L", "98-107", ""),
            ("BUN", "18", "mg/dL", "7-20", ""),
            ("Creatinine", "0.9", "mg/dL", "0.6-1.2", ""),
        ],
    )
    c.showPage()
    c.save()
    return out


def build_lipid_panel() -> Path:
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    out = LAB_DIR / "lipid_panel.pdf"
    c = canvas.Canvas(str(out), pagesize=LETTER)
    _draw_lab_header(c, title="Lipid Panel", accession="A-2025-002")
    _draw_lab_table(
        c,
        rows=[
            ("Total Cholesterol", "245", "mg/dL", "<200", "H"),
            ("Triglycerides", "180", "mg/dL", "<150", "H"),
            ("HDL Cholesterol", "38", "mg/dL", ">40", "L"),
            ("LDL Cholesterol", "171", "mg/dL", "<100", "H"),
            ("Non-HDL Cholesterol", "207", "mg/dL", "<130", "H"),
        ],
    )
    c.showPage()
    c.save()
    return out


# ---------------------------------------------------------------------------
# Intake forms
# ---------------------------------------------------------------------------


def _draw_intake_header(c: canvas.Canvas, *, title: str) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "DEMO CLINIC — Patient Intake Form")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 10.0 * inch, title)
    c.setFont("Helvetica", 9)
    c.drawString(1 * inch, 9.85 * inch, "Patient (synthetic): DEMO, PATIENT      DOB: 1972-03-14")
    c.line(1 * inch, 9.7 * inch, 7.5 * inch, 9.7 * inch)


def build_intake_chest_pain() -> Path:
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    out = INTAKE_DIR / "intake_chest_pain.pdf"
    c = canvas.Canvas(str(out), pagesize=LETTER)
    _draw_intake_header(c, title="Today's Visit")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 9.4 * inch, "Chief complaint:")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.2 * inch, "Chest pain x 2 days, worse with exertion.")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 8.8 * inch, "Current medications:")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 8.6 * inch, "1. metoprolol  50 mg  BID")
    c.drawString(1 * inch, 8.4 * inch, "2. atorvastatin  20 mg  daily")
    c.drawString(1 * inch, 8.2 * inch, "3. aspirin  81 mg  daily")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 7.8 * inch, "Allergies:")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 7.6 * inch, "amoxicillin — rash")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 7.2 * inch, "Pain (0-10):  6 / 10")
    c.drawString(1 * inch, 7.0 * inch, "Smoker:  Yes")
    c.drawString(1 * inch, 6.8 * inch, "Family history of heart disease:  Yes (father, MI age 58)")
    c.showPage()
    c.save()
    return out


def build_intake_nkda_annual() -> Path:
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    out = INTAKE_DIR / "intake_nkda_annual.pdf"
    c = canvas.Canvas(str(out), pagesize=LETTER)
    _draw_intake_header(c, title="Annual Physical")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 9.4 * inch, "Chief complaint:")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.2 * inch, "Annual physical, no acute concerns.")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 8.8 * inch, "Current medications:")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 8.6 * inch, "None.")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 8.2 * inch, "Allergies:")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 8.0 * inch, "NKDA")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1 * inch, 7.6 * inch, "Pain (0-10):  0 / 10")
    c.drawString(1 * inch, 7.4 * inch, "Smoker:  No")
    c.drawString(1 * inch, 7.2 * inch, "Family history of heart disease:  No")
    c.showPage()
    c.save()
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


BUILDERS = (
    build_glucose_panel,
    build_lipid_panel,
    build_intake_chest_pain,
    build_intake_nkda_annual,
)


def main() -> None:
    for builder in BUILDERS:
        path = builder()
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
