"""Backfill ``source_type`` and ``field_or_chunk_id`` on existing extracted
citations in ``data/extracted/*.json``.

One-shot, idempotent. Safe to re-run — fields are added with ``setdefault``,
so existing values are preserved. Operates on raw JSON (no Pydantic round
trip) so it works regardless of the schema's required-field state.

For extracted-document citations, ``field_or_chunk_id`` is a JSON-pointer-
style path to the leaf field whose ``.citation`` is being backfilled, e.g.
``observations[0].value`` or ``current_medications[2].dose``. The path is
derived from the location of the citation in the fixture, mirroring the
schema-walk path the extractor will thread through ``build_extracted_citation``
in PR 1b.

Usage::

    uv run python scripts/backfill_citation_fields.py

Run after editing ``documents/schemas/citation.py`` to add the new fields.
Commits the modified fixtures; verify the diff looks like additions of
``"source_type": "extracted_document"`` and ``"field_or_chunk_id": "<path>"``
on every populated citation block.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXTRACTED_DIR = Path(__file__).resolve().parent.parent / "data" / "extracted"


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


def _backfill_file(path: Path) -> bool:
    original = path.read_text()
    data = json.loads(original)
    _walk(data, [])
    rewritten = json.dumps(data, indent=2, ensure_ascii=False)
    if not rewritten.endswith("\n"):
        rewritten += "\n"
    if rewritten == original:
        return False
    path.write_text(rewritten)
    return True


def main() -> None:
    fixtures = sorted(EXTRACTED_DIR.glob("*.json"))
    if not fixtures:
        raise SystemExit(f"No fixtures found in {EXTRACTED_DIR}")
    changed = 0
    for fixture in fixtures:
        if _backfill_file(fixture):
            changed += 1
            print(f"backfilled: {fixture.name}")
        else:
            print(f"unchanged:  {fixture.name}")
    print(f"\n{changed}/{len(fixtures)} fixtures modified.")


if __name__ == "__main__":
    main()
