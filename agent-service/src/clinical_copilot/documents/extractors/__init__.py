"""Per-format extractor modules invoked through the registry in
:mod:`clinical_copilot.documents.extractor`.

Lab and intake extractors still live in the parent ``extractor.py``
because they share the VLM call helpers; new text-based extractors
(referral, workbook, hl7) live here so each stays focused on one
parsing strategy.
"""
