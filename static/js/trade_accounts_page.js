(function () {
    const page = document.querySelector("[data-trade-accounts-page]");
    if (!page) {
        return;
    }

    const alertBanner = document.getElementById("tradeAccountAlert");
    const total = document.getElementById("tradeAccountTotal");

    const setBanner = (message, tone) => {
        if (!alertBanner) {
            return;
        }
        if (!message) {
            alertBanner.textContent = "";
            alertBanner.className = "account-alert";
            alertBanner.hidden = true;
            return;
        }
        alertBanner.textContent = message;
        alertBanner.className = `account-alert ${tone || "info"}`;
        alertBanner.hidden = false;
    };

    const updateTotal = (remainingCount) => {
        if (typeof remainingCount !== "number" || !total) {
            return;
        }
        total.textContent = `${remainingCount} total`;
    };

    const syncCardState = (activePubkeyValue, defaultPubkeyValue) => {
        document.querySelectorAll("[data-trade-account-card]").forEach((card) => {
            const pubkey = card.dataset.tradeAccountPubkey || "";
            const isActive = Boolean(activePubkeyValue) && pubkey === activePubkeyValue;
            const isDefault = Boolean(defaultPubkeyValue) && pubkey === defaultPubkeyValue;
            const activeChip = card.querySelector(".active-chip");
            const defaultChip = card.querySelector(".default-chip");
            const switchForm = card.querySelector(".switch-account-form");
            const defaultForm = card.querySelector(".default-account-form");

            card.classList.toggle("active", isActive);
            if (activeChip) {
                activeChip.hidden = !isActive;
            }
            if (defaultChip) {
                defaultChip.hidden = !isDefault;
            }
            if (switchForm) {
                switchForm.hidden = isActive;
            }
            if (defaultForm) {
                defaultForm.hidden = isDefault;
            }
        });
    };

    const bindSimpleDialog = (dialogId, openSelector, cancelId) => {
        const dialog = document.getElementById(dialogId);
        const openButtons = Array.from(document.querySelectorAll(openSelector));
        const cancelButton = document.getElementById(cancelId);
        if (!dialog || typeof dialog.showModal !== "function" || !openButtons.length || !cancelButton) {
            return null;
        }

        let activeTrigger = null;

        const closeDialog = () => {
            dialog.close();
            if (activeTrigger) {
                activeTrigger.focus();
            }
        };

        openButtons.forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                activeTrigger = button;
                dialog.showModal();
            });
        });

        cancelButton.addEventListener("click", closeDialog);
        dialog.addEventListener("close", () => {
            activeTrigger = null;
        });

        return {
            dialog,
            open(button) {
                activeTrigger = button || null;
                dialog.showModal();
            },
            close: closeDialog,
        };
    };

    const editorDialog = document.getElementById("tradeAccountEditorDialog");
    const editorForm = document.getElementById("tradeAccountEditorForm");
    const editorTitle = document.getElementById("tradeAccountEditorTitle");
    const editorMode = document.getElementById("tradeAccountEditorMode");
    const editorHelp = document.getElementById("tradeAccountEditorHelp");
    const editorSubmit = document.getElementById("submitTradeAccountEditor");
    const editorCancel = document.getElementById("cancelTradeAccountEditor");
    const defaultRow = document.getElementById("tradeAccountDialogDefaultRow");
    const defaultInput = document.getElementById("tradeAccountDialogDefault");
    const nameInput = document.getElementById("tradeAccountDialogName");
    const externalIdInput = document.getElementById("tradeAccountDialogExternalId");
    const sizeInput = document.getElementById("tradeAccountDialogSize");
    const typeInput = document.getElementById("tradeAccountDialogType");
    const defaultStrategySelect = document.getElementById("tradeAccountDialogDefaultStrategy");
    const defaultStrategyLabel = document.getElementById("tradeAccountDialogDefaultStrategyLabel");
    const defaultStrategyHint = document.getElementById("tradeAccountDialogDefaultStrategyHint");
    const editorButtons = Array.from(document.querySelectorAll("[data-open-trade-account-editor]"));

    const syncDefaultStrategyCopyForAccountType = () => {
        if (!typeInput || !defaultStrategyLabel || !defaultStrategyHint) {
            return;
        }
        const isFutures = String(typeInput.value || "").trim().toUpperCase() === "FUTURES";
        defaultStrategyLabel.textContent = isFutures
            ? "Default strategy for imports (optional)"
            : "Default strategy for imports & MT5 sync (optional)";
        defaultStrategyHint.textContent = isFutures
            ? "Only affects newly imported trades on this account. Manual trades still use the strategy field on the trade form."
            : "Only affects newly imported or MT5-synced trades on this account. Manual trades still use the strategy field on the trade form.";
    };

    if (
        editorDialog &&
        typeof editorDialog.showModal === "function" &&
        editorForm &&
        editorTitle &&
        editorMode &&
        editorHelp &&
        editorSubmit &&
        editorCancel &&
        defaultRow &&
        defaultInput &&
        nameInput &&
        externalIdInput &&
        sizeInput &&
        typeInput &&
        defaultStrategySelect &&
        editorButtons.length
    ) {
        let activeEditorTrigger = null;
        const createAction = editorForm.dataset.createAction || "";
        const updateActionTemplate = editorForm.dataset.updateActionTemplate || "";

        const resetEditor = () => {
            editorForm.reset();
            editorForm.action = createAction;
            editorTitle.textContent = "Create Account";
            editorMode.textContent = "New";
            editorHelp.textContent = "Use one account per broker, challenge, or funded program so your stats stay separated and easier to review.";
            editorSubmit.textContent = "Create Account";
            defaultRow.hidden = false;
            defaultInput.checked = false;
            typeInput.value = "CFD";
            defaultStrategySelect.value = "";
            syncDefaultStrategyCopyForAccountType();
        };

        const openEditor = (trigger) => {
            const mode = trigger.dataset.editorMode || "create";
            resetEditor();
            activeEditorTrigger = trigger;

            if (mode === "edit") {
                const pubkey = trigger.dataset.editorAccountPubkey || "";
                editorForm.action = updateActionTemplate.replace("__TRADE_ACCOUNT_PUBKEY__", encodeURIComponent(pubkey));
                editorTitle.textContent = "Edit Account";
                editorMode.textContent = "Selected";
                editorHelp.textContent = "Update the account details without keeping the full form open on the page.";
                editorSubmit.textContent = "Save Changes";
                defaultRow.hidden = true;
                defaultInput.checked = false;
                nameInput.value = trigger.dataset.editorName || "";
                externalIdInput.value = trigger.dataset.editorExternalId || "";
                sizeInput.value = trigger.dataset.editorAccountSize || "";
                typeInput.value = trigger.dataset.editorAccountType || "CFD";
                defaultStrategySelect.value =
                    trigger.dataset.editorDefaultTradeProfilePubkey || "";
            }

            syncDefaultStrategyCopyForAccountType();
            editorDialog.showModal();
            requestAnimationFrame(() => nameInput.focus());
        };

        const closeEditor = () => {
            editorDialog.close();
            resetEditor();
            if (activeEditorTrigger) {
                activeEditorTrigger.focus();
            }
        };

        editorButtons.forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                openEditor(button);
            });
        });

        editorCancel.addEventListener("click", closeEditor);
        typeInput.addEventListener("change", syncDefaultStrategyCopyForAccountType);
        editorDialog.addEventListener("close", () => {
            resetEditor();
            activeEditorTrigger = null;
        });

        const initialEditPubkey = page.dataset.initialEditPubkey || "";
        const initialDeletePubkey = page.dataset.initialDeletePubkey || "";
        if (initialEditPubkey && !initialDeletePubkey) {
            const initialButton = editorButtons.find(
                (button) => button.dataset.editorAccountPubkey === initialEditPubkey
            );
            if (initialButton) {
                openEditor(initialButton);
            }
        }
    }

    bindSimpleDialog("deleteAllTradeAccountsDialog", "#openDeleteAllTradeAccountsDialog", "cancelDeleteAllTradeAccounts");

    const dialog = document.getElementById("deleteTradeAccountDialog");
    const form = document.getElementById("deleteTradeAccountForm");
    const lead = document.getElementById("deleteTradeAccountLead");
    const status = document.getElementById("deleteTradeAccountStatus");
    const cancelButton = document.getElementById("cancelDeleteTradeAccount");
    const submitButton = document.getElementById("confirmDeleteTradeAccount");
    const confirmationInput = document.getElementById("deleteTradeAccountConfirmation");
    const acknowledgeInput = document.getElementById("deleteTradeAccountAcknowledge");
    const openButtons = Array.from(document.querySelectorAll("[data-open-delete-account-modal]"));

    if (
        dialog &&
        typeof dialog.showModal === "function" &&
        form &&
        lead &&
        status &&
        cancelButton &&
        submitButton &&
        confirmationInput &&
        acknowledgeInput &&
        openButtons.length
    ) {
        const actionTemplate = form.dataset.actionTemplate || "";
        if (!actionTemplate) {
            return;
        }

        let activeTrigger = null;
        let activePubkey = "";

        const pluralize = (count, singular, plural) => `${count} ${count === 1 ? singular : plural}`;

        const setModalStatus = (message) => {
            status.textContent = message || "";
            status.hidden = !message;
        };

        const setPending = (isPending) => {
            submitButton.disabled = isPending;
            cancelButton.disabled = isPending;
        };

        const resetDialog = () => {
            form.reset();
            setModalStatus("");
            activePubkey = "";
        };

        const closeDialog = () => {
            dialog.close();
            resetDialog();
            if (activeTrigger) {
                activeTrigger.focus();
            }
        };

        const openDialog = (trigger) => {
            activeTrigger = trigger;
            activePubkey = trigger.dataset.deleteAccountPubkey || "";
            const accountName = trigger.dataset.deleteAccountName || "this trade account";
            const tradeCount = Number(trigger.dataset.deleteTradeCount || "0");
            const reviewCount = Number(trigger.dataset.deleteReviewCount || "0");
            lead.textContent = `This permanently deletes ${accountName}, ${pluralize(tradeCount, "linked trade", "linked trades")}, and ${pluralize(reviewCount, "linked AI review", "linked AI reviews")}. If MT5 was connected, saved MT5 credentials are removed and limited VM cleanup metadata may remain until terminal cleanup finishes.`;
            form.action = actionTemplate.replace("__TRADE_ACCOUNT_PUBKEY__", encodeURIComponent(activePubkey));
            dialog.showModal();
            requestAnimationFrame(() => confirmationInput.focus());
        };

        openButtons.forEach((trigger) => {
            trigger.addEventListener("click", (event) => {
                event.preventDefault();
                openDialog(trigger);
            });
        });

        cancelButton.addEventListener("click", closeDialog);
        dialog.addEventListener("cancel", resetDialog);
        dialog.addEventListener("close", resetDialog);

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!activePubkey) {
                return;
            }

            setModalStatus("");
            setPending(true);
            try {
                const response = await fetch(form.action, {
                    method: "POST",
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                        Accept: "application/json",
                    },
                    body: new FormData(form),
                    credentials: "same-origin",
                });
                const payload = await response.json().catch(() => null);

                if (!response.ok || !payload || !payload.ok) {
                    if (response.status === 401 && payload && payload.redirect_url) {
                        window.location.assign(payload.redirect_url);
                        return;
                    }
                    setModalStatus((payload && payload.message) || "Could not delete that trade account right now. Please try again.");
                    return;
                }

                const deletedCard = document.querySelector(`[data-trade-account-card][data-trade-account-pubkey="${payload.deleted_pubkey}"]`);
                if (deletedCard) {
                    deletedCard.remove();
                }

                syncCardState(payload.active_trade_account_pubkey, payload.default_trade_account_pubkey);
                updateTotal(payload.remaining_account_count);
                setBanner(payload.message, "success");
                dialog.close();

                if (payload.requires_reload) {
                    window.location.assign(payload.redirect_url);
                }
            } catch {
                setModalStatus("Could not delete that trade account right now. Please try again.");
            } finally {
                setPending(false);
            }
        });

        if (initialDeletePubkey) {
            const initialDeleteButton = openButtons.find(
                (button) => button.dataset.deleteAccountPubkey === initialDeletePubkey
            );
            if (initialDeleteButton) {
                openDialog(initialDeleteButton);
            }
        }
    }
})();
