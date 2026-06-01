(() => {
    const tradeLogPanel = document.getElementById("trade-journal");
    const dialog = document.getElementById("dashboardTradeNoteDialog");
    if (!tradeLogPanel || !dialog) {
        return;
    }

    const urlPattern = tradeLogPanel.dataset.tradeNoteUrlPattern || "";
    const csrfToken = tradeLogPanel.dataset.csrfToken || "";
    const titleEl = document.getElementById("dashboardTradeNoteTitle");
    const subtitleEl = document.getElementById("dashboardTradeNoteSubtitle");
    const inputEl = document.getElementById("dashboardTradeNoteInput");
    const errorEl = document.getElementById("dashboardTradeNoteError");
    const statusEl = document.getElementById("dashboardTradeNoteStatus");
    const cancelBtn = document.getElementById("dashboardTradeNoteCancel");
    const saveBtn = document.getElementById("dashboardTradeNoteSave");

    if (!urlPattern || !inputEl || !saveBtn) {
        return;
    }

    let activeRow = null;
    let activePubkey = "";
    let isSaving = false;

    const noteUrlFor = (pubkey) => urlPattern.split("__TRADE_PUBKEY__").join(String(pubkey || "").trim());

    const showError = (message) => {
        if (!errorEl) {
            return;
        }
        errorEl.textContent = message || "";
        errorEl.hidden = !message;
    };

    const showStatus = (message) => {
        if (!statusEl) {
            return;
        }
        statusEl.textContent = message || "";
        statusEl.hidden = !message;
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

    const updateRowNoteState = (row, hasTradeNote) => {
        if (!row) {
            return;
        }
        row.dataset.hasTradeNote = hasTradeNote ? "1" : "0";
        const button = row.querySelector("[data-trade-note-btn]");
        if (!button) {
            return;
        }
        button.textContent = hasTradeNote ? "Edit note" : "Add note";
        button.classList.toggle("trade-note-btn--empty", !hasTradeNote);
        const symbol = (row.dataset.symbol || "trade").trim();
        button.setAttribute(
            "aria-label",
            hasTradeNote ? `Edit note for ${symbol}` : `Add note for ${symbol}`
        );
    };

    const closeDialog = () => {
        if (typeof dialog.close === "function") {
            dialog.close();
        }
        activeRow = null;
        activePubkey = "";
        isSaving = false;
        saveBtn.disabled = false;
        showError("");
        showStatus("");
    };

    const openDialog = async (row) => {
        const pubkey = (row.dataset.tradeDetailUrl || "").split("/").filter(Boolean).pop();
        if (!pubkey) {
            return;
        }

        activeRow = row;
        activePubkey = pubkey;
        showError("");
        showStatus("");
        inputEl.value = "";

        const hasTradeNote = row.dataset.hasTradeNote === "1";
        if (titleEl) {
            titleEl.textContent = hasTradeNote ? "Edit trade note" : "Add trade note";
        }
        if (subtitleEl) {
            subtitleEl.textContent = (row.dataset.symbol || "").trim();
        }

        if (typeof dialog.showModal === "function") {
            dialog.showModal();
        }

        if (hasTradeNote) {
            try {
                const response = await fetch(noteUrlFor(pubkey), {
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                    },
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    showError(responseErrorMessage(response, data, "Could not load this trade note."));
                    inputEl.focus();
                    return;
                }
                inputEl.value = data.trade_note || "";
                if (subtitleEl && data.trade_label) {
                    subtitleEl.textContent = data.trade_label;
                }
            } catch (_error) {
                showError("Could not load this trade note.");
            }
        }

        inputEl.focus();
    };

    tradeLogPanel.addEventListener("click", (event) => {
        const button = event.target.closest("[data-trade-note-btn]");
        if (!button) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        const row = button.closest("tr[data-trade-detail-url]");
        if (!row) {
            return;
        }
        openDialog(row);
    });

    cancelBtn?.addEventListener("click", () => {
        closeDialog();
    });

    dialog.addEventListener("click", (event) => {
        const rect = dialog.getBoundingClientRect();
        const clickedBackdrop =
            event.clientX < rect.left
            || event.clientX > rect.right
            || event.clientY < rect.top
            || event.clientY > rect.bottom;
        if (clickedBackdrop) {
            closeDialog();
        }
    });

    dialog.addEventListener("cancel", () => {
        closeDialog();
    });

    saveBtn.addEventListener("click", async () => {
        if (isSaving || !activePubkey) {
            return;
        }

        isSaving = true;
        saveBtn.disabled = true;
        showError("");
        showStatus("");

        try {
            const response = await fetch(noteUrlFor(activePubkey), {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRFToken": csrfToken,
                },
                body: JSON.stringify({
                    trade_note: inputEl.value,
                }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                showError(responseErrorMessage(response, data, "Could not save this trade note."));
                return;
            }

            updateRowNoteState(activeRow, Boolean(data.has_trade_note));
            showStatus("Note saved.");
            window.setTimeout(() => {
                closeDialog();
            }, 350);
        } catch (_error) {
            showError("Could not save this trade note.");
        } finally {
            isSaving = false;
            saveBtn.disabled = false;
        }
    });
})();
