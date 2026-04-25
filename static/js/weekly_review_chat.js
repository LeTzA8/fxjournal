(function () {
    const chatRoots = document.querySelectorAll("[data-weekly-review-chat]");
    if (!chatRoots.length) {
        return;
    }

    const createBubble = (role, text, extraClass) => {
        const bubble = document.createElement("div");
        bubble.className = `weekly-review-chat-bubble weekly-review-chat-bubble--${role}`;
        if (extraClass) {
            bubble.classList.add(extraClass);
        }
        bubble.textContent = text;
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
                    throw new Error(payload.error || "Could not answer that right now.");
                }
                loadingBubble.remove();
                log.appendChild(createBubble("assistant", payload.reply || "I could not produce a reply."));
            } catch (err) {
                loadingBubble.remove();
                showError(err.message || "Could not answer that right now. Please try again.");
            } finally {
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
