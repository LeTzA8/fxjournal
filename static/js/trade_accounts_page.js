(function () {
    const dialog = document.getElementById("deleteTradeAccountDialog");
    const form = document.getElementById("deleteTradeAccountForm");
    const lead = document.getElementById("deleteTradeAccountLead");
    const status = document.getElementById("deleteTradeAccountStatus");
    const alertBanner = document.getElementById("tradeAccountAlert");
    const total = document.getElementById("tradeAccountTotal");
    const editor = document.getElementById("editor");
    const cancelButton = document.getElementById("cancelDeleteTradeAccount");
    const submitButton = document.getElementById("confirmDeleteTradeAccount");
    const confirmationInput = document.getElementById("deleteTradeAccountConfirmation");
    const acknowledgeInput = document.getElementById("deleteTradeAccountAcknowledge");
    const openButtons = Array.from(document.querySelectorAll("[data-open-delete-account-modal]"));
    if (!dialog || !form || !lead || !status || !alertBanner || !total || !cancelButton || !submitButton || !confirmationInput || !acknowledgeInput || !openButtons.length) {
        return;
    }

    const actionTemplate = form.dataset.actionTemplate || "";
    if (!actionTemplate || typeof dialog.showModal !== "function") {
        return;
    }

    let activeTrigger = null;
    let activePubkey = "";

    const pluralize = (count, singular, plural) => `${count} ${count === 1 ? singular : plural}`;

    const setModalStatus = (message) => {
        status.textContent = message || "";
        status.hidden = !message;
    };

    const setBanner = (message, tone) => {
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

    const setPending = (isPending) => {
        submitButton.disabled = isPending;
        cancelButton.disabled = isPending;
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

    const updateTotal = (remainingCount) => {
        if (typeof remainingCount !== "number") {
            return;
        }
        total.textContent = `${remainingCount} total`;
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
        lead.textContent = `This permanently deletes ${accountName}, ${pluralize(tradeCount, "linked trade", "linked trades")}, and ${pluralize(reviewCount, "linked AI review", "linked AI reviews")}.`;
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
    dialog.addEventListener("cancel", () => {
        resetDialog();
    });
    dialog.addEventListener("close", () => {
        resetDialog();
    });

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
                    "Accept": "application/json",
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

            const editTargetPubkey = editor ? (editor.dataset.editTargetPubkey || "") : "";
            if (payload.requires_reload || (editTargetPubkey && editTargetPubkey === payload.deleted_pubkey)) {
                window.location.assign(payload.redirect_url);
            }
        } catch {
            setModalStatus("Could not delete that trade account right now. Please try again.");
        } finally {
            setPending(false);
        }
    });
})();
