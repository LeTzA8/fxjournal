(function () {
    const forms = Array.from(
        document.querySelectorAll("form.trade-form--edit[data-themed-validation], form.trade-form--view")
    );
    if (!forms.length) {
        return;
    }

    const openDetailsForField = (field) => {
        let node = field;
        while (node && node !== document.body) {
            if (node.tagName === "DETAILS") {
                node.open = true;
                node.classList.add("has-section-error");
            }
            node = node.parentElement;
        }
    };

    forms.forEach((form) => {
        form.addEventListener(
            "invalid",
            (event) => {
                if (event.target instanceof HTMLElement) {
                    openDetailsForField(event.target);
                }
            },
            true
        );

        form.addEventListener("submit", (event) => {
            if (form.checkValidity()) {
                return;
            }
            event.preventDefault();
            const invalidField = form.querySelector(":invalid");
            if (invalidField instanceof HTMLElement) {
                openDetailsForField(invalidField);
                invalidField.focus({ preventScroll: false });
                invalidField.reportValidity();
            }
        });

        form.querySelectorAll(".is-invalid").forEach((field) => {
            openDetailsForField(field);
        });
    });
})();
