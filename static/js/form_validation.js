(function () {
    const forms = Array.from(document.querySelectorAll("form[data-themed-validation]"));
    if (!forms.length) {
        return;
    }

    const fieldSelector = "input, select, textarea";

    const isCandidateField = (field) => {
        if (!field || !(field instanceof HTMLElement) || field.disabled) {
            return false;
        }
        const tagName = field.tagName;
        if (!["INPUT", "SELECT", "TEXTAREA"].includes(tagName)) {
            return false;
        }
        const type = (field.getAttribute("type") || "").toLowerCase();
        return !["hidden", "submit", "button", "reset", "image"].includes(type);
    };

    const getFields = (form) =>
        Array.from(form.querySelectorAll(fieldSelector)).filter(isCandidateField);

    const clearFieldState = (field) => {
        field.classList.remove("is-invalid");
        field.removeAttribute("aria-invalid");
    };

    const syncFieldState = (field) => {
        if (field.checkValidity()) {
            clearFieldState(field);
            return;
        }
        field.classList.add("is-invalid");
        field.setAttribute("aria-invalid", "true");
    };

    forms.forEach((form) => {
        const fields = getFields(form);

        fields.forEach((field) => {
            const eventName =
                (field.type || "").toLowerCase() === "checkbox" || field.tagName === "SELECT"
                    ? "change"
                    : "input";

            field.addEventListener(eventName, () => {
                if (field.validity.valid || field.classList.contains("is-invalid")) {
                    syncFieldState(field);
                }
            });

            field.addEventListener("blur", () => {
                if (field.value || field.classList.contains("is-invalid")) {
                    syncFieldState(field);
                }
            });

            field.addEventListener("invalid", () => {
                // Keep native browser warnings and autofill behavior intact.
                syncFieldState(field);
            });
        });

        form.addEventListener("reset", () => {
            window.setTimeout(() => {
                fields.forEach(clearFieldState);
            }, 0);
        });
    });
})();
