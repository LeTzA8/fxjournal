(function () {
    const armButton = document.getElementById("batchDeleteArmButton");
    const batchSelect = document.getElementById("import_signature");
    const submitButton = document.getElementById("batchDeleteSubmit");
    if (armButton && batchSelect && submitButton) {
        let isArmed = false;

        const syncBatchDeleteState = () => {
            batchSelect.disabled = !isArmed;
            submitButton.disabled = !isArmed || !batchSelect.value;
            armButton.classList.toggle("is-armed", isArmed);
            submitButton.classList.toggle("is-armed", isArmed);
            armButton.textContent = isArmed ? "Delete Mode On" : "Enable Delete";
            if (!isArmed) {
                batchSelect.value = "";
            }
        };

        armButton.addEventListener("click", () => {
            isArmed = !isArmed;
            syncBatchDeleteState();
        });

        batchSelect.addEventListener("change", syncBatchDeleteState);
        syncBatchDeleteState();
    }

    const bulkDeleteForm = document.querySelector(".bulk-delete-form");
    const selectVisibleTrades = document.getElementById("selectVisibleTrades");
    const deleteSelectedTrades = document.getElementById("deleteSelectedTrades");
    const tradeCheckboxes = Array.from(document.querySelectorAll(".trade-select"));

    const table = document.querySelector(".trade-log table");
    if (!table) {
        return;
    }

    const tbody = table.querySelector("tbody");
    const rows = Array.from(tbody.querySelectorAll("tr:not(.empty-row)"));
    if (!rows.length) {
        return;
    }

    const filterField = document.getElementById("filterField");
    const pairFilter = document.getElementById("pairFilter");
    const dateFilter = document.getElementById("dateFilter");
    const sideFilter = document.getElementById("sideFilter");
    const statusFilter = document.getElementById("statusFilter");
    const applyFiltersBtn = document.getElementById("applyFilters");
    const clearFiltersBtn = document.getElementById("clearFilters");
    const sortSelect = document.getElementById("tradeSort");
    const valueSelects = {
        pair: pairFilter,
        date: dateFilter,
        side: sideFilter,
        status: statusFilter,
    };

    const noMatchRow = document.createElement("tr");
    noMatchRow.className = "no-match-row hidden-row";
    const columnCount = table.querySelectorAll("thead th").length || 9;
    noMatchRow.innerHTML = `<td colspan="${columnCount}" class="muted">No trades match current filters.</td>`;
    tbody.appendChild(noMatchRow);

    const toUpper = (value) => (value || "").toString().trim().toUpperCase();
    const toNumber = (value) => {
        const num = Number(value);
        return Number.isFinite(num) ? num : null;
    };
    const toDateMs = (value) => {
        if (!value) return null;
        const ms = Date.parse(value);
        return Number.isFinite(ms) ? ms : null;
    };
    const unique = (items) => Array.from(new Set(items.filter(Boolean)));
    const fillSelect = (select, values, allLabel) => {
        if (!select) return;
        select.innerHTML = "";
        const allOption = document.createElement("option");
        allOption.value = "";
        allOption.textContent = allLabel;
        select.appendChild(allOption);
        values.forEach((value) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = value;
            select.appendChild(option);
        });
    };

    fillSelect(
        pairFilter,
        unique(rows.map((row) => toUpper(row.dataset.pair))).sort(),
        "All Symbols"
    );
    fillSelect(
        dateFilter,
        unique(rows.map((row) => row.dataset.date)).sort((a, b) => b.localeCompare(a)),
        "All Dates"
    );

    const updateValueControl = () => {
        const active = filterField ? filterField.value : "";
        Object.entries(valueSelects).forEach(([key, select]) => {
            if (!select) return;
            select.hidden = key !== active;
        });
    };

    const getSortValue = (row, type) => {
        if (type === "date") {
            return toDateMs(row.dataset.date) ?? -Infinity;
        }
        if (type === "pnl") {
            return toNumber(row.dataset.pnl) ?? -Infinity;
        }
        return toNumber(row.dataset.lot) ?? -Infinity;
    };

    const sortRows = () => {
        const mode = sortSelect ? sortSelect.value : "date_desc";
        const [field, order] = mode.split("_");
        rows.sort((a, b) => {
            const av = getSortValue(a, field);
            const bv = getSortValue(b, field);
            return order === "asc" ? av - bv : bv - av;
        });
        rows.forEach((row) => tbody.appendChild(row));
    };

    const visibleTradeRows = () => rows.filter((row) => !row.classList.contains("hidden-row"));

    const syncBulkDeleteState = () => {
        if (!deleteSelectedTrades) {
            return;
        }
        const selectedCount = tradeCheckboxes.filter((input) => input.checked).length;
        deleteSelectedTrades.disabled = selectedCount === 0;

        if (!selectVisibleTrades) {
            return;
        }
        const visibleCheckboxes = visibleTradeRows()
            .map((row) => row.querySelector(".trade-select"))
            .filter(Boolean);
        const allVisibleSelected = visibleCheckboxes.length > 0 && visibleCheckboxes.every((input) => input.checked);
        selectVisibleTrades.checked = allVisibleSelected;
        selectVisibleTrades.indeterminate = visibleCheckboxes.some((input) => input.checked) && !allVisibleSelected;
    };

    const applyFilters = () => {
        const filterType = filterField ? filterField.value : "";
        const rawValue = filterType && valueSelects[filterType] ? valueSelects[filterType].value : "";
        const filterValue = toUpper(rawValue);

        let visibleCount = 0;
        rows.forEach((row) => {
            let show = true;
            if (filterType === "pair") {
                show = !filterValue || toUpper(row.dataset.pair) === filterValue;
            } else if (filterType === "date") {
                show = !rawValue || row.dataset.date === rawValue;
            } else if (filterType === "side") {
                show = !filterValue || toUpper(row.dataset.side) === filterValue;
            } else if (filterType === "status") {
                show = !filterValue || toUpper(row.dataset.status) === filterValue;
            }

            row.classList.toggle("hidden-row", !show);
            if (!show) {
                const hiddenCheckbox = row.querySelector(".trade-select");
                if (hiddenCheckbox) {
                    hiddenCheckbox.checked = false;
                }
            }
            if (show) {
                visibleCount += 1;
            }
        });

        noMatchRow.classList.toggle("hidden-row", visibleCount !== 0);
        syncBulkDeleteState();
    };

    const applyAll = () => {
        sortRows();
        applyFilters();
    };

    if (filterField) {
        filterField.addEventListener("change", () => {
            updateValueControl();
            applyAll();
        });
    }

    if (applyFiltersBtn) {
        applyFiltersBtn.addEventListener("click", applyAll);
    }

    if (clearFiltersBtn) {
        clearFiltersBtn.addEventListener("click", () => {
            if (filterField) filterField.value = "";
            Object.values(valueSelects).forEach((select) => {
                if (select) {
                    select.value = "";
                }
            });
            updateValueControl();
            applyAll();
        });
    }

    if (sortSelect) {
        sortSelect.addEventListener("change", applyAll);
    }

    if (selectVisibleTrades) {
        selectVisibleTrades.addEventListener("change", () => {
            visibleTradeRows().forEach((row) => {
                const checkbox = row.querySelector(".trade-select");
                if (checkbox) {
                    checkbox.checked = selectVisibleTrades.checked;
                }
            });
            syncBulkDeleteState();
        });
    }

    tradeCheckboxes.forEach((checkbox) => {
        checkbox.addEventListener("change", syncBulkDeleteState);
    });

    if (bulkDeleteForm) {
        bulkDeleteForm.addEventListener("submit", (event) => {
            if (!tradeCheckboxes.some((input) => input.checked)) {
                event.preventDefault();
            }
        });
    }

    updateValueControl();
    applyAll();
    syncBulkDeleteState();
})();
