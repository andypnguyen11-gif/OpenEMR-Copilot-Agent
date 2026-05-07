<?php

/**
 * Helpers for the editable-confirm document review surface.
 *
 * Two responsibilities, both pure (no DB or HTTP) so they're trivially
 * testable in isolation:
 *
 * - {@see FactsFormHelper::renderFacts()} walks an extracted-facts JSON
 *   tree and emits ``<input>`` elements named ``facts[path][to][field]``
 *   so the browser submits a nested array the server can deserialize
 *   back into the same shape.
 * - {@see FactsFormHelper::overlayEdits()} merges the form-submitted
 *   value-only edits onto the original full-shape facts so the typed
 *   PUT route gets a body that still satisfies
 *   ``ExtractedField.value_xor_abstain``.
 *
 * The rendering and overlay are intentionally kept in one class because
 * they share the "what counts as an ExtractedField leaf" definition;
 * splitting them risks the two getting out of sync.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Andy Nguyen <andy.nguyen@challenger.gauntletai.com>
 * @copyright Copyright (c) 2026 Andy Nguyen
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Services\Copilot\Documents;

final class FactsFormHelper
{
    /**
     * Recursive editable renderer entry-point. ``$node`` is the inner
     * facts dict (i.e. the per-type Pydantic model JSON dump minus the
     * outer ``document_id``). ``$namePath`` is the form-name prefix —
     * pass empty string for the top level.
     *
     * @param mixed $node
     */
    public static function renderFacts(mixed $node, string $namePath, string $label): string
    {
        if (is_array($node) && self::isExtractedField($node)) {
            return self::renderExtractedField($node, $namePath, $label);
        }
        if (is_array($node) && array_is_list($node)) {
            return self::renderList($node, $namePath, $label);
        }
        if (is_array($node)) {
            return self::renderObject($node, $namePath, $label);
        }
        return self::renderScalar($node, $namePath, $label);
    }

    /**
     * Overlay form-submitted edits onto the original full-shape facts.
     * The form only carries leaf ``value`` keys; ``citation`` and
     * ``abstain_reason`` come from the original. When the user fills
     * in a value for a field that was originally abstaining, the
     * abstain_reason is cleared so the validator accepts the leaf.
     *
     * @param mixed $original
     * @param mixed $edits
     */
    public static function overlayEdits(mixed $original, mixed $edits): mixed
    {
        if (is_array($original) && self::isExtractedField($original)) {
            if (!is_array($edits) || !array_key_exists('value', $edits)) {
                return $original;
            }
            $editedValue = $edits['value'];
            if (!is_string($editedValue) || $editedValue === '') {
                return $original;
            }
            return [
                'value' => self::coerceValue($editedValue, $original['value'] ?? null),
                'citation' => $original['citation'] ?? null,
                'abstain_reason' => null,
            ];
        }
        if (is_array($original) && array_is_list($original)) {
            if (!is_array($edits)) {
                return $original;
            }
            $merged = [];
            foreach ($original as $idx => $item) {
                $merged[] = self::overlayEdits($item, $edits[$idx] ?? null);
            }
            return $merged;
        }
        if (is_array($original)) {
            if (!is_array($edits)) {
                return $original;
            }
            $merged = [];
            foreach ($original as $key => $value) {
                $merged[$key] = self::overlayEdits($value, $edits[$key] ?? null);
            }
            return $merged;
        }
        if (is_string($edits) && $edits !== '') {
            return $edits;
        }
        return $original;
    }

    /**
     * The ``ExtractedField`` shape — three exact keys: ``value``,
     * ``citation``, ``abstain_reason``. Anything else with the same
     * key set is the same shape by construction.
     *
     * @param array<mixed,mixed> $node
     */
    public static function isExtractedField(array $node): bool
    {
        $keys = array_keys($node);
        sort($keys);
        return $keys === ['abstain_reason', 'citation', 'value'];
    }

    /**
     * Coerce the form's string back into the original value's type
     * (int / float / string). Unknown types pass through as string.
     *
     * @param mixed $original
     */
    public static function coerceValue(string $edited, mixed $original): mixed
    {
        if (is_int($original)) {
            $isNegative = str_starts_with($edited, '-') && ctype_digit(substr($edited, 1));
            return ctype_digit($edited) || $isNegative ? (int) $edited : $edited;
        }
        if (is_float($original)) {
            return is_numeric($edited) ? (float) $edited : $edited;
        }
        return $edited;
    }

    /**
     * @param array<mixed,mixed> $node
     */
    private static function renderExtractedField(array $node, string $namePath, string $label): string
    {
        $value = $node['value'] ?? null;
        $abstainReason = $node['abstain_reason'] ?? null;
        $citation = $node['citation'] ?? null;
        $citationText = '';
        if (is_array($citation) && isset($citation['raw_text']) && is_string($citation['raw_text'])) {
            $page = isset($citation['page']) && is_int($citation['page']) ? $citation['page'] : 0;
            $citationText = $page > 0
                ? sprintf('p.%d: %s', $page, $citation['raw_text'])
                : $citation['raw_text'];
        }

        $inputType = 'text';
        $stringValue = '';
        if (is_string($value)) {
            $stringValue = $value;
            if (preg_match('/^\d{4}-\d{2}-\d{2}$/', $value) === 1) {
                $inputType = 'date';
            }
        } elseif (is_int($value) || is_float($value)) {
            $stringValue = (string) $value;
            $inputType = 'number';
        }

        $abstainBadge = '';
        if (is_string($abstainReason) && $abstainReason !== '') {
            $escaped = htmlspecialchars($abstainReason, ENT_QUOTES, 'UTF-8');
            $abstainBadge = sprintf('<span class="abstain-badge" title="%s">%s</span>', $escaped, $escaped);
        }

        $citationHint = $citationText !== ''
            ? sprintf(
                '<div class="citation-hint">📎 %s</div>',
                htmlspecialchars(mb_strimwidth($citationText, 0, 200, '…'), ENT_QUOTES, 'UTF-8'),
            )
            : '';

        $namePathHtml = htmlspecialchars($namePath . '[value]', ENT_QUOTES, 'UTF-8');
        return sprintf(
            '<div class="field-row">'
                . '<label class="field-label" for="%s">%s</label>'
                . '<input type="%s" name="%s" id="%s" value="%s" class="field-input">'
                . '%s%s'
                . '</div>',
            $namePathHtml,
            htmlspecialchars($label, ENT_QUOTES, 'UTF-8'),
            $inputType,
            $namePathHtml,
            $namePathHtml,
            htmlspecialchars($stringValue, ENT_QUOTES, 'UTF-8'),
            $abstainBadge,
            $citationHint,
        );
    }

    /**
     * @param list<mixed> $items
     */
    private static function renderList(array $items, string $namePath, string $label): string
    {
        if ($items === []) {
            return sprintf(
                '<div class="field-row empty"><label class="field-label">%s</label><span class="empty-hint">(no entries)</span></div>',
                htmlspecialchars($label, ENT_QUOTES, 'UTF-8'),
            );
        }

        $out = sprintf(
            '<fieldset class="list-group"><legend>%s (%d)</legend>',
            htmlspecialchars($label, ENT_QUOTES, 'UTF-8'),
            count($items),
        );
        foreach ($items as $idx => $item) {
            $itemPath = $namePath . '[' . $idx . ']';
            $itemLabel = sprintf('#%d', $idx + 1);
            $out .= '<div class="list-item">'
                . sprintf('<div class="list-item-index">%s</div>', htmlspecialchars($itemLabel, ENT_QUOTES, 'UTF-8'))
                . self::renderFacts($item, $itemPath, $itemLabel)
                . '</div>';
        }
        $out .= '</fieldset>';
        return $out;
    }

    /**
     * @param array<mixed,mixed> $node
     */
    private static function renderObject(array $node, string $namePath, string $label): string
    {
        $out = $label === ''
            ? ''
            : sprintf('<fieldset class="object-group"><legend>%s</legend>', htmlspecialchars($label, ENT_QUOTES, 'UTF-8'));
        foreach ($node as $key => $value) {
            if ($key === 'document_id') {
                continue;
            }
            $childPath = $namePath === '' ? "facts[$key]" : $namePath . "[$key]";
            $childLabel = is_string($key) ? $key : (string) $key;
            $out .= self::renderFacts($value, $childPath, $childLabel);
        }
        if ($label !== '') {
            $out .= '</fieldset>';
        }
        return $out;
    }

    /**
     * @param mixed $value
     */
    private static function renderScalar(mixed $value, string $namePath, string $label): string
    {
        $stringValue = is_scalar($value) ? (string) $value : '';
        $namePathHtml = htmlspecialchars($namePath, ENT_QUOTES, 'UTF-8');
        return sprintf(
            '<div class="field-row"><label class="field-label" for="%s">%s</label>'
                . '<input type="text" name="%s" id="%s" value="%s" class="field-input"></div>',
            $namePathHtml,
            htmlspecialchars($label, ENT_QUOTES, 'UTF-8'),
            $namePathHtml,
            $namePathHtml,
            htmlspecialchars($stringValue, ENT_QUOTES, 'UTF-8'),
        );
    }
}
