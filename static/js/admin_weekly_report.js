(() => {
    const dataNode = document.getElementById("weeklyAuditData");
    if (!dataNode) {
        return;
    }

    let pageData = {};
    try {
        pageData = JSON.parse(dataNode.textContent || "{}");
    } catch {
        return;
    }

    const records = Array.isArray(pageData.records) ? pageData.records : [];
    if (!records.length) {
        return;
    }

    const recordMap = new Map(records.map((record) => [String(record.id), record]));
    let selectedRecordId = String(pageData.initial_record_id || records[0].id);

    const chartShared = window.FXJEquityCurveShared;
    const smoothPath = chartShared
        ? (points) => chartShared.smoothPath(points, 0.18)
        : (points) => points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
    const formatAxisMoney = chartShared
        ? chartShared.formatAxisPnl
        : (value) => `${value > 0 ? "+" : value < 0 ? "-" : ""}$${Math.abs(Number(value) || 0).toFixed(0)}`;

    const currencyFormatter = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    const integerFormatter = new Intl.NumberFormat("en-US", {
        maximumFractionDigits: 0,
    });
    const numberFormatter = new Intl.NumberFormat("en-US", {
        minimumFractionDigits: 0,
        maximumFractionDigits: 2,
    });
    const percentFormatter = new Intl.NumberFormat("en-US", {
        minimumFractionDigits: 0,
        maximumFractionDigits: 1,
    });

    const asNumber = (value) => {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    };

    const compactPeriodLabel = (record) => {
        const raw = String(record.period_label || "").trim();
        if (!raw) {
            return "Saved week";
        }
        const parts = raw.split("-");
        if (parts.length > 1) {
            return parts[0].trim();
        }
        return raw.replace(/^Week of /, "");
    };

    const formatSignedCurrency = (value) => {
        const numeric = asNumber(value);
        if (numeric === null) {
            return "-";
        }
        const formatted = currencyFormatter.format(Math.abs(numeric));
        return numeric > 0 ? `+${formatted}` : numeric < 0 ? `-${formatted}` : formatted;
    };

    const formatCurrency = (value) => {
        const numeric = asNumber(value);
        return numeric === null ? "-" : currencyFormatter.format(numeric);
    };

    const formatPercent = (value) => {
        const numeric = asNumber(value);
        return numeric === null ? "-" : `${percentFormatter.format(numeric)}%`;
    };

    const formatNumber = (value) => {
        const numeric = asNumber(value);
        return numeric === null ? "-" : numberFormatter.format(numeric);
    };

    const formatInteger = (value) => {
        const numeric = asNumber(value);
        return numeric === null ? "-" : integerFormatter.format(numeric);
    };

    const titleCase = (value) => String(value || "")
        .split("_")
        .join(" ")
        .replace(/\b\w/g, (match) => match.toUpperCase());

    const setText = (id, text) => {
        const node = document.getElementById(id);
        if (!node) {
            return;
        }
        node.textContent = text;
    };

    const setTone = (id, value) => {
        const node = document.getElementById(id);
        if (!node) {
            return;
        }
        node.classList.remove("good", "bad");
        const numeric = asNumber(value);
        if (numeric === null || numeric === 0) {
            return;
        }
        node.classList.add(numeric > 0 ? "good" : "bad");
    };

    const renderStatList = (containerId, items) => {
        const container = document.getElementById(containerId);
        if (!container) {
            return;
        }
        container.innerHTML = "";
        const filteredItems = items.filter((item) => item && item.label);
        if (!filteredItems.length) {
            const dt = document.createElement("dt");
            dt.textContent = "Status";
            const dd = document.createElement("dd");
            dd.textContent = "No stored values";
            container.append(dt, dd);
            return;
        }
        filteredItems.forEach((item) => {
            const dt = document.createElement("dt");
            dt.textContent = item.label;
            const dd = document.createElement("dd");
            dd.textContent = item.value;
            container.append(dt, dd);
        });
    };

    const renderTagList = (containerId, items, type) => {
        const container = document.getElementById(containerId);
        if (!container) {
            return;
        }
        container.innerHTML = "";
        if (!Array.isArray(items) || !items.length) {
            const emptyCard = document.createElement("article");
            emptyCard.className = "tag-card";
            const title = document.createElement("p");
            title.className = "tag-card-title";
            title.textContent = "No stored values";
            const meta = document.createElement("p");
            meta.className = "tag-card-meta";
            meta.textContent = "This section was empty in the saved payload.";
            emptyCard.append(title, meta);
            container.appendChild(emptyCard);
            return;
        }

        items.forEach((item) => {
            const card = document.createElement("article");
            card.className = "tag-card";
            const title = document.createElement("p");
            title.className = "tag-card-title";
            title.textContent = String(item.symbol || item.name || "Unknown");

            const meta = document.createElement("p");
            meta.className = "tag-card-meta";
            const parts = [`Trades: ${formatInteger(item.count)}`];
            if (type !== "weekday") {
                parts.push(`Win rate: ${formatPercent(item.win_rate)}`);
            }
            parts.push(`Net PnL: ${formatSignedCurrency(item.net_pnl)}`);
            meta.textContent = parts.join(" · ");

            card.append(title, meta);
            container.appendChild(card);
        });
    };

    const getSelectedRecord = () => recordMap.get(String(selectedRecordId)) || records[0];

    const setSelectedRecord = (recordId) => {
        const key = String(recordId);
        if (!recordMap.has(key)) {
            return;
        }
        selectedRecordId = key;
        renderWeekStrip();
        renderCharts();
        renderSelectedRecord();
    };

    const renderWeekStrip = () => {
        const container = document.getElementById("weeklyAuditWeekStrip");
        if (!container) {
            return;
        }
        container.innerHTML = "";
        records.forEach((record) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = `trend-week-button${String(record.id) === String(selectedRecordId) ? " is-active" : ""}`;
            button.textContent = record.generation_count > 1
                ? `${record.period_label} · ${record.generation_count} gens`
                : record.period_label;
            button.addEventListener("click", () => setSelectedRecord(record.id));
            container.appendChild(button);
        });
    };

    const chartConfigs = [
        {
            svgId: "weeklyAuditPnlChart",
            valueAccessor: (record) => asNumber(((record.summary || {}).net_pnl)),
            lineColor: "#4f46e5",
            fillColor: "rgba(79, 70, 229, 0.12)",
            baseline: 0,
            formatValue: formatAxisMoney,
        },
        {
            svgId: "weeklyAuditWinRateChart",
            valueAccessor: (record) => asNumber(((record.summary || {}).win_rate)),
            lineColor: "#16a34a",
            fillColor: "rgba(22, 163, 74, 0.12)",
            baseline: 50,
            min: 0,
            max: 100,
            formatValue: (value) => `${Math.round(Number(value) || 0)}%`,
        },
        {
            svgId: "weeklyAuditEmotionChart",
            valueAccessor: (record) => asNumber(((record.emotional_index || {}).score)),
            lineColor: "#f3b84a",
            fillColor: "rgba(243, 184, 74, 0.16)",
            baseline: 0,
            min: 0,
            max: Math.max(
                ...records.map((record) => asNumber(((record.emotional_index || {}).score)) || 0),
                4,
            ),
            formatValue: (value) => numberFormatter.format(Number(value) || 0),
        },
    ];

    const renderTrendChart = (config) => {
        const svg = document.getElementById(config.svgId);
        const emptyNode = document.querySelector(`[data-empty-for="${config.svgId}"]`);
        if (!svg) {
            return;
        }

        const values = records
            .map((record, index) => {
                const value = config.valueAccessor(record);
                return value === null ? null : { record, index, value };
            })
            .filter(Boolean);

        svg.innerHTML = "";
        if (emptyNode) {
            emptyNode.hidden = values.length > 0;
        }
        if (!values.length) {
            return;
        }

        const width = 560;
        const height = 220;
        const pad = { t: 18, r: 56, b: 34, l: 18 };
        const plotW = width - pad.l - pad.r;
        const plotH = height - pad.t - pad.b;

        let minValue = config.min ?? Math.min(...values.map((item) => item.value));
        let maxValue = config.max ?? Math.max(...values.map((item) => item.value));
        if (config.baseline !== undefined) {
            minValue = Math.min(minValue, config.baseline);
            maxValue = Math.max(maxValue, config.baseline);
        }
        if (minValue === maxValue) {
            const offset = Math.max(Math.abs(minValue) * 0.2, 1);
            minValue -= offset;
            maxValue += offset;
        }

        const xFor = (index, count) => (count === 1
            ? width / 2
            : pad.l + (index / (count - 1)) * plotW);
        const yFor = (value) => pad.t + ((maxValue - value) / (maxValue - minValue || 1)) * plotH;
        const plottedPoints = values.map((item, visibleIndex) => ({
            ...item,
            x: xFor(visibleIndex, values.length),
            y: yFor(item.value),
        }));

        for (let step = 0; step < 4; step += 1) {
            const y = pad.t + (plotH / 3) * step;
            const gridLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
            gridLine.setAttribute("x1", String(pad.l));
            gridLine.setAttribute("x2", String(width - pad.r));
            gridLine.setAttribute("y1", String(y));
            gridLine.setAttribute("y2", String(y));
            gridLine.setAttribute("stroke", "rgba(148, 163, 184, 0.18)");
            gridLine.setAttribute("stroke-width", "1");
            svg.appendChild(gridLine);

            const axisValue = maxValue - ((maxValue - minValue) / 3) * step;
            const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
            label.setAttribute("x", String(width - 8));
            label.setAttribute("y", String(y));
            label.setAttribute("text-anchor", "end");
            label.setAttribute("dominant-baseline", step === 0 ? "hanging" : step === 3 ? "auto" : "middle");
            label.setAttribute("fill", "currentColor");
            label.setAttribute("opacity", "0.76");
            label.setAttribute("font-size", "10");
            label.setAttribute("font-weight", "700");
            label.textContent = config.formatValue(axisValue);
            svg.appendChild(label);
        }

        if (config.baseline !== undefined && config.baseline >= minValue && config.baseline <= maxValue) {
            const baselineY = yFor(config.baseline);
            const baseline = document.createElementNS("http://www.w3.org/2000/svg", "line");
            baseline.setAttribute("x1", String(pad.l));
            baseline.setAttribute("x2", String(width - pad.r));
            baseline.setAttribute("y1", String(baselineY));
            baseline.setAttribute("y2", String(baselineY));
            baseline.setAttribute("stroke", "rgba(148, 163, 184, 0.28)");
            baseline.setAttribute("stroke-width", "1");
            baseline.setAttribute("stroke-dasharray", "4 4");
            svg.appendChild(baseline);
        }

        const linePath = smoothPath(plottedPoints);
        const areaBaseY = yFor(config.baseline !== undefined ? config.baseline : minValue);
        const areaPath = `${linePath} L ${plottedPoints[plottedPoints.length - 1].x} ${areaBaseY} L ${plottedPoints[0].x} ${areaBaseY} Z`;

        const area = document.createElementNS("http://www.w3.org/2000/svg", "path");
        area.setAttribute("d", areaPath);
        area.setAttribute("fill", config.fillColor);
        svg.appendChild(area);

        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", linePath);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", config.lineColor);
        path.setAttribute("stroke-width", "3");
        path.setAttribute("stroke-linecap", "round");
        path.setAttribute("stroke-linejoin", "round");
        svg.appendChild(path);

        const labelIndices = new Set([0, Math.floor((plottedPoints.length - 1) / 2), plottedPoints.length - 1]);
        plottedPoints.forEach((point, pointIndex) => {
            const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            const isSelected = String(point.record.id) === String(selectedRecordId);
            circle.setAttribute("cx", String(point.x));
            circle.setAttribute("cy", String(point.y));
            circle.setAttribute("r", isSelected ? "5" : "3.25");
            circle.setAttribute("fill", isSelected ? "#ffffff" : config.lineColor);
            circle.setAttribute("stroke", config.lineColor);
            circle.setAttribute("stroke-width", isSelected ? "3" : "2");
            circle.setAttribute("tabindex", "0");
            circle.setAttribute("role", "button");
            circle.setAttribute("aria-label", `${point.record.period_label}: ${config.formatValue(point.value)}`);
            circle.style.cursor = "pointer";
            circle.addEventListener("click", () => setSelectedRecord(point.record.id));
            circle.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setSelectedRecord(point.record.id);
                }
            });
            svg.appendChild(circle);

            if (labelIndices.has(pointIndex)) {
                const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
                label.setAttribute("x", String(point.x));
                label.setAttribute("y", String(height - 8));
                label.setAttribute("text-anchor", pointIndex === 0 ? "start" : pointIndex === plottedPoints.length - 1 ? "end" : "middle");
                label.setAttribute("fill", "currentColor");
                label.setAttribute("opacity", "0.76");
                label.setAttribute("font-size", "10");
                label.setAttribute("font-weight", "700");
                label.textContent = compactPeriodLabel(point.record);
                svg.appendChild(label);
            }
        });
    };

    const renderCharts = () => {
        chartConfigs.forEach(renderTrendChart);
    };

    const renderSelectedRecord = () => {
        const record = getSelectedRecord();
        const summary = record.summary || {};
        const emotionalIndex = record.emotional_index || {};
        const signals = emotionalIndex.signals || {};
        const historicalContext = record.historical_context || {};
        const historicalSummary = historicalContext.summary || {};
        const notesCoverageValue = record.notes_coverage === null || record.notes_coverage === undefined
            ? "-"
            : formatPercent(Number(record.notes_coverage) * 100);

        setText("weeklyAuditSelectedPeriod", record.period_label || "-");
        setText(
            "weeklyAuditSelectedGenerationCount",
            record.generation_count > 1
                ? `Showing latest of ${formatInteger(record.generation_count)} saved generations for this week`
                : "Showing the only saved generation for this week",
        );
        setText("weeklyAuditGeneratedAt", record.generated_at_label || "-");
        setText("weeklyAuditModel", record.model || "-");
        setText("weeklyAuditAccountLabel", record.trade_account_label || "-");
        setText("weeklyAuditTradesUsed", formatInteger(record.trade_count_used));

        setText("weeklyAuditClosedTrades", formatInteger(summary.closed_trades));
        setText("weeklyAuditWinRate", formatPercent(summary.win_rate));
        setText("weeklyAuditNetPnl", formatSignedCurrency(summary.net_pnl));
        setText("weeklyAuditEmotionScore", formatNumber(emotionalIndex.score));
        setText("weeklyAuditDrawdown", formatCurrency(summary.max_drawdown));
        setText("weeklyAuditNotesCoverage", notesCoverageValue);
        setText(
            "weeklyAuditNotesConfidence",
            record.notes_confidence ? `Notes confidence: ${titleCase(record.notes_confidence)}` : "Notes confidence unavailable",
        );
        setText(
            "weeklyAuditEmotionLabel",
            emotionalIndex.label ? `${titleCase(emotionalIndex.label)} signal` : "No label saved",
        );
        setTone("weeklyAuditNetPnl", summary.net_pnl);
        setTone("weeklyAuditDrawdown", summary.max_drawdown ? -Math.abs(summary.max_drawdown) : 0);

        renderStatList("weeklyAuditSignalList", [
            { label: "Bundle Count", value: formatInteger(signals.bundle_count) },
            { label: "Revenge Trades", value: formatInteger(signals.revenge_trade_count) },
            { label: "Reactive Trades", value: formatInteger(signals.reactive_trade_count) },
            { label: "Corrective Trades", value: formatInteger(signals.corrective_trade_count) },
            { label: "Closed Trades", value: formatInteger(signals.total_closed_trades) },
            {
                label: "Self-Report Mismatch",
                value: emotionalIndex.self_report_mismatch === true
                    ? "Yes"
                    : emotionalIndex.self_report_mismatch === false
                    ? "No"
                    : "-",
            },
        ]);

        setText(
            "weeklyAuditHistoryScope",
            historicalContext.comparison_scope ? titleCase(historicalContext.comparison_scope) : "No comparison scope",
        );
        renderStatList("weeklyAuditHistoryList", [
            {
                label: "Window",
                value: historicalContext.window_days
                    ? `${formatInteger(historicalContext.window_days)} days`
                    : "-",
            },
            { label: "Closed Trades", value: formatInteger(historicalSummary.closed_trades) },
            { label: "Win Rate", value: formatPercent(historicalSummary.win_rate) },
            { label: "Net PnL", value: formatSignedCurrency(historicalSummary.net_pnl) },
            { label: "Max Drawdown", value: formatCurrency(historicalSummary.max_drawdown) },
            { label: "Account Age", value: record.account_age_days ? `${formatInteger(record.account_age_days)} days` : "-" },
        ]);

        renderTagList("weeklyAuditPairsList", historicalContext.top_pairs || [], "pair");
        renderTagList("weeklyAuditSessionsList", historicalContext.top_sessions || [], "session");
        renderTagList("weeklyAuditWeekdaysList", historicalContext.top_weekdays || [], "weekday");

        const prompt = record.prompt || {};
        setText("weeklyAuditPromptId", prompt.id || "Prompt ID unavailable");
        setText("weeklyAuditPromptSource", prompt.source_path || "Prompt source unavailable");
        setText("weeklyAuditPromptIdMeta", prompt.id || "-");
        setText("weeklyAuditPromptSourceMeta", prompt.source_path || "-");
        setText("weeklyAuditPromptCreatedAt", prompt.created_at_label || "-");
        setText("weeklyAuditResponsePeriod", record.period_label || "-");

        const responseNode = document.getElementById("weeklyAuditResponseText");
        if (responseNode) {
            responseNode.textContent = (record.response_text || "").trim() || "No response text was stored for this record.";
        }

        const payloadNode = document.getElementById("weeklyAuditPayloadJson");
        if (payloadNode) {
            if (record.payload && typeof record.payload === "object") {
                payloadNode.textContent = JSON.stringify(record.payload, null, 2);
            } else if (record.payload_raw) {
                const prefix = record.payload_parse_error ? `${record.payload_parse_error}\n\n` : "";
                payloadNode.textContent = `${prefix}${record.payload_raw}`;
            } else {
                payloadNode.textContent = "No payload JSON was stored for this record.";
            }
        }
    };

    renderWeekStrip();
    renderCharts();
    renderSelectedRecord();
})();
