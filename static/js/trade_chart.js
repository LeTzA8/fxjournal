(function () {
    "use strict";

    var baseUrl = window.__TRADE_CHART_DATA_URL__;
    if (!baseUrl) return;

    var panel = document.getElementById("tradeChartPanel");
    var container = document.getElementById("tradeChartContainer");
    var statusEl = document.getElementById("tradeChartStatus");
    var tfGroup = document.getElementById("tradeChartTfGroup");
    if (!panel || !container) return;

    var chartInstance = null;
    var currentTf = "M5";
    var resizeWired = false;
    var MAX_CHART_PRICE_DECIMALS = 6;
    /** Bumps when chart is torn down so in-flight candle reveal animations stop. */
    var chartRevealGeneration = 0;
    /** From last successful chart-data response: avoids a second HTTP/DB round-trip when toggling M5/M15. */
    var chartPrefetchByTf = null;
    var lastChartReadyMeta = null;

    function tradeChartPixelSize() {
        var w = container.clientWidth;
        var h = container.clientHeight;
        if (!h || h < 200) h = 420;
        return { width: w, height: Math.round(h) };
    }

    function wireResizeOnce() {
        if (resizeWired) return;
        resizeWired = true;
        window.addEventListener("resize", function () {
            if (chartInstance) {
                var sz = tradeChartPixelSize();
                chartInstance.applyOptions({ width: sz.width, height: sz.height });
            }
        });
    }

    function chartDataUrl(timeframe) {
        var u = baseUrl.indexOf("?") >= 0 ? baseUrl + "&" : baseUrl + "?";
        return u + "timeframe=" + encodeURIComponent(timeframe);
    }

    function getCssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function hidePanel() {
        panel.setAttribute("data-chart-unavailable", "true");
    }

    function setStatus(text) {
        if (statusEl) {
            statusEl.style.display = "";
            statusEl.textContent = text;
        }
    }

    function destroyChart() {
        chartRevealGeneration += 1;
        if (chartInstance) {
            chartInstance.remove();
            chartInstance = null;
        }
    }

    function prefersReducedMotion() {
        try {
            return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        } catch (e) {
            return false;
        }
    }

    /** Ease-in-out cubic; same family as CSS ease-in-out curves used on cards. */
    function easeInOutCubic(t) {
        if (t <= 0) return 0;
        if (t >= 1) return 1;
        return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    }

    function buildChartPayloadFromPrefetch(tf) {
        if (!chartPrefetchByTf || !lastChartReadyMeta) return null;
        var bars = chartPrefetchByTf[tf];
        if (!bars || !bars.length) return null;
        var out = {
            status: "ready",
            timeframe: tf,
            available_timeframes: lastChartReadyMeta.available_timeframes,
            bars: bars,
            markers: lastChartReadyMeta.markers,
            display_timezone: lastChartReadyMeta.display_timezone,
        };
        var srcMap = lastChartReadyMeta.prefetched_bars_source;
        if (srcMap && srcMap[tf]) {
            out.bars_source = srcMap[tf];
        }
        return out;
    }

    function rememberChartPrefetch(data) {
        if (data && data.status === "ready" && data.prefetched_bars && typeof data.prefetched_bars === "object") {
            chartPrefetchByTf = data.prefetched_bars;
            lastChartReadyMeta = {
                available_timeframes: data.available_timeframes || [],
                markers: data.markers || {},
                display_timezone: data.display_timezone,
                prefetched_bars_source: data.prefetched_bars_source || null,
            };
        } else {
            chartPrefetchByTf = null;
            lastChartReadyMeta = null;
        }
    }

    /** Keep wheel events on the chart (zoom) instead of scrolling the page behind it. */
    function wireChartWheelCapture(chart) {
        if (!chart || typeof chart.chartElement !== "function") return;
        var el = chart.chartElement();
        if (!el) return;
        el.addEventListener(
            "wheel",
            function (ev) {
                ev.preventDefault();
            },
            { passive: false }
        );
    }

    function formatAxisPrice(p, precision) {
        if (p == null || !isFinite(Number(p))) return "";
        var n = Number(p);
        if (precision != null && isFinite(Number(precision))) {
            var bounded = Math.max(0, Math.min(MAX_CHART_PRICE_DECIMALS, Number(precision)));
            return n.toFixed(bounded);
        }
        var a = Math.abs(n);
        if (a >= 1000) return n.toFixed(2);
        if (a >= 10) return n.toFixed(3);
        return n.toFixed(5);
    }

    /**
     * Decimal count for display — never use String(Number(x)) (float garbage → 16dp on the axis).
     */
    function meaningfulDecimalPlaces(raw) {
        if (raw == null || raw === "") return 0;
        var n = Number(raw);
        if (!isFinite(n)) return 0;
        var s = n.toFixed(10);
        s = s.replace(/(\.\d*?[1-9])0+$/, "$1");
        s = s.replace(/\.0+$/, "");
        var dot = s.indexOf(".");
        if (dot < 0) return 0;
        return Math.min(s.length - dot - 1, 8);
    }

    function derivePricePrecision(bars, markers) {
        var values = [];
        if (bars && bars.length) {
            for (var i = 0; i < bars.length; i++) {
                values.push(bars[i].open, bars[i].high, bars[i].low, bars[i].close);
            }
        }
        if (markers) {
            values.push(markers.entry_price, markers.exit_price, markers.stop_loss, markers.take_profit);
        }

        var maxAbs = 0;
        var observed = 0;
        for (var j = 0; j < values.length; j++) {
            var n = Number(values[j]);
            if (!isFinite(n)) continue;
            var a = Math.abs(n);
            if (a > maxAbs) maxAbs = a;
            var dp = meaningfulDecimalPlaces(values[j]);
            if (dp > observed) observed = dp;
        }

        var baseline;
        if (maxAbs < 20) baseline = 5;
        else if (maxAbs < 250) baseline = 3;
        else baseline = 2;

        return Math.min(Math.max(observed, baseline), MAX_CHART_PRICE_DECIMALS);
    }

    /**
     * Bar open time (Unix) for the candle that contains `unix` — last bar with open <= event.
     * Avoids nearest-abs-diff, which can pin exit/entry to the wrong end of the range.
     */
    function barOpenContainingTime(unix, bars) {
        if (!bars || !bars.length || unix == null) return null;
        var t = Number(unix);
        var best = null;
        for (var i = 0; i < bars.length; i++) {
            var bt = bars[i].time;
            if (bt <= t) best = bt;
        }
        return best != null ? best : bars[0].time;
    }

    /** First bar open strictly after `unix`, else null. */
    function nextBarAfter(unix, bars) {
        if (!bars || !bars.length || unix == null) return null;
        var t = Number(unix);
        for (var i = 0; i < bars.length; i++) {
            if (bars[i].time > t) return bars[i].time;
        }
        return null;
    }

    /** IANA zone for axis labels — matches session display timezone from API; fallback = browser. */
    function resolveChartTimeZone(iana) {
        if (!iana || typeof iana !== "string" || !iana.trim()) {
            try {
                return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
            } catch (e) {
                return "UTC";
            }
        }
        var t = iana.trim();
        try {
            Intl.DateTimeFormat(undefined, { timeZone: t });
            return t;
        } catch (e) {
            try {
                return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
            } catch (e2) {
                return "UTC";
            }
        }
    }

    /**
     * Format UTC bar timestamps on the time scale and crosshair using a fixed IANA zone
     * (same wall-clock convention as the trade detail form), not the browser default alone.
     */
    function buildChartTimeFormatters(tzId) {
        var TickYear = 0;
        var TickMonth = 1;
        var TickDay = 2;
        var TickTimeSec = 4;
        return {
            tickMarkFormatter: function (time, tickMarkType, locale) {
                var sec = typeof time === "number" ? time : NaN;
                if (!isFinite(sec)) return null;
                var d = new Date(sec * 1000);
                if (isNaN(d.getTime())) return null;
                var opts;
                if (tickMarkType === TickYear) {
                    opts = { timeZone: tzId, year: "numeric" };
                } else if (tickMarkType === TickMonth) {
                    opts = { timeZone: tzId, month: "short", year: "numeric" };
                } else if (tickMarkType === TickDay) {
                    opts = { timeZone: tzId, month: "short", day: "numeric" };
                } else if (tickMarkType === TickTimeSec) {
                    opts = {
                        timeZone: tzId,
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                        hour12: false,
                    };
                } else {
                    opts = {
                        timeZone: tzId,
                        hour: "2-digit",
                        minute: "2-digit",
                        hour12: false,
                    };
                }
                try {
                    return new Intl.DateTimeFormat(locale || undefined, opts).format(d);
                } catch (e) {
                    return new Intl.DateTimeFormat(undefined, opts).format(d);
                }
            },
            crosshairTimeFormatter: function (time) {
                var sec = typeof time === "number" ? time : NaN;
                if (!isFinite(sec)) return "";
                var d = new Date(sec * 1000);
                if (isNaN(d.getTime())) return "";
                try {
                    return new Intl.DateTimeFormat(undefined, {
                        timeZone: tzId,
                        weekday: "short",
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                        hour12: false,
                    }).format(d);
                } catch (e) {
                    return "";
                }
            },
        };
    }

    function setTfButtonsActive(tf, available) {
        if (!tfGroup) return;
        var buttons = tfGroup.querySelectorAll("[data-trade-tf]");
        for (var i = 0; i < buttons.length; i++) {
            var b = buttons[i];
            var t = b.getAttribute("data-trade-tf");
            var has = !available || !available.length || available.indexOf(t) >= 0;
            b.classList.toggle("is-active", t === tf);
            b.disabled = !has;
            b.setAttribute("aria-pressed", t === tf ? "true" : "false");
        }
    }

    function renderChart(payload) {
        if (statusEl) statusEl.style.display = "none";
        destroyChart();

        var bg = getCssVar("--card") || "#1a1a2e";
        var ink = getCssVar("--ink") || "#e8e8f0";
        var border = getCssVar("--border") || "#2a2a3e";
        var muted = getCssVar("--muted") || "#888";
        var good = getCssVar("--good") || "#4caf82";
        var bad = getCssVar("--bad") || "#e05c6a";
        var accent = getCssVar("--accent") || "#818cf8";

        var chartSize = tradeChartPixelSize();
        var tzId = resolveChartTimeZone(payload.display_timezone);
        var timeFmt = buildChartTimeFormatters(tzId);
        var chart = LightweightCharts.createChart(container, {
            width: chartSize.width,
            height: chartSize.height,
            layout: {
                background: { color: bg },
                textColor: ink,
            },
            localization: {
                locale: typeof navigator !== "undefined" && navigator.language ? navigator.language : "en-US",
                dateFormat: "dd MMM 'yy",
                timeFormatter: timeFmt.crosshairTimeFormatter,
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
                rightOffset: 0,
                tickMarkFormatter: timeFmt.tickMarkFormatter,
                /* fixLeftEdge + fixRightEdge together can block zoom-out when all bars are in view (LC quirk). */
                fixLeftEdge: false,
                fixRightEdge: false,
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
        chartInstance = chart;
        wireChartWheelCapture(chart);

        var series = chart.addCandlestickSeries({
            upColor: good,
            downColor: bad,
            borderUpColor: good,
            borderDownColor: bad,
            wickUpColor: muted,
            wickDownColor: muted,
            /* Hide library default “current / last close” line + scale tag; we only show trade lines */
            priceLineVisible: false,
            lastValueVisible: false,
        });

        var bars = payload.bars;
        var m = payload.markers || {};
        var isBuy = (m.side || "").toUpperCase() === "BUY";
        var pricePrecision = derivePricePrecision(bars, m);
        var minMove = Math.pow(10, -pricePrecision);

        series.applyOptions({
            priceFormat: {
                type: "price",
                precision: pricePrecision,
                minMove: minMove,
            },
        });

        function applyTradeChartDecorations() {
            var markers = [];
            var entryRaw = m.entry_time != null ? Number(m.entry_time) : null;
            var exitRaw = m.exit_time != null ? Number(m.exit_time) : null;
            var entryT = entryRaw != null ? barOpenContainingTime(entryRaw, bars) : null;
            var exitT = exitRaw != null ? barOpenContainingTime(exitRaw, bars) : null;

            if (entryT != null && exitT != null && exitRaw != null && entryRaw != null) {
                if (exitRaw > entryRaw && exitT <= entryT) {
                    var nextExit = nextBarAfter(entryT, bars);
                    if (nextExit != null) exitT = nextExit;
                }
            }

            /* Entry / SL / TP / Exit: full-width price lines — entry & exit solid, SL & TP dashed. */
            if (m.entry_price != null && m.entry_price !== "") {
                series.createPriceLine({
                    price: Number(m.entry_price),
                    color: good,
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Solid,
                    axisLabelVisible: true,
                    title: "Entry " + formatAxisPrice(m.entry_price, pricePrecision),
                });
            }

            if (m.stop_loss != null && m.stop_loss !== "") {
                series.createPriceLine({
                    price: Number(m.stop_loss),
                    color: bad,
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: "SL " + formatAxisPrice(m.stop_loss, pricePrecision),
                });
            }
            if (m.take_profit != null && m.take_profit !== "") {
                series.createPriceLine({
                    price: Number(m.take_profit),
                    color: good,
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: "TP " + formatAxisPrice(m.take_profit, pricePrecision),
                });
            }
            if (m.exit_price != null && m.exit_price !== "") {
                series.createPriceLine({
                    price: Number(m.exit_price),
                    color: accent,
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Solid,
                    axisLabelVisible: true,
                    title: "Exit " + formatAxisPrice(m.exit_price, pricePrecision),
                });
            }

            var markerSize = 2;
            if (entryT != null) {
                markers.push({
                    time: entryT,
                    position: isBuy ? "belowBar" : "aboveBar",
                    color: good,
                    shape: isBuy ? "arrowUp" : "arrowDown",
                    text: "Entry",
                    size: markerSize,
                });
            }
            if (exitT != null) {
                markers.push({
                    time: exitT,
                    position: isBuy ? "aboveBar" : "belowBar",
                    color: accent,
                    shape: isBuy ? "arrowDown" : "arrowUp",
                    text: "Exit",
                    size: markerSize,
                });
            }
            if (markers.length) {
                series.setMarkers(markers);
            }

            chart.timeScale().fitContent();
            /* Start slightly zoomed in so wheel “zoom out” has headroom (fitContent alone pins max zoom-out). */
            requestAnimationFrame(function () {
                var ts = chart.timeScale();
                var r = ts.getVisibleLogicalRange();
                if (!r || !bars.length || bars.length < 10) return;
                var span = r.to - r.from;
                var inset = Math.min(span * 0.08, 4);
                if (span <= inset * 2 + 0.5) return;
                ts.setVisibleLogicalRange({ from: r.from + inset, to: r.to - inset });
            });
        }

        /* Canvas series can’t use CSS transitions; ease-in-out drives how fast new candles appear. */
        var revealGen = chartRevealGeneration;
        if (prefersReducedMotion() || bars.length <= 3) {
            series.setData(bars);
            applyTradeChartDecorations();
        } else {
            var n = bars.length;
            var durationMs = Math.min(2800, Math.max(520, 380 + n * 14));
            var t0 = performance.now();
            series.setData(bars.slice(0, 1));

            function tickBarReveal(now) {
                if (revealGen !== chartRevealGeneration) return;
                var u = Math.min(1, (now - t0) / durationMs);
                var eased = easeInOutCubic(u);
                var count = u >= 1 ? n : Math.max(1, Math.ceil(eased * n));
                series.setData(bars.slice(0, count));
                if (count >= n) {
                    applyTradeChartDecorations();
                    return;
                }
                requestAnimationFrame(tickBarReveal);
            }

            requestAnimationFrame(tickBarReveal);
        }

        wireResizeOnce();
    }

    function loadTimeframe(tf) {
        currentTf = tf;
        setTfButtonsActive(tf, null);
        setStatus("Loading chart…");
        fetch(chartDataUrl(tf))
            .then(function (res) {
                return res.json();
            })
            .then(function (data) {
                if (data.status === "unavailable") {
                    rememberChartPrefetch(null);
                    hidePanel();
                    return;
                }
                if (data.status === "pending") {
                    rememberChartPrefetch(null);
                    setStatus("Chart data is being prepared — check back after the next sync.");
                    return;
                }
                if (data.status === "ready") {
                    rememberChartPrefetch(data);
                    var avail = data.available_timeframes || [];
                    setTfButtonsActive(tf, avail);

                    if (!data.bars || !data.bars.length) {
                        var other = avail.filter(function (x) {
                            return x !== tf;
                        });
                        if (other.length) {
                            loadTimeframe(other[0]);
                            return;
                        }
                        setStatus("No bars for this timeframe yet. Run MT5 sync to refresh.");
                        return;
                    }
                    if (typeof LightweightCharts === "undefined") {
                        setStatus("Chart library failed to load.");
                        return;
                    }
                    renderChart(data);
                    return;
                }
                rememberChartPrefetch(null);
                setStatus("Could not load chart data.");
            })
            .catch(function () {
                rememberChartPrefetch(null);
                setStatus("Could not load chart data.");
            });
    }

    if (tfGroup) {
        tfGroup.addEventListener("click", function (ev) {
            var btn = ev.target.closest("[data-trade-tf]");
            if (!btn || btn.disabled) return;
            var tf = btn.getAttribute("data-trade-tf");
            if (!tf || tf === currentTf) return;
            var fromPrefetch = buildChartPayloadFromPrefetch(tf);
            if (fromPrefetch) {
                destroyChart();
                currentTf = tf;
                var avail = lastChartReadyMeta ? lastChartReadyMeta.available_timeframes : [];
                setTfButtonsActive(tf, avail);
                if (statusEl) statusEl.style.display = "none";
                renderChart(fromPrefetch);
                return;
            }
            destroyChart();
            loadTimeframe(tf);
        });
    }

    wireResizeOnce();
    loadTimeframe(currentTf);
}());

