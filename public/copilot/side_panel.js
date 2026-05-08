/**
 * Clinical Co-Pilot — side-panel iframe client (PR 17).
 *
 * Sibling of ``chat.js``. Three differences:
 *
 * 1. Pid is locked to ``data-copilot-pid`` on the shell (set server-side
 *    from the iframe URL). No patient picker — the iframe is recreated
 *    on patient switch, so within a single iframe lifetime the pid is
 *    constant.
 * 2. Every query carries ``lane: 'fast'`` so the agent service routes
 *    through the Haiku-backed lane (PR 10). PR 17 acceptance is <5s on
 *    a warm-cache patient, which only the fast lane meets.
 * 3. ``pagehide`` listener fires the session DELETE so the launcher
 *    closing the iframe (or the user navigating to a different chart)
 *    cleans up the agent-side session. PRD §3: history drops on patient
 *    switch / panel close.
 *
 * Most rendering helpers are duplicated from chat.js verbatim. Folding
 * them into a shared module is a follow-up; keeping them inline now
 * means the side panel ships without a build step.
 */

(function () {
    "use strict";

    const config = window.__copilotSideConfig || {};
    const queryUrl = config.queryUrl;
    const sessionDeleteUrl = config.sessionDeleteUrl || "";

    // Same CSRF-resolution dance as chat.js — top-level frame's freshly-
    // minted token wins, falling back to the iframe's own render. The
    // side panel's iframe sits two frames deep (host page → demographics
    // tab → side-panel iframe), so window.top can be cross-origin in
    // some embeds. Guard with try/catch and fall back to local config.
    let csrfToken = "";
    try {
        if (window.top && typeof window.top.api_csrf_token_js === "string"
            && window.top.api_csrf_token_js !== "") {
            csrfToken = window.top.api_csrf_token_js;
        }
    } catch (e) {
        // Cross-origin parent — fall back to local config.
    }
    if (!csrfToken) {
        csrfToken = config.csrfToken || "";
    }

    const shell = document.querySelector("[data-copilot-shell][data-copilot-side]");
    if (!shell) {
        return;
    }
    const lockedPid = shell.getAttribute("data-copilot-pid") || "";
    const lane = shell.getAttribute("data-copilot-lane") || "fast";
    const thread = shell.querySelector("[data-copilot-thread]");
    const form = shell.querySelector("[data-copilot-form]");
    const input = shell.querySelector("[data-copilot-input]");
    const submitBtn = shell.querySelector("[data-copilot-submit]");

    let currentSessionId = null;
    // Same lock-and-notify posture as chat.js: an idle timeout severs
    // the session, blocks sends until explicit ack, and surfaces a
    // banner so the user knows history was dropped (rather than
    // silently rotating into a fresh thread).
    let sessionLockedByIdle = false;
    let idleTimer = null;

    if (lockedPid === "") {
        // Server-rendered the disabled state already; no client wiring
        // needed. Bailing here keeps the listeners off the dead form.
        return;
    }

    idleTimer = createIdleTimer();

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (sessionLockedByIdle) {
            return;
        }
        const text = input.value.trim();
        if (!text) {
            return;
        }
        sendQuery(text);
        input.value = "";
    });

    input.addEventListener("input", touchIdleTimer);
    input.addEventListener("keydown", touchIdleTimer);

    // Fire-and-forget DELETE on iframe teardown so the agent's session
    // store doesn't accumulate orphans when the launcher closes the
    // panel. ``pagehide`` is the right event for iframes — it fires
    // even when the parent removes the frame, which beforeunload does
    // not reliably do across browsers.
    window.addEventListener("pagehide", function () {
        deleteServerSession({ keepalive: true });
        currentSessionId = null;
        if (idleTimer) {
            idleTimer.destroy();
        }
    });

    function deleteServerSession(opts) {
        if (!currentSessionId || !sessionDeleteUrl) {
            return;
        }
        const url = sessionDeleteUrl + "/" + encodeURIComponent(currentSessionId)
            + "?patient_id=" + encodeURIComponent(lockedPid);
        const init = {
            method: "DELETE",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "apicsrftoken": csrfToken
            }
        };
        // ``keepalive: true`` is only meaningful (and only safe) on the
        // unload path — it tells the browser to let the request finish
        // after the document is gone. Idle-timeout DELETEs run while
        // the page is alive and skip it so the request behaves like a
        // normal fetch.
        if (opts && opts.keepalive) {
            init.keepalive = true;
        }
        fetch(url, init).catch(function () {
            // TTL covers us if this fails.
        });
    }

    function createIdleTimer() {
        return window.CopilotIdleTimer.create({
            timeoutMs: window.CopilotIdleTimer.DEFAULT_TIMEOUT_MS,
            onTimeout: handleIdleTimeout
        });
    }

    function touchIdleTimer() {
        if (sessionLockedByIdle || !idleTimer) {
            return;
        }
        idleTimer.reset();
    }

    function handleIdleTimeout() {
        deleteServerSession();
        currentSessionId = null;
        sessionLockedByIdle = true;
        submitBtn.disabled = true;
        input.disabled = true;
        renderIdleNotice();
    }

    function renderIdleNotice() {
        const banner = document.createElement("div");
        banner.className = "copilot-idle-notice";

        const message = document.createElement("div");
        message.textContent = "Session ended after 15 minutes of inactivity. "
            + "Conversation history has been cleared.";
        banner.appendChild(message);

        const resumeBtn = document.createElement("button");
        resumeBtn.type = "button";
        resumeBtn.className = "btn btn-secondary btn-sm";
        resumeBtn.textContent = "Start new session";
        resumeBtn.addEventListener("click", function () {
            banner.remove();
            sessionLockedByIdle = false;
            submitBtn.disabled = false;
            input.disabled = false;
            idleTimer = createIdleTimer();
            input.focus();
        });
        banner.appendChild(resumeBtn);

        thread.appendChild(banner);
        thread.scrollTop = thread.scrollHeight;
    }

    function sendQuery(text) {
        appendUserMessage(text);
        const spinner = appendSpinner();
        submitBtn.disabled = true;

        const body = {
            patient_id: lockedPid,
            query: text,
            lane: lane
        };
        if (currentSessionId) {
            body.session_id = currentSessionId;
        }

        fetch(queryUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "apicsrftoken": csrfToken
            },
            body: JSON.stringify(body)
        }).then(function (resp) {
            return resp.json().then(function (jsonBody) {
                return { status: resp.status, body: jsonBody };
            }).catch(function () {
                return { status: resp.status, body: null };
            });
        }).then(function (result) {
            spinner.remove();
            if (result.status >= 200 && result.status < 300 && result.body) {
                if (typeof result.body.session_id === "string" && result.body.session_id !== "") {
                    currentSessionId = result.body.session_id;
                }
                renderAgentResponse(result.body);
                touchIdleTimer();
            } else {
                renderError(result);
            }
        }).catch(function (err) {
            spinner.remove();
            renderError({ status: 0, body: null, message: String(err) });
        }).finally(function () {
            if (!sessionLockedByIdle) {
                submitBtn.disabled = false;
            }
        });
    }

    function appendUserMessage(text) {
        clearEmpty();
        const wrap = document.createElement("div");
        wrap.className = "copilot-message copilot-message-user";
        const body = document.createElement("div");
        body.textContent = text;
        wrap.appendChild(body);
        thread.appendChild(wrap);
        thread.scrollTop = thread.scrollHeight;
    }

    function appendSpinner() {
        const spinner = document.createElement("div");
        spinner.className = "copilot-spinner";
        spinner.textContent = "Thinking…";
        thread.appendChild(spinner);
        thread.scrollTop = thread.scrollHeight;
        return spinner;
    }

    function clearEmpty() {
        const empty = thread.querySelector(".copilot-empty");
        if (empty) {
            empty.remove();
        }
    }

    function renderAgentResponse(body) {
        const wrap = document.createElement("div");
        wrap.className = "copilot-message copilot-message-agent";

        if (body.abstention) {
            wrap.appendChild(renderAbstention(body.abstention));
            thread.appendChild(wrap);
            thread.scrollTop = thread.scrollHeight;
            return;
        }

        const recordIndex = indexToolResults(body.tool_results);
        if (Array.isArray(body.cards) && body.cards.length > 0) {
            wrap.appendChild(renderCards(body.cards, recordIndex));
        }
        if (Array.isArray(body.prose) && body.prose.length > 0) {
            wrap.appendChild(renderProse(body.prose));
        }
        if (!wrap.firstChild) {
            const note = document.createElement("div");
            note.className = "copilot-empty";
            note.textContent = "(agent returned no claims and no abstention)";
            wrap.appendChild(note);
        }

        thread.appendChild(wrap);
        thread.scrollTop = thread.scrollHeight;
    }

    function renderCards(cards, recordIndex) {
        const list = document.createElement("div");
        list.className = "copilot-cards";
        cards.forEach(function (card) {
            const item = document.createElement("div");
            item.className = "copilot-card";
            const title = document.createElement("div");
            title.className = "copilot-card-title";
            title.textContent = card.title + " (" + card.kind + ")";
            item.appendChild(title);
            (card.source_ids || []).forEach(function (sid) {
                item.appendChild(renderRecordRow(sid, recordIndex[sid]));
            });
            list.appendChild(item);
        });
        return list;
    }

    function indexToolResults(toolResults) {
        const index = {};
        if (!Array.isArray(toolResults)) {
            return index;
        }
        toolResults.forEach(function (tr) {
            (tr.records || []).forEach(function (rec) {
                if (rec && typeof rec.source_id === "string") {
                    index[rec.source_id] = rec;
                }
            });
        });
        return index;
    }

    function renderRecordRow(sourceId, record) {
        const row = document.createElement("div");
        row.className = "copilot-card-record";
        if (!record) {
            row.classList.add("copilot-card-source");
            row.textContent = sourceId;
            return row;
        }
        const summary = document.createElement("span");
        summary.className = "copilot-card-record-summary";
        summary.textContent = summarizeRecord(record);
        row.appendChild(summary);
        const cite = document.createElement("span");
        cite.className = "copilot-card-source";
        cite.textContent = sourceId;
        row.appendChild(cite);
        return row;
    }

    function summarizeRecord(rec) {
        if (typeof rec.name === "string") {
            return joinNonEmpty([rec.name, rec.dose, rec.status, rec.started_on ? "started " + formatDateTime(rec.started_on) : ""]);
        }
        if (typeof rec.substance === "string") {
            return joinNonEmpty([rec.substance, rec.reaction, rec.severity]);
        }
        if (typeof rec.display === "string" && typeof rec.value !== "undefined") {
            const valueWithUnit = rec.unit ? rec.value + " " + rec.unit : String(rec.value);
            return joinNonEmpty([rec.display, valueWithUnit, formatDateTime(rec.observed_on), rec.reference_range ? "(ref " + rec.reference_range + ")" : ""]);
        }
        if (typeof rec.display === "string") {
            return joinNonEmpty([rec.display, rec.status, formatDateTime(rec.onset_date)]);
        }
        if (typeof rec.encounter_type === "string") {
            return joinNonEmpty([rec.encounter_type, formatDateTime(rec.visited_on), rec.chief_complaint]);
        }
        if (typeof rec.note_date === "string") {
            return joinNonEmpty([formatDateTime(rec.note_date), rec.author, rec.body ? truncate(rec.body, 140) : ""]);
        }
        if (typeof rec.rationale === "string") {
            return joinNonEmpty([rec.rule_id, rec.category, rec.rationale]);
        }
        return rec.source_id || "(unrecognized record)";
    }

    /**
     * Re-shape an ISO 8601 date / datetime string into ``yyyy/mm/dd``
     * (date-only inputs) or ``yyyy/mm/dd hh:mm`` (date+time). Mirrors
     * the same helper in chat.js so the side panel and chat tab format
     * dates identically. Anything not matching the ISO shape passes
     * through unchanged.
     */
    function formatDateTime(value) {
        if (typeof value !== "string" || value === "") {
            return value;
        }
        const m = value.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
        if (!m) {
            return value;
        }
        const datePart = m[1] + "/" + m[2] + "/" + m[3];
        return m[4] ? datePart + " " + m[4] + ":" + m[5] : datePart;
    }

    function joinNonEmpty(parts) {
        return parts.filter(function (p) { return typeof p === "string" && p !== ""; }).join(" — ");
    }

    function truncate(s, n) {
        return s.length <= n ? s : s.slice(0, n - 1) + "…";
    }

    function renderProse(claims) {
        const wrap = document.createElement("div");
        wrap.className = "copilot-prose";
        claims.forEach(function (claim) {
            const line = document.createElement("span");
            line.className = "copilot-claim";
            // Mirrors chat.js renderProse — wraps embedded citation
            // patterns (FHIR resource ids, corpus chunk ids) in
            // styled spans so the reader can distinguish source
            // references from surrounding prose.
            line.appendChild(renderTextWithCitations(claim.text + " "));
            const cite = document.createElement("span");
            cite.className = "copilot-citation";
            cite.textContent = "[" + claim.source_id + "]";
            line.appendChild(cite);
            wrap.appendChild(line);
        });
        return wrap;
    }

    /**
     * Split a string into alternating plain-text nodes and citation
     * spans. Recognises two shapes:
     *   * Chart FHIR ids — ``Observation/abc-123``, ``MedicationRequest/<uuid>``.
     *   * Corpus chunk ids — ``nih/foo#7``, ``uspstf/bar#3``.
     * Anything else passes through unchanged. Identical to the helper
     * in chat.js; folding into a shared module is a follow-up.
     */
    function renderTextWithCitations(text) {
        const frag = document.createDocumentFragment();
        const pattern = /\b([A-Z][A-Za-z]+\/[A-Za-z0-9_-]+)|\b([a-z][a-z0-9_-]*\/[a-z0-9_-]+#\d+)/g;
        let lastIndex = 0;
        let match;
        while ((match = pattern.exec(text)) !== null) {
            if (match.index > lastIndex) {
                frag.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
            }
            const span = document.createElement("span");
            span.className = "copilot-citation-inline";
            span.textContent = match[0];
            frag.appendChild(span);
            lastIndex = match.index + match[0].length;
        }
        if (lastIndex < text.length) {
            frag.appendChild(document.createTextNode(text.slice(lastIndex)));
        }
        return frag;
    }

    function renderAbstention(abstention) {
        const wrap = document.createElement("div");
        wrap.className = "copilot-abstention";
        wrap.setAttribute("data-state", abstention.state || "");

        const state = document.createElement("div");
        state.className = "copilot-abstention-state";
        state.textContent = abstention.state;
        wrap.appendChild(state);

        const reason = document.createElement("div");
        reason.textContent = describeAbstention(abstention);
        wrap.appendChild(reason);

        return wrap;
    }

    function describeAbstention(abstention) {
        switch (abstention.state) {
            case "NO_DATA":
                return abstention.reason
                    ? "The agent could not answer this question. " + abstention.reason
                    : "The chart does not contain the data needed to answer this question.";
            case "VERIFICATION_FAILED":
                return "The agent's response failed verification — at least one cited source could not be confirmed against the chart. " + (abstention.reason || "");
            case "TOOL_FAILURE":
                return "A backend tool failed while answering this question. Try again. " + (abstention.reason || "");
            case "UNAUTHORIZED":
                return "Access to that patient is not authorized for this session. The attempt has been logged.";
            default:
                return abstention.reason || "The agent declined to answer.";
        }
    }

    function renderError(result) {
        clearEmpty();
        const wrap = document.createElement("div");
        wrap.className = "copilot-error";
        const status = result.status || "?";
        const body = result.body || {};
        wrap.textContent = "Gateway error (" + status + "): " +
            (body.error || result.message || "request failed");
        thread.appendChild(wrap);
        thread.scrollTop = thread.scrollHeight;
    }
})();
