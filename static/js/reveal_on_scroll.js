(function () {
    const revealTargets = Array.from(
        document.querySelectorAll(".metric, .panel, .login-card, .card, .landing-reveal, [data-reveal]")
    ).filter((el, index, items) => items.indexOf(el) === index);
    if (!revealTargets.length) {
        return;
    }

    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

    const getScope = (el) =>
        el.closest("[data-reveal-scope]") ||
        el.closest("tbody") ||
        el.closest(".metric-grid") ||
        el.closest(".kpi-grid") ||
        el.closest(".panel-grid") ||
        el.closest(".analytics-grid") ||
        el.closest(".auth-shell") ||
        el.closest("main") ||
        document.body;

    const groups = new Map();
    revealTargets.forEach((el) => {
        const key = getScope(el);
        if (!groups.has(key)) {
            groups.set(key, []);
        }
        groups.get(key).push(el);
    });

    const markVisible = (elements) => {
        elements.forEach((el) => el.classList.add("is-visible"));
    };

    const setRevealDelays = () => {
        groups.forEach((items, scope) => {
            const baseDelay = Number(scope.dataset.revealBaseDelay || 0);
            const rowStep = Number(scope.dataset.revealRowStep || 60);
            const colStep = Number(scope.dataset.revealColStep || 34);
            const rects = new Map();
            items.forEach((item) => {
                rects.set(item, item.getBoundingClientRect());
            });
            const sorted = items.slice().sort((a, b) => {
                const ra = rects.get(a);
                const rb = rects.get(b);
                if (Math.abs(ra.top - rb.top) > 2) {
                    return ra.top - rb.top;
                }
                return ra.left - rb.left;
            });

            const rowAnchors = [];
            sorted.forEach((el) => {
                const rect = rects.get(el);
                let rowIndex = rowAnchors.findIndex((top) => Math.abs(top - rect.top) < 24);
                if (rowIndex === -1) {
                    rowAnchors.push(rect.top);
                    rowIndex = rowAnchors.length - 1;
                }

                const colIndex = sorted
                    .filter((candidate) => {
                        const c = rects.get(candidate);
                        return Math.abs(c.top - rowAnchors[rowIndex]) < 24 && c.left < rect.left;
                    })
                    .length;

                const explicitDelay = el.dataset.revealDelay;
                const staggerDelay =
                    explicitDelay != null && explicitDelay !== ""
                        ? Number(explicitDelay)
                        : baseDelay + rowIndex * rowStep + colIndex * colStep;
                el.style.setProperty("--reveal-delay", `${staggerDelay}ms`);
                if (el.classList.contains("landing-reveal")) {
                    el.style.setProperty("--enter-delay", `${staggerDelay}ms`);
                }
            });
        });
    };

    const revealImmediateScopes = () => {
        groups.forEach((items, scope) => {
            if (scope.hasAttribute("data-reveal-immediate")) {
                markVisible(items);
            }
        });
    };

    if (prefersReducedMotion.matches) {
        markVisible(revealTargets);
        document.documentElement.classList.remove("js-reveal-pending");
        return;
    }

    document.body.classList.add("has-reveal");
    revealTargets.forEach((el) => {
        el.classList.add("reveal-item");
    });

    const observer = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) {
                    return;
                }

                const target = entry.target;
                if (target.hasAttribute("data-reveal-scope")) {
                    markVisible(groups.get(target) || []);
                } else {
                    target.classList.add("is-visible");
                }
                observer.unobserve(target);
            });
        },
        {
            threshold: 0.12,
            rootMargin: "0px 0px -8% 0px",
        }
    );

    requestAnimationFrame(() => {
        setRevealDelays();
        revealImmediateScopes();
        requestAnimationFrame(() => {
            window.setTimeout(() => {
                const observedScopes = new Set();
                revealTargets.forEach((el) => {
                    const scope = getScope(el);
                    if (scope.hasAttribute("data-reveal-immediate")) {
                        return;
                    }
                    if (scope.hasAttribute("data-reveal-scope")) {
                        if (!observedScopes.has(scope)) {
                            observedScopes.add(scope);
                            observer.observe(scope);
                        }
                        return;
                    }
                    observer.observe(el);
                });
            }, 90);
        });
    });

    const onMotionChange = () => {
        if (prefersReducedMotion.matches) {
            markVisible(revealTargets);
        }
    };

    if (typeof prefersReducedMotion.addEventListener === "function") {
        prefersReducedMotion.addEventListener("change", onMotionChange);
    } else if (typeof prefersReducedMotion.addListener === "function") {
        prefersReducedMotion.addListener(onMotionChange);
    }

    document.documentElement.classList.remove("js-reveal-pending");
})();
