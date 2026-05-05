"""End-to-end test for the BM25 retriever against a tiny tmpdir corpus."""

from __future__ import annotations

import textwrap
from pathlib import Path

import numpy as np
import pytest

from clinical_copilot.corpus.embedder import _l2_normalize
from clinical_copilot.corpus.index import build_index
from clinical_copilot.corpus.retriever import CorpusRetriever


@pytest.fixture
def tmp_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Build a 2-doc corpus + index inside `tmp_path`."""

    sources = tmp_path / "sources"
    (sources / "uspstf").mkdir(parents=True)
    (sources / "cdc").mkdir(parents=True)

    (sources / "uspstf" / "lung.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Lung cancer screening (test)
            source: USPSTF
            source_url: https://example.test/lung
            date: "2021-03-09"
            topics: [lung cancer, screening]
            ---
            The Task Force recommends annual low-dose CT screening
            for lung cancer in eligible high-risk adults. Eligibility
            requires a 20 pack-year smoking history. Screening
            reduces lung cancer mortality.
            """
        ),
        encoding="utf-8",
    )

    (sources / "cdc" / "flu.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Influenza vaccine (test)
            source: CDC
            source_url: https://example.test/flu
            date: "2024-09-01"
            topics: [vaccines, influenza]
            ---
            Annual influenza vaccination is recommended for all
            adults. Any licensed flu vaccine appropriate for the
            patient's age is acceptable. Vaccinate in September or
            October when possible.
            """
        ),
        encoding="utf-8",
    )

    data_dir = tmp_path / "data"
    build_index(sources_dir=sources, data_dir=data_dir)
    return sources, data_dir


def test_retriever_returns_top_hit_for_lung_query(tmp_corpus: tuple[Path, Path]) -> None:
    _, data_dir = tmp_corpus
    retriever = CorpusRetriever(bm25_path=data_dir / "bm25.pkl")
    hits = retriever.retrieve("low-dose CT lung cancer screening", k=2)
    assert hits, "BM25 should return at least one hit"
    assert hits[0].source == "USPSTF"
    assert "lung" in hits[0].text.lower()
    assert hits[0].source_url == "https://example.test/lung"


def test_retriever_returns_flu_for_vaccine_query(tmp_corpus: tuple[Path, Path]) -> None:
    _, data_dir = tmp_corpus
    retriever = CorpusRetriever(bm25_path=data_dir / "bm25.pkl")
    hits = retriever.retrieve("influenza vaccination annual adults", k=2)
    assert hits[0].source == "CDC"


def test_retriever_drops_zero_score_hits(tmp_corpus: tuple[Path, Path]) -> None:
    _, data_dir = tmp_corpus
    retriever = CorpusRetriever(bm25_path=data_dir / "bm25.pkl")
    # No tokens overlap the corpus → all chunks score 0 and are dropped.
    hits = retriever.retrieve("zzzzz qqqq", k=5)
    assert hits == []


# ---------------------------------------------------------------------------
# Hybrid (BM25 + dense) tests using a deterministic mock embedder
# ---------------------------------------------------------------------------


class _MockEmbedder:
    """Deterministic embedder for tests.

    Maps any string to a 4-dim vector by counting hits in synonym
    groups — words within a group share a dimension. This lets the
    embedding space connect a query word to a chunk word that
    BM25's exact-token tokenizer can't (e.g., "immunization" in the
    query maps to the same dim as "vaccine" / "vaccination" in the
    chunk).
    """

    dim: int = 4
    # Each tuple is a "synonym group"; words in the same group fire
    # the same dimension. Designed so the test query "immunization"
    # connects to the CDC chunk via "vaccine" / "vaccination".
    _GROUPS: tuple[tuple[str, ...], ...] = (
        ("vaccine", "vaccination", "immunization", "shot"),  # dim 0
        ("ct", "low-dose", "scan"),                          # dim 1
        ("lung", "pulmonary"),                                # dim 2
        ("smoking", "tobacco"),                                # dim 3
    )

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            low = t.lower()
            for j, group in enumerate(self._GROUPS):
                for kw in group:
                    if kw in low:
                        out[i, j] += 1.0
        return _l2_normalize(out)


@pytest.fixture
def tmp_corpus_with_dense(tmp_path: Path) -> Path:
    """Build the same 2-doc corpus, but also build the dense index
    using the mock embedder. Returns the data dir."""

    sources = tmp_path / "sources"
    (sources / "uspstf").mkdir(parents=True)
    (sources / "cdc").mkdir(parents=True)
    (sources / "uspstf" / "lung.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Lung cancer screening (test)
            source: USPSTF
            source_url: https://example.test/lung
            date: "2021-03-09"
            topics: [lung cancer, screening]
            ---
            The Task Force recommends annual low-dose CT screening
            for lung cancer in eligible high-risk adults. Eligibility
            requires a 20 pack-year smoking history.
            """
        ),
        encoding="utf-8",
    )
    (sources / "cdc" / "flu.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Influenza vaccine (test)
            source: CDC
            source_url: https://example.test/flu
            date: "2024-09-01"
            topics: [vaccines, influenza]
            ---
            Annual influenza vaccination is recommended for all
            adults. Any licensed flu vaccine appropriate for the
            patient's age is acceptable.
            """
        ),
        encoding="utf-8",
    )

    data_dir = tmp_path / "data"
    result = build_index(
        sources_dir=sources, data_dir=data_dir, embedder=_MockEmbedder()
    )
    assert result["dense_path"] is not None
    return data_dir


def test_dense_path_retrieves_paraphrase_bm25_misses(
    tmp_corpus_with_dense: Path,
) -> None:
    """A query whose tokens don't overlap any corpus chunk should
    still surface the right chunk via the dense path."""

    retriever = CorpusRetriever(
        bm25_path=tmp_corpus_with_dense / "bm25.pkl",
        dense_path=tmp_corpus_with_dense / "dense.pkl",
        embedder=_MockEmbedder(),
    )
    assert retriever.hybrid_enabled is True

    # The query is a single word that's not in any chunk's tokens
    # (so BM25 returns nothing) but is in the same embedding synonym
    # group as "vaccine" / "vaccination" in the CDC chunk. Hybrid
    # retrieval should still surface CDC via the dense path.
    hits = retriever.retrieve("immunization", k=2)
    assert hits, "hybrid should surface the CDC chunk via dense path"
    assert hits[0].source == "CDC"


def test_retriever_falls_back_to_bm25_when_no_embedder(
    tmp_corpus_with_dense: Path,
) -> None:
    """If a dense file exists but no embedder is configured, the
    retriever logs and degrades to BM25-only — same public API."""

    retriever = CorpusRetriever(
        bm25_path=tmp_corpus_with_dense / "bm25.pkl",
        dense_path=tmp_corpus_with_dense / "dense.pkl",
        embedder=None,
        # default_embedder() will return None in this test env
        # (no OPENAI_API_KEY); that's the case we care about.
    )
    assert retriever.hybrid_enabled is False
    # BM25 path still works for an in-vocab query.
    hits = retriever.retrieve("low-dose CT lung", k=2)
    assert hits and hits[0].source == "USPSTF"
