/**
 * Client-side filter for admin panel lists/tables (current page DOM only).
 */
(function () {
    "use strict";

    var main = document.querySelector(".admin-app .admin-main[data-admin-section]");
    if (!main) {
        return;
    }

    var section = main.getAttribute("data-admin-section") || "";
    var input = document.getElementById("adminPageFilter");
    var clearBtn = document.getElementById("adminPageFilterClear");
    var statusEl = document.getElementById("adminPageFilterStatus");

    if (!input || !clearBtn || !statusEl) {
        return;
    }

    var storageKey = "fxj_admin_page_filter_" + section;

    function norm(s) {
        return String(s || "")
            .trim()
            .toLowerCase();
    }

    function queryAll(root, sel) {
        return Array.prototype.slice.call(root.querySelectorAll(sel));
    }

    /**
     * @returns {Element[]}
     */
    function collectFilterTargets() {
        var app = document.querySelector(".admin-app");
        if (!app) {
            return [];
        }

        switch (section) {
            case "users":
                return queryAll(app, ".admin-users-layout .pending-card").concat(
                    queryAll(app, ".admin-users-layout table.admin-table tbody tr"),
                );
            case "codes":
                return queryAll(
                    app,
                    "section.admin-main-grid:not(.admin-users-layout):not(.admin-mt5-layout):not(.admin-cfd-symbols-layout) .codes-panel .codes-list .code-card",
                );
            case "mt5":
                return queryAll(app, ".admin-mt5-layout .mt5-batches-panel article.code-card").concat(
                    queryAll(app, ".admin-mt5-layout .mt5-accounts-panel table.admin-table tbody tr"),
                );
            case "cfd_symbols":
                return queryAll(app, ".admin-cfd-symbols-layout .cfd-aliases-panel table.admin-table tbody tr");
            case "weekly_report":
                return queryAll(app, ".weekly-audit-scope-panel").concat(
                    queryAll(app, ".weekly-audit-grid > article.panel"),
                    queryAll(app, ".weekly-audit-empty"),
                );
            default:
                return [];
        }
    }

    function showEl(el) {
        el.style.removeProperty("display");
        el.removeAttribute("data-admin-filtered-out");
    }

    function hideEl(el) {
        el.style.display = "none";
        el.setAttribute("data-admin-filtered-out", "true");
    }

    function applyFilter() {
        var raw = norm(input.value);
        var items = collectFilterTargets();

        try {
            if (raw) {
                sessionStorage.setItem(storageKey, input.value);
            } else {
                sessionStorage.removeItem(storageKey);
            }
        } catch (_e) {
            /* ignore quota / private mode */
        }

        clearBtn.hidden = !input.value;

        if (!raw) {
            items.forEach(showEl);
            statusEl.textContent = "";
            statusEl.hidden = true;
            return;
        }

        var visible = 0;
        items.forEach(function (el) {
            var text = norm(el.textContent);
            if (text.indexOf(raw) !== -1) {
                showEl(el);
                visible += 1;
            } else {
                hideEl(el);
            }
        });

        statusEl.textContent =
            visible === 0
                ? 'No matches for "' + input.value.trim() + '"'
                : visible + " match" + (visible === 1 ? "" : "es");
        statusEl.hidden = false;
    }

    input.addEventListener("input", applyFilter);
    clearBtn.addEventListener("click", function () {
        input.value = "";
        applyFilter();
        input.focus();
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
        e.preventDefault();
        input.focus();
    });

    try {
        var saved = sessionStorage.getItem(storageKey);
        if (saved) {
            input.value = saved;
            applyFilter();
        }
    } catch (_e) {
        /* ignore */
    }
})();
