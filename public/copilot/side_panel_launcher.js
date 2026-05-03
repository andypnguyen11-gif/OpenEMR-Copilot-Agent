/**
 * Clinical Co-Pilot — side-panel launcher (PR 17).
 *
 * Runs on the demographics tab. The mount HTML is emitted by
 * SidePanelSubscriber on RenderEvent::EVENT_HANDLE; this script wires
 * the launcher button to a slide-out drawer that hosts the chat iframe.
 *
 * Iframe lazy-loading: we keep the real URL on `data-agent-src` and set
 * the iframe `src` only on first open. That way an unopened side panel
 * costs zero gateway round-trips on chart load.
 *
 * Patient-switch isolation: when the user navigates to a different
 * chart, OpenEMR reloads the demographics frame — which re-runs the
 * mount script with a fresh `data-agent-pid`. The iframe is recreated
 * and the in-iframe chat starts fresh, so PRD §3's "history drops on
 * patient switch" rule holds even though the launcher itself doesn't
 * postMessage on switch.
 */

(function () {
    "use strict";

    const mounts = document.querySelectorAll("[data-agent-side-panel-mount]");
    if (mounts.length === 0) {
        return;
    }
    // Subscriber guards single-emission, but defend in depth here too —
    // a stale mount left over from a prior render shouldn't fight the
    // current one for the launcher button focus.
    mounts.forEach(function (mount, index) {
        if (index > 0) {
            mount.parentNode.removeChild(mount);
            return;
        }
        wireMount(mount);
    });

    function wireMount(mount) {
        const toggle = mount.querySelector("[data-agent-side-panel-toggle]");
        const panel = mount.querySelector("[data-agent-side-panel]");
        const closeBtn = mount.querySelector("[data-agent-side-panel-close]");
        const frame = mount.querySelector("[data-agent-side-panel-frame]");
        if (!toggle || !panel || !frame) {
            return;
        }
        const targetSrc = frame.getAttribute("data-agent-src") || "";

        toggle.addEventListener("click", function () {
            const isOpen = !panel.hasAttribute("hidden");
            if (isOpen) {
                close();
            } else {
                open();
            }
        });

        if (closeBtn) {
            closeBtn.addEventListener("click", close);
        }

        function open() {
            // Lazy-load on first open. Reset to about:blank on every
            // close so the next open starts a fresh chat surface, which
            // is the cheap version of a session reset for the iframe.
            if (frame.getAttribute("src") === "about:blank" && targetSrc) {
                frame.setAttribute("src", targetSrc);
            }
            panel.removeAttribute("hidden");
            toggle.setAttribute("aria-expanded", "true");
        }

        function close() {
            panel.setAttribute("hidden", "");
            toggle.setAttribute("aria-expanded", "false");
            // Drop the iframe document so the next open starts clean.
            // The in-iframe client also clears its own session on
            // pagehide via beforeunload; this belt-and-suspenders both
            // sides — handy when the host browser delays beforeunload.
            frame.setAttribute("src", "about:blank");
        }
    }
})();
