"""Tests for the corpus PHI-shape scrub."""

from __future__ import annotations

import pytest

from clinical_copilot.corpus.scrub import PhiInCorpusError, scrub_or_raise


def test_clean_text_passes() -> None:
    scrub_or_raise(
        source_doc_id="d",
        text="USPSTF recommends screening for colorectal cancer at age 45.",
    )


@pytest.mark.parametrize(
    "tainted",
    [
        "Contact: 555-123-4567 for questions.",
        "patient SSN 123-45-6789",
        "Email the lab at someone@example.com for results.",
        "MRN: 1234567 was opened in 2021.",
    ],
)
def test_phi_shapes_raise(tainted: str) -> None:
    with pytest.raises(PhiInCorpusError):
        scrub_or_raise(source_doc_id="d", text=tainted)
