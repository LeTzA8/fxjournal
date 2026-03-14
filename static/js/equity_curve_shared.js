(function () {
    const smoothPath = (points, tension = 0.22) => {
        if (!points.length) return "";
        if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
        let d = `M ${points[0].x} ${points[0].y}`;
        for (let i = 0; i < points.length - 1; i += 1) {
            const p0 = points[i - 1] || points[i];
            const p1 = points[i];
            const p2 = points[i + 1];
            const p3 = points[i + 2] || p2;
            const cp1x = p1.x + (p2.x - p0.x) * tension;
            const cp1y = p1.y + (p2.y - p0.y) * tension;
            const cp2x = p2.x - (p3.x - p1.x) * tension;
            const cp2y = p2.y - (p3.y - p1.y) * tension;
            d += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${p2.x} ${p2.y}`;
        }
        return d;
    };

    const pointTone = (equity) => {
        if (equity > 0) return "positive";
        if (equity < 0) return "negative";
        return "flat";
    };

    const formatAxisPnl = (value) => {
        const absValue = Math.abs(value);
        const sign = value > 0 ? "+" : value < 0 ? "-" : "";
        if (absValue >= 1000000) {
            return `${sign}$${(absValue / 1000000).toFixed(absValue >= 10000000 ? 0 : 1)}m`;
        }
        if (absValue >= 1000) {
            return `${sign}$${(absValue / 1000).toFixed(absValue >= 10000 ? 0 : 1)}k`;
        }
        if (absValue >= 100) {
            return `${sign}$${Math.round(absValue)}`;
        }
        const decimals = absValue >= 10 ? 1 : 2;
        return `${sign}$${absValue.toFixed(decimals)}`.replace(/\.00$/, "");
    };

    const restartCurveAnimation = ({ svg, paths, areas }) => {
        paths.forEach((element) => {
            const totalLength = Math.max(Math.ceil(element.getTotalLength()), 1);
            element.style.animation = "none";
            element.style.strokeDasharray = String(totalLength);
            element.style.strokeDashoffset = String(totalLength);
        });

        areas.forEach((element) => {
            element.style.animation = "none";
            element.style.opacity = "0";
        });

        void svg.getBoundingClientRect();

        paths.forEach((element) => {
            element.style.animation = "draw-line 4.4s var(--curve-draw-ease) forwards";
        });

        areas.forEach((element) => {
            element.style.animation = "area-fade 3.6s ease forwards";
            element.style.opacity = "";
        });
    };

    const downsamplePoints = (points, maxPoints, options = {}) => {
        if (!Array.isArray(points)) {
            return [];
        }

        const threshold = Math.max(3, Number.parseInt(maxPoints, 10) || 0);
        if (points.length <= threshold) {
            return points.slice();
        }

        const getX = typeof options.getX === "function" ? options.getX : ((point, index) => index);
        const getY = typeof options.getY === "function" ? options.getY : ((point) => Number(point && point.equity) || 0);
        const sampled = [points[0]];
        const every = (points.length - 2) / (threshold - 2);
        let anchorIndex = 0;

        for (let bucket = 0; bucket < threshold - 2; bucket += 1) {
            const avgStart = Math.floor((bucket + 1) * every) + 1;
            const avgEnd = Math.min(Math.floor((bucket + 2) * every) + 1, points.length);
            let avgX = 0;
            let avgY = 0;
            let avgLength = 0;

            for (let i = avgStart; i < avgEnd; i += 1) {
                avgX += Number(getX(points[i], i));
                avgY += Number(getY(points[i], i));
                avgLength += 1;
            }

            if (!avgLength) {
                const fallbackIndex = points.length - 1;
                avgX = Number(getX(points[fallbackIndex], fallbackIndex));
                avgY = Number(getY(points[fallbackIndex], fallbackIndex));
                avgLength = 1;
            }

            avgX /= avgLength;
            avgY /= avgLength;

            const rangeStart = Math.floor(bucket * every) + 1;
            const rangeEnd = Math.min(Math.floor((bucket + 1) * every) + 1, points.length - 1);
            const anchorX = Number(getX(points[anchorIndex], anchorIndex));
            const anchorY = Number(getY(points[anchorIndex], anchorIndex));
            let maxArea = -1;
            let nextIndex = rangeStart;

            for (let i = rangeStart; i < rangeEnd; i += 1) {
                const pointX = Number(getX(points[i], i));
                const pointY = Number(getY(points[i], i));
                const area = Math.abs(
                    (anchorX - avgX) * (pointY - anchorY) -
                    (anchorX - pointX) * (avgY - anchorY)
                );
                if (area > maxArea) {
                    maxArea = area;
                    nextIndex = i;
                }
            }

            sampled.push(points[nextIndex]);
            anchorIndex = nextIndex;
        }

        sampled.push(points[points.length - 1]);
        return sampled;
    };

    window.FXJEquityCurveShared = {
        downsamplePoints,
        formatAxisPnl,
        pointTone,
        restartCurveAnimation,
        smoothPath,
    };
})();
