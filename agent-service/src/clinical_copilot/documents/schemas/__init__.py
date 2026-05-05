"""Document-extraction schemas (PRD2 §6).

One module per supported document type. Each module exposes a
``*Facts`` Pydantic model whose fields are ``ExtractedField[T]`` so a
field can either carry a value with a resolved citation, or carry an
abstention reason — never both, never neither (see
``citation.value_xor_abstain``).
"""

from __future__ import annotations
