(function () {
    const toUpper = (value) => (value || "").toString().trim().toUpperCase();
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

    const populateSelects = (configs) => {
        configs.forEach(({ select, rows, allLabel, getValue, sortValues }) => {
            if (!select) return;
            const values = unique(rows.map(getValue));
            if (sortValues) {
                values.sort(sortValues);
            }
            fillSelect(select, values, allLabel);
        });
    };

    const updateValueControl = (filterField, valueSelects) => {
        const active = filterField ? filterField.value : "";
        Object.entries(valueSelects).forEach(([key, select]) => {
            if (!select) return;
            select.hidden = key !== active;
        });
    };

    const rowMatchesFilter = (row, filterType, rawValue, fieldMap) => {
        if (!filterType) {
            return true;
        }
        const config = fieldMap[filterType];
        if (!config) {
            return true;
        }
        if (!rawValue) {
            return true;
        }
        const normalize = config.normalize || toUpper;
        return normalize(row.dataset[config.datasetKey]) === normalize(rawValue);
    };

    const parseJournalFilterQuery = (search) => {
        try {
            const raw = typeof search === "string" ? search : window.location.search || "";
            const params = new URLSearchParams(raw.startsWith("?") || raw === "" ? raw : `?${raw}`);
            const pair = (params.get("pair") || params.get("symbol") || "").trim();
            const sess = (params.get("session") || "").trim();
            if (pair) {
                return { filterKey: "pair", value: pair };
            }
            if (sess) {
                return { filterKey: "session", value: sess };
            }
        } catch (_) {
            /* ignore */
        }
        return null;
    };

    window.FXJTradeFiltersShared = {
        toUpper,
        populateSelects,
        updateValueControl,
        rowMatchesFilter,
        parseJournalFilterQuery,
    };
})();
