/**
 * Client-side quick filters for admin panel lists/tables.
 */
(function () {
    "use strict";

    var main = document.querySelector(".admin-app .admin-main[data-admin-section]");
    if (!main) {
        return;
    }

    var section = main.getAttribute("data-admin-section") || "";
    var panels = queryAll(main, "[data-admin-filter-panel]");
    if (!panels.length) {
        return;
    }

    function norm(s) {
        return String(s || "")
            .trim()
            .toLowerCase();
    }

    function queryAll(root, sel) {
        return Array.prototype.slice.call(root.querySelectorAll(sel));
    }

    function isVisible(el) {
        return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    }

    function showEl(el) {
        el.style.removeProperty("display");
        el.removeAttribute("data-admin-filtered-out");
    }

    function hideEl(el) {
        el.style.display = "none";
        el.setAttribute("data-admin-filtered-out", "true");
    }

    function buildPanelContext(panel) {
        var key = panel.getAttribute("data-admin-filter-key") || "";
        var label = panel.getAttribute("data-admin-filter-label") || "this panel";
        var itemSelector = panel.getAttribute("data-admin-filter-item-selector") || "";
        var input = panel.querySelector("[data-admin-filter-input]");
        var clearBtn = panel.querySelector("[data-admin-filter-clear]");
        var statusEl = panel.querySelector("[data-admin-filter-status]");

        if (!key || !itemSelector || !input || !clearBtn || !statusEl) {
            return null;
        }

        return {
            clearBtn: clearBtn,
            input: input,
            itemSelector: itemSelector,
            key: key,
            label: label,
            panel: panel,
            statusEl: statusEl,
            storageKey: "fxj_admin_panel_filter_" + section + "_" + key,
        };
    }

    function collectPanelItems(ctx) {
        return queryAll(ctx.panel, ctx.itemSelector);
    }

    function applyFilter(ctx) {
        var raw = norm(ctx.input.value);
        var items = collectPanelItems(ctx);

        try {
            if (raw) {
                sessionStorage.setItem(ctx.storageKey, ctx.input.value);
            } else {
                sessionStorage.removeItem(ctx.storageKey);
            }
        } catch (_e) {
            /* ignore quota / private mode */
        }

        ctx.clearBtn.hidden = !ctx.input.value;

        if (!raw) {
            items.forEach(showEl);
            ctx.statusEl.textContent = "";
            ctx.statusEl.hidden = true;
            return;
        }

        var visible = 0;
        var total = items.length;
        items.forEach(function (el) {
            var text = norm(el.textContent);
            if (text.indexOf(raw) !== -1) {
                showEl(el);
                visible += 1;
            } else {
                hideEl(el);
            }
        });

        ctx.statusEl.textContent =
            visible === 0
                ? 'No matches in ' + ctx.label.toLowerCase() + ' for "' + ctx.input.value.trim() + '"'
                : visible +
                  " of " +
                  total +
                  " shown in " +
                  ctx.label.toLowerCase();
        ctx.statusEl.hidden = false;
    }

    var contexts = panels
        .map(buildPanelContext)
        .filter(function (ctx) {
            return !!ctx;
        });

    if (!contexts.length) {
        return;
    }

    contexts.forEach(function (ctx) {
        ctx.input.addEventListener("input", function () {
            applyFilter(ctx);
        });

        ctx.clearBtn.addEventListener("click", function () {
            ctx.input.value = "";
            applyFilter(ctx);
            ctx.input.focus();
        });

        try {
            var saved = sessionStorage.getItem(ctx.storageKey);
            if (saved) {
                ctx.input.value = saved;
                applyFilter(ctx);
            }
        } catch (_e) {
            /* ignore */
        }
    });

    document.addEventListener("keydown", function (e) {
        if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) {
            return;
        }
        var t = e.target;
        if (!t || typeof t.tagName !== "string") {
            return;
        }
        var tag = t.tagName.toUpperCase();
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON") {
            return;
        }
        if (t.isContentEditable) {
            return;
        }
        var shortcutInput = contexts
            .map(function (ctx) {
                return ctx.input;
            })
            .find(isVisible);
        if (!shortcutInput) {
            return;
        }
        e.preventDefault();
        shortcutInput.focus();
        shortcutInput.select();
    });
})();
