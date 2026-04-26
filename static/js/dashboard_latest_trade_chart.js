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

    function setLoading(root) {
        root.classList.remove("is-ready");
    }

    function destroyChart(root) {
        if (root.__latestTradeResizeObserver) {
            root.__latestTradeResizeObserver.disconnect();
            root.__latestTradeResizeObserver = null;
        }
        if (root.__latestTradeChart) {
            root.__latestTradeChart.remove();
            root.__latestTradeChart = null;
        }
    }

    function setTimeframeButtons(root, activeTimeframe, availableTimeframes) {
        var buttons = root.querySelectorAll("[data-latest-trade-timeframe]");
        if (!buttons.length) return;
        var available = Array.isArray(availableTimeframes) ? availableTimeframes : [];
        buttons.forEach(function (button) {
            var tf = button.getAttribute("data-latest-trade-timeframe");
            var enabled = !available.length || available.indexOf(tf) >= 0;
            button.classList.toggle("is-active", tf === activeTimeframe);
            button.disabled = !enabled;
            button.setAttribute("aria-pressed", tf === activeTimeframe ? "true" : "false");
        });
    }

    function wireWheelZoomCapture(chart) {
        if (!chart || typeof chart.chartElement !== "function") return;
        var el = chart.chartElement();
        if (!el) return;
        el.addEventListener(
            "wheel",
            function (event) {
                event.preventDefault();
            },
            { passive: false }
        );
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

    function finiteNumber(value) {
        var n = Number(value);
        return isFinite(n) ? n : null;
    }

    function markerPrices(markers) {
        var prices = [];
        ["entry_price", "exit_price", "stop_loss", "take_profit"].forEach(function (key) {
            var n = finiteNumber(markers ? markers[key] : null);
            if (n != null) prices.push(n);
        });
        return prices;
    }

    function barValues(bar) {
        return [bar.open, bar.high, bar.low, bar.close]
            .map(finiteNumber)
            .filter(function (n) {
                return n != null && n > 0;
            });
    }

    function tradeWindowBars(bars, markers) {
        var entry = finiteNumber(markers ? markers.entry_time : null);
        var exit = finiteNumber(markers ? markers.exit_time : null);
        if (entry == null && exit == null) return bars || [];
        var start = entry != null ? entry : exit;
        var end = exit != null ? exit : entry;
        if (start > end) {
            var swap = start;
            start = end;
            end = swap;
        }
        var pad = Math.max(1800, (end - start) * 0.12);
        return (bars || []).filter(function (bar) {
            var t = finiteNumber(bar.time);
            return t != null && t >= start - pad && t <= end + pad;
        });
    }

    function percentile(sorted, pct) {
        if (!sorted.length) return null;
        var index = (sorted.length - 1) * pct;
        var low = Math.floor(index);
        var high = Math.ceil(index);
        if (low === high) return sorted[low];
        return sorted[low] + (sorted[high] - sorted[low]) * (index - low);
    }

    function entryExitAnchoredRange(markers, scaleMultiplier) {
        var entry = finiteNumber(markers ? markers.entry_price : null);
        var exit = finiteNumber(markers ? markers.exit_price : null);
        if (entry == null || exit == null || entry <= 0 || exit <= 0) return null;

        var low = Math.min(entry, exit);
        var high = Math.max(entry, exit);
        var mid = (entry + exit) / 2;
        var tradeMove = Math.abs(entry - exit);
        var minReadableMove = Math.max(Math.abs(mid) * 0.0022, 1e-8);
        var gauge = Math.max(tradeMove, minReadableMove);
        var multiplier = isFinite(scaleMultiplier) ? Math.max(0.35, Math.min(4, scaleMultiplier)) : 1;
        var pad = gauge * 0.8 * multiplier;

        return {
            minValue: low - pad,
            maxValue: high + pad,
        };
    }

    function tradeFocusedPriceRange(bars, markers, scaleMultiplier) {
        var entryExitRange = entryExitAnchoredRange(markers, scaleMultiplier);
        if (entryExitRange) return entryExitRange;

        var markerVals = markerPrices(markers);
        var windowBars = tradeWindowBars(bars, markers);
        var windowVals = [];
        windowBars.forEach(function (bar) {
            windowVals = windowVals.concat(barValues(bar));
        });

        var rangeVals = markerVals.slice();
        if (markerVals.length) {
            var markerMin = Math.min.apply(null, markerVals);
            var markerMax = Math.max.apply(null, markerVals);
            var markerMid = (markerMin + markerMax) / 2;
            var markerSpan = Math.max(markerMax - markerMin, Math.abs(markerMid) * 0.001, 1e-8);
            var outlierBand = Math.max(markerSpan * 8, Math.abs(markerMid) * 0.012);
            windowVals.forEach(function (value) {
                if (value >= markerMin - outlierBand && value <= markerMax + outlierBand) {
                    rangeVals.push(value);
                }
            });
        } else {
            rangeVals = windowVals;
        }

        if (!rangeVals.length) {
            (bars || []).forEach(function (bar) {
                rangeVals = rangeVals.concat(barValues(bar));
            });
        }
        if (!rangeVals.length) return null;

        rangeVals.sort(function (a, b) {
            return a - b;
        });
        var low = rangeVals[0];
        var high = rangeVals[rangeVals.length - 1];

        if (!markerVals.length && rangeVals.length >= 10) {
            low = percentile(rangeVals, 0.05);
            high = percentile(rangeVals, 0.95);
        }

        if (!isFinite(low) || !isFinite(high)) return null;
        var mid = (low + high) / 2;
        var span = Math.max(high - low, Math.abs(mid) * 0.0025, 1e-8);
        var multiplier = isFinite(scaleMultiplier) ? Math.max(0.35, Math.min(4, scaleMultiplier)) : 1;
        var paddedSpan = span * 1.28 * multiplier;
        return {
            minValue: mid - paddedSpan / 2,
            maxValue: mid + paddedSpan / 2,
        };
    }

    function fullDataPriceRange(bars, markers) {
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

    function applyChartScale(series, bars, markers, scaleState) {
        var range = scaleState.mode === "full"
            ? fullDataPriceRange(bars, markers)
            : tradeFocusedPriceRange(bars, markers, scaleState.multiplier);
        series.applyOptions({
            autoscaleInfoProvider: range
                ? function () {
                    return { priceRange: range };
                }
                : undefined,
        });
        series.setData(bars);
    }

    function wireScaleControls(root, series, bars, markers, scaleState) {
        var buttons = root.querySelectorAll("[data-latest-trade-scale]");
        if (!buttons.length) return;

        function updateButtons() {
            buttons.forEach(function (button) {
                var action = button.getAttribute("data-latest-trade-scale");
                button.disabled = action === "reset" && scaleState.mode === "focused" && scaleState.multiplier === 1;
            });
        }

        buttons.forEach(function (button) {
            button.onclick = function () {
                var action = button.getAttribute("data-latest-trade-scale");
                if (action === "in") {
                    scaleState.mode = "focused";
                    scaleState.multiplier = Math.max(0.45, scaleState.multiplier * 0.75);
                } else if (action === "out") {
                    scaleState.mode = "focused";
                    scaleState.multiplier = Math.min(4, scaleState.multiplier * 1.35);
                } else if (action === "reset") {
                    scaleState.mode = "focused";
                    scaleState.multiplier = 1;
                }
                applyChartScale(series, bars, markers, scaleState);
                updateButtons();
            };
        });

        updateButtons();
    }

    function draw(root, payload) {
        var canvas = root.querySelector("[data-latest-trade-chart-canvas]");
        if (!canvas || typeof LightweightCharts === "undefined") {
            setStatus(root, "Chart library failed to load.");
            return;
        }

        destroyChart(root);

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
                mouseWheel: true,
                pinch: true,
                axisPressedMouseMove: { time: true, price: true },
                axisDoubleClickReset: true,
            },
        });
        root.__latestTradeChart = chart;
        wireWheelZoomCapture(chart);

        var good = cssVar("--good", "#22c55e");
        var bad = cssVar("--bad", "#ef4444");
        var accent = cssVar("--accent", "#818cf8");
        var muted = cssVar("--muted", "#94a3b8");
        var precision = pricePrecision(bars, markers);
        var scaleState = {
            mode: "focused",
            multiplier: 1,
        };
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

        var series = chart.addCandlestickSeries(seriesOptions);
        series.applyOptions({
            priceFormat: {
                type: "price",
                precision: precision,
                minMove: Math.pow(10, -precision),
            },
        });
        applyChartScale(series, bars, markers, scaleState);

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
        wireScaleControls(root, series, bars, markers, scaleState);
        setTimeframeButtons(root, payload.timeframe || "M5", payload.available_timeframes || []);
        setReady(root);

        if (typeof ResizeObserver !== "undefined") {
            var ro = new ResizeObserver(function () {
                chart.applyOptions({
                    width: canvas.clientWidth || root.clientWidth,
                    height: canvas.clientHeight || root.clientHeight,
                });
            });
            ro.observe(canvas);
            root.__latestTradeResizeObserver = ro;
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

        function loadTimeframe(timeframe, fallbackTried) {
            setLoading(root);
            destroyChart(root);
            setStatus(root, "Loading chart data...");
            setTimeframeButtons(root, timeframe, null);
            fetch(appendTimeframe(url, timeframe), { credentials: "same-origin", cache: "no-store" })
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
                var available = payload.available_timeframes || [];
                if ((!payload.bars || !payload.bars.length) && available.length && !fallbackTried) {
                    var fallback = available.indexOf(timeframe) >= 0 ? available[0] : available[0];
                    if (fallback && fallback !== timeframe) {
                        loadTimeframe(fallback, true);
                        return;
                    }
                }
                draw(root, payload);
            })
            .catch(function () {
                setStatus(root, "Could not load this trade chart.");
            });
        }

        root.addEventListener("click", function (event) {
            var button = event.target.closest("[data-latest-trade-timeframe]");
            if (!button || button.disabled) return;
            var timeframe = button.getAttribute("data-latest-trade-timeframe");
            if (!timeframe || button.classList.contains("is-active")) return;
            loadTimeframe(timeframe, false);
        });

        loadTimeframe("M5", false);
    });
}());
