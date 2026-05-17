(function () {
    const surface = document.querySelector("[data-dashboard-ai-journal]");
    if (!surface) {
        return;
    }

    const createUrl = surface.dataset.createSessionUrl || "";
    const contextCandidatesUrl = surface.dataset.contextCandidatesUrl || "";
    const csrfToken = surface.dataset.csrfToken || "";
    const sessionUrlPattern = surface.dataset.journalSessionUrlPattern || "";
    const sessionUrlPlaceholder = surface.dataset.journalSessionUrlPlaceholder || "";

    const startPanel = surface.querySelector("[data-dashboard-journal-start-panel]");
    const sessionPanel = surface.querySelector("[data-dashboard-journal-session-panel]");
    const sessionError = surface.querySelector("[data-dashboard-journal-error]");
    const candidatesPanel = surface.querySelector("[data-dashboard-journal-candidates]");

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
        if (candidatesPanel) {
            candidatesPanel.innerHTML = "";
            candidatesPanel.hidden = true;
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
        // Dashboard journal is the user-facing chat: skip the research feedback row.
        section.dataset.hideFeedback = "1";

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

        const chat = document.createElement("div");
        chat.className = "dashboard-journal-chat";

        const log = document.createElement("div");
        log.className = "admin-journal-chat-log";
        log.setAttribute("data-journal-chat-log", "");

        const form = document.createElement("form");
        form.className = "dashboard-journal-chat-form";
        form.setAttribute("data-journal-chat-form", "");
        form.innerHTML = `
            <p class="bad dashboard-journal-chat-error" data-journal-chat-error hidden></p>
            <div class="dashboard-journal-composer">
                <textarea id="dashboardJournalInlineChat" data-journal-chat-input rows="1" maxlength="1200" placeholder="Reply to keep reflecting..." aria-label="Message"></textarea>
                <button type="submit" class="dashboard-journal-send">Send</button>
            </div>
        `;

        chat.appendChild(log);
        chat.appendChild(form);

        section.appendChild(details);
        section.appendChild(chat);

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
            return null;
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
        back.textContent = "New reflection";
        back.addEventListener("click", resetToStart);
        toolbar.appendChild(back);
        sessionPanel.appendChild(toolbar);

        const section = buildSessionSection(payload);
        sessionPanel.appendChild(section);

        if (typeof window.FXJBindAdminJournalSessionRoot === "function") {
            window.FXJBindAdminJournalSessionRoot(section);
        }

        const chatInput = section.querySelector("[data-journal-chat-input]");
        const chatForm = section.querySelector("[data-journal-chat-form]");
        if (chatInput) {
            const maxComposerHeight = 176;
            const resizeComposer = () => {
                chatInput.style.height = "auto";
                chatInput.style.height = `${Math.min(chatInput.scrollHeight, maxComposerHeight)}px`;
            };
            chatInput.addEventListener("input", resizeComposer);
            chatInput.addEventListener("keydown", (event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    if (chatForm) {
                        chatForm.requestSubmit();
                    }
                }
            });
            if (chatForm) {
                chatForm.addEventListener("submit", () => {
                    window.setTimeout(resizeComposer, 0);
                });
            }
            resizeComposer();
            chatInput.focus();
        }
        return section;
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

    const sessionBodyForCandidate = (candidate) => {
        if (candidate && candidate.session_payload) {
            return candidate.session_payload;
        }
        const scopeType = candidate ? candidate.scope_type : "";
        const body = { scope_type: scopeType };
        if (candidate && candidate.scope_trade_pubkey) {
            body.scope_trade_pubkey = candidate.scope_trade_pubkey;
        }
        if (candidate && candidate.scope_date) {
            body.scope_date = candidate.scope_date;
        }
        return body;
    };

    const sendInitialMessage = async (section, message) => {
        if (!section || !message) {
            return;
        }
        if (typeof window.FXJSendJournalMessage === "function") {
            await window.FXJSendJournalMessage(section, message, {
                input: section.querySelector("[data-journal-chat-input]"),
            });
            return;
        }
        const input = section.querySelector("[data-journal-chat-input]");
        const form = section.querySelector("[data-journal-chat-form]");
        if (input && form) {
            input.value = message;
            form.requestSubmit();
        }
    };

    const confirmCandidate = async (candidate, message, root) => {
        if (!candidate) {
            return;
        }
        clearSurfaceError();
        const buttons = root ? root.querySelectorAll("button") : [];
        buttons.forEach((button) => {
            button.disabled = true;
        });
        try {
            const payload = await postJson(createUrl, sessionBodyForCandidate(candidate));
            const section = openSessionPayload(payload);
            await sendInitialMessage(section, message);
        } catch (err) {
            showSurfaceError(err.message || "Could not start reflection.");
            buttons.forEach((button) => {
                button.disabled = false;
            });
        }
    };

    const renderCandidateCard = (candidate, message, options) => {
        const settings = options || {};
        const card = document.createElement("article");
        card.className = "ai-journal-context-card";
        const title = document.createElement("h4");
        title.textContent = candidate.label || "Suggested context";
        const reason = document.createElement("p");
        reason.textContent = candidate.reason || "";
        const actions = document.createElement("div");
        actions.className = "ai-journal-context-actions";

        const useButton = document.createElement("button");
        useButton.type = "button";
        useButton.textContent = "Use this context";
        useButton.addEventListener("click", () => confirmCandidate(candidate, message, card));
        actions.appendChild(useButton);

        if (settings.showAlternativesButton) {
            const chooseButton = document.createElement("button");
            chooseButton.type = "button";
            chooseButton.textContent = "Choose another";
            chooseButton.setAttribute("data-dashboard-journal-show-alternatives", "");
            chooseButton.addEventListener("click", () => {
                renderCandidates(settings.payload, message, true);
            });
            actions.appendChild(chooseButton);
        }

        card.appendChild(title);
        card.appendChild(reason);
        card.appendChild(actions);
        return card;
    };

    function renderCandidates(payload, message, showAll) {
        if (!candidatesPanel) {
            return;
        }
        const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
        const recommended = payload.recommended || candidates[0];
        candidatesPanel.innerHTML = "";
        candidatesPanel.hidden = false;

        const heading = document.createElement("p");
        heading.className = "ai-journal-context-heading";
        heading.textContent = showAll ? "Choose the context for this reflection" : "I think you mean...";
        candidatesPanel.appendChild(heading);

        if (!candidates.length || !recommended) {
            const empty = document.createElement("p");
            empty.className = "soft";
            empty.textContent = "No context candidates were found. Try naming a trade, day, or week.";
            candidatesPanel.appendChild(empty);
            return;
        }

        const visibleCandidates = showAll ? candidates : [recommended];
        visibleCandidates.forEach((candidate) => {
            candidatesPanel.appendChild(
                renderCandidateCard(candidate, message, {
                    payload,
                    showAlternativesButton: !showAll && candidates.length > 1,
                }),
            );
        });
    }

    surface.querySelectorAll("[data-dashboard-journal-resolve-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            clearSurfaceError();
            if (candidatesPanel) {
                candidatesPanel.innerHTML = "";
                candidatesPanel.hidden = true;
            }
            const input = form.querySelector("[data-dashboard-journal-resolve-input]");
            const message = input ? String(input.value || "").trim() : "";
            if (!message) {
                showSurfaceError("Type what you want to reflect on first.");
                if (input) {
                    input.focus();
                }
                return;
            }
            const submit = form.querySelector("button[type='submit']");
            if (submit) {
                submit.disabled = true;
            }
            try {
                const payload = await postJson(contextCandidatesUrl, { message });
                renderCandidates(payload, message, false);
            } catch (err) {
                showSurfaceError(err.message || "Could not resolve journal context.");
            } finally {
                if (submit) {
                    submit.disabled = false;
                }
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
