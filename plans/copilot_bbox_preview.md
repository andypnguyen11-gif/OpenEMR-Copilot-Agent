# Side-by-side bbox preview panel for the Co-Pilot review pages

## Context

The multimodal extractor produces per-field `SourceCitation` records with
normalized 0–1 bboxes (`agent-service/src/clinical_copilot/documents/schemas/citation.py`).
The current `intake_review.php` and `lab_review.php` pages render those
citations as text only ("p.1: <raw>") next to each form field. That gives
the clinician zero way to see *what was missed* — fields the VLM didn't
extract are simply absent from the form, with no visual cue that the
extractor skipped them.

This change adds a side-by-side preview of the source PDF/PNG with
color-coded overlay rectangles:

* **Green** = the field was extracted AND has a chart write-path in the
  corresponding save handler (`new_patient_save_ai.php` for intake,
  `lab_save_ai.php` for lab).
* **Yellow** = the field was extracted but no chart write-path exists
  (e.g. `family_history` and `pain_scale` on intake — collected but not
  persisted today).
* **Missing entirely** = no bbox renders, so the clinician can see at a
  glance which sections of the form the extractor didn't read.

Hovering or focusing a form field on the left highlights its
corresponding bbox on the right and scrolls it into view. One-way
interaction only (form → bbox); clicking a bbox does not jump back.

User decisions applied:
* Scope: **intake + lab** in one pass.
* Interaction: **one-way** (form field → bbox).

## Architecture

```
┌─────────────── intake_review.php ────────────────┐  ┌──── source PDF/PNG preview ────┐
│  Demographics                                     │  │                                 │
│  ┌─ First name [Margaret      ] (cite p.1)  <──── │──┤  ┌──┐                            │
│  └─ Last name  [Chen          ] (cite p.1)        │  │  │GR│ Margaret      page 1       │
│                                                   │  │  └──┘                            │
│  Active problems                                  │  │  ┌────┐                          │
│  ┌─ Type 2 DM ICD-10 E11.9   <─────────────────── │──┤  │GRN │ Chen                     │
│                                                   │  │  └────┘                          │
│  Family history (NOT mapped → yellow boxes)       │  │  ┌────┐                          │
│  ┌─ Father MI age 61   <───────────────────────── │──┤  │YEL │ Father                   │
└───────────────────────────────────────────────────┘  └────────────────────────────────┘
```

The preview panel renders one `<img>` per page, stacked vertically in a
scroll container. Each bbox is an absolute-positioned `<div>` inside the
page wrapper, sized via percentage coordinates (matches the normalized
bbox). On hover/focus of a form field with `data-citation-id="..."`, the
corresponding bbox `<div>` with the same `data-citation-id` gets a
highlight class and `scrollIntoView({block: 'center'})`.

## Files to create / edit

### Agent-service (Python)

**EDIT — `agent-service/src/clinical_copilot/documents/store.py`**

Add a parallel blob-store keyed on the same `document_id`:

* `BLOB_DIR = Path(...) / "data" / "blobs"`
* `write_blob(document_id: str, content: bytes, suffix: str) -> Path` — writes `data/blobs/<safe-id>.<ext>`. Replace `:` with `_` in safe-id so colons in `openemr:doc:1234` work cross-platform.
* `read_blob(document_id: str) -> tuple[bytes, str] | None` — returns `(bytes, suffix)` or None.
* Same-suffix overwrite on re-ingest is intentional: the most recent upload wins.

**EDIT — `agent-service/src/clinical_copilot/main.py`**

In the existing `POST /api/agent/internal/ingest` handler, after reading
`contents = await file.read()` and before deleting the temp, also call
`facts_store.write_blob(document_id, contents, suffix)`.

Add new route `GET /api/agent/internal/document/{document_id}/page/{page_number}.jpg`:

* `X-Internal-Token` gated (mirror existing `extracted_read_route`).
* `page_number` is 1-indexed (matches `SourceCitation.page`).
* Reads the blob via `facts_store.read_blob(document_id)`. 404 if missing.
* Writes bytes to a temp file (pypdfium2 wants a path), calls existing
  `documents.fetcher.render_document(path)`, indexes the result by
  `page_number - 1`. 404 if out of range.
* JPEG-encodes via existing `documents.fetcher.encode_jpeg_bytes(image, quality=80)`.
* Returns `Response(content=jpeg_bytes, media_type="image/jpeg",
  headers={"Cache-Control": "private, max-age=300"})` so repeated hovers
  don't re-render.

Optionally (small win): add `total_pages` to the `IngestResponse` model
and populate from the existing rendered-pages list — saves the browser
one round-trip to discover page count. Cuttable; the browser can also
discover by enumerating distinct `page` values across all citations.

### OpenEMR PHP

**NEW — `interface/copilot/citation_overlay.php`**

A `require`-able PHP partial that emits the right-side preview panel
markup for a given facts JSON. Centralises the bbox-walking + green/yellow
classification so both `intake_review.php` and `lab_review.php` can
include it. Signature: takes `$documentId`, `$factsArr`, and
`$mappedFieldKeys` (a string set of field paths the save handler
actually writes). Emits:

```html
<aside class="copilot-preview" data-copilot-preview>
  <div class="copilot-preview-page" data-page="1">
    <img src="<webroot>/apis/default/api/agent/internal/document/<doc-id>/page/1.jpg" />
    <div class="copilot-bbox copilot-bbox-mapped"
         data-citation-id="legal_first_name"
         style="left:22%;top:15.5%;width:26%;height:3%"></div>
    <div class="copilot-bbox copilot-bbox-unmapped"
         data-citation-id="family_history-0-relation"
         style="..."></div>
  </div>
  <!-- one .copilot-preview-page per distinct page in citations -->
</aside>
```

The image src calls through OpenEMR's existing `/apis/default/api/agent/...`
proxy so the browser's same-origin policy is satisfied and the
`X-Internal-Token` is added server-side. Add a tiny route to
`apis/routes/_rest_routes_copilot.inc.php` that proxies
`GET /api/agent/internal/document/{id}/page/{n}.jpg` straight through —
mirrors the existing `agent/healthz` proxy pattern (~15 lines).

**NEW — `public/copilot/citation_overlay.css`**

Two-column layout, sticky preview panel, color tokens for
`.copilot-bbox-mapped` (green border + 12% green fill) /
`.copilot-bbox-unmapped` (yellow border + 12% yellow fill) /
`.copilot-bbox-active` (thicker border + higher fill alpha + box-shadow).

**NEW — `public/copilot/citation_overlay.js`**

Vanilla JS, no jQuery. ~80 lines:

1. On `DOMContentLoaded`, build a map `citationId → bbox-element`.
2. For each form input/select/textarea that has a `data-citation-id` attr,
   attach `mouseenter` / `focusin` handlers that toggle
   `.copilot-bbox-active` on the matching bbox + call
   `bbox.scrollIntoView({behavior: 'smooth', block: 'center'})`.
3. `mouseleave` / `focusout` removes the active class.

**EDIT — `interface/copilot/intake_review.php`**

* Two-column layout: form on the left (`flex: 1 1 0`), preview on the
  right (`flex: 0 0 50%`, `position: sticky; top: 1rem;
  max-height: calc(100vh - 2rem); overflow-y: auto`).
* Add `data-citation-id` to every form field that has an extracted value.
  Naming convention: scalar fields use the field key (`legal_first_name`,
  `chief_complaint`, `tobacco_status`); list rows use
  `<list>-<idx>-<col>` (`current_medications-0-name`,
  `family_history-2-relation`).
* Define `$mappedFieldKeys` (the set the save handler writes) and pass it
  to `citation_overlay.php`. Mapped (green): `legal_first_name`,
  `legal_last_name`, `date_of_birth`, `sex_assigned_at_birth`, `phone`,
  `email`, `medical_record_number`, `chief_complaint`, `tobacco_status`,
  `tobacco_pack_years`, `current_medications-*-*`, `reported_allergies-*-*`,
  `active_problems-*-*`. Unmapped (yellow): `family_history-*-*`,
  `pain_scale`. The `*` are wildcards; the partial expands by walking the
  facts and emitting one bbox per row column that has a citation.
* Include `<link>` and `<script>` for the new CSS + JS.

**EDIT — `interface/copilot/lab_review.php`**

* Same two-column layout.
* `data-citation-id` per form input: `observations-<idx>-display`,
  `observations-<idx>-value`, `observations-<idx>-unit`, etc. Use
  `display`'s citation as the row's primary citation since that's the
  one the extractor anchors per the demo simplification (one citation
  per observation row).
* `$mappedFieldKeys` for lab: `observations-*-*` (everything is mapped).
  No yellow boxes on the lab page in practice — every row goes to
  `procedure_result` if confirmed.
* Include the same CSS + JS.

**EDIT — `apis/routes/_rest_routes_copilot.inc.php`**

Add `GET /api/agent/internal/document/{id}/page/{n}.jpg` that proxies
through to the agent-service via `AgentHttpClient::getInternal` (or a new
`getInternalBinary` if needed — `getInternal` may try to JSON-decode the
JPEG body and throw). The new variant returns the raw `ResponseInterface`
without JSON-decoding. ~30 lines.

**EDIT — `src/Services/Copilot/AgentHttpClient.php`**

Add `getInternalBinary(string $path, string $internalToken):
ResponseInterface` — same shape as `getInternal` but returns the raw
response without `json_decode`. Used by the new image-proxy route.

## Reused existing pieces

| Pattern | Source |
|---|---|
| `render_document` → list[RenderedPage] | `agent-service/src/clinical_copilot/documents/fetcher.py:44` |
| `encode_jpeg_bytes(image, quality)` | `agent-service/src/clinical_copilot/documents/fetcher.py:115` |
| `SourceCitation.bbox` (x0,y0,x1,y1 normalized 0–1, top-left) | `agent-service/src/clinical_copilot/documents/schemas/citation.py:40` |
| `ExtractedFieldHelper::value/citationText/rowList` | `src/Services/Copilot/ExtractedFieldHelper.php` |
| `X-Internal-Token` middleware (Python) | `agent-service/src/clinical_copilot/auth/internal_token.py` |
| `AgentHttpClient::getInternal` proxy pattern | `src/Services/Copilot/AgentHttpClient.php:230` |

## Implementation order

1. **Blob persistence** (~20 min). Add `documents/store.py::write_blob/read_blob`, wire into the ingest route. Verify by re-ingesting a Chen intake and inspecting `data/blobs/`.

2. **Page-image route on agent-service** (~30 min). New `GET /api/agent/internal/document/{id}/page/{n}.jpg`. Curl-test from the OpenEMR container (already verified that path works for the existing internal routes).

3. **PHP image proxy** (~20 min). New route in `_rest_routes_copilot.inc.php` + `AgentHttpClient::getInternalBinary`. Curl-test from a logged-in browser session.

4. **`citation_overlay.php` partial + CSS + JS** (~1.5h). Self-contained: takes facts + mapped-field set, renders panel. Lives outside both review pages so it can be sourced from each.

5. **`intake_review.php` integration** (~45 min). Two-column layout, add `data-citation-id` to every input, include the partial, define mapped-field set.

6. **`lab_review.php` integration** (~30 min). Same shape, simpler mapped-field set.

7. **Browser smoke** (~20 min). Drive Chen intake + Chen lipid via Playwright. Confirm green/yellow boxes render at correct positions, hover scrolls panel, all three example PNG/PDF intake docs render.

Total: ~4 hours. Can ship intake first (~3h), lab as a 30-min follow-up if time tightens.

## Decisions made (user can redirect)

* **Mapped vs unmapped is hard-coded server-side**, not derived dynamically from `new_patient_save_ai.php`. The save handler is the source of truth, but introspecting it would be brittle. Maintenance burden: when a new field gets a write path, also add it to `$mappedFieldKeys`. Documented inline in `intake_review.php`.
* **Color scheme is green/yellow only.** Abstained fields have no `citation` per the schema's xor invariant, so they don't render as bboxes — the empty form input + inline abstain marker (already there) communicates the gap. No third color.
* **Page images render on demand**, not pre-rendered at ingest. Each page is one JPEG fetch from the browser; cache-control headers handle repeats.
* **No PDF.js / Mozilla viewer.** Plain `<img>` tags with absolute-positioned `<div>` overlays. The agent service has pypdfium2 + PIL for free; no need to ship a 2 MB JS PDF renderer to the client.
* **Filename safety for blob store**: replace `:` with `_` in the on-disk filename. The OpenEMR-generated `document_id` is `openemr:doc:1234` which is fine on POSIX but breaks Windows tooling and shell completion.
* **Intake + lab in same change** per user choice. Both pages get the same partial.
* **Form field → bbox interaction only.** Clicking a bbox does nothing. Per user choice.

## Verification

1. **Blob round-trip (offline):**
   ```bash
   cd agent-service && uv run pytest tests/unit/documents/test_store.py
   ```
   New test: write a 100KB blob, read it back, assert byte-equality.

2. **Page-image route (offline + live):**
   ```bash
   uv run pytest tests/integration/test_internal_routes.py::test_document_page_route_*
   curl -fsS -H "X-Internal-Token: dev-insecure-internal-token-32bytes!" \
     "http://localhost:8000/api/agent/internal/document/openemr:doc:4404/page/1.jpg" \
     -o /tmp/page1.jpg && file /tmp/page1.jpg
   ```
   Expected: 200, JPEG file, ~100–500 KB.

3. **PHP proxy (browser):**
   In an authenticated browser tab, hit
   `http://localhost:8300/apis/default/api/agent/internal/document/openemr%3Adoc%3A4404/page/1.jpg`
   and confirm the page renders.

4. **Intake review preview (Playwright + manual):**
   * Navigate to `intake_review.php?document_id=...` for a previously-extracted Chen intake.
   * Snapshot to confirm `aside.copilot-preview` is present with 3
     `.copilot-preview-page` children (Chen intake is 3 pages).
   * Hover the "First name" input and assert the bbox with
     `data-citation-id="legal_first_name"` gains the
     `copilot-bbox-active` class.
   * Visually: green boxes around all populated fields, yellow boxes
     around any rendered family-history rows, no box for sections the
     extractor didn't read (e.g. emergency contact).

5. **Lab review preview:** Same pattern with Chen lipid panel — 5–6
   green boxes corresponding to the lipid rows.

## Out of scope

* Click-from-bbox-to-form-field (user explicitly chose one-way).
* Drag-to-resize a bbox to fix a wrong citation.
* Page-image preloading or thumbnail nav rail.
* PDF text-layer overlay (would let the user select source text — nice
  but heavier; not needed for the demo).
* Citation provenance for fields that DON'T have one (e.g. abstained
  fields). Already absent from the bbox surface by design.
* Encrypted-document handling on the OpenEMR side. The current ingest
  flow stores unencrypted bytes in the agent-service blob store; we read
  from there, not from OpenEMR's `documents.url`.

## Open risks (with mitigations)

| Risk | Mitigation |
|---|---|
| Page render takes >2s on first hover, feels janky | JPEG caching headers + browser cache; ~300 KB per page; budget under 1.5s p95 on Chen intake |
| Bbox positions are off because the VLM's bbox coords are slightly inaccurate | This is a VLM accuracy issue, not a layout bug. Surface the inaccuracy honestly — if a green box covers the wrong region, that's information for the clinician |
| `<img>` natural size vs displayed size mismatch causes bbox drift | Use `position: relative` on the page wrapper sized to the image's rendered dimensions; bboxes use `%` coords inside it. CSS-only, no resize observer needed |
| Multi-page intake (Chen) renders 3 page images = ~1.5 MB total per review-page load | Acceptable for the demo; lazy-load (Intersection Observer) is a follow-up if it ever matters |
| Existing `getInternal` JSON-decodes the response — passing a JPEG through it would throw | Solved by adding `getInternalBinary` (a 25-line near-copy that skips JSON decoding) |
