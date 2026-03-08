(function () {
    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const forms = Array.from(document.querySelectorAll("form[data-themed-validation]"));
    if (!forms.length) {
        return;
    }

    const fieldSelector = "input, select, textarea";

    const isCandidateField = (field) => {
        if (!field || !(field instanceof HTMLElement)) {
            return false;
        }
        if (field.disabled) {
            return false;
        }
        const tagName = field.tagName;
        if (!["INPUT", "SELECT", "TEXTAREA"].includes(tagName)) {
            return false;
        }
        const type = (field.getAttribute("type") || "").toLowerCase();
        return !["hidden", "submit", "button", "reset", "image"].includes(type);
    };

    const getFields = (form) => Array.from(form.querySelectorAll(fieldSelector)).filter(isCandidateField);

    const getFieldLabel = (field) => {
        const explicitLabel = field.id ? document.querySelector(`label[for="${field.id}"]`) : null;
        const wrappingLabel = field.closest("label");
        const label = explicitLabel || wrappingLabel;
        if (!label) {
            return field.getAttribute("aria-label") || field.name || "This field";
        }
        const text = label.textContent.replace(/\s+/g, " ").trim();
        return text || field.getAttribute("aria-label") || field.name || "This field";
    };

    const getFieldMessage = (field) => {
        if (field.validity.valueMissing) {
            return `${getFieldLabel(field)} is required.`;
        }
        if (field.validity.typeMismatch) {
            if ((field.getAttribute("type") || "").toLowerCase() === "email") {
                return "Enter a valid email address.";
            }
            return field.validationMessage;
        }
        if (field.validity.tooShort) {
            return `${getFieldLabel(field)} is too short.`;
        }
        if (field.validity.tooLong) {
            return `${getFieldLabel(field)} is too long.`;
        }
        if (field.validity.rangeUnderflow || field.validity.rangeOverflow || field.validity.stepMismatch) {
            return field.validationMessage;
        }
        if (field.validity.patternMismatch) {
            return field.validationMessage || `${getFieldLabel(field)} is not in the expected format.`;
        }
        return field.validationMessage || `${getFieldLabel(field)} is invalid.`;
    };

    const getFieldContainer = (field) => {
        if ((field.type || "").toLowerCase() === "checkbox" || (field.type || "").toLowerCase() === "radio") {
            return field.closest("label") || field.parentElement || field;
        }
        return field;
    };

    const getErrorElement = (field) => {
        const host = getFieldContainer(field);
        let errorEl = host.nextElementSibling;
        if (!errorEl || !errorEl.classList.contains("field-error")) {
            errorEl = document.createElement("p");
            errorEl.className = "field-error";
            errorEl.hidden = true;
            host.insertAdjacentElement("afterend", errorEl);
        }
        return errorEl;
    };

    const getSummaryElement = (form) => {
        let summary = Array.from(form.children).find((el) => el.classList && el.classList.contains("form-alert") && el.classList.contains("form-alert-error"));
        if (!summary) {
            summary = document.createElement("div");
            summary.className = "form-alert form-alert-error";
            summary.hidden = true;
            summary.setAttribute("role", "alert");
            summary.setAttribute("aria-live", "assertive");
            form.prepend(summary);
        }
        return summary;
    };

    const clearFieldState = (field) => {
        const errorEl = getErrorElement(field);
        errorEl.hidden = true;
        errorEl.textContent = "";
        field.classList.remove("is-invalid");
        field.removeAttribute("aria-invalid");
        const describedBy = (field.getAttribute("aria-describedby") || "")
            .split(/\s+/)
            .filter(Boolean)
            .filter((id) => id !== errorEl.id);
        if (describedBy.length) {
            field.setAttribute("aria-describedby", describedBy.join(" "));
        } else {
            field.removeAttribute("aria-describedby");
        }
    };

    const ensureErrorId = (field, errorEl) => {
        if (!errorEl.id) {
            const fallbackId = field.id || field.name || `field-${Math.random().toString(36).slice(2, 8)}`;
            errorEl.id = `${fallbackId}-error`;
        }
        const describedBy = new Set((field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
        describedBy.add(errorEl.id);
        field.setAttribute("aria-describedby", Array.from(describedBy).join(" "));
    };

    const applyFieldState = (field) => {
        if (field.checkValidity()) {
            clearFieldState(field);
            return true;
        }
        const errorEl = getErrorElement(field);
        errorEl.textContent = getFieldMessage(field);
        errorEl.hidden = false;
        ensureErrorId(field, errorEl);
        field.classList.add("is-invalid");
        field.setAttribute("aria-invalid", "true");
        return false;
    };

    const focusFirstInvalid = (field) => {
        if (!field) {
            return;
        }
        if (typeof field.focus === "function") {
            field.focus();
        }
        if (typeof field.scrollIntoView === "function") {
            field.scrollIntoView({
                block: "center",
                behavior: prefersReducedMotion.matches ? "auto" : "smooth",
            });
        }
    };

    forms.forEach((form) => {
        form.setAttribute("novalidate", "novalidate");
        const summary = getSummaryElement(form);
        const fields = getFields(form);

        const syncSummary = () => {
            const invalidFields = fields.filter((field) => !field.checkValidity());
            if (!invalidFields.length) {
                summary.hidden = true;
                summary.textContent = "";
                return [];
            }
            summary.hidden = false;
            summary.textContent =
                invalidFields.length === 1
                    ? "Please fix the highlighted field before continuing."
                    : `Please fix the ${invalidFields.length} highlighted fields before continuing.`;
            return invalidFields;
        };

        fields.forEach((field) => {
            const eventName = (field.type || "").toLowerCase() === "checkbox" || field.tagName === "SELECT" ? "change" : "input";
            field.addEventListener(eventName, () => {
                applyFieldState(field);
                syncSummary();
            });
            field.addEventListener("blur", () => {
                applyFieldState(field);
                syncSummary();
            });
            field.addEventListener("invalid", (event) => {
                event.preventDefault();
                applyFieldState(field);
                syncSummary();
            });
        });

        form.addEventListener("submit", (event) => {
            const invalidFields = fields.filter((field) => !applyFieldState(field));
            if (!invalidFields.length) {
                summary.hidden = true;
                summary.textContent = "";
                return;
            }
            event.preventDefault();
            syncSummary();
            focusFirstInvalid(invalidFields[0]);
        });

        form.addEventListener("reset", () => {
            window.setTimeout(() => {
                fields.forEach(clearFieldState);
                summary.hidden = true;
                summary.textContent = "";
            }, 0);
        });
    });
})();

