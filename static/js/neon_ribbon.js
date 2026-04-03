(function () {
    const canvas = document.getElementById("neonRibbonCanvas");
    if (!canvas) {
        return;
    }

    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) {
        return;
    }

    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const MAX_DPR = 1.25;
    const TARGET_FPS = 30;
    const FRAME_INTERVAL_MS = 1000 / TARGET_FPS;
    let rafId = 0;
    let width = 0;
    let height = 0;
    let dpr = 1;
    let didFirstFrame = false;
    let lastFrameTimeMs = 0;

    const getTheme = () =>
        document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    const getThemeValue = (name, fallback) => {
        const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return value || fallback;
    };
    const getRgbTriplet = (name, fallback) => {
        const rawValue = getThemeValue(name, "");
        if (!rawValue) {
            return fallback;
        }
        const parsed = rawValue
            .split(/[\s,]+/)
            .map((part) => Number.parseFloat(part))
            .filter((part) => Number.isFinite(part));
        return parsed.length >= 3 ? parsed.slice(0, 3) : fallback;
    };

    const resize = () => {
        dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
        width = Math.max(window.innerWidth, 1);
        height = Math.max(window.innerHeight, 1);
        canvas.width = Math.floor(width * dpr);
        canvas.height = Math.floor(height * dpr);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const lerp = (a, b, t) => a + (b - a) * t;

    const mixColor = (a, b, t, alpha) => {
        const r = Math.round(lerp(a[0], b[0], t));
        const g = Math.round(lerp(a[1], b[1], t));
        const bVal = Math.round(lerp(a[2], b[2], t));
        return `rgba(${r}, ${g}, ${bVal}, ${alpha})`;
    };

    const strokeRibbonFamily = (time, palette, spec) => {
        const lanes = spec.lanes;
        const laneCenter = (lanes - 1) * 0.5;
        for (let i = 0; i < lanes; i += 1) {
            const laneT = lanes > 1 ? i / (lanes - 1) : 0.5;
            const laneOffset = (i - laneCenter) * spec.spacing;
            const phase = time * spec.speed + spec.phase + i * spec.lanePhase;

            const x0 = -width * 0.12;
            const x3 = width * 1.12;
            const c1x = width * (0.28 + Math.sin(phase * 0.27) * 0.06 + spec.curveBias);
            const c2x = width * (0.72 + Math.cos(phase * 0.23) * 0.07 - spec.curveBias);

            const y0 = height * spec.y0 + Math.sin(phase * 0.74 + 0.2) * height * spec.swingA + laneOffset;
            const y1 = height * spec.y1 + Math.cos(phase * 0.59 + 1.2) * height * spec.swingB + laneOffset * 0.5;
            const y2 = height * spec.y2 + Math.sin(phase * 0.67 + 2.1) * height * spec.swingC - laneOffset * 0.38;
            const y3 = height * spec.y3 + Math.cos(phase * 0.81 + 0.8) * height * spec.swingD - laneOffset * 0.92;

            ctx.save();
            ctx.globalCompositeOperation = "lighter";
            ctx.strokeStyle = mixColor(palette.a, palette.b, laneT, spec.glowAlpha);
            ctx.lineWidth = spec.glowWidth;
            ctx.lineCap = "round";
            ctx.beginPath();
            ctx.moveTo(x0, y0);
            ctx.bezierCurveTo(c1x, y1, c2x, y2, x3, y3);
            ctx.stroke();
            ctx.restore();

            ctx.save();
            ctx.globalCompositeOperation = "lighter";
            ctx.filter = "none";
            ctx.strokeStyle = mixColor(palette.a, palette.b, laneT, spec.coreAlpha);
            ctx.lineWidth = spec.coreWidth;
            ctx.lineCap = "round";
            ctx.beginPath();
            ctx.moveTo(x0, y0);
            ctx.bezierCurveTo(c1x, y1, c2x, y2, x3, y3);
            ctx.stroke();
            ctx.restore();
        }
    };

    const drawDarkNeonRibbon = (time) => {
        const bg = ctx.createLinearGradient(0, 0, 0, height);
        bg.addColorStop(0, getThemeValue("--ribbon-dark-bg-start", "#020304"));
        bg.addColorStop(0.55, getThemeValue("--ribbon-dark-bg-mid", "#05070B"));
        bg.addColorStop(1, getThemeValue("--ribbon-dark-bg-end", "#090D15"));
        ctx.fillStyle = bg;
        ctx.fillRect(0, 0, width, height);

        const wash = ctx.createRadialGradient(
            width * 0.62,
            height * 0.72,
            0,
            width * 0.62,
            height * 0.72,
            Math.max(width, height) * 0.78
        );
        wash.addColorStop(0, getThemeValue("--ribbon-dark-wash-start", "rgba(69, 98, 255, 0.22)"));
        wash.addColorStop(0.5, getThemeValue("--ribbon-dark-wash-mid", "rgba(41, 58, 140, 0.12)"));
        wash.addColorStop(1, getThemeValue("--ribbon-dark-wash-end", "rgba(0, 0, 0, 0)"));
        ctx.fillStyle = wash;
        ctx.fillRect(0, 0, width, height);

        const primaryPalette = {
            a: getRgbTriplet("--ribbon-dark-primary-a-rgb", [110, 138, 255]),
            b: getRgbTriplet("--ribbon-dark-primary-b-rgb", [69, 98, 255]),
        };
        const secondaryPalette = {
            a: getRgbTriplet("--ribbon-dark-secondary-a-rgb", [98, 78, 226]),
            b: getRgbTriplet("--ribbon-dark-secondary-b-rgb", [62, 84, 190]),
        };

        strokeRibbonFamily(time, primaryPalette, {
            lanes: 3,
            spacing: 12,
            speed: 0.86,
            phase: 0.4,
            lanePhase: 0.22,
            y0: 0.39,
            y1: 0.54,
            y2: 0.46,
            y3: 0.74,
            swingA: 0.028,
            swingB: 0.072,
            swingC: 0.058,
            swingD: 0.05,
            curveBias: 0.0,
            glowBlur: 15,
            glowWidth: Math.max(28, width * 0.016),
            glowAlpha: 0.16,
            coreWidth: Math.max(2.2, width * 0.0018),
            coreAlpha: 0.84,
        });

        strokeRibbonFamily(time, secondaryPalette, {
            lanes: 2,
            spacing: 16,
            speed: 0.72,
            phase: 2.0,
            lanePhase: 0.27,
            y0: 0.84,
            y1: 0.52,
            y2: 0.38,
            y3: 0.05,
            swingA: 0.04,
            swingB: 0.08,
            swingC: 0.06,
            swingD: 0.045,
            curveBias: 0.05,
            glowBlur: 18,
            glowWidth: Math.max(24, width * 0.014),
            glowAlpha: 0.1,
            coreWidth: Math.max(1.6, width * 0.0014),
            coreAlpha: 0.48,
        });
    };

    const drawLightNeonRibbon = (time) => {
        const bg = ctx.createLinearGradient(0, 0, 0, height);
        bg.addColorStop(0, getThemeValue("--ribbon-light-bg-start", "#FFFFFF"));
        bg.addColorStop(0.62, getThemeValue("--ribbon-light-bg-mid", "#FCFCFF"));
        bg.addColorStop(1, getThemeValue("--ribbon-light-bg-end", "#F4F0FF"));
        ctx.fillStyle = bg;
        ctx.fillRect(0, 0, width, height);

        const wash = ctx.createRadialGradient(
            width * 0.22,
            height * 0.12,
            0,
            width * 0.22,
            height * 0.12,
            Math.max(width, height) * 0.92
        );
        wash.addColorStop(0, getThemeValue("--ribbon-light-wash-1-start", "rgba(167, 139, 250, 0.16)"));
        wash.addColorStop(0.5, getThemeValue("--ribbon-light-wash-1-mid", "rgba(129, 140, 248, 0.08)"));
        wash.addColorStop(1, getThemeValue("--ribbon-light-wash-1-end", "rgba(255, 255, 255, 0)"));
        ctx.fillStyle = wash;
        ctx.fillRect(0, 0, width, height);

        const wash2 = ctx.createRadialGradient(
            width * 0.82,
            height * 0.84,
            0,
            width * 0.82,
            height * 0.84,
            Math.max(width, height) * 0.72
        );
        wash2.addColorStop(0, getThemeValue("--ribbon-light-wash-2-start", "rgba(196, 181, 253, 0.17)"));
        wash2.addColorStop(0.56, getThemeValue("--ribbon-light-wash-2-mid", "rgba(167, 139, 250, 0.08)"));
        wash2.addColorStop(1, getThemeValue("--ribbon-light-wash-2-end", "rgba(255, 255, 255, 0)"));
        ctx.fillStyle = wash2;
        ctx.fillRect(0, 0, width, height);

        const primaryPalette = {
            a: getRgbTriplet("--ribbon-light-primary-a-rgb", [167, 139, 250]),
            b: getRgbTriplet("--ribbon-light-primary-b-rgb", [129, 140, 248]),
        };
        const secondaryPalette = {
            a: getRgbTriplet("--ribbon-light-secondary-a-rgb", [196, 181, 253]),
            b: getRgbTriplet("--ribbon-light-secondary-b-rgb", [167, 139, 250]),
        };

        strokeRibbonFamily(time, primaryPalette, {
            lanes: 2,
            spacing: 9,
            speed: 0.8,
            phase: 0.35,
            lanePhase: 0.2,
            y0: 0.37,
            y1: 0.53,
            y2: 0.46,
            y3: 0.67,
            swingA: 0.015,
            swingB: 0.046,
            swingC: 0.04,
            swingD: 0.03,
            curveBias: -0.01,
            glowBlur: 8,
            glowWidth: Math.max(12, width * 0.008),
            glowAlpha: 0.14,
            coreWidth: Math.max(1.3, width * 0.00105),
            coreAlpha: 0.68,
        });

        strokeRibbonFamily(time, secondaryPalette, {
            lanes: 2,
            spacing: 12,
            speed: 0.66,
            phase: 2.2,
            lanePhase: 0.24,
            y0: 0.82,
            y1: 0.55,
            y2: 0.39,
            y3: 0.13,
            swingA: 0.022,
            swingB: 0.05,
            swingC: 0.04,
            swingD: 0.03,
            curveBias: 0.06,
            glowBlur: 9,
            glowWidth: Math.max(9, width * 0.0065),
            glowAlpha: 0.08,
            coreWidth: Math.max(1.1, width * 0.0009),
            coreAlpha: 0.38,
        });
    };

    const drawFrame = (timeMs) => {
        if (timeMs - lastFrameTimeMs < FRAME_INTERVAL_MS) {
            if (!prefersReducedMotion.matches) {
                rafId = requestAnimationFrame(drawFrame);
            }
            return;
        }

        lastFrameTimeMs = timeMs;
        const time = timeMs * 0.001;
        const isDark = getTheme() === "dark";
        ctx.clearRect(0, 0, width, height);
        if (isDark) {
            drawDarkNeonRibbon(time);
        } else {
            drawLightNeonRibbon(time);
        }
        if (!didFirstFrame) {
            didFirstFrame = true;
            document.body.classList.add("neon-ribbon-ready");
        }
        if (!prefersReducedMotion.matches) {
            rafId = requestAnimationFrame(drawFrame);
        }
    };

    const render = () => {
        cancelAnimationFrame(rafId);
        lastFrameTimeMs = 0;
        drawFrame(performance.now());
    };

    const onMotionPreferenceChange = () => {
        render();
    };

    resize();
    render();

    window.addEventListener("resize", resize, { passive: true });
    window.addEventListener("resize", render, { passive: true });
    window.addEventListener("fxj:themechange", render);
    if (typeof prefersReducedMotion.addEventListener === "function") {
        prefersReducedMotion.addEventListener("change", onMotionPreferenceChange);
    } else if (typeof prefersReducedMotion.addListener === "function") {
        prefersReducedMotion.addListener(onMotionPreferenceChange);
    }
})();
