(function () {
    const root = document.getElementById("adminSendEmailRoot");
    if (!root) {
        return;
    }

    const recipientsUrl = root.dataset.recipientsUrl || "";
    const sendUrl = root.dataset.sendUrl || "";
    const signaturesUrl = root.dataset.signaturesUrl || "";
    const signaturesSaveUrl = root.dataset.signaturesSaveUrl || "";
    const signaturesDeleteUrl = root.dataset.signaturesDeleteUrl || "";
    const csrfToken = root.dataset.csrfToken || "";
    const placeholderToken = root.dataset.placeholder || "{{name}}";
    const sampleName = root.dataset.sampleName || "Alex";
    const logoUrl = root.dataset.logoUrl || "";
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
    const messageInput = document.getElementById("adminSendEmailMessage");
    const insertPlaceholderBtn = document.getElementById("adminSendEmailInsertPlaceholder");
    const insertLogoBtn = document.getElementById("adminSendEmailInsertLogo");
    const insertImageUrlBtn = document.getElementById("adminSendEmailInsertImageUrl");
    const sendBtn = document.getElementById("adminSendEmailSendBtn");
    const sendHint = document.getElementById("adminSendEmailSendHint");
    const progressWrap = document.getElementById("adminSendEmailProgress");
    const progressFill = document.getElementById("adminSendEmailProgressFill");
    const progressText = document.getElementById("adminSendEmailProgressText");
    const summaryEl = document.getElementById("adminSendEmailSummary");
    const previewRecipient = document.getElementById("adminSendEmailPreviewRecipient");
    const previewSubject = document.getElementById("adminSendEmailPreviewSubject");
    const previewBody = document.getElementById("adminSendEmailPreviewBody");
    const signatureSelect = document.getElementById("adminSendEmailSignatureSelect");
    const insertSignatureBtn = document.getElementById("adminSendEmailInsertSignature");
    const saveSignatureBtn = document.getElementById("adminSendEmailSaveSignature");
    const deleteSignatureBtn = document.getElementById("adminSendEmailDeleteSignature");
    const signatureStatus = document.getElementById("adminSendEmailSignatureStatus");
    const signatureDialog = document.getElementById("adminSendEmailSignatureDialog");
    const signatureDialogTitle = document.getElementById("adminSendEmailSignatureDialogTitle");
    const signatureForm = document.getElementById("adminSendEmailSignatureForm");
    const signatureIdInput = document.getElementById("adminSendEmailSignatureId");
    const signatureNameInput = document.getElementById("adminSendEmailSignatureName");
    const signatureBodyInput = document.getElementById("adminSendEmailSignatureBody");
    const signatureCancelBtn = document.getElementById("adminSendEmailSignatureCancel");

    const selectedById = new Map();
    let visibleRecipients = [];
    let signatures = [];
    let fetchTimer = null;
    let highlightedIndex = -1;
    let sending = false;

    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    const HTML_LINE_RE = /^\s*<\/?(?:img|a|p|div|span|br|strong|em|ul|ol|li|h[1-6])\b/im;

    const escapeHtml = (text) =>
        String(text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");

    const messageContainsHtml = (text) => HTML_LINE_RE.test(String(text || ""));

    const buildHtmlBody = (text) => {
        if (!messageContainsHtml(text)) {
            return "";
        }
        return String(text)
            .split("\n")
            .map((line) => {
                const trimmed = line.trim();
                if (HTML_LINE_RE.test(trimmed)) {
                    return trimmed;
                }
                return escapeHtml(line);
            })
            .join("<br>");
    };

    const buildLogoMarkup = () => {
        if (!logoUrl) {
            return '<img src="/static/site-logo.png" alt="MyFXJournal" height="36" style="display:block;" />';
        }
        return `<img src="${logoUrl}" alt="MyFXJournal" height="36" style="display:block;" />`;
    };

    const buildImageMarkup = (url) =>
        `<img src="${url}" alt="" style="display:block;max-width:100%;height:auto;" />`;

    const isSafeImageUrl = (rawUrl) => {
        const value = String(rawUrl || "").trim();
        if (!value) {
            return false;
        }
        try {
            const parsed = new URL(value);
            return parsed.protocol === "http:" || parsed.protocol === "https:";
        } catch (_err) {
            return false;
        }
    };

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

    const setEmptyPreview = (message) => {
        previewBody.classList.remove("is-html");
        previewBody.textContent = "";
        const empty = document.createElement("p");
        empty.className = "soft admin-send-email-empty-preview";
        empty.textContent = message;
        previewBody.appendChild(empty);
    };

    const updateSendHint = () => {
        if (sending) {
            return;
        }
        const selectedCount = selectedById.size;
        sendBtn.disabled = selectedCount === 0;
        sendHint.textContent = selectedCount
            ? `${selectedCount} selected. Emails send one at a time.`
            : "Select at least one user.";
    };

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
            removeBtn.textContent = "x";
            removeBtn.addEventListener("click", () => {
                selectedById.delete(recipient.id);
                renderChips();
                renderRecipientList();
            });
            chip.appendChild(removeBtn);
            chipsEl.appendChild(chip);
        });
        updatePreview();
        updateSendHint();
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

    const toggleRecipient = (recipient) => {
        if (selectedById.has(recipient.id)) {
            selectedById.delete(recipient.id);
        } else {
            selectedById.set(recipient.id, recipient);
        }
        renderChips();
        renderRecipientList();
    };

    const renderRecipientList = () => {
        recipientList.textContent = "";
        visibleRecipients.forEach((recipient, index) => {
            const item = document.createElement("li");
            item.className = "admin-send-email-recipient-item";
            item.setAttribute("role", "option");
            item.setAttribute("aria-selected", selectedById.has(recipient.id) ? "true" : "false");
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
                ? `Inactive, last login ${formatLastLogin(recipient.last_login_at)}`
                : `Last login ${formatLastLogin(recipient.last_login_at)}`;

            meta.appendChild(nameEl);
            meta.appendChild(emailEl);
            meta.appendChild(tagEl);

            item.addEventListener("click", (event) => {
                if (event.target === checkbox) {
                    return;
                }
                toggleRecipient(recipient);
            });
            checkbox.addEventListener("click", (event) => {
                event.stopPropagation();
                toggleRecipient(recipient);
            });

            item.appendChild(checkbox);
            item.appendChild(meta);
            recipientList.appendChild(item);
        });

        const count = visibleRecipients.length;
        const selectedCount = getSelectedRecipients().length;
        recipientStatus.textContent = count
            ? `${count} shown, ${selectedCount} selected`
            : "No users match this filter";
        recipientPanel.hidden = count === 0 && !searchInput.value.trim();
        updateSendHint();
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
            visibleRecipients = [];
            recipientList.textContent = "";
            recipientStatus.textContent = "Could not load recipients.";
            updateSendHint();
        }
    };

    const scheduleFetchRecipients = () => {
        if (fetchTimer) {
            clearTimeout(fetchTimer);
        }
        fetchTimer = setTimeout(fetchRecipients, 220);
    };

    function updatePreview() {
        const selected = getSelectedRecipients();
        const previewTarget = selected[0] || { name: sampleName, email: "" };
        const subject = subjectInput.value.trim();
        const message = messageInput.value.trim();

        if (selected.length) {
            previewRecipient.textContent = `${previewTarget.name} <${previewTarget.email}>`;
        } else {
            previewRecipient.textContent = `Sample preview (${sampleName})`;
        }

        previewSubject.textContent =
            applyPlaceholders(subject, previewTarget.name) || "(No subject)";

        if (!message) {
            setEmptyPreview("Message preview will appear here.");
            return;
        }

        const previewContent = applyPlaceholders(message, previewTarget.name);
        if (messageContainsHtml(previewContent)) {
            previewBody.classList.add("is-html");
            previewBody.innerHTML = buildHtmlBody(previewContent);
            return;
        }

        previewBody.classList.remove("is-html");
        previewBody.textContent = previewContent;
    }

    const insertAtCursor = (field, token) => {
        field.focus();
        const start = field.selectionStart ?? field.value.length;
        const end = field.selectionEnd ?? field.value.length;
        field.value = `${field.value.slice(0, start)}${token}${field.value.slice(end)}`;
        const nextPosition = start + token.length;
        field.setSelectionRange(nextPosition, nextPosition);
        field.dispatchEvent(new Event("input", { bubbles: true }));
    };

    const insertPlaceholderAtCursor = () => {
        const active = document.activeElement;
        insertAtCursor(active === subjectInput ? subjectInput : messageInput, placeholderToken);
    };

    const insertLogoAtCursor = () => {
        insertAtCursor(messageInput, `\n${buildLogoMarkup()}\n`);
    };

    const insertImageUrlAtCursor = () => {
        const rawUrl = window.prompt("Image URL (https://...)", logoUrl || "https://");
        if (rawUrl === null) {
            return;
        }
        if (!isSafeImageUrl(rawUrl)) {
            window.alert("Enter a valid http or https image URL.");
            return;
        }
        insertAtCursor(messageInput, `\n${buildImageMarkup(rawUrl.trim())}\n`);
    };

    const setSignatureStatus = (message) => {
        if (signatureStatus) {
            signatureStatus.textContent = message;
        }
    };

    const getSelectedSignature = () => {
        const signatureId = signatureSelect ? signatureSelect.value : "";
        if (!signatureId) {
            return null;
        }
        return signatures.find((item) => item.id === signatureId) || null;
    };

    const renderSignatureSelect = (preferredId = "") => {
        if (!signatureSelect) {
            return;
        }
        const selectedId = preferredId || signatureSelect.value || "";
        signatureSelect.textContent = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = signatures.length ? "Choose a saved signature" : "No saved signatures yet";
        signatureSelect.appendChild(placeholder);
        signatures.forEach((signature) => {
            const option = document.createElement("option");
            option.value = signature.id;
            option.textContent = signature.name;
            signatureSelect.appendChild(option);
        });
        signatureSelect.value = signatures.some((item) => item.id === selectedId) ? selectedId : "";
        if (deleteSignatureBtn) {
            deleteSignatureBtn.disabled = !signatureSelect.value;
        }
    };

    const fetchSignatures = async () => {
        if (!signaturesUrl) {
            return;
        }
        try {
            const response = await fetch(signaturesUrl, {
                credentials: "same-origin",
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) {
                throw new Error("fetch_failed");
            }
            const data = await response.json();
            signatures = Array.isArray(data.signatures) ? data.signatures : [];
            renderSignatureSelect();
        } catch (_err) {
            signatures = [];
            renderSignatureSelect();
            setSignatureStatus("Could not load saved signatures.");
        }
    };

    const getMessageSelection = () => {
        const start = messageInput.selectionStart;
        const end = messageInput.selectionEnd;
        if (start == null || end == null || start === end) {
            return "";
        }
        return messageInput.value.slice(start, end);
    };

    const openSignatureDialog = () => {
        if (!signatureDialog || !signatureForm) {
            return;
        }
        const selected = getSelectedSignature();
        const selectedText = getMessageSelection();
        if (selected) {
            signatureDialogTitle.textContent = "Edit signature";
            signatureIdInput.value = selected.id;
            signatureNameInput.value = selected.name;
            signatureBodyInput.value = selected.body;
        } else {
            signatureDialogTitle.textContent = "Save signature";
            signatureIdInput.value = "";
            signatureNameInput.value = "";
            signatureBodyInput.value = selectedText;
        }
        signatureDialog.showModal();
        signatureNameInput.focus();
    };

    const closeSignatureDialog = () => {
        if (signatureDialog && signatureDialog.open) {
            signatureDialog.close();
        }
    };

    const saveSignatureFromDialog = async () => {
        if (!signaturesSaveUrl) {
            return;
        }
        const payload = {
            id: signatureIdInput.value.trim() || undefined,
            name: signatureNameInput.value.trim(),
            body: signatureBodyInput.value.trim(),
        };
        try {
            const response = await fetch(signaturesSaveUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify(payload),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.message || "Could not save signature.");
            }
            signatures = Array.isArray(data.signatures) ? data.signatures : signatures;
            const savedId = data.signature && data.signature.id ? data.signature.id : "";
            renderSignatureSelect(savedId);
            closeSignatureDialog();
            setSignatureStatus(
                payload.id ? "Signature updated." : "Signature saved."
            );
        } catch (err) {
            window.alert(err.message || "Could not save signature.");
        }
    };

    const insertSelectedSignature = () => {
        const selected = getSelectedSignature();
        if (!selected) {
            setSignatureStatus("Choose a saved signature first, or save a new one.");
            signatureSelect.focus();
            return;
        }
        const body = selected.body || "";
        const needsGap =
            messageInput.value.length > 0 &&
            !messageInput.value.endsWith("\n");
        const prefix = needsGap ? "\n\n" : messageInput.value.length ? "\n" : "";
        insertAtCursor(messageInput, `${prefix}${body}`);
        setSignatureStatus(`Inserted "${selected.name}".`);
    };

    const deleteSelectedSignature = async () => {
        const selected = getSelectedSignature();
        if (!selected) {
            setSignatureStatus("Choose a saved signature to delete.");
            signatureSelect.focus();
            return;
        }
        const confirmed = window.confirm(`Delete saved signature "${selected.name}"?`);
        if (!confirmed) {
            return;
        }
        try {
            const response = await fetch(signaturesDeleteUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({ id: selected.id }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.message || "Could not delete signature.");
            }
            signatures = Array.isArray(data.signatures) ? data.signatures : [];
            renderSignatureSelect();
            setSignatureStatus(`Deleted "${selected.name}".`);
        } catch (err) {
            window.alert(err.message || "Could not delete signature.");
        }
    };

    const renderSummary = (sentCount, failedDetails) => {
        summaryEl.textContent = "";
        summaryEl.hidden = false;

        const sent = document.createElement("span");
        sent.className = "good";
        sent.textContent = `${sentCount} sent`;
        summaryEl.appendChild(sent);

        if (failedDetails.length) {
            summaryEl.appendChild(document.createTextNode(" / "));
            const failed = document.createElement("span");
            failed.className = "bad";
            failed.textContent = `${failedDetails.length} failed`;
            summaryEl.appendChild(failed);

            const list = document.createElement("ul");
            list.className = "admin-send-email-failure-list soft";
            failedDetails.slice(0, 8).forEach((detail) => {
                const item = document.createElement("li");
                item.textContent = detail;
                list.appendChild(item);
            });
            if (failedDetails.length > 8) {
                const item = document.createElement("li");
                item.textContent = `${failedDetails.length - 8} more failed.`;
                list.appendChild(item);
            }
            summaryEl.appendChild(list);
        }
    };

    const sendToSelected = async () => {
        const recipients = getSelectedRecipients();
        const subject = subjectInput.value.trim();
        const textBody = messageInput.value.trim();
        const htmlBody = buildHtmlBody(textBody);

        if (!recipients.length) {
            sendHint.textContent = "Select at least one recipient.";
            searchInput.focus();
            return;
        }
        if (!subject) {
            sendHint.textContent = "Enter a subject.";
            subjectInput.focus();
            return;
        }
        if (!textBody) {
            sendHint.textContent = "Write a message body.";
            messageInput.focus();
            return;
        }

        const confirmed = window.confirm(
            `Send this email individually to ${recipients.length} selected user${
                recipients.length === 1 ? "" : "s"
            }?`
        );
        if (!confirmed) {
            return;
        }

        sending = true;
        sendBtn.disabled = true;
        sendHint.textContent = "Sending. Keep this page open until it finishes.";
        summaryEl.hidden = true;
        progressWrap.hidden = false;
        progressFill.style.width = "0%";

        let sentCount = 0;
        const failedDetails = [];
        const total = recipients.length;

        for (let index = 0; index < recipients.length; index += 1) {
            const recipient = recipients[index];
            const done = index + 1;
            const pct = Math.round((done / total) * 100);
            progressFill.style.width = `${pct}%`;
            progressText.textContent = `Sending ${done} of ${total} to ${recipient.email}...`;

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
                    failedDetails.push(`${recipient.email} (${data.mode || data.error || "error"})`);
                }
            } catch (_err) {
                failedDetails.push(`${recipient.email} (network)`);
            }

            if (index < recipients.length - 1) {
                await sleep(sendDelayMs);
            }
        }

        progressFill.style.width = "100%";
        progressText.textContent = "Done.";
        renderSummary(sentCount, failedDetails);

        sending = false;
        updateSendHint();
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
                toggleRecipient(recipient);
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
    messageInput.addEventListener("input", updatePreview);
    insertPlaceholderBtn.addEventListener("click", insertPlaceholderAtCursor);
    if (insertLogoBtn) {
        insertLogoBtn.addEventListener("click", insertLogoAtCursor);
    }
    if (insertImageUrlBtn) {
        insertImageUrlBtn.addEventListener("click", insertImageUrlAtCursor);
    }
    if (insertSignatureBtn) {
        insertSignatureBtn.addEventListener("click", insertSelectedSignature);
    }
    if (saveSignatureBtn) {
        saveSignatureBtn.addEventListener("click", openSignatureDialog);
    }
    if (deleteSignatureBtn) {
        deleteSignatureBtn.addEventListener("click", deleteSelectedSignature);
    }
    if (signatureSelect) {
        signatureSelect.addEventListener("change", () => {
            if (deleteSignatureBtn) {
                deleteSignatureBtn.disabled = !signatureSelect.value;
            }
        });
        signatureSelect.addEventListener("dblclick", () => {
            if (signatureSelect.value) {
                openSignatureDialog();
            }
        });
    }
    if (signatureForm) {
        signatureForm.addEventListener("submit", (event) => {
            event.preventDefault();
            saveSignatureFromDialog();
        });
    }
    if (signatureCancelBtn) {
        signatureCancelBtn.addEventListener("click", closeSignatureDialog);
    }
    if (signatureDialog) {
        signatureDialog.addEventListener("cancel", (event) => {
            event.preventDefault();
            closeSignatureDialog();
        });
    }
    sendBtn.addEventListener("click", sendToSelected);

    inactiveDaysInput.value = String(defaultInactiveDays);
    updatePreview();
    updateSendHint();
    fetchRecipients();
    fetchSignatures();
})();
