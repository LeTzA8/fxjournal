(function () {
    const storageKey = "fxj-theme";
    const root = document.documentElement;
    const validThemes = new Set(["dark", "light"]);

    const syncChartPalette = (theme) => {
        const isDark = theme === "dark";
        const strokeA = "#4F46E5";
        const strokeB = "#4338CA";
        const areaA = isDark ? "rgba(79, 70, 229, 0.32)" : "rgba(79, 70, 229, 0.24)";
        const areaB = isDark ? "rgba(79, 70, 229, 0.04)" : "rgba(79, 70, 229, 0.03)";

        const strokeStops = document.querySelectorAll("#pnlStrokeGradient stop");
        if (strokeStops.length >= 2) {
            strokeStops[0].setAttribute("stop-color", strokeA);
            strokeStops[1].setAttribute("stop-color", strokeB);
        }

        const areaStops = document.querySelectorAll("#pnlAreaGradient stop");
        if (areaStops.length >= 2) {
            areaStops[0].setAttribute("stop-color", areaA);
            areaStops[1].setAttribute("stop-color", areaB);
        }
    };

    const getTheme = () => {
        const current = root.getAttribute("data-theme");
        return validThemes.has(current) ? current : "dark";
    };

    const setTheme = (theme) => {
        const nextTheme = validThemes.has(theme) ? theme : "dark";
        root.setAttribute("data-theme", nextTheme);
        try {
            localStorage.setItem(storageKey, nextTheme);
        } catch (_error) {}
        syncChartPalette(nextTheme);
        window.dispatchEvent(
            new CustomEvent("fxj:themechange", { detail: { theme: nextTheme } })
        );
    };

    const toggleTheme = () => {
        setTheme(getTheme() === "dark" ? "light" : "dark");
    };

    const getThemeLabel = (theme) => (theme === "dark" ? "Light Mode" : "Dark Mode");

    const syncToggleButton = (button) => {
        if (!button) {
            return;
        }
        const theme = getTheme();
        const isDark = theme === "dark";
        button.textContent = getThemeLabel(theme);
        button.setAttribute("aria-pressed", String(isDark));
    };

    const bindToggle = (button) => {
        if (!button || button.dataset.themeBound === "1") {
            return;
        }
        button.dataset.themeBound = "1";
        syncToggleButton(button);
        button.addEventListener("click", () => {
            toggleTheme();
            syncToggleButton(button);
        });
    };

    window.fxjTheme = {
        getTheme,
        setTheme,
        toggleTheme,
        bindToggle,
    };

    document.querySelectorAll("[data-theme-toggle]").forEach(bindToggle);
    syncChartPalette(getTheme());
    window.addEventListener("fxj:themechange", () => {
        document.querySelectorAll("[data-theme-toggle]").forEach(syncToggleButton);
    });
})();
