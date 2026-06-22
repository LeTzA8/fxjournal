(() => {
    const form = document.querySelector("[data-mt5-request-form]");
    if (!form) {
        return;
    }

    const consentInput = form.querySelector("[data-mt5-consent]");
    const submitButton = form.querySelector("[data-mt5-submit]");
    const alertBanner = form.querySelector("[data-mt5-form-alert]");
    const passwordInput = form.querySelector("#mt5_investor_password");
    const passwordToggle = form.querySelector("[data-mt5-password-toggle]");
    const formShell = document.querySelector("[data-mt5-form-shell]");
    const progressTrack = document.querySelector("[data-mt5-progress]");
    const summaryPill = document.querySelector("[data-mt5-status-pill]");
    const summaryCopy = document.querySelector("[data-mt5-panel-copy-text]") || document.querySelector("[data-mt5-panel-copy]");
    const softLabel = document.querySelector("#mt5-access .soft");
    const defaultSubmitText = submitButton ? submitButton.textContent.trim() : "Start MT5 Sync";
    const serverInput = form.querySelector("[data-mt5-server-input]");
    const serverWarning = form.querySelector("[data-mt5-server-warning]");
    const wizardEnabled = form.hasAttribute("data-mt5-setup-wizard");
    const wizardSteps = wizardEnabled
        ? Array.from(form.querySelectorAll("[data-mt5-wizard-step]"))
        : [];
    const wizardBackButton = form.querySelector("[data-mt5-wizard-back]");
    const wizardNextButton = form.querySelector("[data-mt5-wizard-next]");
    const wizardStepLabel = form.querySelector("[data-mt5-wizard-step-label]");
    const wizardStepTitles = {
        1: "Investor account number",
        2: "Investor password",
        3: "MT5 server",
        4: "Confirm investor account",
    };
    let wizardCurrentStep = 1;

    let isSubmitting = false;

    const KNOWN_BROKER_NAMES = new Set([
        "exness",
        "ic markets",
        "icmarkets",
        "pepperstone",
        "xm",
        "fbs",
        "hotforex",
        "fxpro",
        "tickmill",
        "octafx",
        "hantec",
    ]);

    const SERVERISH_SUFFIX = /(live|demo|real|ecn|edge|mt5|mt4|sc|pro)$/i;

    const normalizeBrokerGuess = (value) => value.trim().toLowerCase().replace(/\s+/g, " ");

    const looksLikeBrokerNameNotServer = (raw) => {
        const trimmed = raw.trim();
        if (!trimmed) {
            return false;
        }
        if (/[-_]/.test(trimmed) || /\d/.test(trimmed)) {
            return false;
        }
        const norm = normalizeBrokerGuess(raw);
        if (KNOWN_BROKER_NAMES.has(norm)) {
            return true;
        }
        const words = norm.split(" ");
        if (words.length === 1) {
            const w = words[0];
            if (!/^[a-z]+$/.test(w) || w.length < 3 || w.length > 14) {
                return false;
            }
            if (SERVERISH_SUFFIX.test(w)) {
                return false;
            }
            return true;
        }
        return false;
    };

    const syncServerWarning = () => {
        if (!serverInput || !serverWarning) {
            return;
        }
        const show = looksLikeBrokerNameNotServer(serverInput.value);
        serverWarning.hidden = !show;
        serverInput.setAttribute("aria-invalid", show ? "true" : "false");
        const helperEl = form.querySelector("[data-mt5-server-helper]");
        const helperId = helperEl?.id;
        const warnId = serverWarning.id;
        const describedBy = [helperId, show ? warnId : null].filter(Boolean).join(" ");
        if (describedBy) {
            serverInput.setAttribute("aria-describedby", describedBy);
        } else {
            serverInput.removeAttribute("aria-describedby");
        }
    };

    const setAlert = (message, tone) => {
        if (!alertBanner) {
            return;
        }
        if (!message) {
            alertBanner.textContent = "";
            alertBanner.hidden = true;
            alertBanner.className = "mt5-request-form-alert";
            return;
        }
        alertBanner.textContent = message;
        alertBanner.hidden = false;
        alertBanner.className = `mt5-request-form-alert is-${tone || "error"}`;
    };

    const syncSubmitState = () => {
        if (!submitButton) {
            return;
        }
        // consentInput is optional — retry form has no consent checkbox
        const consentGiven = !consentInput || consentInput.checked;
        submitButton.disabled = isSubmitting || !consentGiven;
        submitButton.textContent = isSubmitting ? "Saving..." : defaultSubmitText;
    };

    const getWizardStepNode = (stepNumber) =>
        wizardSteps.find((node) => Number(node.getAttribute("data-mt5-wizard-step")) === stepNumber);

    const getWizardFocusTarget = (stepNode) => {
        if (!stepNode) {
            return null;
        }
        return stepNode.querySelector("input:not([type='hidden']), textarea, select, button");
    };

    const validateWizardStep = (stepNumber) => {
        const stepNode = getWizardStepNode(stepNumber);
        if (!stepNode) {
            return true;
        }
        const fields = stepNode.querySelectorAll("input, select, textarea");
        for (const field of fields) {
            if (typeof field.reportValidity === "function" && !field.reportValidity()) {
                field.focus();
                return false;
            }
        }
        if (stepNumber === 3) {
            syncServerWarning();
            if (serverInput && serverWarning && !serverWarning.hidden) {
                serverInput.focus();
                return false;
            }
        }
        return true;
    };

    const renderWizardStep = (stepNumber) => {
        wizardCurrentStep = stepNumber;
        wizardSteps.forEach((stepNode) => {
            const stepValue = Number(stepNode.getAttribute("data-mt5-wizard-step"));
            const isActive = stepValue === stepNumber;
            stepNode.hidden = !isActive;
            stepNode.classList.toggle("is-active", isActive);
        });
        if (wizardStepLabel) {
            const title = wizardStepTitles[stepNumber] || "";
            wizardStepLabel.textContent = `Step ${stepNumber} of ${wizardSteps.length} · ${title}`;
        }
        if (wizardBackButton) {
            wizardBackButton.hidden = stepNumber <= 1;
        }
        if (wizardNextButton) {
            wizardNextButton.hidden = stepNumber >= wizardSteps.length;
        }
        if (submitButton) {
            submitButton.hidden = stepNumber !== wizardSteps.length;
        }
        syncSubmitState();
        const focusTarget = getWizardFocusTarget(getWizardStepNode(stepNumber));
        if (focusTarget && typeof focusTarget.focus === "function") {
            focusTarget.focus();
        }
    };

    if (wizardEnabled && wizardSteps.length > 0) {
        renderWizardStep(1);
        if (wizardNextButton) {
            wizardNextButton.addEventListener("click", () => {
                if (!validateWizardStep(wizardCurrentStep)) {
                    return;
                }
                if (wizardCurrentStep < wizardSteps.length) {
                    renderWizardStep(wizardCurrentStep + 1);
                }
            });
        }
        if (wizardBackButton) {
            wizardBackButton.addEventListener("click", () => {
                if (wizardCurrentStep > 1) {
                    renderWizardStep(wizardCurrentStep - 1);
                }
            });
        }
    }

    const updateProgressTrack = (node, stage) => {
        if (!node) {
            return;
        }
        node.dataset.currentStage = String(stage);
        node.querySelectorAll("[data-mt5-progress-step]").forEach((stepNode) => {
            const stepNumber = Number(stepNode.getAttribute("data-mt5-progress-step"));
            stepNode.classList.remove("is-complete", "is-current", "is-upcoming");
            stepNode.removeAttribute("aria-current");
            if (stepNumber < stage) {
                stepNode.classList.add("is-complete");
                return;
            }
            if (stepNumber === stage) {
                stepNode.classList.add("is-current");
                stepNode.setAttribute("aria-current", "step");
                return;
            }
            stepNode.classList.add("is-upcoming");
        });
    };

    const updateStatusText = (node, label) => {
        if (!node || !label) {
            return;
        }
        node.textContent = label;
    };

    const buildSuccessCard = (message, accountName, importUrl) => {
        const card = document.createElement("div");
        card.className = "mt5-success-card";

        const title = document.createElement("p");
        title.className = "mt5-success-title";
        title.textContent = "Setup queued";
        card.appendChild(title);

        const copy = document.createElement("p");
        copy.className = "mt5-success-copy";
        copy.textContent = "We'll email you when sync is ready.";
        card.appendChild(copy);

        const bridge = document.createElement("p");
        bridge.className = "mt5-success-copy";
        bridge.textContent = "Import a report while setup runs if you want an early preview.";
        card.appendChild(bridge);

        if (accountName) {
            const meta = document.createElement("p");
            meta.className = "mt5-success-meta";
            meta.textContent = `Account: ${accountName}`;
            card.appendChild(meta);
        }

        if (importUrl) {
            const actions = document.createElement("div");
            actions.className = "mt5-success-actions";
            const importLink = document.createElement("a");
            importLink.className = "ghost-btn";
            importLink.href = importUrl;
            importLink.textContent = "Import a report";
            actions.appendChild(importLink);
            card.appendChild(actions);
        }

        if (message && message !== copy.textContent) {
            const detail = document.createElement("p");
            detail.className = "mt5-success-meta";
            detail.textContent = message;
            card.appendChild(detail);
        }

        return card;
    };

    if (consentInput) {
        consentInput.addEventListener("change", syncSubmitState);
    }
    syncSubmitState();

    if (serverInput) {
        serverInput.addEventListener("input", syncServerWarning);
        serverInput.addEventListener("blur", syncServerWarning);
        syncServerWarning();
    }

    if (passwordToggle && passwordInput) {
        passwordToggle.addEventListener("click", () => {
            const shouldReveal = passwordInput.type === "password";
            passwordInput.type = shouldReveal ? "text" : "password";
            passwordToggle.textContent = shouldReveal ? "Hide" : "Show";
            passwordToggle.setAttribute("aria-pressed", shouldReveal ? "true" : "false");
        });
    }

    form.addEventListener("submit", async (event) => {
        if (typeof window.fetch !== "function") {
            return;
        }
        event.preventDefault();

        if (wizardEnabled) {
            for (let step = 1; step <= wizardSteps.length; step += 1) {
                if (!validateWizardStep(step)) {
                    renderWizardStep(step);
                    return;
                }
            }
        } else if (!form.reportValidity()) {
            return;
        }

        isSubmitting = true;
        setAlert("", "");
        syncSubmitState();

        try {
            const response = await window.fetch(form.action, {
                method: "POST",
                body: new window.FormData(form),
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                    Accept: "application/json",
                },
                credentials: "same-origin",
            });

            const contentType = response.headers.get("content-type") || "";
            if (!contentType.includes("application/json")) {
                window.location.assign(response.url || window.location.href);
                return;
            }

            const payload = await response.json();
            if (!response.ok || !payload.ok) {
                setAlert(payload.message || "Could not start MT5 sync setup right now. Please try again.", "error");
                return;
            }

            updateStatusText(summaryPill, payload.status_label);
            updateProgressTrack(progressTrack, Number(payload.progress_stage) || 2);
            if (summaryCopy && payload.status_note) {
                summaryCopy.textContent = payload.status_note;
            }
            if (softLabel && payload.status_label) {
                softLabel.textContent = payload.status_label;
            }

            if (formShell) {
                formShell.classList.remove("guided-focus-panel", "guided-focus-panel-pulse");
                const importUrl = form.getAttribute("data-mt5-import-url") || "";
                formShell.replaceChildren(
                    buildSuccessCard(payload.message || "", payload.account_name || "", importUrl)
                );
            }
        } catch (_error) {
            setAlert("Could not start MT5 sync setup right now. Please try again.", "error");
        } finally {
            isSubmitting = false;
            syncSubmitState();
        }
    });
})();
