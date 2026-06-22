(function () {
    const forms = Array.from(
        document.querySelectorAll("form.trade-form--new, form.trade-form--edit")
    );
    if (!forms.length) {
        return;
    }

    const METRICS_URL = "/api/trade-form-metrics";
    const METRIC_FIELDS = [
        "symbol",
        "side",
        "contract_code",
        "entry_price",
        "exit_price",
        "lot_size",
        "stop_loss",
        "take_profit",
        "commission",
        "swap",
        "pnl",
    ];

    const trimDecimal = (value, maxPlaces) => {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return "";
        }
        const places = Math.max(0, Math.min(Number(maxPlaces) || 2, 16));
        return Number(value)
            .toFixed(places)
            .replace(/\.?0+$/, "");
    };

    const formatSigned = (value, maxPlaces) => {
        const text = trimDecimal(value, maxPlaces);
        if (!text || text === "-") {
            return "-";
        }
        const numeric = Number(text);
        if (numeric > 0) {
            return `+${text}`;
        }
        return text;
    };

    const formatRiskReward = (value) => {
        const text = trimDecimal(value, 4);
        return text ? `${text}R` : "-";
    };

    const readFieldValue = (form, name) => {
        const field = form.elements.namedItem(name);
        if (!field) {
            return "";
        }
        if (field instanceof RadioNodeList) {
            return field.value || "";
        }
        return field.value || "";
    };

    const setTextValue = (form, id, value) => {
        const field = form.querySelector(`#${id}`);
        if (field) {
            field.value = value;
        }
    };

    const buildPayload = (form) => ({
        symbol: readFieldValue(form, "symbol"),
        side: readFieldValue(form, "side"),
        contract_code: readFieldValue(form, "contract_code"),
        entry_price: readFieldValue(form, "entry_price"),
        exit_price: readFieldValue(form, "exit_price"),
        lot_size: readFieldValue(form, "lot_size"),
        stop_loss: readFieldValue(form, "stop_loss"),
        take_profit: readFieldValue(form, "take_profit"),
        commission: readFieldValue(form, "commission"),
        swap: readFieldValue(form, "swap"),
        pnl: readFieldValue(form, "pnl"),
    });

    const readCsrfToken = (form) => readFieldValue(form, "csrf_token");

    forms.forEach((form) => {
        const pnlField = form.elements.namedItem("pnl");
        let requestId = 0;
        let debounceTimer = null;

        const applyMetrics = (metrics) => {
            if (!metrics) {
                return;
            }

            const currentPnl = readFieldValue(form, "pnl");
            if (
                pnlField instanceof HTMLInputElement &&
                metrics.pnl !== null &&
                metrics.pnl !== undefined &&
                !currentPnl
            ) {
                pnlField.value = trimDecimal(metrics.pnl, 2);
            }

            if (
                metrics.exit_price !== null &&
                metrics.exit_price !== undefined &&
                !readFieldValue(form, "exit_price") &&
                readFieldValue(form, "pnl")
            ) {
                const exitField = form.elements.namedItem("exit_price");
                if (exitField instanceof HTMLInputElement) {
                    exitField.value = String(metrics.exit_price);
                }
            }

            setTextValue(
                form,
                "net_pnl_display",
                metrics.net_pnl === null || metrics.net_pnl === undefined
                    ? "-"
                    : trimDecimal(metrics.net_pnl, 2)
            );
            setTextValue(form, "planned_rr_display", formatRiskReward(metrics.planned_rr));
            setTextValue(form, "actual_rr_display", formatRiskReward(metrics.actual_rr));

            if (metrics.pips !== null && metrics.pips !== undefined) {
                setTextValue(
                    form,
                    "pips_display",
                    `${formatSigned(metrics.pips, 4)} pips`
                );
            } else {
                setTextValue(form, "pips_display", "-");
            }

            if (metrics.ticks !== null && metrics.ticks !== undefined) {
                setTextValue(
                    form,
                    "ticks_display",
                    `${formatSigned(metrics.ticks, 4)} ticks`
                );
            } else {
                setTextValue(form, "ticks_display", "-");
            }
        };

        const refreshMetrics = () => {
            const currentRequest = ++requestId;
            const payload = buildPayload(form);

            fetch(METRICS_URL, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    Accept: "application/json",
                    "X-CSRFToken": readCsrfToken(form),
                },
                body: JSON.stringify(payload),
            })
                .then((response) => {
                    if (!response.ok) {
                        return null;
                    }
                    return response.json();
                })
                .then((metrics) => {
                    if (currentRequest !== requestId || !metrics) {
                        return;
                    }
                    applyMetrics(metrics);
                })
                .catch(() => {});
        };

        const scheduleRefresh = () => {
            window.clearTimeout(debounceTimer);
            debounceTimer = window.setTimeout(refreshMetrics, 180);
        };

        if (pnlField instanceof HTMLInputElement) {
            pnlField.addEventListener("input", scheduleRefresh);
        }

        METRIC_FIELDS.forEach((fieldName) => {
            const field = form.elements.namedItem(fieldName);
            if (!(field instanceof HTMLElement)) {
                return;
            }
            field.addEventListener("input", scheduleRefresh);
            field.addEventListener("change", scheduleRefresh);
        });

        scheduleRefresh();
    });
})();
