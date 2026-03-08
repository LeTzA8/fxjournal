(() => {
    const parseJson = (id, fallback = []) => {
        const node = document.getElementById(id);
        if (!node) return fallback;
        try {
            const parsed = JSON.parse(node.textContent || "[]");
            return Array.isArray(parsed) ? parsed : fallback;
        } catch {
            return fallback;
        }
    };

    const equityTradeData = parseJson("analyticsEquityTradesData");
    const equityDayData = parseJson("analyticsEquityDaysData");
    const weekdayData = parseJson("analyticsWeekdayData");
    const sessionData = parseJson("analyticsSessionData");

    const chartShared = window.FXJEquityCurveShared;
    if (!chartShared) {
        return;
    }

    const smoothPath = (points) => chartShared.smoothPath(points, 0.2);

    const renderEquityChart = () => {
        const shell = document.getElementById("equityChartShell");
        const svg = document.getElementById("equityChart");
        const grid = document.getElementById("equityChartGrid");
        const areaPositive = document.getElementById("equityChartAreaPositive");
        const areaNegative = document.getElementById("equityChartAreaNegative");
        const glowPositive = document.getElementById("equityChartGlowPositive");
        const glowNegative = document.getElementById("equityChartGlowNegative");
        const pathPositive = document.getElementById("equityChartPathPositive");
        const pathNegative = document.getElementById("equityChartPathNegative");
        const positiveClipRect = document.getElementById("analyticsPositiveClipRect");
        const negativeClipRect = document.getElementById("analyticsNegativeClipRect");
        const yAxis = document.getElementById("equityChartYAxis");
        const zeroLine = document.getElementById("equityChartZero");
        const pointsGroup = document.getElementById("equityChartPoints");
        const tooltip = document.getElementById("equityChartTooltip");
        const axis = document.getElementById("equityChartAxis");
        const modeSelect = document.getElementById("equityMode");
        if (
            !shell || !svg || !grid || !areaPositive || !areaNegative || !glowPositive || !glowNegative ||
            !pathPositive || !pathNegative || !positiveClipRect || !negativeClipRect || !yAxis || !zeroLine ||
            !pointsGroup || !tooltip || !axis
        ) {
            return;
        }

        const width = 760;
        const height = 280;
        const pad = { t: 22, r: 64, b: 28, l: 26 };
        const plotW = width - pad.l - pad.r;
        const plotH = height - pad.t - pad.b;

        const normalizePoint = (point, index) => {
            const equity = Number(point && point.equity);
            if (!Number.isFinite(equity)) return null;
            const label = (point && point.label) ? String(point.label) : `${index + 1}`;
            const dateLabel = (point && point.date) ? String(point.date) : label;
            return { label, dateLabel, equity };
        };

        const getData = () => {
            const mode = modeSelect ? modeSelect.value : "trade";
            const source = mode === "day" ? equityDayData : equityTradeData;
            return source.map(normalizePoint).filter(Boolean);
        };

        const pointTone = (equity) => chartShared.pointTone(equity);

        const restartCurveAnimation = () => {
            chartShared.restartCurveAnimation({
                svg,
                paths: [glowPositive, glowNegative, pathPositive, pathNegative],
                areas: [areaPositive, areaNegative],
            });
        };

        const formatAxisPnl = (value) => chartShared.formatAxisPnl(value);

        let renderedPoints = [];

        const getViewport = () => {
            const rect = svg.getBoundingClientRect();
            const scale = Math.min(rect.width / width, rect.height / height);
            return {
                rect,
                scale,
                offsetX: (rect.width - width * scale) / 2,
                offsetY: (rect.height - height * scale) / 2,
            };
        };

        const setTooltip = (point) => {
            const shellRect = shell.getBoundingClientRect();
            const viewport = getViewport();
            tooltip.hidden = false;
            tooltip.textContent = `${point.dateLabel}: ${point.equity > 0 ? "+" : ""}$${point.equity.toFixed(2)}`;
            tooltip.style.left = `${viewport.rect.left - shellRect.left + viewport.offsetX + point.x * viewport.scale}px`;
            tooltip.style.top = `${viewport.rect.top - shellRect.top + viewport.offsetY + point.y * viewport.scale}px`;
        };

        const hideTooltip = () => {
            tooltip.hidden = true;
        };

        const renderAxis = (points) => {
            axis.innerHTML = "";
            if (!points.length) return;
            const step = points.length > 10 ? Math.ceil(points.length / 6) : 1;
            points.forEach((point, index) => {
                const isEdge = index === 0 || index === points.length - 1;
                if (!isEdge && index % step !== 0) return;
                const label = document.createElement("span");
                label.className = "axis-label";
                label.textContent = point.label;
                label.style.left = `${(point.x / width) * 100}%`;
                axis.appendChild(label);
            });
        };

        const render = () => {
            const data = getData();
            if (!data.length) {
                pathPositive.setAttribute("d", "");
                pathNegative.setAttribute("d", "");
                areaPositive.setAttribute("d", "");
                areaNegative.setAttribute("d", "");
                glowPositive.setAttribute("d", "");
                glowNegative.setAttribute("d", "");
                yAxis.innerHTML = "";
                pointsGroup.innerHTML = "";
                axis.innerHTML = "";
                return;
            }

            const values = data.map((item) => item.equity);
            const maxAbsValue = Math.max(...values.map((value) => Math.abs(value)), 1);
            const chartLimit = maxAbsValue * 1.1;
            const minV = -chartLimit;
            const maxV = chartLimit;
            const xFor = (index, count) => count === 1 ? width / 2 : pad.l + (index / (count - 1)) * plotW;
            const yFor = (value) => pad.t + ((maxV - value) / (maxV - minV || 1)) * plotH;

            renderedPoints = data.map((item, index) => ({
                ...item,
                x: xFor(index, data.length),
                y: yFor(item.equity),
            }));

            grid.innerHTML = "";
            yAxis.innerHTML = "";
            for (let i = 0; i < 5; i += 1) {
                const y = pad.t + (plotH / 4) * i;
                const axisValue = maxV - ((maxV - minV) / 4) * i;
                const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
                line.setAttribute("class", "grid-line");
                line.setAttribute("x1", String(pad.l));
                line.setAttribute("x2", String(width - pad.r));
                line.setAttribute("y1", String(y));
                line.setAttribute("y2", String(y));
                grid.appendChild(line);

                const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
                tick.setAttribute("class", "y-axis-tick");
                tick.setAttribute("x1", String(width - pad.r + 6));
                tick.setAttribute("x2", String(width - pad.r + 14));
                tick.setAttribute("y1", String(y));
                tick.setAttribute("y2", String(y));
                yAxis.appendChild(tick);

                const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
                label.setAttribute("class", "y-axis-label");
                label.setAttribute("x", String(width - 6));
                label.setAttribute("y", String(y));
                label.setAttribute("text-anchor", "end");
                label.setAttribute("dominant-baseline", i === 0 ? "hanging" : i === 4 ? "auto" : "middle");
                label.textContent = formatAxisPnl(axisValue);
                yAxis.appendChild(label);
            }

            const lineD = smoothPath(renderedPoints);
            const zeroY = yFor(0);
            const areaD = `${lineD} L ${renderedPoints[renderedPoints.length - 1].x} ${zeroY} L ${renderedPoints[0].x} ${zeroY} Z`;

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
            negativeClipRect.setAttribute("height", String(Math.max(height - zeroY, 0)));

            zeroLine.setAttribute("x1", String(pad.l));
            zeroLine.setAttribute("x2", String(width - pad.r));
            zeroLine.setAttribute("y1", String(zeroY));
            zeroLine.setAttribute("y2", String(zeroY));

            pointsGroup.innerHTML = "";
            renderedPoints.forEach((point) => {
                const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
                circle.setAttribute("class", `chart-point ${pointTone(point.equity)}`);
                circle.setAttribute("cx", String(point.x));
                circle.setAttribute("cy", String(point.y));
                circle.setAttribute("r", "2.2");
                circle.addEventListener("mouseenter", () => setTooltip(point));
                circle.addEventListener("focus", () => setTooltip(point));
                circle.addEventListener("mouseleave", hideTooltip);
                circle.addEventListener("blur", hideTooltip);
                circle.setAttribute("tabindex", "0");
                pointsGroup.appendChild(circle);
            });

            renderAxis(renderedPoints);
        };

        svg.addEventListener("pointerleave", hideTooltip);
        shell.addEventListener("mouseleave", hideTooltip);
        if (modeSelect) {
            modeSelect.addEventListener("change", render);
        }
        window.addEventListener("resize", render, { passive: true });
        render();
    };

    const renderBarChart = (svgId, data, options) => {
        const svg = document.getElementById(svgId);
        if (!svg) return;
        const width = 560;
        const height = 240;
        const pad = { t: 24, r: 18, b: 34, l: 18 };
        const plotW = width - pad.l - pad.r;
        const plotH = height - pad.t - pad.b;
        const cleaned = data.filter((item) => Number.isFinite(Number(options.value(item))));
        if (!cleaned.length) {
            svg.innerHTML = "";
            return;
        }

        const minValue = Number.isFinite(Number(options.min)) ? Number(options.min) : Math.min(...cleaned.map((item) => Number(options.value(item))), 0);
        const maxValue = Number.isFinite(Number(options.max)) ? Number(options.max) : Math.max(...cleaned.map((item) => Number(options.value(item))), 0);
        const baselineValue = Number.isFinite(Number(options.baseline)) ? Number(options.baseline) : 0;
        const valueSpan = Math.max(maxValue - minValue, 1);
        const yFor = (value) => pad.t + ((maxValue - value) / valueSpan) * plotH;
        const zeroY = yFor(baselineValue);
        const colW = plotW / cleaned.length;
        svg.innerHTML = "";

        const baseline = document.createElementNS("http://www.w3.org/2000/svg", "line");
        baseline.setAttribute("x1", String(pad.l));
        baseline.setAttribute("x2", String(width - pad.r));
        baseline.setAttribute("y1", String(zeroY));
        baseline.setAttribute("y2", String(zeroY));
        baseline.setAttribute("stroke", "color-mix(in srgb, var(--border) 70%, transparent)");
        baseline.setAttribute("stroke-width", "1");
        svg.appendChild(baseline);

        cleaned.forEach((item, index) => {
            const value = Number(options.value(item));
            const distanceFromBaseline = Math.abs(value - baselineValue);
            const maxDistance = Math.max(Math.abs(maxValue - baselineValue), Math.abs(baselineValue - minValue), 1);
            const barH = Math.max((distanceFromBaseline / maxDistance) * (plotH / 2 - 14), 2);
            const x = pad.l + index * colW + colW * 0.15;
            const barW = colW * 0.7;
            const y = value >= baselineValue ? zeroY - barH : zeroY;
            const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
            rect.setAttribute("x", String(x));
            rect.setAttribute("y", String(y));
            rect.setAttribute("width", String(barW));
            rect.setAttribute("height", String(barH));
            rect.setAttribute("rx", "8");
            rect.setAttribute("fill", value >= baselineValue ? "rgba(34, 197, 94, 0.72)" : "rgba(239, 68, 68, 0.72)");
            svg.appendChild(rect);

            const caption = document.createElementNS("http://www.w3.org/2000/svg", "text");
            caption.setAttribute("x", String(x + barW / 2));
            caption.setAttribute("y", String(height - 12));
            caption.setAttribute("text-anchor", "middle");
            caption.setAttribute("font-size", "11");
            caption.setAttribute("font-weight", "700");
            caption.setAttribute("fill", "currentColor");
            caption.textContent = options.label(item);
            svg.appendChild(caption);

            const topLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
            topLabel.setAttribute("x", String(x + barW / 2));
            topLabel.setAttribute("y", String(value >= baselineValue ? y - 6 : y + barH + 14));
            topLabel.setAttribute("text-anchor", "middle");
            topLabel.setAttribute("font-size", "10");
            topLabel.setAttribute("font-weight", "700");
            topLabel.setAttribute("fill", "currentColor");
            topLabel.textContent = options.caption(item);
            svg.appendChild(topLabel);
        });
    };

    renderEquityChart();
    renderBarChart("weekdayChart", weekdayData, {
        value: (item) => item.win_rate ?? 0,
        min: 0,
        max: 100,
        baseline: 50,
        label: (item) => item.label || item.name?.slice(0, 3) || "",
        caption: (item) => item.count ? `${Number(item.win_rate ?? 0).toFixed(0)}%` : "0",
    });
    renderBarChart("sessionChart", sessionData, {
        value: (item) => item.net_pnl ?? 0,
        label: (item) => item.name || "",
        caption: (item) => `$${Number(item.net_pnl ?? 0) > 0 ? "+" : ""}${Number(item.net_pnl ?? 0).toFixed(0)}`,
    });
})();
