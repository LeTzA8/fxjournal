(function () {
    "use strict";

    var dataUrl = window.__TRADE_CHART_DATA_URL__;
    if (!dataUrl) return;

    var panel = document.getElementById("tradeChartPanel");
    var container = document.getElementById("tradeChartContainer");
    var statusEl = document.getElementById("tradeChartStatus");
    var tfLabel = document.getElementById("tradeChartTf");
    if (!panel || !container) return;

    function getCssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function hidePanel() {
        panel.setAttribute("data-chart-unavailable", "true");
    }

    function setStatus(text) {
        if (statusEl) statusEl.textContent = text;
    }

    function renderChart(payload) {
        if (statusEl) statusEl.style.display = "none";
        if (tfLabel) tfLabel.textContent = payload.timeframe || "";

        var bg = getCssVar("--card") || "#1a1a2e";
        var ink = getCssVar("--ink") || "#e8e8f0";
        var border = getCssVar("--border") || "#2a2a3e";
        var muted = getCssVar("--muted") || "#888";
        var good = getCssVar("--good") || "#4caf82";
        var bad = getCssVar("--bad") || "#e05c6a";

        var chart = LightweightCharts.createChart(container, {
            width: container.clientWidth,
            height: 320,
            layout: {
                background: { color: bg },
                textColor: ink,
            },
            grid: {
                vertLines: { color: border },
                horzLines: { color: border },
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
            },
            rightPriceScale: {
                borderColor: border,
            },
            timeScale: {
                borderColor: border,
                timeVisible: true,
                secondsVisible: false,
            },
        });

        var series = chart.addCandlestickSeries({
            upColor: good,
            downColor: bad,
            borderUpColor: good,
            borderDownColor: bad,
            wickUpColor: muted,
            wickDownColor: muted,
        });

        series.setData(payload.bars);

        var m = payload.markers || {};

        // SL / TP price lines
        if (m.stop_loss) {
            series.createPriceLine({
                price: m.stop_loss,
                color: bad,
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: "SL",
            });
        }
        if (m.take_profit) {
            series.createPriceLine({
                price: m.take_profit,
                color: good,
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: "TP",
            });
        }

        // Entry / exit markers
        var isBuy = (m.side || "").toUpperCase() === "BUY";
        var markers = [];
        if (m.entry_time && m.entry_price) {
            markers.push({
                time: m.entry_time,
                position: isBuy ? "belowBar" : "aboveBar",
                color: good,
                shape: isBuy ? "arrowUp" : "arrowDown",
                text: "Entry " + m.entry_price,
                size: 1,
            });
        }
        if (m.exit_time && m.exit_price) {
            markers.push({
                time: m.exit_time,
                position: isBuy ? "aboveBar" : "belowBar",
                color: bad,
                shape: "square",
                text: "Exit " + m.exit_price,
                size: 1,
            });
        }
        if (markers.length) {
            series.setMarkers(markers);
        }

        chart.timeScale().fitContent();

        // Resize when window resizes
        window.addEventListener("resize", function () {
            chart.applyOptions({ width: container.clientWidth });
        });
    }

    fetch(dataUrl)
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (data.status === "unavailable") {
                hidePanel();
            } else if (data.status === "pending") {
                setStatus("Chart data is being prepared — check back after the next sync.");
            } else if (data.status === "ready" && data.bars && data.bars.length > 0) {
                if (typeof LightweightCharts === "undefined") {
                    setStatus("Chart library failed to load.");
                    return;
                }
                renderChart(data);
            } else {
                setStatus("No bar data available for this trade.");
            }
        })
        .catch(function () {
            setStatus("Could not load chart data.");
        });
}());
