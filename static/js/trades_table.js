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
    let shiftClickAnchorVisibleIndex = null;

    const table = document.querySelector(".trade-log table");
    if (!table) {
        return;
    }

    const tbody = table.querySelector("tbody");
    const rows = Array.from(tbody.querySelectorAll("tr:not(.empty-row)"));
    if (!rows.length) {
        return;
    }

    const bundlePalette = [
        "#5A8DEE",
        "#2E9E6F",
        "#D79A2B",
        "#D46B6B",
        "#7D7AE6",
        "#3FA7B4",
    ];
    const bundleRowsByKey = new Map();
    rows.forEach((row) => {
        const bundleKey = (row.dataset.bundle || "").trim();
        if (!bundleKey) {
            return;
        }
        if (!bundleRowsByKey.has(bundleKey)) {
            bundleRowsByKey.set(bundleKey, []);
        }
        bundleRowsByKey.get(bundleKey).push(row);
    });

    const toggleBundleHover = (bundleKey, isActive) => {
        const bundleRows = bundleRowsByKey.get(bundleKey) || [];
        bundleRows.forEach((bundleRow) => {
            bundleRow.classList.toggle("bundle-hover", isActive);
        });
    };

    Array.from(bundleRowsByKey.entries()).forEach(([bundleKey, bundleRows], index) => {
        const accent = bundlePalette[index % bundlePalette.length];
        bundleRows.forEach((row) => {
            row.style.setProperty("--bundle-accent", accent);
            row.addEventListener("mouseenter", () => toggleBundleHover(bundleKey, true));
            row.addEventListener("mouseleave", () => toggleBundleHover(bundleKey, false));
        });
    });

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

    const tradeFiltersShared = window.FXJTradeFiltersShared;
    if (!tradeFiltersShared) {
        return;
    }

    const toUpper = tradeFiltersShared.toUpper;
    const toNumber = (value) => {
        const num = Number(value);
        return Number.isFinite(num) ? num : null;
    };
    const toDateMs = (value) => {
        if (!value) return null;
        const ms = Date.parse(value);
        return Number.isFinite(ms) ? ms : null;
    };
    tradeFiltersShared.populateSelects([
        {
            select: pairFilter,
            rows,
            allLabel: "All Symbols",
            getValue: (row) => toUpper(row.dataset.pair),
            sortValues: (a, b) => a.localeCompare(b),
        },
        {
            select: strategyFilter,
            rows,
            allLabel: "All Strategies",
            getValue: (row) => row.dataset.strategy,
            sortValues: (a, b) => a.localeCompare(b),
        },
        {
            select: dateFilter,
            rows,
            allLabel: "All Dates",
            getValue: (row) => row.dataset.date,
            sortValues: (a, b) => b.localeCompare(a),
        },
        {
            select: sessionFilter,
            rows,
            allLabel: "All Sessions",
            getValue: (row) => row.dataset.session,
            sortValues: (a, b) => a.localeCompare(b),
        },
    ]);

    const updateValueControl = () => tradeFiltersShared.updateValueControl(filterField, valueSelects);
    const filterFieldMap = {
        pair: { datasetKey: "pair" },
        strategy: { datasetKey: "strategy" },
        date: { datasetKey: "date", normalize: (value) => value || "" },
        side: { datasetKey: "side" },
        session: { datasetKey: "session" },
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

        let visibleCount = 0;
        rows.forEach((row) => {
            const show = tradeFiltersShared.rowMatchesFilter(row, filterType, rawValue, filterFieldMap);
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
        shiftClickAnchorVisibleIndex = null;
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
            shiftClickAnchorVisibleIndex = null;
            syncBulkDeleteState();
        });
    }

    tradeCheckboxes.forEach((checkbox) => {
        // Range select must use mousedown: by "click", many browsers have already toggled
        // the checkbox, so !checkbox.checked was wrong and the range flipped/cleared oddly.
        checkbox.addEventListener("mousedown", (event) => {
            if (!selectVisibleTrades || event.button !== 0) {
                return;
            }
            if (!event.shiftKey || shiftClickAnchorVisibleIndex === null) {
                return;
            }
            event.preventDefault();
            const visibleRows = visibleTradeRows();
            const row = checkbox.closest("tr");
            const idx = visibleRows.indexOf(row);
            if (idx === -1) {
                return;
            }
            const wantChecked = !checkbox.checked;
            const start = Math.min(shiftClickAnchorVisibleIndex, idx);
            const end = Math.max(shiftClickAnchorVisibleIndex, idx);
            for (let i = start; i <= end; i += 1) {
                const cb = visibleRows[i].querySelector(".trade-select");
                if (cb) {
                    cb.checked = wantChecked;
                }
            }
            shiftClickAnchorVisibleIndex = idx;
            syncBulkDeleteState();
        });
        checkbox.addEventListener("click", (event) => {
            if (!selectVisibleTrades) {
                return;
            }
            if (event.shiftKey && shiftClickAnchorVisibleIndex !== null) {
                event.preventDefault();
            }
        });
        checkbox.addEventListener("change", () => {
            if (!selectVisibleTrades) {
                return;
            }
            const visibleRows = visibleTradeRows();
            const row = checkbox.closest("tr");
            const idx = visibleRows.indexOf(row);
            if (idx !== -1) {
                shiftClickAnchorVisibleIndex = idx;
            }
            syncBulkDeleteState();
        });
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

    const journalParsed = tradeFiltersShared.parseJournalFilterQuery();
    if (journalParsed && filterField) {
        const fieldName = journalParsed.filterKey === "pair" ? "pair" : "session";
        filterField.value = fieldName;
        updateValueControl();
        const targetSelect = valueSelects[fieldName];
        if (targetSelect) {
            const want =
                journalParsed.filterKey === "pair"
                    ? tradeFiltersShared.toUpper(journalParsed.value)
                    : journalParsed.value;
            const opts = Array.from(targetSelect.options);
            const exact = opts.find((opt) => opt.value === want);
            const folded = opts.find(
                (opt) =>
                    opt.value &&
                    want &&
                    String(opt.value).toLowerCase() === String(want).toLowerCase(),
            );
            const pick = exact || folded;
            if (pick) {
                targetSelect.value = pick.value;
            }
        }
    }

    updateValueControl();
    applyAll();
    syncBulkDeleteState();
})();
