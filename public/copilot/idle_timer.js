/**
 * Clinical Co-Pilot — shared idle-timer helper.
 *
 * Both the full chat surface (``chat.js``) and the side panel iframe
 * (``side_panel.js``) need the same idle-timeout behavior to match
 * ARCHITECTURE §4.4: 15-minute idle window, observable to the user
 * (lock-and-notify, not silent rotate). The server enforces the same
 * window via the agent-service ``SessionStore.DEFAULT_TTL_SECONDS``
 * constant (also 15 min) — both numbers must move together.
 *
 * Why this is its own file rather than two copies of the same JS:
 *
 *   - It's the third sibling helper that would otherwise need to ship in
 *     two places (the rendering and CSRF helpers are still duplicated; if
 *     a fourth surface lands, that's where extraction makes sense).
 *   - "Did this surface keep its idle timer in sync with the spec" should
 *     be answerable from a single file rather than by diffing two copies.
 *
 * Shape:
 *
 *   var timer = window.CopilotIdleTimer.create({
 *       timeoutMs: 15 * 60 * 1000,
 *       onTimeout: function () { ... fires once after timeoutMs of idle ... }
 *   });
 *   timer.reset();    // call on every activity (typing, send)
 *   timer.destroy();  // call on lifecycle end (unload, etc.)
 *
 * Activity definition is the caller's responsibility — the helper only
 * provides ``reset()``. chat.js and side_panel.js bind the same set of
 * signals (``input`` / ``keydown`` on the input box, plus a successful
 * send). Mouse-motion is intentionally NOT counted: clinicians often
 * leave the panel open while reviewing the main chart in another pane
 * with the mouse moving across it, and that should not extend the
 * session.
 *
 * Background-tab semantics: ``setTimeout`` continues to count down when
 * the tab is hidden, so background time DOES count toward idle. This is
 * the correct posture for PHI: a tab the clinician forgot about should
 * time out the same as one they're actively staring at.
 *
 * After ``onTimeout`` fires, the helper goes inert. Calling ``reset()``
 * after timeout is a no-op until the caller tears down and rebuilds the
 * timer (which the new-session flow does). This is deliberate: the
 * caller's timeout handler is supposed to clear the in-memory session_id
 * and block sends pending an explicit ack, and we don't want a late
 * keystroke to silently re-arm a timer the caller has logically
 * destroyed.
 */

(function () {
    "use strict";

    function create(opts) {
        var timeoutMs = opts && opts.timeoutMs;
        var onTimeout = opts && opts.onTimeout;
        if (typeof timeoutMs !== "number" || timeoutMs <= 0) {
            throw new Error("CopilotIdleTimer.create: timeoutMs must be a positive number");
        }
        if (typeof onTimeout !== "function") {
            throw new Error("CopilotIdleTimer.create: onTimeout must be a function");
        }

        var handle = null;
        // ``fired`` latches at the first onTimeout invocation. The latch
        // exists separately from ``handle === null`` because handle is
        // also null between construction and the first reset(); we need
        // to distinguish "never armed" (reset should arm) from "fired"
        // (reset should no-op).
        var fired = false;

        function arm() {
            if (fired) {
                return;
            }
            handle = setTimeout(function () {
                handle = null;
                fired = true;
                onTimeout();
            }, timeoutMs);
        }

        function reset() {
            if (fired) {
                return;
            }
            if (handle !== null) {
                clearTimeout(handle);
                handle = null;
            }
            arm();
        }

        function destroy() {
            if (handle !== null) {
                clearTimeout(handle);
                handle = null;
            }
            // Latch ``fired`` so a stray reset() after destroy() can't
            // re-arm against the now-stale onTimeout closure.
            fired = true;
        }

        return {
            reset: reset,
            destroy: destroy
        };
    }

    window.CopilotIdleTimer = {
        create: create,
        // Surfaced as a constant so the two call sites read identically
        // ("15 minutes per ARCHITECTURE §4.4") instead of repeating the
        // arithmetic. Server side pins the matching number in
        // ``orchestrator/sessions.py::DEFAULT_TTL_SECONDS``.
        DEFAULT_TIMEOUT_MS: 15 * 60 * 1000
    };
})();
