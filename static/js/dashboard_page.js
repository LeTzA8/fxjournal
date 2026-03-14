(() => {
    const chartShell = document.getElementById("pnlChartShell");
    const svg = document.getElementById("pnlLineChart");
    const grid = document.getElementById("pnlGrid");
    const pathPositive = document.getElementById("pnlPathPositive");
    const pathNegative = document.getElementById("pnlPathNegative");
    const glowPositive = document.getElementById("pnlGlowPositive");
    const glowNegative = document.getElementById("pnlGlowNegative");
    const areaPositive = document.getElementById("pnlAreaPositive");
    const areaNegative = document.getElementById("pnlAreaNegative");
    const positiveClipRect = document.getElementById("pnlPositiveClipRect");
    const negativeClipRect = document.getElementById("pnlNegativeClipRect");
    const yAxis = document.getElementById("pnlYAxis");
    const pointsGroup = document.getElementById("pnlPoints");
    const zeroLine = document.getElementById("pnlZeroLine");
    const tooltip = document.getElementById("pnlTooltip");
    const xAxis = document.getElementById("pnlXAxis");
    const dataNode = document.getElementById("pnlChartData");
    const rangeSelect = document.getElementById("equityRange");

    if (
        !chartShell || !svg || !grid || !pathPositive || !pathNegative || !glowPositive || !glowNegative ||
        !areaPositive || !areaNegative || !positiveClipRect || !negativeClipRect || !yAxis || !pointsGroup ||
        !zeroLine || !tooltip || !xAxis || !dataNode
    ) {
        return;
    }

    let chartData = [];
    try {
        chartData = JSON.parse(dataNode.textContent || "[]");
    } catch {
        chartData = [];
    }

    if (!Array.isArray(chartData) || !chartData.length) {
        pathPositive.setAttribute("d", "");
        pathNegative.setAttribute("d", "");
        glowPositive.setAttribute("d", "");
        glowNegative.setAttribute("d", "");
        areaPositive.setAttribute("d", "");
        areaNegative.setAttribute("d", "");
        yAxis.innerHTML = "";
        xAxis.innerHTML = '<span class="x-label" style="left: 50%;">-</span>';
        return;
    }

    const w = 600;
    const h = 220;
    const pad = { t: 18, r: 56, b: 26, l: 42 };
    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;

    const toEquity = (point) => {
        const equityValue = Number(point.equity);
        if (Number.isFinite(equityValue)) {
            return equityValue;
        }
        const legacyPnlValue = Number(point.pnl);
        return Number.isFinite(legacyPnlValue) ? legacyPnlValue : null;
    };

    const chartShared = window.FXJEquityCurveShared;
    if (!chartShared) {
        return;
    }

    const smoothPath = (points) => chartShared.smoothPath(points, 0.22);
    const pointTone = (equity) => chartShared.pointTone(equity);
    const formatAxisPnl = (value) => chartShared.formatAxisPnl(value);
    const downsamplePoints = (points, maxPoints) => chartShared.downsamplePoints(points, maxPoints, {
        getX: (point) => point.date.getTime(),
        getY: (point) => point.equity,
    });
    const MAX_RENDERED_POINTS = 48;

    const restartCurveAnimation = () => {
        chartShared.restartCurveAnimation({
            svg,
            paths: [glowPositive, glowNegative, pathPositive, pathNegative],
            areas: [areaPositive, areaNegative],
        });
    };

    const getViewportTransform = () => {
        const svgRect = svg.getBoundingClientRect();
        const scale = Math.min(svgRect.width / w, svgRect.height / h);
        const drawWidth = w * scale;
        const drawHeight = h * scale;
        const offsetX = (svgRect.width - drawWidth) / 2;
        const offsetY = (svgRect.height - drawHeight) / 2;
        return { svgRect, scale, offsetX, offsetY };
    };

    const setTooltip = (pt, dateText) => {
        const shellRect = chartShell.getBoundingClientRect();
        const { svgRect, scale, offsetX, offsetY } = getViewportTransform();
        const xOffset = svgRect.left - shellRect.left + offsetX;
        const yOffset = svgRect.top - shellRect.top + offsetY;
        const sign = pt.equity > 0 ? "+" : pt.equity < 0 ? "-" : "";
        tooltip.textContent = `${dateText}: ${sign}$${Math.abs(pt.equity).toFixed(2)}`;
        tooltip.hidden = false;
        tooltip.style.left = `${xOffset + pt.x * scale}px`;
        tooltip.style.top = `${yOffset + pt.y * scale}px`;
    };

    const hideTooltip = () => {
        tooltip.hidden = true;
    };

    const renderXAxis = (pts, labels) => {
        xAxis.innerHTML = "";
        const labelCount = pts.length;
        const labelStep = labelCount > 18 ? Math.ceil(labelCount / 8) : 1;
        xAxis.classList.toggle("dense", labelStep > 1);
        const { svgRect, scale, offsetX } = getViewportTransform();

        pts.forEach((pt, idx) => {
            const isEdge = idx === 0 || idx === pts.length - 1;
            if (!isEdge && idx % labelStep !== 0) {
                return;
            }
            const label = document.createElement("span");
            label.className = "x-label";
            label.textContent = labels[idx];
            const left = ((offsetX + pt.x * scale) / svgRect.width) * 100;
            label.style.left = `${left}%`;
            xAxis.appendChild(label);
        });
    };

    const normalizedPoints = chartData
        .map((d, idx) => {
            const equity = toEquity(d);
            if (!Number.isFinite(equity)) {
                return null;
            }

            let dateObj = null;
            const rawDate = (d && typeof d.date === "string") ? d.date.trim() : "";
            if (rawDate) {
                const parsed = new Date(`${rawDate}T00:00:00`);
                if (Number.isFinite(parsed.getTime())) {
                    dateObj = parsed;
                }
            }
            if (!dateObj) {
                const fallback = new Date();
                fallback.setHours(0, 0, 0, 0);
                fallback.setDate(fallback.getDate() - (chartData.length - idx - 1));
                dateObj = fallback;
            }

            const label = d.label || dateObj.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
            return {
                date: dateObj,
                dateKey: dateObj.toISOString().slice(0, 10),
                label,
                equity,
            };
        })
        .filter(Boolean)
        .sort((a, b) => a.date - b.date);

    if (!normalizedPoints.length) {
        return;
    }

    let renderedPoints = [];
    let hoverPoint = null;
    let drawdownHoverLocked = false;

    const hideHoverPoint = () => {
        if (hoverPoint) {
            hoverPoint.hidden = true;
            hoverPoint.classList.remove("is-active");
        }
        hideTooltip();
    };

    const findMaxDrawdown = (points) => {
        if (points.length < 2) {
            return null;
        }

        let peak = points[0].equity;
        let maxDrawdown = 0;
        let troughIndex = 0;

        for (let i = 1; i < points.length; i += 1) {
            if (points[i].equity > peak) {
                peak = points[i].equity;
            }
            const drawdown = peak - points[i].equity;
            if (drawdown > maxDrawdown) {
                maxDrawdown = drawdown;
                troughIndex = i;
            }
        }

        if (maxDrawdown <= 0) {
            return null;
        }

        return {
            troughIndex,
            amount: maxDrawdown,
        };
    };

    const showNearestHoverPoint = (clientX) => {
        if (drawdownHoverLocked || !hoverPoint || !renderedPoints.length) {
            return;
        }

        const { svgRect, scale, offsetX } = getViewportTransform();
        const rawSvgX = (clientX - svgRect.left - offsetX) / scale;
        const svgX = Math.max(pad.l, Math.min(w - pad.r, rawSvgX));

        let nearest = renderedPoints[0];
        for (let i = 1; i < renderedPoints.length; i += 1) {
            if (Math.abs(renderedPoints[i].x - svgX) < Math.abs(nearest.x - svgX)) {
                nearest = renderedPoints[i];
            }
        }

        hoverPoint.hidden = false;
        hoverPoint.className.baseVal = `chart-point hover-marker ${pointTone(nearest.equity)} is-active`;
        hoverPoint.setAttribute("cx", String(nearest.x));
        hoverPoint.setAttribute("cy", String(nearest.y));
        setTooltip(nearest, nearest.dateKey);
    };

    const pointsForRange = () => {
        const rangeValue = rangeSelect ? rangeSelect.value : "7";
        if (rangeValue === "all") {
            return downsamplePoints(normalizedPoints, MAX_RENDERED_POINTS);
        }

        const latestDate = normalizedPoints[normalizedPoints.length - 1].date;
        const startDate = new Date(latestDate);
        if (rangeValue === "month") {
            startDate.setDate(1);
        } else {
            const days = Number.parseInt(rangeValue, 10);
            const validDays = Number.isFinite(days) && days > 0 ? days : 7;
            startDate.setDate(startDate.getDate() - validDays + 1);
        }

        const filtered = normalizedPoints.filter((point) => point.date >= startDate);
        const fallback = normalizedPoints.slice(-Math.min(7, normalizedPoints.length));
        return downsamplePoints(filtered.length ? filtered : fallback, MAX_RENDERED_POINTS);
    };

    const renderChart = () => {
        const points = pointsForRange();
        const values = points.map((p) => p.equity);
        const maxAbsValue = Math.max(...values.map((value) => Math.abs(value)), 1);
        const chartLimit = maxAbsValue * 1.1;
        const minV = -chartLimit;
        const maxV = chartLimit;
        const xFor = (i, n) => (n === 1 ? w / 2 : pad.l + (i / (n - 1)) * plotW);
        const yFor = (v) => pad.t + ((maxV - v) / (maxV - minV)) * plotH;

        const pts = points.map((point, i) => ({
            x: xFor(i, points.length),
            y: yFor(point.equity),
            equity: point.equity,
            label: point.label,
            dateKey: point.dateKey,
        }));

        grid.innerHTML = "";
        yAxis.innerHTML = "";
        for (let i = 0; i < 5; i += 1) {
            const y = pad.t + (plotH / 4) * i;
            const axisValue = maxV - ((maxV - minV) / 4) * i;
            const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
            line.setAttribute("class", "grid-line");
            line.setAttribute("x1", String(pad.l));
            line.setAttribute("x2", String(w - pad.r));
            line.setAttribute("y1", String(y));
            line.setAttribute("y2", String(y));
            grid.appendChild(line);

            const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
            tick.setAttribute("class", "y-axis-tick");
            tick.setAttribute("x1", String(w - pad.r + 4));
            tick.setAttribute("x2", String(w - pad.r + 10));
            tick.setAttribute("y1", String(y));
            tick.setAttribute("y2", String(y));
            yAxis.appendChild(tick);

            const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
            label.setAttribute("class", "y-axis-label");
            label.setAttribute("x", String(w - 4));
            label.setAttribute("y", String(y));
            label.setAttribute("text-anchor", "end");
            label.setAttribute("dominant-baseline", i === 0 ? "hanging" : i === 4 ? "auto" : "middle");
            label.textContent = formatAxisPnl(axisValue);
            yAxis.appendChild(label);
        }

        const lineD = smoothPath(pts);
        const zeroY = yFor(0);
        const areaD = `${lineD} L ${pts[pts.length - 1].x} ${zeroY} L ${pts[0].x} ${zeroY} Z`;

        pathPositive.setAttribute("d", lineD);
        pathNegative.setAttribute("d", lineD);
        glowPositive.setAttribute("d", lineD);
        glowNegative.setAttribute("d", lineD);
        areaPositive.setAttribute("d", areaD);
        areaNegative.setAttribute("d", areaD);
        restartCurveAnimation();

        positiveClipRect.setAttribute("x", String(pad.l));
        positiveClipRect.setAttribute("y", "0");
        positiveClipRect.setAttribute("width", String(plotW));
        positiveClipRect.setAttribute("height", String(Math.max(zeroY, 0)));
        negativeClipRect.setAttribute("x", String(pad.l));
        negativeClipRect.setAttribute("y", String(zeroY));
        negativeClipRect.setAttribute("width", String(plotW));
        negativeClipRect.setAttribute("height", String(Math.max(h - zeroY, 0)));

        zeroLine.setAttribute("x1", String(pad.l));
        zeroLine.setAttribute("x2", String(w - pad.r));
        zeroLine.setAttribute("y1", String(zeroY));
        zeroLine.setAttribute("y2", String(zeroY));

        pointsGroup.innerHTML = "";

        const maxDrawdown = findMaxDrawdown(pts);
        if (maxDrawdown) {
            const troughPoint = pts[maxDrawdown.troughIndex];
            const drawdownMarker = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            drawdownMarker.setAttribute("class", "chart-point drawdown-marker");
            drawdownMarker.setAttribute("cx", String(troughPoint.x));
            drawdownMarker.setAttribute("cy", String(troughPoint.y));
            drawdownMarker.setAttribute("r", "2.6");
            drawdownMarker.setAttribute("tabindex", "0");
            drawdownMarker.setAttribute(
                "aria-label",
                `Max drawdown point ${troughPoint.dateKey} drawdown ${maxDrawdown.amount.toFixed(2)}`
            );
            drawdownMarker.addEventListener("mouseenter", () => {
                drawdownHoverLocked = true;
                if (hoverPoint) {
                    hoverPoint.hidden = true;
                    hoverPoint.classList.remove("is-active");
                }
                setTooltip(troughPoint, `${troughPoint.dateKey} Max DD`);
            });
            drawdownMarker.addEventListener("focus", () => {
                drawdownHoverLocked = true;
                if (hoverPoint) {
                    hoverPoint.hidden = true;
                    hoverPoint.classList.remove("is-active");
                }
                setTooltip(troughPoint, `${troughPoint.dateKey} Max DD`);
            });
            drawdownMarker.addEventListener("mouseleave", () => {
                drawdownHoverLocked = false;
                hideTooltip();
            });
            drawdownMarker.addEventListener("blur", () => {
                drawdownHoverLocked = false;
                hideTooltip();
            });
            pointsGroup.appendChild(drawdownMarker);

            const ddLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
            ddLabel.setAttribute("class", "drawdown-label");
            ddLabel.setAttribute("x", String(troughPoint.x + 8));
            ddLabel.setAttribute("y", String(Math.max(pad.t + 12, troughPoint.y - 8)));
            ddLabel.textContent = "MDD";
            pointsGroup.appendChild(ddLabel);
        }

        hoverPoint = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        hoverPoint.setAttribute("class", "chart-point hover-marker");
        hoverPoint.setAttribute("r", "2.6");
        hoverPoint.hidden = true;
        pointsGroup.appendChild(hoverPoint);

        renderedPoints = pts;

        renderXAxis(pts, points.map((point) => point.label));
        hideHoverPoint();
    };

    svg.addEventListener("pointermove", (event) => {
        showNearestHoverPoint(event.clientX);
    });
    svg.addEventListener("pointerdown", (event) => {
        showNearestHoverPoint(event.clientX);
    });
    svg.addEventListener("pointerleave", hideHoverPoint);
    svg.addEventListener("pointercancel", hideHoverPoint);
    chartShell.addEventListener("mouseleave", hideHoverPoint);

    renderChart();
    if (rangeSelect) {
        rangeSelect.addEventListener("change", () => {
            renderChart();
        });
    }
    window.addEventListener(
        "resize",
        () => {
            renderChart();
        },
        { passive: true }
    );
})();

(() => {
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
    const symbolFilter = document.getElementById("symbolFilter");
    const strategyFilter = document.getElementById("strategyFilter");
    const dateFilter = document.getElementById("dateFilter");
    const sideFilter = document.getElementById("sideFilter");
    const sessionFilter = document.getElementById("sessionFilter");
    const applyFiltersBtn = document.getElementById("applyFilters");
    const clearFiltersBtn = document.getElementById("clearFilters");
    const sortSelect = document.getElementById("tradeSort");
    const valueSelects = {
        symbol: symbolFilter,
        strategy: strategyFilter,
        date: dateFilter,
        side: sideFilter,
        session: sessionFilter,
    };

    const noMatchRow = document.createElement("tr");
    noMatchRow.className = "no-match-row hidden-row";
    noMatchRow.innerHTML = '<td colspan="6" class="muted">No trades match current filters.</td>';
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
            select: symbolFilter,
            rows,
            allLabel: "All Symbols",
            getValue: (row) => toUpper(row.dataset.symbol),
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
            select: sideFilter,
            rows,
            allLabel: "All Sides",
            getValue: (row) => row.dataset.side,
            sortValues: (a, b) => a.localeCompare(b),
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
        symbol: { datasetKey: "symbol" },
        strategy: { datasetKey: "strategy" },
        date: { datasetKey: "date", normalize: (value) => value || "" },
        side: { datasetKey: "side" },
        session: { datasetKey: "session" },
    };

    const getSortValue = (row, type) => {
        if (type === "date") {
            return toDateMs(row.dataset.date) ?? -Infinity;
        }
        if (type === "pnl") {
            return toNumber(row.dataset.pnl) ?? -Infinity;
        }
        return "";
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

    const applyFilters = () => {
        const filterType = filterField ? filterField.value : "";
        const rawValue = filterType && valueSelects[filterType] ? valueSelects[filterType].value : "";

        let visibleCount = 0;
        rows.forEach((row) => {
            const show = tradeFiltersShared.rowMatchesFilter(row, filterType, rawValue, filterFieldMap);
            row.classList.toggle("hidden-row", !show);
            if (show) {
                visibleCount += 1;
            }
        });

        noMatchRow.classList.toggle("hidden-row", visibleCount !== 0);
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

    updateValueControl();
    applyAll();
})();
