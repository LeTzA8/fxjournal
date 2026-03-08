(function () {
    const body = document.body;
    if (!body || !body.classList.contains("landing-layout")) {
        return;
    }

    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    body.classList.add("js-landing-animate");

    const pauseConveyorIfNeeded = () => {
        const belt = document.querySelector(".review-belt");
        if (!belt) {
            return;
        }
        belt.style.animation = prefersReducedMotion.matches ? "none" : "";
    };

    const splitHeroTitleWords = () => {
        const title = document.querySelector(".hero-title");
        if (!title || title.dataset.wordsReady === "1") {
            return;
        }

        const text = title.textContent.trim().replace(/\s+/g, " ");
        if (!text) {
            return;
        }

        title.dataset.wordsReady = "1";
        title.setAttribute("aria-label", text);
        title.textContent = "";

        const fragment = document.createDocumentFragment();
        text.split(" ").forEach((word, index, arr) => {
            const span = document.createElement("span");
            span.className = "hero-word";
            span.textContent = word;
            span.setAttribute("aria-hidden", "true");
            span.style.setProperty("--word-index", String(index));
            span.style.setProperty("--word-base-delay", "80ms");
            span.style.setProperty("--word-delay", `${80 + index * 40}ms`);
            fragment.appendChild(span);
            if (index < arr.length - 1) {
                fragment.appendChild(document.createTextNode(" "));
            }
        });
        title.appendChild(fragment);
    };

    const buildReplayChart = () => {
        const chartRoot = document.getElementById("replayCandles");
        if (!chartRoot || chartRoot.dataset.ready === "1") {
            return;
        }

        chartRoot.dataset.ready = "1";

        const candleCount = 74;
        let close = 24;

        const fragment = document.createDocumentFragment();
        const noise = (i, seed = 0) => {
            const raw = Math.sin((i + 1) * 12.9898 + seed * 78.233) * 43758.5453;
            return raw - Math.floor(raw);
        };
        const lerp = (a, b, t) => a + (b - a) * t;
        const targetAt = (i) => {
            if (i < 8) {
                const t = i / 8;
                return lerp(24, 20, t) + Math.sin(i * 0.9) * 2.4;
            }
            if (i < 14) {
                const t = (i - 8) / 6;
                return lerp(20, 46, t) + Math.sin(i * 0.62) * 2.8;
            }
            if (i < 20) {
                const t = (i - 14) / 6;
                return lerp(46, 34, t) + Math.sin(i * 0.84) * 3.2;
            }
            if (i < 26) {
                const t = (i - 20) / 6;
                return lerp(34, 40, t) + Math.sin(i * 0.74) * 2.6;
            }
            if (i < 31) {
                const t = (i - 26) / 5;
                return lerp(40, 58, t) + Math.sin(i * 0.66) * 3;
            }
            const t = (i - 31) / 3;
            return lerp(58, 54, t) + Math.sin(i * 0.95) * 4.2;
        };

        for (let i = 0; i < candleCount; i += 1) {
            const open = close;
            const target = targetAt(i);
            const tendency = (target - open) * 0.62;
            const micro = (noise(i, 1) - 0.5) * 7.8 + Math.sin(i * 1.05 + 0.4) * 1.5;
            const burst =
                ((i % 6 === 0) ? (noise(i, 4) - 0.5) * 4.5 : 0) +
                ((i % 9 === 0) ? (noise(i, 5) - 0.5) * 3.4 : 0);
            close = Math.max(6, Math.min(96, open + tendency + micro + burst));

            const bodyHeight = Math.max(0.9, Math.abs(close - open));
            const wickUp =
                0.35 +
                (noise(i, 2) * 2.2) +
                (i % 5 === 0 ? 2.2 : 0) +
                (i % 9 === 0 ? 1.6 : 0);
            const wickDown =
                0.32 +
                (noise(i, 3) * 2) +
                (i % 7 === 0 ? 2.1 : 0) +
                (i % 11 === 0 ? 1.4 : 0);
            const high = Math.min(99, Math.max(open, close) + wickUp);
            const low = Math.max(1, Math.min(open, close) - wickDown);
            const bodyBottom = Math.min(open, close);
            const bodyTop = bodyBottom + bodyHeight;
            const wickTopHeight = Math.max(0.2, high - bodyTop);
            const wickBottomHeight = Math.max(0.2, bodyBottom - low);
            const candleStep = 103 / (candleCount - 1);
            const x = -1.5 + (i * candleStep);

            const candle = document.createElement("span");
            candle.className = `replay-candle ${close >= open ? "up" : "down"}`;
            candle.style.setProperty("--x", x.toFixed(3));
            candle.style.setProperty("--candle-step", candleStep.toFixed(3));
            candle.style.setProperty("--wick-low", low.toFixed(3));
            candle.style.setProperty("--wick-top-height", wickTopHeight.toFixed(3));
            candle.style.setProperty("--wick-bottom-height", wickBottomHeight.toFixed(3));
            candle.style.setProperty("--body-bottom", bodyBottom.toFixed(3));
            candle.style.setProperty("--body-height", bodyHeight.toFixed(3));
            const wickTopEl = document.createElement("span");
            wickTopEl.className = "wick-top";
            const wickBottomEl = document.createElement("span");
            wickBottomEl.className = "wick-bottom";
            const bodyEl = document.createElement("span");
            bodyEl.className = "candle-body";
            candle.append(wickTopEl, wickBottomEl, bodyEl);
            fragment.appendChild(candle);
        }

        chartRoot.appendChild(fragment);
    };

    splitHeroTitleWords();
    buildReplayChart();
    pauseConveyorIfNeeded();

    const onMotionChange = () => {
        pauseConveyorIfNeeded();
    };

    if (typeof prefersReducedMotion.addEventListener === "function") {
        prefersReducedMotion.addEventListener("change", onMotionChange);
    } else if (typeof prefersReducedMotion.addListener === "function") {
        prefersReducedMotion.addListener(onMotionChange);
    }
})();
