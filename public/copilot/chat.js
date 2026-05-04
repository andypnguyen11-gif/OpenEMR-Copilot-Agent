/**
 * Clinical Co-Pilot — chat-page client (M3 MVP, PR 9 multi-turn).
 *
 * Vanilla JS, no framework. Posts the user's query + selected patient_id
 * (+ optional session_id continuation) to the OpenEMR-side gateway at
 * /apis/default/api/agent/query and renders the structured AgentResponse
 * (cards, prose, abstention) into the thread.
 *
 * Session lifecycle:
 *   - First turn: client omits session_id; server mints + returns one.
 *   - Subsequent turns: client echoes the server's id back.
 *   - "Clear chat" or patient switch: fire-and-forget DELETE of the active
 *     session_id, then null it out so the next turn starts fresh.
 *   - The agent's composite-key store also handles patient-switch isolation
 *     server-side (the new JWT's patient_id won't match the stale session
 *     entry), so the DELETE is belt-and-suspenders.
 */

(function () {
    "use strict";

    const config = window.__copilotConfig || {};
    const queryUrl = config.queryUrl;
    const sessionDeleteUrl = config.sessionDeleteUrl || "";
    // Prefer the parent OpenEMR window's freshly-minted api csrf token
    // (set by ``interface/main/tabs/main.php`` line 134) over chat.php's
    // own render. chat.php is loaded inside an iframe and its rendered
    // HTML is cached across session rotations; a CSRF failure on the
    // gateway path rotates the OpenEMR cookie, leaving the iframe's
    // baked-in token referencing a now-dead private_key. Reading from
    // ``window.top`` walks up to the live frame where the token is
    // regenerated on every navigation.
    let csrfToken = "";
    try {
        if (window.top && typeof window.top.api_csrf_token_js === "string"
            && window.top.api_csrf_token_js !== "") {
            csrfToken = window.top.api_csrf_token_js;
        }
    } catch (e) {
        // Cross-origin parent window — fall back to local config.
    }
    if (!csrfToken) {
        csrfToken = config.csrfToken || "";
    }

    const shell = document.querySelector("[data-copilot-shell]");
    if (!shell) {
        return;
    }
    const patientSelect = shell.querySelector("[data-copilot-patient]");
    const thread = shell.querySelector("[data-copilot-thread]");
    const form = shell.querySelector("[data-copilot-form]");
    const input = shell.querySelector("[data-copilot-input]");
    const submitBtn = shell.querySelector("[data-copilot-submit]");
    const resetBtn = shell.querySelector("[data-copilot-reset]");

    let lastPatientId = patientSelect.value;
    let currentSessionId = null;
    // Block sends between an idle timeout and the user's explicit ack.
    // Without the latch, a keystroke racing the timeout banner could
    // POST a stale session_id over the wire — the server would mint a
    // fresh one and the user would get a confusingly-empty thread back.
    let sessionLockedByIdle = false;
    let idleTimer = createIdleTimer();

    patientSelect.addEventListener("change", function () {
        if (patientSelect.value !== lastPatientId) {
            // PRD §3: history drops on patient switch. The DELETE must
            // bind to the OUTGOING patient_id (the one the session
            // belongs to under the JWT principal it was created with),
            // so snapshot it before mutating lastPatientId.
            const previousPatientId = lastPatientId;
            lastPatientId = patientSelect.value;
            clearThread(previousPatientId);
        }
    });

    resetBtn.addEventListener("click", function () {
        clearThread(lastPatientId);
    });

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (sessionLockedByIdle) {
            // The ack-banner click rebuilds the timer + clears the lock;
            // until then submits are dead. The submit button is also
            // disabled visually, but a user could still trigger via Enter.
            return;
        }
        const text = input.value.trim();
        if (!text) {
            return;
        }
        sendQuery(text);
        input.value = "";
    });

    // Typing-as-activity: reset the idle timer on every keystroke and
    // every input change (covers paste, IME, autofill). Keeps the timer
    // in sync with what the user is actually doing inside the chat,
    // independent of ambient mouse motion in the surrounding chart UI.
    input.addEventListener("input", touchIdleTimer);
    input.addEventListener("keydown", touchIdleTimer);

    // Page-unload cleanup. Mirrors side_panel.js's pagehide handler so
    // navigating away from chat.php (tab close, back button, link
    // click) drops the agent session immediately instead of waiting on
    // TTL. ``pagehide`` is the right event — ``beforeunload`` doesn't
    // reliably fire on mobile or when the tab is discarded by the
    // browser. ``keepalive: true`` lets the request finish after the
    // document is gone.
    window.addEventListener("pagehide", function () {
        deleteServerSession(lastPatientId, { keepalive: true });
        currentSessionId = null;
    });

    function clearThread(patientIdForDelete) {
        deleteServerSession(patientIdForDelete);
        currentSessionId = null;

        thread.innerHTML = '<div class="copilot-empty">' +
            'Pick a patient and ask a question.' +
            '</div>';
    }

    function deleteServerSession(patientIdForDelete, opts) {
        // Server-side state cleanup. Fire-and-forget: failure to reach
        // the gateway here is non-fatal — the agent's TTL eviction
        // bounds orphaned sessions, and the next request from this
        // tab will mint a fresh session_id anyway because the caller
        // drops currentSessionId.
        if (!currentSessionId || !sessionDeleteUrl || !patientIdForDelete) {
            return;
        }
        const url = sessionDeleteUrl + "/" + encodeURIComponent(currentSessionId)
            + "?patient_id=" + encodeURIComponent(patientIdForDelete);
        const init = {
            method: "DELETE",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "apicsrftoken": csrfToken
            }
        };
        // ``keepalive: true`` is only meaningful on the unload path — it
        // lets the browser finish the request after the document is
        // gone. In-page paths (idle timeout, patient switch, reset
        // button) skip it so the request behaves like a normal fetch.
        if (opts && opts.keepalive) {
            init.keepalive = true;
        }
        fetch(url, init).catch(function () {
            // Intentional swallow — TTL covers us.
        });
    }

    function createIdleTimer() {
        return window.CopilotIdleTimer.create({
            timeoutMs: window.CopilotIdleTimer.DEFAULT_TIMEOUT_MS,
            onTimeout: handleIdleTimeout
        });
    }

    function touchIdleTimer() {
        // Activity post-timeout never re-arms the same timer instance;
        // the new-session ack rebuilds it. Calling reset() here while
        // locked is harmless (the helper no-ops after fire), but skip
        // the call to keep intent obvious in the trace.
        if (sessionLockedByIdle) {
            return;
        }
        idleTimer.reset();
    }

    function handleIdleTimeout() {
        // Sever the agent-side session immediately so a stolen tab
        // can't continue under the prior context. The DELETE binds to
        // the patient the session belongs to, not whatever's currently
        // selected (a switch would have already reset the timer).
        deleteServerSession(lastPatientId);
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
            // Build a fresh timer instance — the prior one latched
            // ``fired`` at timeout and will not re-arm.
            idleTimer = createIdleTimer();
            input.focus();
        });
        banner.appendChild(resumeBtn);

        thread.appendChild(banner);
        thread.scrollTop = thread.scrollHeight;
    }

    function sendQuery(text) {
        const patientId = patientSelect.value;
        appendUserMessage(patientId, text);
        const spinner = appendSpinner();
        submitBtn.disabled = true;

        const body = {
            patient_id: patientId,
            query: text
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
            return resp.json().then(function (body) {
                return { status: resp.status, body: body };
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
                // A successful round-trip is the strongest "user is
                // engaged" signal we have. Reset here so the timer
                // captures send-without-typing patterns (e.g. clicking
                // a follow-up suggestion in a future iteration).
                touchIdleTimer();
            } else {
                renderError(result);
            }
        }).catch(function (err) {
            spinner.remove();
            renderError({ status: 0, body: null, message: String(err) });
        }).finally(function () {
            // Don't undo the idle lock if the timeout fired mid-flight —
            // the user has already been told the session is gone.
            if (!sessionLockedByIdle) {
                submitBtn.disabled = false;
            }
        });
    }

    function appendUserMessage(patientId, text) {
        clearEmpty();
        const wrap = document.createElement("div");
        wrap.className = "copilot-message copilot-message-user";
        const label = document.createElement("div");
        label.textContent = "You (patient " + patientId + ")";
        const body = document.createElement("div");
        body.textContent = text;
        wrap.appendChild(label);
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

        // Abstention takes precedence — when present, cards/prose are
        // empty by contract, and the UI surfaces only the abstention.
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
            // Verification couldn't resolve the record — fall back to the
            // bare source id so the card isn't silently empty.
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
        // Per-kind formatting mirrors the Daily Brief card sections so the
        // chat surface and the brief surface read the same. Inferred from
        // which fields are present rather than a typed kind tag — the
        // tool_results records don't carry one.
        if (typeof rec.name === "string") {
            return joinNonEmpty([rec.name, rec.dose, rec.status, rec.started_on ? "started " + rec.started_on : ""]);
        }
        if (typeof rec.substance === "string") {
            return joinNonEmpty([rec.substance, rec.reaction, rec.severity]);
        }
        if (typeof rec.display === "string" && typeof rec.value !== "undefined") {
            const valueWithUnit = rec.unit ? rec.value + " " + rec.unit : String(rec.value);
            return joinNonEmpty([rec.display, valueWithUnit, rec.observed_on, rec.reference_range ? "(ref " + rec.reference_range + ")" : ""]);
        }
        if (typeof rec.display === "string") {
            return joinNonEmpty([rec.display, rec.status, rec.onset_date]);
        }
        if (typeof rec.encounter_type === "string") {
            return joinNonEmpty([rec.encounter_type, rec.visited_on, rec.chief_complaint]);
        }
        if (typeof rec.note_date === "string") {
            return joinNonEmpty([rec.note_date, rec.author, rec.body ? truncate(rec.body, 140) : ""]);
        }
        if (typeof rec.rationale === "string") {
            return joinNonEmpty([rec.rule_id, rec.category, rec.rationale]);
        }
        return rec.source_id || "(unrecognized record)";
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
            line.textContent = claim.text + " ";
            const cite = document.createElement("span");
            cite.className = "copilot-citation";
            cite.textContent = "[" + claim.source_id + "]";
            line.appendChild(cite);
            wrap.appendChild(line);
        });
        return wrap;
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
