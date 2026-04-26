(function () {
    "use strict";

    var roots = document.querySelectorAll("[data-latest-trade-chart]");
    if (!roots.length) return;

    function cssVar(name, fallback) {
        var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return value || fallback;
    }

    function appendTimeframe(url, timeframe) {
        return url + (url.indexOf("?") >= 0 ? "&" : "?") + "timeframe=" + encodeURIComponent(timeframe);
    }

    function setStatus(root, text) {
        var status = root.querySelector("[data-latest-trade-chart-status]");
        if (status) status.textContent = text;
    }

    function setReady(root) {
        root.classList.add("is-ready");
    }

    function barOpenContainingTime(unix, bars) {
        if (unix == null || !bars || !bars.length) return null;
        var target = Number(unix);
        var best = null;
        for (var i = 0; i < bars.length; i += 1) {
            if (Number(bars[i].time) <= target) best = bars[i].time;
        }
        return best != null ? best : bars[0].time;
    }

    function pricePrecision(bars, markers) {
        var maxAbs = 0;
        var values = [];
        (bars || []).forEach(function (bar) {
            values.push(bar.open, bar.high, bar.low, bar.close);
        });
        if (markers) {
            values.push(markers.entry_price, markers.exit_price, markers.stop_loss, markers.take_profit);
        }
        values.forEach(function (value) {
            var n = Number(value);
            if (isFinite(n)) maxAbs = Math.max(maxAbs, Math.abs(n));
        });
        if (maxAbs < 20) return 5;
        if (maxAbs < 250) return 3;
        return 2;
    }

    function fixedPriceRange(bars, markers) {
        var min = Infinity;
        var max = -Infinity;
        var values = [];
        (bars || []).forEach(function (bar) {
            values.push(bar.open, bar.high, bar.low, bar.close);
        });
        if (markers) {
            values.push(markers.entry_price, markers.exit_price, markers.stop_loss, markers.take_profit);
        }
        values.forEach(function (value) {
            var n = Number(value);
            if (!isFinite(n)) return;
            min = Math.min(min, n);
            max = Math.max(max, n);
        });
        if (!isFinite(min) || !isFinite(max)) return null;
        var span = max - min;
        var pad = span > 0 ? span * 0.06 : Math.max(Math.abs(min) * 0.0005, 1e-8);
        return { minValue: min - pad, maxValue: max + pad };
    }

    function draw(root, payload) {
        var canvas = root.querySelector("[data-latest-trade-chart-canvas]");
        if (!canvas || typeof LightweightCharts === "undefined") {
            setStatus(root, "Chart library failed to load.");
            return;
        }

        var bars = payload.bars || [];
        if (!bars.length) {
            setStatus(root, "No chart bars for this trade yet.");
            return;
        }

        var markers = payload.markers || {};
        var chart = LightweightCharts.createChart(canvas, {
            width: canvas.clientWidth || root.clientWidth,
            height: canvas.clientHeight || root.clientHeight,
            layout: {
                background: { color: cssVar("--card", "#10131f") },
                textColor: cssVar("--ink", "#f8fafc"),
            },
            grid: {
                vertLines: { color: cssVar("--border", "#273042") },
                horzLines: { color: cssVar("--border", "#273042") },
            },
            rightPriceScale: {
                borderColor: cssVar("--border", "#273042"),
            },
            timeScale: {
                borderColor: cssVar("--border", "#273042"),
                timeVisible: true,
                secondsVisible: false,
                rightOffset: 1,
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
            },
            handleScroll: {
                mouseWheel: false,
                pressedMouseMove: true,
                horzTouchDrag: true,
                vertTouchDrag: false,
            },
            handleScale: {
                mouseWheel: false,
                pinch: true,
                axisPressedMouseMove: false,
                axisDoubleClickReset: true,
            },
        });

        var good = cssVar("--good", "#22c55e");
        var bad = cssVar("--bad", "#ef4444");
        var accent = cssVar("--accent", "#818cf8");
        var muted = cssVar("--muted", "#94a3b8");
        var precision = pricePrecision(bars, markers);
        var range = fixedPriceRange(bars, markers);
        var seriesOptions = {
            upColor: good,
            downColor: bad,
            borderUpColor: good,
            borderDownColor: bad,
            wickUpColor: muted,
            wickDownColor: muted,
            priceLineVisible: false,
            lastValueVisible: false,
        };
        if (range) {
            seriesOptions.autoscaleInfoProvider = function () {
                return { priceRange: range };
            };
        }

        var series = chart.addCandlestickSeries(seriesOptions);
        series.applyOptions({
            priceFormat: {
                type: "price",
                precision: precision,
                minMove: Math.pow(10, -precision),
            },
        });
        series.setData(bars);

        [
            ["entry_price", "Entry", good, LightweightCharts.LineStyle.Solid],
            ["exit_price", "Exit", accent, LightweightCharts.LineStyle.Solid],
            ["stop_loss", "SL", bad, LightweightCharts.LineStyle.Dashed],
            ["take_profit", "TP", good, LightweightCharts.LineStyle.Dashed],
        ].forEach(function (line) {
            var value = markers[line[0]];
            if (value == null || value === "") return;
            var n = Number(value);
            if (!isFinite(n)) return;
            series.createPriceLine({
                price: n,
                color: line[2],
                lineWidth: 1,
                lineStyle: line[3],
                axisLabelVisible: true,
                title: line[1],
            });
        });

        var isBuy = String(markers.side || "").toUpperCase() === "BUY";
        var tradeMarkers = [];
        var entryTime = barOpenContainingTime(markers.entry_time, bars);
        var exitTime = barOpenContainingTime(markers.exit_time, bars);
        if (entryTime != null) {
            tradeMarkers.push({
                time: entryTime,
                position: isBuy ? "belowBar" : "aboveBar",
                color: good,
                shape: isBuy ? "arrowUp" : "arrowDown",
                text: "Entry",
                size: 1,
            });
        }
        if (exitTime != null) {
            tradeMarkers.push({
                time: exitTime,
                position: isBuy ? "aboveBar" : "belowBar",
                color: accent,
                shape: isBuy ? "arrowDown" : "arrowUp",
                text: "Exit",
                size: 1,
            });
        }
        if (tradeMarkers.length) series.setMarkers(tradeMarkers);

        chart.timeScale().fitContent();
        setReady(root);

        if (typeof ResizeObserver !== "undefined") {
            var ro = new ResizeObserver(function () {
                chart.applyOptions({
                    width: canvas.clientWidth || root.clientWidth,
                    height: canvas.clientHeight || root.clientHeight,
                });
            });
            ro.observe(canvas);
        } else {
            window.addEventListener("resize", function () {
                chart.applyOptions({
                    width: canvas.clientWidth || root.clientWidth,
                    height: canvas.clientHeight || root.clientHeight,
                });
            });
        }
    }

    roots.forEach(function (root) {
        var url = root.getAttribute("data-chart-url");
        if (!url) return;
        if (typeof LightweightCharts === "undefined") {
            setStatus(root, "Chart library failed to load.");
            return;
        }
        fetch(appendTimeframe(url, "M5"), { credentials: "same-origin", cache: "no-store" })
            .then(function (response) {
                return response.json();
            })
            .then(function (payload) {
                if (!payload || payload.status === "unavailable") {
                    setStatus(root, "Price chart is available for closed MT5 trades.");
                    return;
                }
                if (payload.status === "pending") {
                    setStatus(root, "Chart bars are still syncing for this trade.");
                    return;
                }
                if (payload.status !== "ready") {
                    setStatus(root, "Could not load this trade chart.");
                    return;
                }
                draw(root, payload);
            })
            .catch(function () {
                setStatus(root, "Could not load this trade chart.");
            });
    });
}());
