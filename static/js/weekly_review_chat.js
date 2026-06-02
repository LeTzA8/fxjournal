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
        button.dataset.citationLabel = segment.label || "";
        button.dataset.citationTone = segment.tone || "neutral";
        button.dataset.citationType = segment.citation_type || "";
        if (segment.ref) {
            button.dataset.citationRef = String(segment.ref);
        }
        if (segment.trade_id !== undefined && segment.trade_id !== null && segment.trade_id !== "") {
            button.dataset.citationTradeId = String(segment.trade_id);
        }
        if (segment.trade_pubkey) {
            button.dataset.citationTradePubkey = String(segment.trade_pubkey);
        }
        if (segment.bundle_key) {
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

    const createDynamicPromptRow = (prompts) => {
        const row = document.createElement("div");
        row.className = "weekly-review-chat-prompts weekly-review-chat-prompts--dynamic";
        row.setAttribute("data-review-chat-dynamic", "");
        row.setAttribute("aria-label", "Suggested follow-up questions");
        (prompts || []).forEach((prompt) => {
            const text = String(prompt || "").trim();
            if (!text) {
                return;
            }
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.reviewChatPrompt = text;
            button.textContent = text;
            row.appendChild(button);
        });
        return row;
    };

    const limitErrorMessages = {
        review_limit_reached: "You've reached the follow-up limit for this review.",
        daily_limit_reached: "You've reached today's AI chat limit.",
        weekly_followup_trial_limit_reached:
            "Your free trial includes 5 follow-up messages. Join the waitlist to unlock full review conversations.",
        weekly_followup_trial_required:
            "Follow-up chat is part of the Trader workflow. Join the waitlist for early access.",
        rate_limit_exceeded: "You're sending messages too quickly. Try again in a moment.",
    };

    const MIN_SEND_SPACING_MS = 2000;

    const scrollChatToBottom = (log) => {
        if (!log) {
            return;
        }
        log.scrollTop = log.scrollHeight;
    };

    chatRoots.forEach((root) => {
        const form = root.querySelector("[data-review-chat-form]");
        const input = root.querySelector("[data-review-chat-input]");
        const log = root.querySelector("[data-review-chat-log]");
        const error = root.querySelector("[data-review-chat-error]");
        const starterPrompts = root.querySelector("[data-review-chat-starters]");
        const chatUrl = root.dataset.chatUrl || "";
        const csrfToken = root.dataset.csrfToken || "";
        let canSend = root.dataset.canSend !== "false";

        if (!form || !input || !log || !chatUrl) {
            return;
        }

        scrollChatToBottom(log);

        const getPromptButtons = () =>
            root.querySelectorAll(
                "[data-review-chat-starters] [data-review-chat-prompt], [data-review-chat-dynamic] [data-review-chat-prompt]",
            );

        const clearDynamicPromptRows = () => {
            log.querySelectorAll("[data-review-chat-dynamic]").forEach((row) => row.remove());
        };

        const setBusy = (isBusy) => {
            input.disabled = isBusy || !canSend;
            form.querySelectorAll("button").forEach((button) => {
                button.disabled = isBusy || !canSend;
            });
            getPromptButtons().forEach((button) => {
                button.disabled = isBusy || !canSend;
            });
        };

        const bindPromptButton = (button, submitQuestion) => {
            button.addEventListener("click", () => {
                submitQuestion(button.dataset.reviewChatPrompt || button.textContent || "");
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
            if (!canSend) {
                showError("Follow-up chat is part of the Trader workflow. Join the waitlist for early access.");
                return;
            }
            if (!message) {
                showError("Ask a question about this review first.");
                input.focus();
                return;
            }

            const sendStartedAt = Date.now();
            clearError();
            if (starterPrompts) {
                starterPrompts.hidden = true;
            }
            const userBubble = createBubble("user", message);
            log.appendChild(userBubble);
            scrollChatToBottom(log);
            const loadingBubble = createLoadingBubble();
            log.appendChild(loadingBubble);
            scrollChatToBottom(log);
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
                    if (
                        code === "weekly_followup_trial_limit_reached" ||
                        code === "weekly_followup_trial_required"
                    ) {
                        canSend = false;
                        const upgrade = root.querySelector("[data-review-chat-upgrade]");
                        if (upgrade) {
                            upgrade.hidden = false;
                            if (payload.message) {
                                const copy = upgrade.querySelector("p");
                                if (copy) {
                                    copy.textContent = payload.message;
                                }
                            }
                        }
                    }
                    const friendly =
                        payload.message ||
                        (code && limitErrorMessages[code]) ||
                        code ||
                        "Could not answer that right now.";
                    throw new Error(friendly);
                }
                loadingBubble.remove();
                clearDynamicPromptRows();
                log.appendChild(
                    createBubble(
                        "assistant",
                        payload.reply || "I could not produce a reply.",
                        null,
                        payload.segments,
                    ),
                );
                const dynamicPrompts = Array.isArray(payload.suggested_prompts)
                    ? payload.suggested_prompts
                    : [];
                if (dynamicPrompts.length) {
                    const dynamicRow = createDynamicPromptRow(dynamicPrompts);
                    dynamicRow.querySelectorAll("[data-review-chat-prompt]").forEach((button) => {
                        bindPromptButton(button, submitQuestion);
                    });
                    log.appendChild(dynamicRow);
                }
                scrollChatToBottom(log);
            } catch (err) {
                loadingBubble.remove();
                showError(err.message || "Could not answer that right now. Please try again.");
                scrollChatToBottom(log);
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

        getPromptButtons().forEach((button) => bindPromptButton(button, submitQuestion));
    });
})();
