(function () {
    const batchDeleteMode = document.getElementById("batchDeleteMode");
    const batchSelect = document.getElementById("import_signature");
    const openDeleteImportBatch = document.getElementById("openDeleteImportBatch");
    const openDeleteSelectedTrades = document.getElementById("openDeleteSelectedTrades");
    const selectedTradeCount = document.getElementById("selectedTradeCount");
    if (batchSelect && batchDeleteMode && openDeleteSelectedTrades && openDeleteImportBatch) {
        const syncBatchDeleteState = () => {
            const importedMode = batchDeleteMode.value === "imported";
            batchSelect.hidden = !importedMode;
            batchSelect.disabled = !importedMode;
            openDeleteImportBatch.hidden = !importedMode;
            openDeleteSelectedTrades.hidden = importedMode;
            openDeleteImportBatch.disabled = !importedMode || !batchSelect.value;
            if (!importedMode) {
                batchSelect.value = "";
            }
        };

        batchDeleteMode.addEventListener("change", () => {
            syncBatchDeleteState();
        });

        batchSelect.addEventListener("change", syncBatchDeleteState);
        syncBatchDeleteState();
    }

    const bulkDeleteForm = document.querySelector(".bulk-delete-form");
    const selectVisibleTrades = document.getElementById("selectVisibleTrades");
    const assignTradeProfileButton = document.getElementById("assignTradeProfileButton");
    const deleteSelectedTradesDialog = document.getElementById("deleteSelectedTradesDialog");
    const deleteSelectedTradesForm = document.getElementById("deleteSelectedTradesForm");
    const deleteSelectedTradesInputs = document.getElementById("deleteSelectedTradesInputs");
    const deleteSelectedTradesLead = document.getElementById("deleteSelectedTradesLead");
    const deleteSelectedTradesConfirmation = document.getElementById("deleteSelectedTradesConfirmation");
    const deleteSelectedTradesAcknowledge = document.getElementById("deleteSelectedTradesAcknowledge");
    const cancelDeleteSelectedTrades = document.getElementById("cancelDeleteSelectedTrades");
    const deleteImportBatchDialog = document.getElementById("deleteImportBatchDialog");
    const deleteImportBatchForm = document.getElementById("deleteImportBatchForm");
    const deleteImportBatchLead = document.getElementById("deleteImportBatchLead");
    const deleteImportBatchSignature = document.getElementById("deleteImportBatchSignature");
    const deleteImportBatchConfirmation = document.getElementById("deleteImportBatchConfirmation");
    const deleteImportBatchAcknowledge = document.getElementById("deleteImportBatchAcknowledge");
    const cancelDeleteImportBatch = document.getElementById("cancelDeleteImportBatch");
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
    const strategyFilter = document.getElementById("strategyFilter");
    const dateFilter = document.getElementById("dateFilter");
    const sideFilter = document.getElementById("sideFilter");
    const sessionFilter = document.getElementById("sessionFilter");
    const applyFiltersBtn = document.getElementById("applyFilters");
    const clearFiltersBtn = document.getElementById("clearFilters");
    const sortSelect = document.getElementById("tradeSort");
    const valueSelects = {
        pair: pairFilter,
        strategy: strategyFilter,
        date: dateFilter,
        side: sideFilter,
        session: sessionFilter,
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
        strategyFilter,
        unique(rows.map((row) => row.dataset.strategy)).sort((a, b) => a.localeCompare(b)),
        "All Strategies"
    );
    fillSelect(
        dateFilter,
        unique(rows.map((row) => row.dataset.date)).sort((a, b) => b.localeCompare(a)),
        "All Dates"
    );
    fillSelect(
        sessionFilter,
        unique(rows.map((row) => row.dataset.session)).sort(),
        "All Sessions"
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
            return toDateMs(row.dataset.openedAt) ?? -Infinity;
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
    const selectedTradeCheckboxes = () => tradeCheckboxes.filter((input) => input.checked);

    const syncBulkDeleteState = () => {
        if (!openDeleteSelectedTrades) {
            if (assignTradeProfileButton) {
                const selectedCount = selectedTradeCheckboxes().length;
                assignTradeProfileButton.disabled = selectedCount === 0;
            }
            return;
        }
        const selectedCount = selectedTradeCheckboxes().length;
        openDeleteSelectedTrades.disabled = selectedCount === 0;
        if (assignTradeProfileButton) {
            assignTradeProfileButton.disabled = selectedCount === 0;
        }
        if (selectedTradeCount) {
            selectedTradeCount.textContent = selectedCount === 1
                ? "1 trade selected"
                : `${selectedCount} trades selected`;
        }

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
            } else if (filterType === "strategy") {
                show = !filterValue || toUpper(row.dataset.strategy) === filterValue;
            } else if (filterType === "date") {
                show = !rawValue || row.dataset.date === rawValue;
            } else if (filterType === "side") {
                show = !filterValue || toUpper(row.dataset.side) === filterValue;
            } else if (filterType === "session") {
                show = !filterValue || toUpper(row.dataset.session) === filterValue;
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

    const populateDeleteDialog = () => {
        if (!deleteSelectedTradesInputs || !deleteSelectedTradesLead) {
            return;
        }
        const selected = selectedTradeCheckboxes();
        deleteSelectedTradesInputs.innerHTML = "";
        selected.forEach((input) => {
            const hiddenInput = document.createElement("input");
            hiddenInput.type = "hidden";
            hiddenInput.name = "trade_pubkeys";
            hiddenInput.value = input.value;
            deleteSelectedTradesInputs.appendChild(hiddenInput);
        });
        const count = selected.length;
        deleteSelectedTradesLead.textContent = count === 1
            ? "This permanently deletes the 1 selected trade from your active account."
            : `This permanently deletes the ${count} selected trades from your active account.`;
        if (deleteSelectedTradesConfirmation) {
            deleteSelectedTradesConfirmation.value = "";
        }
        if (deleteSelectedTradesAcknowledge) {
            deleteSelectedTradesAcknowledge.checked = false;
        }
    };

    if (openDeleteSelectedTrades && deleteSelectedTradesDialog) {
        openDeleteSelectedTrades.addEventListener("click", () => {
            if (!selectedTradeCheckboxes().length) {
                return;
            }
            populateDeleteDialog();
            if (typeof deleteSelectedTradesDialog.showModal === "function") {
                deleteSelectedTradesDialog.showModal();
            }
        });
    }

    if (cancelDeleteSelectedTrades && deleteSelectedTradesDialog) {
        cancelDeleteSelectedTrades.addEventListener("click", () => {
            deleteSelectedTradesDialog.close();
        });
    }

    if (deleteSelectedTradesDialog) {
        deleteSelectedTradesDialog.addEventListener("click", (event) => {
            const rect = deleteSelectedTradesDialog.getBoundingClientRect();
            const isInside =
                rect.top <= event.clientY &&
                event.clientY <= rect.top + rect.height &&
                rect.left <= event.clientX &&
                event.clientX <= rect.left + rect.width;
            if (!isInside) {
                deleteSelectedTradesDialog.close();
            }
        });
    }

    if (deleteSelectedTradesForm) {
        deleteSelectedTradesForm.addEventListener("submit", (event) => {
            const confirmationValue = (deleteSelectedTradesConfirmation?.value || "").trim().toUpperCase();
            const acknowledged = Boolean(deleteSelectedTradesAcknowledge?.checked);
            if (!selectedTradeCheckboxes().length || confirmationValue !== "DELETE" || !acknowledged) {
                event.preventDefault();
            }
        });
    }

    const populateImportBatchDialog = () => {
        if (!batchSelect || !deleteImportBatchSignature || !deleteImportBatchLead) {
            return false;
        }
        const selectedOption = batchSelect.options[batchSelect.selectedIndex];
        const signature = batchSelect.value;
        if (!signature) {
            return false;
        }
        deleteImportBatchSignature.value = signature;
        deleteImportBatchLead.textContent = `This permanently deletes every trade in ${selectedOption.textContent}.`;
        if (deleteImportBatchConfirmation) {
            deleteImportBatchConfirmation.value = "";
        }
        if (deleteImportBatchAcknowledge) {
            deleteImportBatchAcknowledge.checked = false;
        }
        return true;
    };

    if (openDeleteImportBatch && deleteImportBatchDialog) {
        openDeleteImportBatch.addEventListener("click", () => {
            if (!populateImportBatchDialog()) {
                return;
            }
            if (typeof deleteImportBatchDialog.showModal === "function") {
                deleteImportBatchDialog.showModal();
            }
        });
    }

    if (cancelDeleteImportBatch && deleteImportBatchDialog) {
        cancelDeleteImportBatch.addEventListener("click", () => {
            deleteImportBatchDialog.close();
        });
    }

    if (deleteImportBatchDialog) {
        deleteImportBatchDialog.addEventListener("click", (event) => {
            const rect = deleteImportBatchDialog.getBoundingClientRect();
            const isInside =
                rect.top <= event.clientY &&
                event.clientY <= rect.top + rect.height &&
                rect.left <= event.clientX &&
                event.clientX <= rect.left + rect.width;
            if (!isInside) {
                deleteImportBatchDialog.close();
            }
        });
    }

    if (deleteImportBatchForm) {
        deleteImportBatchForm.addEventListener("submit", (event) => {
            const confirmationValue = (deleteImportBatchConfirmation?.value || "").trim().toUpperCase();
            const acknowledged = Boolean(deleteImportBatchAcknowledge?.checked);
            if (!deleteImportBatchSignature?.value || confirmationValue !== "DELETE" || !acknowledged) {
                event.preventDefault();
            }
        });
    }

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
