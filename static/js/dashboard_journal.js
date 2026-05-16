(function () {
    const surface = document.querySelector("[data-dashboard-ai-journal]");
    if (!surface) {
        return;
    }

    const createUrl = surface.dataset.createSessionUrl || "";
    const csrfToken = surface.dataset.csrfToken || "";
    const sessionUrlPattern = surface.dataset.journalSessionUrlPattern || "";
    const sessionUrlPlaceholder = surface.dataset.journalSessionUrlPlaceholder || "";

    const startPanel = surface.querySelector("[data-dashboard-journal-start-panel]");
    const sessionPanel = surface.querySelector("[data-dashboard-journal-session-panel]");
    const sessionError = surface.querySelector("[data-dashboard-journal-error]");

    const sessionDetailUrl = (sessionId) => {
        if (sessionUrlPattern && sessionUrlPlaceholder) {
            return sessionUrlPattern.split(sessionUrlPlaceholder).join(String(sessionId));
        }
        return "";
    };

    const responseErrorMessage = (response, data, fallback) => {
        if (data && (data.message || data.error)) {
            return data.message || data.error;
        }
        if (response && response.status) {
            const label = response.statusText ? ` ${response.statusText}` : "";
            return `${fallback || "Request failed."} (${response.status}${label})`;
        }
        return fallback || "Request failed.";
    };

    const showSurfaceError = (message) => {
        if (!sessionError) {
            return;
        }
        sessionError.textContent = message || "";
        sessionError.hidden = !message;
    };

    const clearSurfaceError = () => showSurfaceError("");

    const resetToStart = () => {
        if (sessionPanel) {
            sessionPanel.innerHTML = "";
            sessionPanel.hidden = true;
        }
        if (startPanel) {
            startPanel.hidden = false;
        }
        clearSurfaceError();
    };

    const buildSessionSection = (payload) => {
        const section = document.createElement("section");
        section.className = "admin-journal-session";
        section.setAttribute("data-admin-journal-session", "");
        section.dataset.chatUrl = payload.chat_url || "";
        section.dataset.tagsUrl = payload.tags_url || "";
        section.dataset.feedbackTemplate = payload.feedback_url_template || "";
        section.dataset.csrfToken = csrfToken;

        const meta = document.createElement("div");
        meta.className = "admin-journal-meta";
        meta.setAttribute("data-journal-tags-form", "");
        meta.innerHTML = `
            <div class="admin-journal-title-row">
                <label for="dashboardJournalInlineTitle">Title</label>
                <input id="dashboardJournalInlineTitle" name="title" value="" maxlength="200" placeholder="session label">
            </div>
            <div>
                <label for="dashboardJournalInlineTags">Tags</label>
                <input id="dashboardJournalInlineTags" name="tags" value="" placeholder="emotion:tilted, theme:revenge">
            </div>
            <div>
                <label for="dashboardJournalInlineNotes">Notes</label>
                <textarea id="dashboardJournalInlineNotes" name="notes" rows="2" placeholder="Post-session notes"></textarea>
            </div>
            <p class="admin-journal-save-state soft" data-journal-save-state hidden>Saved</p>
        `;
        meta.querySelector("[name='title']").value = payload.title || "";
        meta.querySelector("[name='tags']").value = Array.isArray(payload.session_tags)
            ? payload.session_tags.join(", ")
            : "";
        meta.querySelector("[name='notes']").value = payload.notes || "";

        const ctx = payload.context_summary || {};
        const refs = Array.isArray(ctx.refs) ? ctx.refs : [];
        const details = document.createElement("details");
        details.className = "admin-journal-context";
        details.open = true;
        const summary = document.createElement("summary");
        summary.textContent = "Context summary";
        details.appendChild(summary);
        const line = document.createElement("p");
        line.textContent = ctx.line || "";
        details.appendChild(line);
        const refList = document.createElement("div");
        refList.className = "admin-journal-ref-list";
        if (refs.length) {
            refs.forEach((ref) => {
                if (ref.url) {
                    const a = document.createElement("a");
                    a.href = ref.url;
                    a.className = "admin-journal-ref-pill";
                    a.textContent = `${ref.ref || ""} ${ref.label || ""}`.trim();
                    refList.appendChild(a);
                } else {
                    const span = document.createElement("span");
                    span.className = "admin-journal-ref-pill";
                    span.textContent = `${ref.ref || ""} ${ref.label || ""}`.trim();
                    refList.appendChild(span);
                }
            });
        } else {
            const span = document.createElement("span");
            span.className = "soft";
            span.textContent = "No closed trades in this scope.";
            refList.appendChild(span);
        }
        details.appendChild(refList);

        const log = document.createElement("div");
        log.className = "admin-journal-chat-log";
        log.setAttribute("data-journal-chat-log", "");

        const form = document.createElement("form");
        form.className = "admin-journal-chat-form";
        form.setAttribute("data-journal-chat-form", "");
        form.innerHTML = `
            <label for="dashboardJournalInlineChat">Message</label>
            <textarea id="dashboardJournalInlineChat" data-journal-chat-input rows="3" maxlength="1200" placeholder="Ask about this scoped set of trades"></textarea>
            <div class="admin-journal-chat-actions">
                <p class="bad admin-journal-error" data-journal-chat-error hidden></p>
                <button type="submit" class="submit-btn">Send</button>
            </div>
        `;

        section.appendChild(meta);
        section.appendChild(details);
        section.appendChild(log);
        section.appendChild(form);

        const appendBubble = window.FXJAppendJournalMessageBubble;
        if (typeof appendBubble === "function" && Array.isArray(payload.messages)) {
            payload.messages.forEach((msg) => {
                const role = msg.role || "user";
                appendBubble(section, log, role, msg.text || "", msg.segments, role === "assistant" ? msg.id : undefined);
            });
        }

        return section;
    };

    const openSessionPayload = (payload) => {
        if (!sessionPanel || !startPanel) {
            return;
        }
        clearSurfaceError();
        startPanel.hidden = true;
        sessionPanel.hidden = false;
        sessionPanel.innerHTML = "";

        const toolbar = document.createElement("div");
        toolbar.className = "dashboard-journal-inline-toolbar";
        const back = document.createElement("button");
        back.type = "button";
        back.className = "ghost-btn";
        back.textContent = "← New reflection";
        back.addEventListener("click", resetToStart);
        toolbar.appendChild(back);
        sessionPanel.appendChild(toolbar);

        const section = buildSessionSection(payload);
        sessionPanel.appendChild(section);

        if (typeof window.FXJBindAdminJournalSessionRoot === "function") {
            window.FXJBindAdminJournalSessionRoot(section);
        }
    };

    const postJson = async (url, body) => {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken,
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(body),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(responseErrorMessage(response, data, "Could not start reflection."));
        }
        return data;
    };

    const loadSession = async (sessionId) => {
        const url = sessionDetailUrl(sessionId);
        if (!url) {
            throw new Error("Missing session URL.");
        }
        const response = await fetch(url, {
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(responseErrorMessage(response, data, "Could not load session."));
        }
        return data;
    };

    surface.querySelectorAll("[data-dashboard-journal-start-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            clearSurfaceError();
            const scope = (form.dataset.journalScope || "").trim().toLowerCase();
            const body = { scope_type: scope };
            if (scope === "trade") {
                const sel = form.querySelector("select[name='scope_trade_pubkey']");
                const v = sel ? String(sel.value || "").trim() : "";
                if (!v) {
                    showSurfaceError("Choose a closed trade first.");
                    return;
                }
                body.scope_trade_pubkey = v;
            } else if (scope === "day") {
                const inp = form.querySelector("input[name='scope_date']");
                const v = inp ? String(inp.value || "").trim() : "";
                if (!v) {
                    showSurfaceError("Pick a date.");
                    return;
                }
                body.scope_date = v;
            }
            try {
                const payload = await postJson(createUrl, body);
                openSessionPayload(payload);
            } catch (err) {
                showSurfaceError(err.message || "Could not start reflection.");
            }
        });
    });

    surface.querySelectorAll("[data-dashboard-journal-open-session]").forEach((button) => {
        button.addEventListener("click", async () => {
            const sid = button.dataset.dashboardJournalOpenSession;
            if (!sid) {
                return;
            }
            clearSurfaceError();
            try {
                const payload = await loadSession(sid);
                openSessionPayload(payload);
            } catch (err) {
                showSurfaceError(err.message || "Could not open session.");
            }
        });
    });
})();
