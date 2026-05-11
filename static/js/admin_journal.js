(function () {
    const roots = document.querySelectorAll("[data-admin-journal-session]");
    if (!roots.length) {
        return;
    }

    const createCitationSegment = (segment) => {
        const link = document.createElement("a");
        link.className = "ai-citation-btn admin-journal-citation";
        link.dataset.citationTone = segment.tone || "neutral";
        link.dataset.citationType = segment.citation_type || "trade";
        link.textContent = segment.label || "";
        link.href = segment.trade_url || "#";
        return link;
    };

    const fillSegments = (node, text, segments) => {
        node.textContent = "";
        if (!Array.isArray(segments) || !segments.length) {
            node.textContent = text || "";
            return;
        }
        segments.forEach((segment) => {
            if (segment && segment.type === "citation") {
                node.appendChild(createCitationSegment(segment));
                return;
            }
            node.appendChild(document.createTextNode((segment && segment.text) || ""));
        });
    };

    const createFeedbackControls = (messageId, root) => {
        const row = document.createElement("div");
        row.className = "admin-journal-feedback";
        row.dataset.feedbackCurrent = "";
        [
            ["useful", "Useful"],
            ["generic", "Generic"],
            ["needed_more_context", "Missing context"],
            ["missing_feature", "Missing feature"],
        ].forEach(([value, label]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.feedback = value;
            button.textContent = label;
            row.appendChild(button);
        });
        const note = document.createElement("input");
        note.type = "text";
        note.dataset.feedbackNote = "";
        note.placeholder = "Optional note";
        row.appendChild(note);
        bindFeedback(row, messageId, root);
        return row;
    };

    const createBubble = (role, text, segments, messageId, root) => {
        const bubble = document.createElement("div");
        bubble.className = `admin-journal-bubble admin-journal-bubble--${role}`;
        if (messageId) {
            bubble.dataset.messageId = String(messageId);
        }
        const body = document.createElement("div");
        body.className = "admin-journal-bubble-body";
        fillSegments(body, text, segments);
        bubble.appendChild(body);
        if (role === "assistant" && messageId) {
            bubble.appendChild(createFeedbackControls(messageId, root));
        }
        return bubble;
    };

    const createLoadingBubble = () => {
        const bubble = document.createElement("div");
        bubble.className = "admin-journal-bubble admin-journal-bubble--assistant admin-journal-bubble--loading";
        bubble.setAttribute("aria-busy", "true");
        bubble.textContent = "Thinking...";
        return bubble;
    };

    const feedbackUrl = (root, messageId) => {
        const template = root.dataset.feedbackTemplate || "";
        return template.replace(/\/0\/feedback$/, `/${messageId}/feedback`);
    };

    function bindFeedback(row, messageId, root) {
        const noteInput = row.querySelector("[data-feedback-note]");
        row.querySelectorAll("[data-feedback]").forEach((button) => {
            button.addEventListener("click", async () => {
                const feedback = button.dataset.feedback || "";
                const feedback_note = noteInput ? noteInput.value.trim() : "";
                row.querySelectorAll("button").forEach((item) => {
                    item.disabled = true;
                });
                try {
                    const response = await fetch(feedbackUrl(root, messageId), {
                        method: "POST",
                        credentials: "same-origin",
                        headers: {
                            "Content-Type": "application/json",
                            "X-CSRFToken": root.dataset.csrfToken || "",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        body: JSON.stringify({ feedback, feedback_note }),
                    });
                    if (!response.ok) {
                        throw new Error("Could not save feedback.");
                    }
                    row.dataset.feedbackCurrent = feedback;
                } catch (error) {
                    window.alert(error.message || "Could not save feedback.");
                } finally {
                    row.querySelectorAll("button").forEach((item) => {
                        item.disabled = false;
                    });
                }
            });
        });
    }

    roots.forEach((root) => {
        const form = root.querySelector("[data-journal-chat-form]");
        const input = root.querySelector("[data-journal-chat-input]");
        const log = root.querySelector("[data-journal-chat-log]");
        const error = root.querySelector("[data-journal-chat-error]");
        const meta = root.querySelector("[data-journal-tags-form]");
        const saveState = root.querySelector("[data-journal-save-state]");

        const showError = (message) => {
            if (!error) {
                return;
            }
            error.textContent = message;
            error.hidden = false;
        };

        const clearError = () => {
            if (!error) {
                return;
            }
            error.textContent = "";
            error.hidden = true;
        };

        const saveMeta = async () => {
            if (!meta || !root.dataset.tagsUrl) {
                return;
            }
            const title = meta.querySelector("[name='title']")?.value || "";
            const tags = meta.querySelector("[name='tags']")?.value || "";
            const notes = meta.querySelector("[name='notes']")?.value || "";
            const response = await fetch(root.dataset.tagsUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": root.dataset.csrfToken || "",
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({ title, tags, notes }),
            });
            if (!response.ok) {
                throw new Error("Could not save session details.");
            }
            if (saveState) {
                saveState.hidden = false;
                window.setTimeout(() => {
                    saveState.hidden = true;
                }, 1800);
            }
        };

        if (meta) {
            meta.querySelectorAll("input, textarea").forEach((field) => {
                field.addEventListener("blur", () => {
                    saveMeta().catch((error) => window.alert(error.message));
                });
            });
        }

        root.querySelectorAll(".admin-journal-feedback").forEach((row) => {
            const bubble = row.closest("[data-message-id]");
            const messageId = bubble ? bubble.dataset.messageId : "";
            if (messageId) {
                bindFeedback(row, messageId, root);
            }
        });

        if (!form || !input || !log) {
            return;
        }

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const message = input.value.trim();
            if (!message) {
                showError("Ask a journal question first.");
                input.focus();
                return;
            }
            clearError();
            const userBubble = createBubble("user", message);
            const loadingBubble = createLoadingBubble();
            log.appendChild(userBubble);
            log.appendChild(loadingBubble);
            input.value = "";
            input.disabled = true;

            try {
                const response = await fetch(root.dataset.chatUrl || "", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": root.dataset.csrfToken || "",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    body: JSON.stringify({ message }),
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(payload.error || "Could not answer that right now.");
                }
                loadingBubble.remove();
                log.appendChild(
                    createBubble(
                        "assistant",
                        payload.reply || "",
                        payload.segments || [],
                        payload.message_id,
                        root,
                    ),
                );
            } catch (error) {
                loadingBubble.remove();
                userBubble.remove();
                showError(error.message || "Could not answer that right now.");
            } finally {
                input.disabled = false;
                input.focus();
            }
        });
    });
})();
