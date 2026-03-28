(function () {
    const root = document.documentElement;
    const fixedTheme = "dark";
    const getThemeValue = (name, fallback) => {
        const value = getComputedStyle(root).getPropertyValue(name).trim();
        return value || fallback;
    };

    const syncChartPalette = () => {
        const strokeStops = document.querySelectorAll("#pnlStrokeGradient stop");
        if (strokeStops.length >= 2) {
            strokeStops[0].setAttribute("stop-color", getThemeValue("--chart-stroke-start", "#4562FF"));
            strokeStops[1].setAttribute("stop-color", getThemeValue("--chart-stroke-end", "#354FF0"));
        }

        const areaStops = document.querySelectorAll("#pnlAreaGradient stop");
        if (areaStops.length >= 2) {
            areaStops[0].setAttribute("stop-color", getThemeValue("--chart-area-start", "rgba(69, 98, 255, 0.24)"));
            areaStops[1].setAttribute("stop-color", getThemeValue("--chart-area-end", "rgba(69, 98, 255, 0.03)"));
        }
    };

    root.setAttribute("data-theme", fixedTheme);
    syncChartPalette();
})();
