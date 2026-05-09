"""Backfill ``source_type`` and ``field_or_chunk_id`` on extracted citations
in JSON files containing the legacy ``SourceCitation`` shape.

One-shot, idempotent. Safe to re-run — fields are added with ``setdefault``,
so existing values are preserved. Operates on raw JSON (no Pydantic round
trip) so it works regardless of the schema's required-field state.

For extracted-document citations, ``field_or_chunk_id`` is a JSON-pointer-
style path to the leaf field whose ``.citation`` is being backfilled, e.g.
``observations[0].value`` or ``current_medications[2].dose``. The path is
derived from the location of the citation in the file, mirroring the
schema-walk path the extractor threads through ``build_extracted_citation``.

Two consumer surfaces need backfilling when the SourceCitation schema gains
``field_or_chunk_id``:

* Runtime extraction cache at ``data/extracted/`` (default target). These
  files are gitignored — backfilling locally upgrades a developer's cache
  so a re-extraction doesn't have to be triggered.
* Cached eval predictions at ``evals/extraction/predictions/`` (use
  ``--predictions-dir``). These ARE tracked. Without this backfill the
  pre-push eval gate fails ``schema_valid`` and ``citation_present``
  because the rubric round-trips every cached citation through Pydantic.
  Predictions wrap their facts under a top-level ``"facts"`` key; the
  walker descends into that wrapper so leaf paths land cleanly without a
  ``facts.`` prefix.

Usage::

    # default — runtime extraction cache
    uv run python scripts/backfill_citation_fields.py

    # cached eval predictions
    uv run python scripts/backfill_citation_fields.py --predictions-dir
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_AGENT_SERVICE = Path(__file__).resolve().parent.parent
EXTRACTED_DIR = _AGENT_SERVICE / "data" / "extracted"
PREDICTIONS_DIR = _AGENT_SERVICE / "evals" / "extraction" / "predictions"


def _render_path(parents: list[str | int]) -> str:
    """Render a parents stack into JSON-pointer-like dot/bracket notation.

    ``["observations", 0, "display"]`` -> ``"observations[0].display"``.
    """
    parts: list[str] = []
    for component in parents:
        if isinstance(component, int):
            if not parts:
                parts.append(f"[{component}]")
            else:
                parts[-1] = f"{parts[-1]}[{component}]"
        else:
            parts.append(component)
    return ".".join(parts)


def _walk(node: Any, parents: list[str | int]) -> None:
    if isinstance(node, dict):
        citation = node.get("citation")
        if isinstance(citation, dict):
            citation.setdefault("source_type", "extracted_document")
            citation.setdefault("field_or_chunk_id", _render_path(parents))
        for key, value in node.items():
            if key == "citation":
                continue
            _walk(value, [*parents, key])
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, [*parents, index])


def _backfill_file(path: Path, *, facts_wrapper: bool) -> bool:
    """Backfill one JSON file in place.

    When ``facts_wrapper`` is True the walker descends into the top-level
    ``"facts"`` key (the eval-prediction layout) so derived paths look
    like ``"chief_complaint"`` rather than ``"facts.chief_complaint"``.
    """

    original = path.read_text()
    data = json.loads(original)
    if facts_wrapper and isinstance(data, dict) and isinstance(data.get("facts"), dict):
        _walk(data["facts"], [])
    else:
        _walk(data, [])
    rewritten = json.dumps(data, indent=2, ensure_ascii=False)
    if not rewritten.endswith("\n"):
        rewritten += "\n"
    if rewritten == original:
        return False
    path.write_text(rewritten)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--predictions-dir",
        action="store_true",
        help=(
            "Operate on the cached eval predictions tree "
            "(evals/extraction/predictions/) instead of data/extracted/."
        ),
    )
    args = parser.parse_args()

    if args.predictions_dir:
        target_dir = PREDICTIONS_DIR
        glob_pattern = "**/*.json"
        facts_wrapper = True
    else:
        target_dir = EXTRACTED_DIR
        glob_pattern = "*.json"
        facts_wrapper = False

    fixtures = sorted(target_dir.glob(glob_pattern))
    if not fixtures:
        raise SystemExit(f"No JSON files found under {target_dir}")
    changed = 0
    for fixture in fixtures:
        if _backfill_file(fixture, facts_wrapper=facts_wrapper):
            changed += 1
            print(f"backfilled: {fixture.relative_to(target_dir)}")
        else:
            print(f"unchanged:  {fixture.relative_to(target_dir)}")
    print(f"\n{changed}/{len(fixtures)} files modified.")


if __name__ == "__main__":
    main()
