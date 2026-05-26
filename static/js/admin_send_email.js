(function () {
    const root = document.getElementById("adminSendEmailRoot");
    if (!root) {
        return;
    }

    const recipientsUrl = root.dataset.recipientsUrl || "";
    const sendUrl = root.dataset.sendUrl || "";
    const csrfToken = root.dataset.csrfToken || "";
    const placeholderToken = root.dataset.placeholder || "{{name}}";
    const defaultInactiveDays = parseInt(root.dataset.defaultInactiveDays || "30", 10) || 30;
    const sendDelayMs = Math.max(100, parseInt(root.dataset.sendDelayMs || "400", 10) || 400);

    const chipsEl = document.getElementById("adminSendEmailChips");
    const searchInput = document.getElementById("adminSendEmailToSearch");
    const recipientPanel = document.getElementById("adminSendEmailRecipientPanel");
    const recipientList = document.getElementById("adminSendEmailRecipientList");
    const recipientStatus = document.getElementById("adminSendEmailRecipientStatus");
    const inactiveOnlyInput = document.getElementById("adminSendEmailInactiveOnly");
    const inactiveDaysInput = document.getElementById("adminSendEmailInactiveDays");
    const selectVisibleBtn = document.getElementById("adminSendEmailSelectVisible");
    const clearSelectedBtn = document.getElementById("adminSendEmailClearSelected");
    const subjectInput = document.getElementById("adminSendEmailSubject");
    const insertPlaceholderBtn = document.getElementById("adminSendEmailInsertPlaceholder");
    const sendBtn = document.getElementById("adminSendEmailSendBtn");
    const sendHint = document.getElementById("adminSendEmailSendHint");
    const progressWrap = document.getElementById("adminSendEmailProgress");
    const progressFill = document.getElementById("adminSendEmailProgressFill");
    const progressText = document.getElementById("adminSendEmailProgressText");
    const summaryEl = document.getElementById("adminSendEmailSummary");
    const previewRecipient = document.getElementById("adminSendEmailPreviewRecipient");
    const previewSubject = document.getElementById("adminSendEmailPreviewSubject");
    const previewBody = document.getElementById("adminSendEmailPreviewBody");

    const selectedById = new Map();
    let visibleRecipients = [];
    let fetchTimer = null;
    let highlightedIndex = -1;
    let sending = false;

    const quill = new Quill("#adminSendEmailEditor", {
        theme: "snow",
        modules: {
            toolbar: [
                ["bold", "italic"],
                ["link", "image"],
            ],
        },
        placeholder: "Write your message…",
    });

    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    const applyPlaceholders = (text, name) => {
        if (!text) {
            return "";
        }
        return String(text).split(placeholderToken).join(name || "");
    };

    const getSelectedRecipients = () =>
        Array.from(selectedById.values()).sort((a, b) =>
            String(a.name || "").localeCompare(String(b.name || ""))
        );

    const renderChips = () => {
        chipsEl.textContent = "";
        getSelectedRecipients().forEach((recipient) => {
            const chip = document.createElement("span");
            chip.className = "admin-send-email-chip";
            chip.dataset.userId = String(recipient.id);

            const label = document.createElement("span");
            label.className = "admin-send-email-chip-label";
            label.textContent = `${recipient.name} <${recipient.email}>`;
            chip.appendChild(label);

            const removeBtn = document.createElement("button");
            removeBtn.type = "button";
            removeBtn.className = "admin-send-email-chip-remove";
            removeBtn.setAttribute("aria-label", `Remove ${recipient.name}`);
            removeBtn.textContent = "×";
            removeBtn.addEventListener("click", () => {
                selectedById.delete(recipient.id);
                renderChips();
                renderRecipientList();
                updatePreview();
            });
            chip.appendChild(removeBtn);
            chipsEl.appendChild(chip);
        });
        updatePreview();
    };

    const formatLastLogin = (isoValue) => {
        if (!isoValue) {
            return "Never";
        }
        const date = new Date(isoValue);
        if (Number.isNaN(date.getTime())) {
            return "Unknown";
        }
        return date.toLocaleDateString(undefined, {
            year: "numeric",
            month: "short",
            day: "numeric",
        });
    };

    const renderRecipientList = () => {
        recipientList.textContent = "";
        visibleRecipients.forEach((recipient, index) => {
            const item = document.createElement("li");
            item.className = "admin-send-email-recipient-item";
            item.setAttribute("role", "option");
            if (selectedById.has(recipient.id)) {
                item.classList.add("is-selected");
            }
            if (index === highlightedIndex) {
                item.classList.add("is-highlighted");
            }

            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = selectedById.has(recipient.id);
            checkbox.tabIndex = -1;

            const meta = document.createElement("div");
            meta.className = "admin-send-email-recipient-meta";

            const nameEl = document.createElement("span");
            nameEl.className = "admin-send-email-recipient-name";
            nameEl.textContent = recipient.name;

            const emailEl = document.createElement("span");
            emailEl.className = "admin-send-email-recipient-email";
            emailEl.textContent = recipient.email;

            const tagEl = document.createElement("span");
            tagEl.className = `admin-send-email-recipient-tag${recipient.inactive ? " is-inactive" : ""}`;
            tagEl.textContent = recipient.inactive
                ? `Inactive · last login ${formatLastLogin(recipient.last_login_at)}`
                : `Last login ${formatLastLogin(recipient.last_login_at)}`;

            meta.appendChild(nameEl);
            meta.appendChild(emailEl);
            meta.appendChild(tagEl);

            const toggle = () => {
                if (selectedById.has(recipient.id)) {
                    selectedById.delete(recipient.id);
                } else {
                    selectedById.set(recipient.id, recipient);
                }
                renderChips();
                renderRecipientList();
            };

            item.addEventListener("click", (event) => {
                if (event.target === checkbox) {
                    return;
                }
                toggle();
            });
            checkbox.addEventListener("click", (event) => {
                event.stopPropagation();
                toggle();
            });

            item.appendChild(checkbox);
            item.appendChild(meta);
            recipientList.appendChild(item);
        });

        const count = visibleRecipients.length;
        const selectedCount = getSelectedRecipients().length;
        recipientStatus.textContent = count
            ? `${count} shown · ${selectedCount} selected`
            : "No users match this filter";
        recipientPanel.hidden = count === 0 && !searchInput.value.trim();
    };

    const fetchRecipients = async () => {
        const params = new URLSearchParams();
        const q = searchInput.value.trim();
        if (q) {
            params.set("q", q);
        }
        const inactiveDays = parseInt(inactiveDaysInput.value, 10) || defaultInactiveDays;
        params.set("inactive_days", String(inactiveDays));
        if (inactiveOnlyInput.checked) {
            params.set("inactive_only", "1");
        }
        try {
            const response = await fetch(`${recipientsUrl}?${params.toString()}`, {
                credentials: "same-origin",
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) {
                throw new Error("fetch_failed");
            }
            const data = await response.json();
            visibleRecipients = Array.isArray(data.recipients) ? data.recipients : [];
            highlightedIndex = visibleRecipients.length ? 0 : -1;
            renderRecipientList();
        } catch (_err) {
            recipientStatus.textContent = "Could not load recipients.";
        }
    };

    const scheduleFetchRecipients = () => {
        if (fetchTimer) {
            clearTimeout(fetchTimer);
        }
        fetchTimer = setTimeout(fetchRecipients, 220);
    };

    const updatePreview = () => {
        const selected = getSelectedRecipients();
        const previewTarget = selected[0] || null;
        const subject = subjectInput.value.trim();
        const htmlBody = quill.root.innerHTML.trim();
        const plainBody = quill.getText().trim();

        if (!previewTarget) {
            previewRecipient.textContent = "Select a recipient";
            previewSubject.textContent = subject || "(No subject)";
            previewBody.innerHTML = htmlBody
                ? applyPlaceholders(htmlBody, "Recipient")
                : "<p class=\"soft\">Message preview will appear here.</p>";
            return;
        }

        previewRecipient.textContent = `${previewTarget.name} <${previewTarget.email}>`;
        previewSubject.textContent = applyPlaceholders(subject, previewTarget.name) || "(No subject)";
        if (htmlBody && htmlBody !== "<p><br></p>") {
            previewBody.innerHTML = applyPlaceholders(htmlBody, previewTarget.name);
        } else if (plainBody) {
            previewBody.textContent = applyPlaceholders(plainBody, previewTarget.name);
        } else {
            previewBody.innerHTML = "<p class=\"soft\">Message preview will appear here.</p>";
        }
    };

    const insertPlaceholderAtCursor = () => {
        const range = quill.getSelection(true);
        if (range) {
            quill.insertText(range.index, placeholderToken);
            quill.setSelection(range.index + placeholderToken.length);
        } else {
            const length = quill.getLength();
            quill.insertText(length - 1, placeholderToken);
        }
        updatePreview();
    };

    const sendToSelected = async () => {
        const recipients = getSelectedRecipients();
        const subject = subjectInput.value.trim();
        const htmlBody = quill.root.innerHTML.trim();
        const textBody = quill.getText().trim();

        if (!recipients.length) {
            sendHint.textContent = "Select at least one recipient.";
            return;
        }
        if (!subject) {
            sendHint.textContent = "Enter a subject.";
            subjectInput.focus();
            return;
        }
        if (!htmlBody || htmlBody === "<p><br></p>") {
            if (!textBody) {
                sendHint.textContent = "Write a message body.";
                return;
            }
        }

        sending = true;
        sendBtn.disabled = true;
        sendHint.textContent = "";
        summaryEl.hidden = true;
        progressWrap.hidden = false;

        let sentCount = 0;
        let failedCount = 0;
        const failedDetails = [];
        const total = recipients.length;

        for (let index = 0; index < recipients.length; index += 1) {
            const recipient = recipients[index];
            const done = index + 1;
            const pct = Math.round((done / total) * 100);
            progressFill.style.width = `${pct}%`;
            progressText.textContent = `Sending ${done} of ${total} to ${recipient.email}…`;

            try {
                const response = await fetch(sendUrl, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": csrfToken,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    body: JSON.stringify({
                        user_id: recipient.id,
                        subject,
                        html_body: htmlBody,
                        text_body: textBody,
                    }),
                });
                const data = await response.json().catch(() => ({}));
                if (response.ok && data.sent) {
                    sentCount += 1;
                } else {
                    failedCount += 1;
                    failedDetails.push(`${recipient.email} (${data.mode || "error"})`);
                }
            } catch (_err) {
                failedCount += 1;
                failedDetails.push(`${recipient.email} (network)`);
            }

            if (index < recipients.length - 1) {
                await sleep(sendDelayMs);
            }
        }

        progressFill.style.width = "100%";
        progressText.textContent = "Done.";
        summaryEl.hidden = false;
        summaryEl.innerHTML = [
            `<span class="good">${sentCount} sent</span>`,
            failedCount ? ` · <span class="bad">${failedCount} failed</span>` : "",
            failedDetails.length
                ? `<p class="soft" style="margin:0.5rem 0 0">${failedDetails.slice(0, 8).join("<br>")}${
                      failedDetails.length > 8 ? "<br>…" : ""
                  }</p>`
                : "",
        ].join("");

        sending = false;
        sendBtn.disabled = false;
    };

    searchInput.addEventListener("input", () => {
        recipientPanel.hidden = false;
        scheduleFetchRecipients();
    });

    searchInput.addEventListener("focus", () => {
        recipientPanel.hidden = false;
        if (!visibleRecipients.length) {
            fetchRecipients();
        }
    });

    searchInput.addEventListener("keydown", (event) => {
        if (!visibleRecipients.length) {
            return;
        }
        if (event.key === "ArrowDown") {
            event.preventDefault();
            highlightedIndex = Math.min(highlightedIndex + 1, visibleRecipients.length - 1);
            renderRecipientList();
        } else if (event.key === "ArrowUp") {
            event.preventDefault();
            highlightedIndex = Math.max(highlightedIndex - 1, 0);
            renderRecipientList();
        } else if (event.key === "Enter" && highlightedIndex >= 0) {
            event.preventDefault();
            const recipient = visibleRecipients[highlightedIndex];
            if (recipient) {
                if (selectedById.has(recipient.id)) {
                    selectedById.delete(recipient.id);
                } else {
                    selectedById.set(recipient.id, recipient);
                }
                renderChips();
                renderRecipientList();
            }
        } else if (event.key === "Escape") {
            recipientPanel.hidden = true;
        }
    });

    inactiveOnlyInput.addEventListener("change", fetchRecipients);
    inactiveDaysInput.addEventListener("change", fetchRecipients);

    selectVisibleBtn.addEventListener("click", () => {
        visibleRecipients.forEach((recipient) => {
            selectedById.set(recipient.id, recipient);
        });
        renderChips();
        renderRecipientList();
    });

    clearSelectedBtn.addEventListener("click", () => {
        selectedById.clear();
        renderChips();
        renderRecipientList();
    });

    subjectInput.addEventListener("input", updatePreview);
    quill.on("text-change", updatePreview);
    insertPlaceholderBtn.addEventListener("click", insertPlaceholderAtCursor);
    sendBtn.addEventListener("click", sendToSelected);

    inactiveDaysInput.value = String(defaultInactiveDays);
    fetchRecipients();
})();
