(function () {
    const page = document.querySelector("[data-trade-profiles-page]");
    if (!page) {
        return;
    }

    const editorDialog = document.getElementById("strategyEditorDialog");
    const editorForm = document.getElementById("strategyEditorForm");
    const editorTitle = document.getElementById("strategyEditorTitle");
    const editorMode = document.getElementById("strategyEditorMode");
    const editorHelp = document.getElementById("strategyEditorHelp");
    const nameInput = document.getElementById("strategyDialogName");
    const descriptionInput = document.getElementById("strategyDialogDescription");
    const submitButton = document.getElementById("submitStrategyEditor");
    const cancelButton = document.getElementById("cancelStrategyEditor");
    const editorButtons = Array.from(document.querySelectorAll("[data-open-strategy-editor]"));

    if (
        editorDialog &&
        typeof editorDialog.showModal === "function" &&
        editorForm &&
        editorTitle &&
        editorMode &&
        editorHelp &&
        nameInput &&
        descriptionInput &&
        submitButton &&
        cancelButton &&
        editorButtons.length
    ) {
        const editActionTemplate = editorForm.dataset.editActionTemplate || "";
        let activeEditorTrigger = null;

        const resetEditor = () => {
            editorForm.reset();
            editorForm.action = createAction;
            editorTitle.textContent = "New Strategy";
            editorMode.textContent = "Version 1";
            editorHelp.textContent = "Start with the setup name and a short description of the entry model, confirmation, or execution rules you want to track.";
            submitButton.textContent = "Create Strategy";
        };

        const createAction = editorForm.getAttribute("action") || "";

        const openEditor = (trigger) => {
            const mode = trigger.dataset.editorMode || "create";
            resetEditor();
            activeEditorTrigger = trigger;

            if (mode === "edit") {
                editorForm.action = editActionTemplate.replace(
                    "__PROFILE_PUBKEY__",
                    encodeURIComponent(trigger.dataset.editorProfilePubkey || "")
                );
                editorTitle.textContent = "Edit Strategy";
                editorMode.textContent = "New version";
                editorHelp.textContent = "Updating a strategy creates a new saved version while keeping the history tied to older trades.";
                submitButton.textContent = "Save New Version";
                nameInput.value = trigger.dataset.editorName || "";
                descriptionInput.value = trigger.dataset.editorDescription || "";
            } else {
                editorForm.action = createAction;
            }

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

        cancelButton.addEventListener("click", closeEditor);
        editorDialog.addEventListener("close", () => {
            resetEditor();
            activeEditorTrigger = null;
        });

        const initialEditPubkey = page.dataset.initialEditPubkey || "";
        if (initialEditPubkey) {
            const initialButton = editorButtons.find(
                (button) => button.dataset.editorProfilePubkey === initialEditPubkey
            );
            if (initialButton) {
                openEditor(initialButton);
            }
        }
    }

    const archiveDialog = document.getElementById("archiveStrategyDialog");
    const archiveForm = document.getElementById("archiveStrategyForm");
    const archiveLead = document.getElementById("archiveStrategyLead");
    const archiveCancel = document.getElementById("cancelArchiveStrategy");
    const archiveButtons = Array.from(document.querySelectorAll("[data-open-archive-strategy-dialog]"));

    if (
        archiveDialog &&
        typeof archiveDialog.showModal === "function" &&
        archiveForm &&
        archiveLead &&
        archiveCancel &&
        archiveButtons.length
    ) {
        const actionTemplate = archiveForm.dataset.actionTemplate || "";
        let activeArchiveTrigger = null;

        const openArchiveDialog = (trigger) => {
            activeArchiveTrigger = trigger;
            const name = trigger.dataset.archiveProfileName || "this strategy";
            archiveLead.textContent = `Archive ${name} to remove it from the active library while keeping its historical trade tags and past versions intact.`;
            archiveForm.action = actionTemplate.replace(
                "__PROFILE_PUBKEY__",
                encodeURIComponent(trigger.dataset.archiveProfilePubkey || "")
            );
            archiveDialog.showModal();
        };

        const closeArchiveDialog = () => {
            archiveDialog.close();
            if (activeArchiveTrigger) {
                activeArchiveTrigger.focus();
            }
        };

        archiveButtons.forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                openArchiveDialog(button);
            });
        });

        archiveCancel.addEventListener("click", closeArchiveDialog);
        archiveDialog.addEventListener("close", () => {
            activeArchiveTrigger = null;
        });
    }
})();
