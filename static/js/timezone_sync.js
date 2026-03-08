(function () {
    const body = document.body;
    if (!body) {
        return;
    }

    const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const currentTimezone = body.dataset.displayTimezone || "";
    const syncUrl = body.dataset.timezoneSyncUrl || "";
    const csrfToken = body.dataset.csrfToken || "";
    const syncKey = "fxj-timezone-sync";

    if (!browserTimezone || !currentTimezone || !syncUrl || !csrfToken) {
        return;
    }

    if (browserTimezone === currentTimezone) {
        try {
            sessionStorage.removeItem(syncKey);
        } catch (_error) {}
        return;
    }

    const syncAttempt = `${currentTimezone}->${browserTimezone}`;
    try {
        if (sessionStorage.getItem(syncKey) === syncAttempt) {
            return;
        }
    } catch (_error) {}

    fetch(syncUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ timezone: browserTimezone }),
    }).then((response) => {
        if (!response.ok) {
            return;
        }
        try {
            sessionStorage.setItem(syncKey, syncAttempt);
        } catch (_error) {}
        window.location.reload();
    }).catch(() => {});
})();
