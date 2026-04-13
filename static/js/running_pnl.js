(() => {
    "use strict";

    const svg = document.getElementById("runningPnlChart");
    const grid = document.getElementById("runningPnlGrid");
    const yAxis = document.getElementById("runningPnlYAxis");
    const zeroLine = document.getElementById("runningPnlZero");
    const pathPnl = document.getElementById("runningPnlPathPnl");
    const pathCash = document.getElementById("runningPnlPathCash");
    const pathNet = document.getElementById("runningPnlPathNet");
    const pointsGroup = document.getElementById("runningPnlPoints");
    const tooltip = document.getElementById("runningPnlTooltip");
    const axisEl = document.getElementById("runningPnlAxis");
    const lineSelect = document.getElementById("runningPnlLines");
    const summaryEl = document.getElementById("runningPnlSummary");
    const summaryPnl = document.getElementById("runningPnlSummaryPnl");
    const summaryCash = document.getElementById("runningPnlSummaryCash");
    const summaryNet = document.getElementById("runningPnlSummaryNet");
    const emptyEl = document.getElementById("runningPnlEmpty");
    const shell = document.getElementById("runningPnlChartShell");

    if (!svg || !pathPnl) return;

    const W = 760, H = 280;
    const pad = { t: 22, r: 64, b: 28, l: 26 };
    const plotW = W - pad.l - pad.r;
    const plotH = H - pad.t - pad.b;
    const MAX_POINTS = 72;

    let cachedEvents = [];
    let cachedSummary = null;

    const formatPnl = (v) => {
        const abs = Math.abs(v);
        if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
        if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
        return v.toFixed(0);
    };

    const downsample = (arr) => {
        if (arr.length <= MAX_POINTS) return arr;
        const step = (arr.length - 1) / (MAX_POINTS - 1);
        const out = [];
        for (let i = 0; i < MAX_POINTS; i++) {
            out.push(arr[Math.round(i * step)]);
        }
        return out;
    };

    const getVisibleLines = () => {
        const v = lineSelect ? lineSelect.value : "pnl";
        return {
            pnl: v === "pnl" || v === "both",
            cash: v === "cash" || v === "both",
            net: v === "net",
        };
    };

    const buildPoints = (events, field) => {
        return events.map((e, i) => ({
            x: 0,
            y: 0,
            value: e[field],
            label: e.date_label || `#${i + 1}`,
            type: e.event_type,
            amount: e.amount,
            desc: e.description,
        }));
    };

    const layoutPoints = (points) => {
        if (!points.length) return [];
        const vals = points.map((p) => p.value);
        let minV = Math.min(0, ...vals);
        let maxV = Math.max(0, ...vals);
        if (minV === maxV) { minV -= 1; maxV += 1; }
        const range = maxV - minV || 1;
        points.forEach((p, i) => {
            p.x = pad.l + (points.length > 1 ? (i / (points.length - 1)) * plotW : plotW / 2);
            p.y = pad.t + plotH - ((p.value - minV) / range) * plotH;
        });
        return { minV, maxV };
    };

    const polyline = (points) => {
        if (!points.length) return "";
        return "M" + points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join("L");
    };

    const renderGrid = (minV, maxV) => {
        if (!grid) return;
        grid.innerHTML = "";
        if (!yAxis) return;
        yAxis.innerHTML = "";
        const steps = 4;
        const range = maxV - minV || 1;
        for (let i = 0; i <= steps; i++) {
            const v = minV + (range * i) / steps;
            const y = pad.t + plotH - ((v - minV) / range) * plotH;
            const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
            line.setAttribute("x1", pad.l);
            line.setAttribute("x2", W - pad.r);
            line.setAttribute("y1", y.toFixed(1));
            line.setAttribute("y2", y.toFixed(1));
            line.setAttribute("class", "grid-line");
            grid.appendChild(line);

            const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
            text.setAttribute("x", W - pad.r + 6);
            text.setAttribute("y", (y + 3.5).toFixed(1));
            text.setAttribute("class", "y-axis-label");
            text.textContent = `$${formatPnl(v)}`;
            yAxis.appendChild(text);
        }
        const zeroY = pad.t + plotH - ((0 - minV) / range) * plotH;
        if (zeroLine) {
            zeroLine.setAttribute("y1", zeroY.toFixed(1));
            zeroLine.setAttribute("y2", zeroY.toFixed(1));
        }
    };

    const renderAxis = (points) => {
        if (!axisEl) return;
        axisEl.innerHTML = "";
        if (!points.length) return;
        const step = points.length > 10 ? Math.ceil(points.length / 6) : 1;
        points.forEach((p, i) => {
            const isEdge = i === 0 || i === points.length - 1;
            if (!isEdge && i % step !== 0) return;
            const lbl = document.createElement("span");
            lbl.className = "axis-label";
            lbl.textContent = p.label;
            lbl.style.left = `${(p.x / W) * 100}%`;
            axisEl.appendChild(lbl);
        });
    };

    const renderDots = (points, cls) => {
        points.forEach((p) => {
            const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            c.setAttribute("cx", p.x.toFixed(1));
            c.setAttribute("cy", p.y.toFixed(1));
            c.setAttribute("r", "3");
            c.setAttribute("class", `running-pnl-point ${cls}`);
            c.addEventListener("mouseenter", () => showTooltip(p));
            c.addEventListener("mouseleave", hideTooltip);
            if (pointsGroup) pointsGroup.appendChild(c);
        });
    };

    const showTooltip = (p) => {
        if (!tooltip || !shell) return;
        tooltip.hidden = false;
        const sign = p.value >= 0 ? "+" : "";
        tooltip.textContent = `${p.label}: ${sign}$${p.value.toFixed(2)} (${p.desc})`;
        const rect = svg.getBoundingClientRect();
        const shellRect = shell.getBoundingClientRect();
        const scaleX = rect.width / W;
        const scaleY = rect.height / H;
        tooltip.style.left = `${rect.left - shellRect.left + p.x * scaleX}px`;
        tooltip.style.top = `${rect.top - shellRect.top + p.y * scaleY}px`;
    };

    const hideTooltip = () => {
        if (tooltip) tooltip.hidden = true;
    };

    const render = () => {
        const events = cachedEvents;
        pathPnl.setAttribute("d", "");
        if (pathCash) pathCash.setAttribute("d", "");
        if (pathNet) pathNet.setAttribute("d", "");
        if (pointsGroup) pointsGroup.innerHTML = "";

        if (!events.length) {
            if (emptyEl) emptyEl.hidden = false;
            if (summaryEl) summaryEl.hidden = true;
            if (grid) grid.innerHTML = "";
            if (yAxis) yAxis.innerHTML = "";
            if (axisEl) axisEl.innerHTML = "";
            return;
        }
        if (emptyEl) emptyEl.hidden = true;

        const vis = getVisibleLines();
        const sampled = downsample(events);

        let allValues = [];
        const pnlPts = buildPoints(sampled, "running_realized_pnl");
        const cashPts = buildPoints(sampled, "running_cash_flow");
        const netPts = buildPoints(sampled, "running_net_result");

        if (vis.pnl) allValues.push(...pnlPts.map((p) => p.value));
        if (vis.cash) allValues.push(...cashPts.map((p) => p.value));
        if (vis.net) allValues.push(...netPts.map((p) => p.value));
        if (!allValues.length) allValues = [0];

        let minV = Math.min(0, ...allValues);
        let maxV = Math.max(0, ...allValues);
        if (minV === maxV) { minV -= 1; maxV += 1; }
        const range = maxV - minV || 1;

        const layout = (pts) => {
            pts.forEach((p, i) => {
                p.x = pad.l + (pts.length > 1 ? (i / (pts.length - 1)) * plotW : plotW / 2);
                p.y = pad.t + plotH - ((p.value - minV) / range) * plotH;
            });
        };

        layout(pnlPts);
        layout(cashPts);
        layout(netPts);

        renderGrid(minV, maxV);
        renderAxis(pnlPts);

        if (vis.pnl) {
            pathPnl.setAttribute("d", polyline(pnlPts));
            pathPnl.style.display = "";
            renderDots(pnlPts, "running-pnl-point--pnl");
        } else {
            pathPnl.style.display = "none";
        }

        if (vis.cash && pathCash) {
            pathCash.setAttribute("d", polyline(cashPts));
            pathCash.style.display = "";
            renderDots(cashPts, "running-pnl-point--cash");
        } else if (pathCash) {
            pathCash.style.display = "none";
        }

        if (vis.net && pathNet) {
            pathNet.setAttribute("d", polyline(netPts));
            pathNet.style.display = "";
            renderDots(netPts, "running-pnl-point--net");
        } else if (pathNet) {
            pathNet.style.display = "none";
        }

        if (cachedSummary && summaryEl) {
            summaryEl.hidden = false;
            const s = cachedSummary;
            const fmtS = (v) => `${v >= 0 ? "+" : ""}$${v.toFixed(2)}`;
            if (summaryPnl) summaryPnl.innerHTML = `Trading P&L: <strong class="${s.total_realized_pnl >= 0 ? "good" : "bad"}">${fmtS(s.total_realized_pnl)}</strong> (${s.trade_close_count} trades)`;
            if (summaryCash) summaryCash.innerHTML = `Cash Flow: <strong>${fmtS(s.total_cash_flow)}</strong> (${s.deposit_count}D / ${s.withdrawal_count}W)`;
            if (summaryNet) summaryNet.innerHTML = `Net: <strong class="${s.total_net_result >= 0 ? "good" : "bad"}">${fmtS(s.total_net_result)}</strong>`;
        }
    };

    const loadData = () => {
        fetch("/api/running-pnl", { credentials: "same-origin" })
            .then((r) => r.ok ? r.json() : Promise.reject(r.statusText))
            .then((data) => {
                cachedEvents = data.events || [];
                cachedSummary = data.summary || null;
                render();
            })
            .catch(() => {
                cachedEvents = [];
                cachedSummary = null;
                render();
            });
    };

    if (lineSelect) lineSelect.addEventListener("change", render);
    loadData();
})();
