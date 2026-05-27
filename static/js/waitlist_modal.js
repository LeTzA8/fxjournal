(function () {
    "use strict";

    var modal = document.getElementById("waitlistModal");
    if (!modal) {
        return;
    }

    var postUrl = modal.getAttribute("data-waitlist-post-url") || "/pricing/waitlist";
    var tierInput = document.getElementById("waitlistTierInput");
    var sourceInput = document.getElementById("waitlistSourceInput");
    var featureInput = document.getElementById("waitlistFeatureInput");
    var ctaContextInput = document.getElementById("waitlistCtaContextInput");
    var tierLabel = document.getElementById("waitlistModalTier");
    var form = document.getElementById("waitlistForm");
    var emailInput = document.getElementById("waitlistEmail");
    var successMsg = document.getElementById("waitlistSuccess");
    var errorMsg = document.getElementById("waitlistError");
    var submitBtn = form ? form.querySelector("[type=submit]") : null;
    var defaultSubmitLabel = submitBtn ? submitBtn.textContent : "Join waitlist";

    var tierLabels = { trader: "Trader", pro: "Pro" };

    function defaultFeatureForTier(tier) {
        return tier === "pro" ? "multi_timeframe_replay" : "advanced_replay";
    }

    function defaultSourceForTier(tier) {
        return "pricing_page";
    }

    function openModal(tier, featureInterest, source, ctaContext) {
        var resolvedTier = tier || "trader";
        if (tierInput) {
            tierInput.value = resolvedTier;
        }
        if (tierLabel) {
            tierLabel.textContent = tierLabels[resolvedTier] || "Trader";
        }
        if (featureInput) {
            featureInput.value = featureInterest || defaultFeatureForTier(resolvedTier);
        }
        if (sourceInput) {
            sourceInput.value = source || defaultSourceForTier(resolvedTier);
        }
        if (ctaContextInput) {
            ctaContextInput.value = ctaContext || "";
        }
        if (successMsg) {
            successMsg.hidden = true;
        }
        if (errorMsg) {
            errorMsg.hidden = true;
            errorMsg.textContent = "Something went wrong — please try again.";
        }
        if (form) {
            form.style.display = "";
        }
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = defaultSubmitLabel;
        }
        modal.hidden = false;
        if (emailInput && !emailInput.readOnly) {
            emailInput.focus();
        } else if (submitBtn) {
            submitBtn.focus();
        }
    }

    function closeModal() {
        modal.hidden = true;
    }

    function markTriggerSaved(trigger) {
        if (!trigger || trigger.tagName !== "BUTTON") {
            return;
        }
        trigger.disabled = true;
        trigger.textContent = "Saved ✓";
    }

    document.addEventListener("click", function (event) {
        var trigger = event.target.closest("[data-waitlist-trigger]");
        if (!trigger || trigger.disabled) {
            return;
        }
        event.preventDefault();
        openModal(
            trigger.getAttribute("data-waitlist-trigger"),
            trigger.getAttribute("data-waitlist-feature"),
            trigger.getAttribute("data-waitlist-source"),
            trigger.getAttribute("data-waitlist-cta-context")
        );
    });

    document.querySelectorAll("[data-waitlist-close]").forEach(function (el) {
        el.addEventListener("click", closeModal);
    });

    modal.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeModal();
        }
    });

    document.addEventListener("waitlist:joined", function (event) {
        var detail = (event && event.detail) || {};
        document.querySelectorAll("[data-waitlist-trigger]").forEach(function (trigger) {
            if (
                trigger.getAttribute("data-waitlist-source") === detail.source &&
                trigger.getAttribute("data-waitlist-feature") === detail.feature_interest
            ) {
                markTriggerSaved(trigger);
            }
        });
    });

    if (!form) {
        return;
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();

        var email = emailInput ? emailInput.value.trim() : "";
        var tier = tierInput ? tierInput.value : "trader";
        var source = sourceInput ? sourceInput.value : "pricing_page";
        var featureInterest = featureInput ? featureInput.value : defaultFeatureForTier(tier);
        var ctaContext = ctaContextInput ? ctaContextInput.value : "";
        var csrfField = form.querySelector("[name=csrf_token]");
        var csrf = csrfField ? csrfField.value : "";

        if (!email) {
            if (emailInput) {
                emailInput.focus();
            }
            return;
        }

        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = "Joining…";
        }
        if (errorMsg) {
            errorMsg.hidden = true;
        }

        fetch(postUrl, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrf,
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify({
                email: email,
                tier: tier,
                source: source,
                feature_interest: featureInterest,
                cta_context: ctaContext,
            }),
        })
            .then(function (response) {
                if (response.status === 429) {
                    throw new Error("You're sending too fast — try again in a minute.");
                }
                return response
                    .json()
                    .catch(function () {
                        return {};
                    })
                    .then(function (data) {
                        data = data || {};
                        if (response.status === 403 && data.error === "support_view_read_only") {
                            throw new Error("That action is not available in read-only support view.");
                        }
                        if (!response.ok || !data.ok) {
                            throw new Error(
                                data.error || data.message || "Something went wrong — please try again."
                            );
                        }
                        return data;
                    });
            })
            .then(function () {
                form.style.display = "none";
                if (successMsg) {
                    successMsg.hidden = false;
                }
                document.dispatchEvent(
                    new CustomEvent("waitlist:joined", {
                        detail: {
                            feature_interest: featureInterest,
                            source: source,
                            cta_context: ctaContext,
                        },
                    })
                );
            })
            .catch(function (err) {
                if (errorMsg) {
                    errorMsg.textContent = err.message || "Something went wrong — please try again.";
                    errorMsg.hidden = false;
                }
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = defaultSubmitLabel;
                }
            });
    });
}());
