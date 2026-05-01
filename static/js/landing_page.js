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

        const parseWordSelection = (value) => {
            if (!value) {
                return new Set();
            }

            const selected = new Set();
            value.split(",").forEach((part) => {
                const token = part.trim();
                if (!token) {
                    return;
                }

                const rangeMatch = token.match(/^(\d+)\s*-\s*(\d+)$/);
                if (rangeMatch) {
                    let start = Number.parseInt(rangeMatch[1], 10);
                    let end = Number.parseInt(rangeMatch[2], 10);
                    if (!Number.isInteger(start) || !Number.isInteger(end)) {
                        return;
                    }
                    if (end < start) {
                        [start, end] = [end, start];
                    }
                    for (let index = start; index <= end; index += 1) {
                        if (index > 0) {
                            selected.add(index);
                        }
                    }
                    return;
                }

                const number = Number.parseInt(token, 10);
                if (Number.isInteger(number) && number > 0) {
                    selected.add(number);
                }
            });

            return selected;
        };

        const text = title.textContent.trim().replace(/\s+/g, " ");
        if (!text) {
            return;
        }

        const accentWords = parseWordSelection(title.dataset.heroAccentWords);
        const underlineWords = parseWordSelection(title.dataset.heroUnderlineWords);

        title.dataset.wordsReady = "1";
        title.setAttribute("aria-label", text);
        title.textContent = "";

        const fragment = document.createDocumentFragment();
        text.split(" ").forEach((word, index, arr) => {
            const span = document.createElement("span");
            span.className = "hero-word";
            if (accentWords.has(index + 1)) {
                span.classList.add("is-accent");
            }
            if (underlineWords.has(index + 1)) {
                span.classList.add("is-underlined");
            }
            span.textContent = word;
            span.setAttribute("aria-hidden", "true");
            span.style.setProperty("--word-index", String(index));
            span.style.setProperty("--word-base-delay", "620ms");
            span.style.setProperty("--word-delay", `${620 + index * 58}ms`);
            fragment.appendChild(span);
            if (index < arr.length - 1) {
                fragment.appendChild(document.createTextNode(" "));
            }
        });
        title.appendChild(fragment);
    };

    const initHeroSubheadlineRotator = () => {
        const root = document.querySelector("[data-hero-subheadline-rotator]");
        const textEl = root?.querySelector("[data-hero-subheadline-text]");
        if (!root || !textEl || root.dataset.rotatorReady === "1") {
            return;
        }

        let lines = [];
        try {
            const parsed = JSON.parse(root.dataset.heroSubheadlines || "[]");
            if (Array.isArray(parsed)) {
                lines = parsed
                    .map((line) => String(line || "").trim())
                    .filter(Boolean);
            }
        } catch (error) {
            lines = [];
        }

        if (lines.length < 2) {
            return;
        }

        root.dataset.rotatorReady = "1";

        let currentIndex = 0;
        let timerId = null;
        let isPaused = false;
        let swapTimeoutId = null;

        const setStableHeight = () => {
            const rootWidth = root.getBoundingClientRect().width;
            if (!rootWidth) {
                return;
            }

            const computed = window.getComputedStyle(root);
            const px = (value) => Number.parseFloat(value) || 0;
            const verticalExtras =
                px(computed.paddingTop) +
                px(computed.paddingBottom) +
                px(computed.borderTopWidth) +
                px(computed.borderBottomWidth);
            const horizontalExtras =
                px(computed.paddingLeft) +
                px(computed.paddingRight) +
                px(computed.borderLeftWidth) +
                px(computed.borderRightWidth);

            const sizer = textEl.cloneNode(false);
            sizer.removeAttribute("data-hero-subheadline-text");
            sizer.style.position = "absolute";
            sizer.style.visibility = "hidden";
            sizer.style.pointerEvents = "none";
            sizer.style.inset = "auto";
            sizer.style.width = `${Math.max(0, rootWidth - horizontalExtras)}px`;
            root.appendChild(sizer);

            const maxTextHeight = lines.reduce((height, line) => {
                sizer.textContent = line;
                return Math.max(height, sizer.getBoundingClientRect().height);
            }, 0);

            sizer.remove();
            root.style.minHeight = `${Math.ceil(maxTextHeight + verticalExtras)}px`;
        };

        const swapLine = () => {
            if (isPaused || prefersReducedMotion.matches || swapTimeoutId) {
                return;
            }

            root.classList.add("is-swapping");
            swapTimeoutId = window.setTimeout(() => {
                swapTimeoutId = null;
                if (prefersReducedMotion.matches) {
                    root.classList.remove("is-swapping");
                    return;
                }
                currentIndex = (currentIndex + 1) % lines.length;
                textEl.textContent = lines[currentIndex];
                root.classList.remove("is-swapping");
            }, 240);
        };

        const stop = () => {
            if (!timerId) {
                return;
            }
            window.clearInterval(timerId);
            timerId = null;
            if (swapTimeoutId) {
                window.clearTimeout(swapTimeoutId);
                swapTimeoutId = null;
                root.classList.remove("is-swapping");
            }
        };

        const start = () => {
            if (timerId || prefersReducedMotion.matches) {
                return;
            }
            timerId = window.setInterval(swapLine, 4400);
        };

        const pause = () => {
            isPaused = true;
        };

        const resume = () => {
            isPaused = false;
        };

        root.addEventListener("pointerenter", pause);
        root.addEventListener("pointerleave", resume);
        root.addEventListener("focusin", pause);
        root.addEventListener("focusout", resume);
        window.addEventListener("resize", setStableHeight);

        setStableHeight();
        start();

        const onMotionChange = () => {
            root.classList.remove("is-swapping");
            if (prefersReducedMotion.matches) {
                stop();
                textEl.textContent = lines[0];
                currentIndex = 0;
                return;
            }
            start();
        };

        if (typeof prefersReducedMotion.addEventListener === "function") {
            prefersReducedMotion.addEventListener("change", onMotionChange);
        } else if (typeof prefersReducedMotion.addListener === "function") {
            prefersReducedMotion.addListener(onMotionChange);
        }
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

    const initFeatureShotLightbox = () => {
        const triggers = Array.from(document.querySelectorAll("[data-feature-shot-trigger]"));
        const lightbox = document.querySelector("[data-feature-shot-lightbox]");
        const lightboxImage = lightbox?.querySelector("[data-feature-shot-image]");
        const lightboxCaption = lightbox?.querySelector("[data-feature-shot-caption]");
        const closeButton = lightbox?.querySelector("[data-feature-shot-close-button]");
        const closeControls = lightbox
            ? Array.from(lightbox.querySelectorAll("[data-feature-shot-close]"))
            : [];

        if (!triggers.length || !lightbox || !lightboxImage || !lightboxCaption || !closeButton) {
            return;
        }

        let lastTrigger = null;

        const closeLightbox = () => {
            if (!lightbox.classList.contains("is-open")) {
                return;
            }

            lightbox.classList.remove("is-open");
            lightbox.setAttribute("aria-hidden", "true");
            lightboxImage.removeAttribute("src");
            lightboxImage.alt = "";
            lightboxCaption.textContent = "";

            if (lastTrigger && typeof lastTrigger.focus === "function") {
                lastTrigger.focus();
            }
        };

        const openLightbox = (trigger) => {
            const sourceImage = trigger.querySelector("img");
            const sourceUrl = sourceImage?.currentSrc || sourceImage?.src;
            if (!sourceImage || !sourceUrl) {
                return;
            }

            lastTrigger = trigger;
            lightboxImage.src = sourceUrl;
            lightboxImage.alt = sourceImage.alt || "";
            lightboxCaption.textContent = sourceImage.alt || "Expanded feature screenshot";
            lightbox.classList.add("is-open");
            lightbox.setAttribute("aria-hidden", "false");
        };

        triggers.forEach((trigger) => {
            trigger.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                openLightbox(trigger);
            });
        });

        closeControls.forEach((control) => {
            control.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                closeLightbox();
            });
        });

        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape" || !lightbox.classList.contains("is-open")) {
                return;
            }

            event.preventDefault();
            closeLightbox();
        });
    };

    const initNavDropdowns = () => {
        const dropdowns = Array.from(document.querySelectorAll("[data-nav-dropdown]"));
        if (!dropdowns.length) {
            return;
        }

        const closeDropdown = (dropdown) => {
            const toggle = dropdown.querySelector("[data-nav-dropdown-toggle]");
            const menu = dropdown.querySelector("[data-nav-dropdown-menu]");
            if (!toggle || !menu) {
                return;
            }

            dropdown.classList.remove("is-open");
            toggle.setAttribute("aria-expanded", "false");
            menu.hidden = true;
        };

        const openDropdown = (dropdown) => {
            const toggle = dropdown.querySelector("[data-nav-dropdown-toggle]");
            const menu = dropdown.querySelector("[data-nav-dropdown-menu]");
            if (!toggle || !menu) {
                return;
            }

            dropdowns.forEach((item) => {
                if (item !== dropdown) {
                    closeDropdown(item);
                }
            });

            dropdown.classList.add("is-open");
            toggle.setAttribute("aria-expanded", "true");
            menu.hidden = false;
        };

        dropdowns.forEach((dropdown) => {
            const toggle = dropdown.querySelector("[data-nav-dropdown-toggle]");
            const menu = dropdown.querySelector("[data-nav-dropdown-menu]");
            if (!toggle || !menu) {
                return;
            }

            toggle.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();

                if (dropdown.classList.contains("is-open")) {
                    closeDropdown(dropdown);
                    return;
                }

                openDropdown(dropdown);
            });

            menu.addEventListener("click", () => {
                closeDropdown(dropdown);
            });
        });

        document.addEventListener("click", (event) => {
            dropdowns.forEach((dropdown) => {
                if (dropdown.contains(event.target)) {
                    return;
                }
                closeDropdown(dropdown);
            });
        });

        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") {
                return;
            }

            dropdowns.forEach((dropdown) => {
                const wasOpen = dropdown.classList.contains("is-open");
                closeDropdown(dropdown);

                if (!wasOpen) {
                    return;
                }

                const toggle = dropdown.querySelector("[data-nav-dropdown-toggle]");
                if (toggle && typeof toggle.focus === "function") {
                    toggle.focus();
                }
            });
        });
    };

    splitHeroTitleWords();
    initHeroSubheadlineRotator();
    buildReplayChart();
    initFeatureShotLightbox();
    initNavDropdowns();
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
