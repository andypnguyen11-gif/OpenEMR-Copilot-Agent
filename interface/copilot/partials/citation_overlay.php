<?php

/**
 * Clinical Co-Pilot — bbox citation overlay partial.
 *
 * Renders the source pages of an extracted document as ``<img>`` plus a
 * canvas overlay drawing every ``SourceCitation.bbox`` from the facts
 * tree the parent review page already loaded. The image bytes come from
 * ``interface/copilot/api/document_page.php``, which proxies the cached
 * PNGs the agent service rendered at ingest time.
 *
 * The page-image route returns a structured 404 on cache miss; the
 * overlay JS handles that by showing a placeholder per page rather
 * than a broken image. PR 5's design treats agent-service as the
 * canonical renderer (the OpenEMR docker image has no Ghostscript so
 * PHP cannot rasterize PDFs in-process).
 *
 * Required variables in scope when including:
 *   - ``$documentId`` (string) — the agent-side document id
 *   - ``$facts`` (array|null) — the facts tree returned by the agent
 *   - ``$webroot`` (string) — already-resolved OpenEMR webroot prefix
 *
 * Click-to-highlight: rows in the parent's table are expected to carry
 * ``data-citation-id="<field_or_chunk_id>"`` so the JS can flip a
 * matching rectangle's color when the row is clicked. The hover/sync
 * two-pane interaction is deferred (see plans/copilot_bbox_preview.md).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use OpenEMR\Services\Copilot\ExtractedFieldHelper;

/** @var string $documentId */
/** @var array<string, mixed>|null $facts */
/** @var string $webroot */
/** @var string|null $documentType */

$documentTypeLocal = $documentType ?? '';

$citations = ExtractedFieldHelper::collectExtractedDocumentCitations($facts);

// Group by 1-indexed page number. Pages without any citations don't
// render — the overlay is purely a citation-explainer, not a generic
// document viewer.
/** @var array<int, list<array{field_id: string, page: int, bbox: array{0: float, 1: float, 2: float, 3: float}, raw_text: string}>> $citationsByPage */
$citationsByPage = [];
foreach ($citations as $entry) {
    $citationsByPage[$entry['page']] ??= [];
    $citationsByPage[$entry['page']][] = $entry;
}
ksort($citationsByPage, SORT_NUMERIC);

$pageImageBase = $webroot . '/interface/copilot/api/document_page.php';
$payload = [
    'document_id' => $documentId,
    'image_url_base' => $pageImageBase,
    'pages' => array_map(
        static fn (array $entries): array => array_map(
            static fn (array $entry): array => [
                'field_id' => $entry['field_id'],
                'bbox' => $entry['bbox'],
                'raw_text' => $entry['raw_text'],
            ],
            $entries,
        ),
        $citationsByPage,
    ),
];
$payloadJson = json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
?>
<section class="copilot-citation-overlay" data-document-id="<?php echo htmlspecialchars($documentId, ENT_QUOTES, 'UTF-8'); ?>">
<?php if ($citationsByPage === []): ?>
    <p class="overlay-empty">No bbox citations were extracted for this document.</p>
<?php else: ?>
    <p class="overlay-help">Click a row in the form to highlight its citation on the page.</p>
    <?php foreach ($citationsByPage as $pageNumber => $entries): ?>
        <?php
        $pageUrl = $pageImageBase
            . '?document_id=' . rawurlencode($documentId)
            . '&page=' . $pageNumber;
        ?>
        <figure class="overlay-page" data-page="<?php echo (int) $pageNumber; ?>">
            <div class="overlay-page-frame" style="position: relative; display: inline-block; max-width: 100%;">
                <img
                    class="overlay-image"
                    src="<?php echo htmlspecialchars($pageUrl, ENT_QUOTES, 'UTF-8'); ?>"
                    alt="Page <?php echo (int) $pageNumber; ?> of document"
                    style="display: block; max-width: 100%; height: auto;"
                    onerror="this.dataset.failed='1'; this.alt='Page preview unavailable.';"
                >
                <canvas class="overlay-canvas" style="position: absolute; left: 0; top: 0; pointer-events: none;"></canvas>
            </div>
            <figcaption>Page <?php echo (int) $pageNumber; ?> — <?php echo count($entries); ?> citation<?php echo count($entries) === 1 ? '' : 's'; ?></figcaption>
        </figure>
    <?php endforeach; ?>
<?php endif; ?>
</section>
<style>
    /* Right-docked panel: source preview lives alongside the form so
       the clinician can compare side-by-side without scrolling. The
       parent page wraps the form in `.copilot-review-form-col` (one
       CSS rule, defined here so the partial owns the layout
       contract). On narrow viewports we stack form-then-panel via
       the media query below. */
    .copilot-review-form-col {
        float: left;
        width: 50%;
        padding-right: 1rem;
        box-sizing: border-box;
    }
    .copilot-review-form-col table,
    .copilot-review-form-col input[type="text"],
    .copilot-review-form-col input[type="number"],
    .copilot-review-form-col input[type="date"] {
        max-width: 100%;
        box-sizing: border-box;
    }
    .copilot-citation-overlay {
        float: right;
        width: 48%;
        max-height: calc(100vh - 4rem);
        overflow-y: auto;
        position: sticky;
        top: 1rem;
        margin: 0 0 1.5rem;
        padding: 0.5rem;
        background: #fafafa;
        border: 1px solid #e5e5e5;
        border-radius: 4px;
        box-sizing: border-box;
    }
    .copilot-citation-overlay .overlay-help { color: #555; font-size: 0.9em; }
    .copilot-citation-overlay .overlay-empty { color: #666; font-style: italic; }
    .copilot-citation-overlay .overlay-page { margin: 0 0 1rem; padding: 0; }
    .copilot-citation-overlay .overlay-page figcaption {
        font-size: 0.85em; color: #555; margin-top: 0.25rem;
    }
    .copilot-citation-overlay .overlay-image {
        border: 1px solid #ddd;
        max-width: 100%;
        height: auto;
    }
    /* Below ~1100px the side-by-side layout cramps the form's input
       widths, so fall back to stacked: form first, panel below. */
    @media (max-width: 1100px) {
        .copilot-review-form-col,
        .copilot-citation-overlay {
            float: none;
            width: 100%;
            position: static;
            max-height: none;
        }
        .copilot-review-form-col { padding-right: 0; }
    }
</style>
<script>
(function () {
    "use strict";
    const payload = <?php echo $payloadJson; ?>;

    /** Draw every bbox for one page on its companion canvas. */
    function drawCanvas(figureEl, entries, highlightFieldId) {
        const img = figureEl.querySelector('img.overlay-image');
        const canvas = figureEl.querySelector('canvas.overlay-canvas');
        if (!img || !canvas) return;
        if (img.dataset.failed === '1') return;
        if (!img.complete || img.naturalWidth === 0) return;

        const renderedWidth = img.clientWidth;
        const renderedHeight = img.clientHeight;
        if (renderedWidth === 0 || renderedHeight === 0) return;

        // Match the canvas CSS size to the rendered image size and the
        // backing store to the device-pixel ratio so rectangles stay
        // crisp on retina displays.
        const dpr = window.devicePixelRatio || 1;
        canvas.style.width = renderedWidth + 'px';
        canvas.style.height = renderedHeight + 'px';
        canvas.width = Math.round(renderedWidth * dpr);
        canvas.height = Math.round(renderedHeight * dpr);

        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, renderedWidth, renderedHeight);

        for (const entry of entries) {
            const bbox = entry.bbox;
            // bbox is normalised to [0,1] on both axes (see the
            // `_bbox_in_unit_square` validator on SourceCitation).
            const x0 = bbox[0] * renderedWidth;
            const y0 = bbox[1] * renderedHeight;
            const x1 = bbox[2] * renderedWidth;
            const y1 = bbox[3] * renderedHeight;
            const w = x1 - x0;
            const h = y1 - y0;
            const isHighlighted = highlightFieldId !== null && entry.field_id === highlightFieldId;
            ctx.lineWidth = isHighlighted ? 3 : 1.5;
            ctx.strokeStyle = isHighlighted ? '#d35400' : 'rgba(204, 0, 0, 0.85)';
            ctx.fillStyle = isHighlighted ? 'rgba(243, 156, 18, 0.25)' : 'rgba(204, 0, 0, 0.10)';
            ctx.fillRect(x0, y0, w, h);
            ctx.strokeRect(x0, y0, w, h);
        }
    }

    function redrawAll(highlightFieldId) {
        const figures = document.querySelectorAll('.copilot-citation-overlay .overlay-page');
        figures.forEach(function (figureEl) {
            const pageNumber = parseInt(figureEl.dataset.page || '0', 10);
            const entries = payload.pages[pageNumber] || [];
            drawCanvas(figureEl, entries, highlightFieldId);
        });
    }

    // Redraw whenever the image actually loads (initial paint + lazy
    // load) and on viewport resize so the canvas keeps tracking the
    // image's intrinsic aspect ratio.
    document.querySelectorAll('.copilot-citation-overlay .overlay-image').forEach(function (img) {
        if (img.complete) {
            redrawAll(null);
        }
        img.addEventListener('load', function () { redrawAll(null); });
    });
    window.addEventListener('resize', function () { redrawAll(null); });

    // Click a row carrying data-citation-id → highlight the matching
    // rect across whichever page it lives on. A second click on the
    // same row clears the highlight.
    let activeFieldId = null;
    document.body.addEventListener('click', function (event) {
        const target = event.target instanceof Element ? event.target.closest('[data-citation-id]') : null;
        if (target === null) return;
        const fieldId = target.getAttribute('data-citation-id') || '';
        if (fieldId === '') return;
        activeFieldId = (activeFieldId === fieldId) ? null : fieldId;
        redrawAll(activeFieldId);
    });
})();
</script>
<?php
