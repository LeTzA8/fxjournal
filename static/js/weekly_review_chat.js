(function () {
    const chatRoots = document.querySelectorAll("[data-weekly-review-chat]");
    if (!chatRoots.length) {
        return;
    }

    const bindCitationButton = (button) => {
        if (window.FXJBindWeeklyReviewCitationButton) {
            window.FXJBindWeeklyReviewCitationButton(button);
            return;
        }
        button.addEventListener("click", () => {
            if (window.FXJActivateWeeklyReviewCitation) {
                window.FXJActivateWeeklyReviewCitation(button);
            }
        });
        button.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" && event.key !== " ") {
                return;
            }
            event.preventDefault();
            if (window.FXJActivateWeeklyReviewCitation) {
                window.FXJActivateWeeklyReviewCitation(button);
            }
        });
    };

    const createCitationSegment = (segment) => {
        const button = document.createElement("span");
        button.className = "ai-citation-btn";
        button.setAttribute("role", "button");
        button.setAttribute("tabindex", "0");
        button.dataset.citationTone = segment.tone || "neutral";
        button.dataset.citationType = segment.citation_type || "";
        if (segment.citation_type === "trade" && segment.trade_id !== undefined && segment.trade_id !== null) {
            button.dataset.citationTradeId = String(segment.trade_id);
        } else if (segment.citation_type === "bundle" && segment.bundle_key) {
            button.dataset.citationBundle = String(segment.bundle_key);
        }
        button.textContent = segment.label || "";
        bindCitationButton(button);
        return button;
    };

    const createBubble = (role, text, extraClass, segments) => {
        const bubble = document.createElement("div");
        bubble.className = `weekly-review-chat-bubble weekly-review-chat-bubble--${role}`;
        if (extraClass) {
            bubble.classList.add(extraClass);
        }
        if (Array.isArray(segments) && segments.length) {
            segments.forEach((segment) => {
                if (segment && segment.type === "citation" && segment.label) {
                    bubble.appendChild(createCitationSegment(segment));
                    return;
                }
                bubble.appendChild(document.createTextNode((segment && segment.text) || ""));
            });
        } else {
            bubble.textContent = text;
        }
        return bubble;
    };

    const createLoadingBubble = () => {
        const bubble = document.createElement("div");
        bubble.className =
            "weekly-review-chat-bubble weekly-review-chat-bubble--assistant weekly-review-chat-bubble--loading";
        bubble.setAttribute("aria-busy", "true");
        bubble.setAttribute("aria-label", "Assistant is responding");
        const row = document.createElement("div");
        row.className = "weekly-review-chat-typing";
        for (let i = 0; i < 3; i += 1) {
            const dot = document.createElement("span");
            dot.className = "weekly-review-chat-typing-dot";
            row.appendChild(dot);
        }
        bubble.appendChild(row);
        return bubble;
    };

    const limitErrorMessages = {
        review_limit_reached: "You've reached the follow-up limit for this review.",
        daily_limit_reached: "You've reached today's AI chat limit.",
        rate_limit_exceeded: "You're sending messages too quickly. Try again in a moment.",
    };

    const MIN_SEND_SPACING_MS = 2000;

    chatRoots.forEach((root) => {
        const form = root.querySelector("[data-review-chat-form]");
        const input = root.querySelector("[data-review-chat-input]");
        const log = root.querySelector("[data-review-chat-log]");
        const error = root.querySelector("[data-review-chat-error]");
        const promptButtons = root.querySelectorAll("[data-review-chat-prompt]");
        const chatUrl = root.dataset.chatUrl || "";
        const csrfToken = root.dataset.csrfToken || "";

        if (!form || !input || !log || !chatUrl) {
            return;
        }

        const setBusy = (isBusy) => {
            input.disabled = isBusy;
            form.querySelectorAll("button").forEach((button) => {
                button.disabled = isBusy;
            });
            promptButtons.forEach((button) => {
                button.disabled = isBusy;
            });
        };

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

        const submitQuestion = async (rawMessage) => {
            const message = String(rawMessage || "").trim();
            if (!message) {
                showError("Ask a question about this review first.");
                input.focus();
                return;
            }

            const sendStartedAt = Date.now();
            clearError();
            log.appendChild(createBubble("user", message));
            const loadingBubble = createLoadingBubble();
            log.appendChild(loadingBubble);
            input.value = "";
            setBusy(true);

            try {
                const response = await fetch(chatUrl, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": csrfToken,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    body: JSON.stringify({ message }),
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    const code = payload.error;
                    const friendly =
                        (code && limitErrorMessages[code]) ||
                        code ||
                        "Could not answer that right now.";
                    throw new Error(friendly);
                }
                loadingBubble.remove();
                log.appendChild(
                    createBubble(
                        "assistant",
                        payload.reply || "I could not produce a reply.",
                        null,
                        payload.segments,
                    ),
                );
            } catch (err) {
                loadingBubble.remove();
                showError(err.message || "Could not answer that right now. Please try again.");
            } finally {
                const elapsed = Date.now() - sendStartedAt;
                if (elapsed < MIN_SEND_SPACING_MS) {
                    await new Promise((resolve) =>
                        setTimeout(resolve, MIN_SEND_SPACING_MS - elapsed),
                    );
                }
                setBusy(false);
                input.focus();
            }
        };

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            submitQuestion(input.value);
        });

        promptButtons.forEach((button) => {
            button.addEventListener("click", () => {
                submitQuestion(button.dataset.reviewChatPrompt || button.textContent || "");
            });
        });
    });
})();
