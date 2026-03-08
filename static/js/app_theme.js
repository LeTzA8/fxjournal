(function () {
    const root = document.documentElement;
    const fixedTheme = "dark";

    const syncChartPalette = () => {
        const strokeStops = document.querySelectorAll("#pnlStrokeGradient stop");
        if (strokeStops.length >= 2) {
            strokeStops[0].setAttribute("stop-color", "#4F46E5");
            strokeStops[1].setAttribute("stop-color", "#4338CA");
        }

        const areaStops = document.querySelectorAll("#pnlAreaGradient stop");
        if (areaStops.length >= 2) {
            areaStops[0].setAttribute("stop-color", "rgba(79, 70, 229, 0.32)");
            areaStops[1].setAttribute("stop-color", "rgba(79, 70, 229, 0.04)");
        }
    };

    root.setAttribute("data-theme", fixedTheme);
    syncChartPalette();
})();
