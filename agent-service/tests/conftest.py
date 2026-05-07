"""Top-level pytest fixtures shared across unit + integration suites.

Single concern today: clear ``OPENAI_API_KEY`` from the test process
env so any code path that calls
:func:`clinical_copilot.corpus.embedder.default_embedder` falls into
the ``EmbedderUnavailable -> None`` branch instead of constructing a
real :class:`OpenAIEmbedder` that would (a) try to hit the live API
during tests, and (b) flip ``CorpusRetriever.hybrid_enabled`` based
on ambient developer-laptop env state.

Several tests assume "no embedder available" (see
``tests/unit/corpus/test_retriever.py::
test_retriever_falls_back_to_bm25_when_no_embedder``). Once
``OPENAI_API_KEY`` started being set in ``agent-service/.env`` to
support the corpus index rebuild, ``config.py``'s
``load_dotenv(...)`` import-time hook started leaking the key into
test processes too — those tests started building real
:class:`OpenAIEmbedder` instances and asserting against the real
``hybrid_enabled`` value, which is wrong.

Tests that explicitly want to exercise hybrid retrieval pass an
explicit ``embedder=`` argument (e.g. ``_MockEmbedder``); they're
unaffected by this fixture because the explicit arg short-circuits
the ``default_embedder()`` lookup before this matters.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True, scope="session")
def _clear_openai_api_key_in_tests() -> Iterator[None]:
    """Remove ``OPENAI_API_KEY`` from the test process env for the
    full session. Restored at session teardown so any process the
    test runner forks afterwards sees the original value (matters
    for IDE test-runner integrations that reuse the parent process)."""

    import os

    saved = os.environ.pop("OPENAI_API_KEY", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["OPENAI_API_KEY"] = saved
