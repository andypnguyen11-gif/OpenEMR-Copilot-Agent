/**
 * Clinical Co-Pilot — chat-page client (M3 MVP).
 *
 * Vanilla JS, no framework. Posts the user's query + selected patient_id
 * to the OpenEMR-side gateway at /apis/default/api/agent/query and renders
 * the structured AgentResponse (cards, prose, abstention) into the thread.
 *
 * The thread is in-memory only — switching patients or hitting "Clear chat"
 * drops it (PRD §3 / M3 acceptance criterion).
 */

(function () {
    "use strict";

    const config = window.__copilotConfig || {};
    const queryUrl = config.queryUrl;
    const csrfToken = config.csrfToken || "";

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

    patientSelect.addEventListener("change", function () {
        // PRD §3: history drops on patient switch.
        if (patientSelect.value !== lastPatientId) {
            lastPatientId = patientSelect.value;
            clearThread();
        }
    });

    resetBtn.addEventListener("click", clearThread);

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        const text = input.value.trim();
        if (!text) {
            return;
        }
        sendQuery(text);
        input.value = "";
    });

    function clearThread() {
        thread.innerHTML = '<div class="copilot-empty">' +
            'Pick a patient and ask a question.' +
            '</div>';
    }

    function sendQuery(text) {
        const patientId = patientSelect.value;
        appendUserMessage(patientId, text);
        const spinner = appendSpinner();
        submitBtn.disabled = true;

        fetch(queryUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "apicsrftoken": csrfToken
            },
            body: JSON.stringify({
                patient_id: patientId,
                query: text
            })
        }).then(function (resp) {
            return resp.json().then(function (body) {
                return { status: resp.status, body: body };
            }).catch(function () {
                return { status: resp.status, body: null };
            });
        }).then(function (result) {
            spinner.remove();
            if (result.status >= 200 && result.status < 300 && result.body) {
                renderAgentResponse(result.body);
            } else {
                renderError(result);
            }
        }).catch(function (err) {
            spinner.remove();
            renderError({ status: 0, body: null, message: String(err) });
        }).finally(function () {
            submitBtn.disabled = false;
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

        if (Array.isArray(body.cards) && body.cards.length > 0) {
            wrap.appendChild(renderCards(body.cards));
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

    function renderCards(cards) {
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
                const src = document.createElement("div");
                src.className = "copilot-card-source";
                src.textContent = sid;
                item.appendChild(src);
            });
            list.appendChild(item);
        });
        return list;
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
                return "The chart does not contain the data needed to answer this question.";
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
