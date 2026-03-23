(function () {
    const bindDialog = ({ dialogId, openIds, cancelId, focusId }) => {
        const dialog = document.getElementById(dialogId);
        const cancelButton = document.getElementById(cancelId);
        const focusTarget = focusId ? document.getElementById(focusId) : null;
        const openButtons = openIds
            .map((id) => document.getElementById(id))
            .filter(Boolean);

        if (!dialog || typeof dialog.showModal !== "function" || !cancelButton || !openButtons.length) {
            return;
        }

        let activeTrigger = null;

        const openDialog = (trigger) => {
            activeTrigger = trigger;
            dialog.showModal();
            if (focusTarget) {
                requestAnimationFrame(() => focusTarget.focus());
            }
        };

        const closeDialog = () => {
            dialog.close();
            if (activeTrigger) {
                activeTrigger.focus();
            }
        };

        openButtons.forEach((button) => {
            button.addEventListener("click", () => openDialog(button));
        });

        cancelButton.addEventListener("click", closeDialog);
        dialog.addEventListener("close", () => {
            activeTrigger = null;
        });
    };

    bindDialog({
        dialogId: "accountDetailsDialog",
        openIds: ["openAccountDetailsDialog"],
        cancelId: "cancelAccountDetailsDialog",
        focusId: "dialog_username",
    });

    bindDialog({
        dialogId: "tradingProfileDialog",
        openIds: ["openTradingProfileDialog"],
        cancelId: "cancelTradingProfileDialog",
        focusId: "profile_trading_style",
    });

    bindDialog({
        dialogId: "deleteAccountDialog",
        openIds: ["openDeleteAccountDialog"],
        cancelId: "cancelDeleteAccountDialog",
        focusId: "delete_confirmation",
    });
})();
