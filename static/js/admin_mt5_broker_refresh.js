(function () {
    var form = document.getElementById("mt5BrokerRefreshForm");
    if (!form) {
        return;
    }

    var vmSelect = document.getElementById("mt5BrokerRefreshVm");
    var accountSelect = document.getElementById("mt5BrokerRefreshAccount");
    var preview = document.getElementById("mt5BrokerRefreshPreview");
    var resultPanel = document.getElementById("mt5BrokerRefreshResult");
    var resultBody = document.getElementById("mt5BrokerRefreshResultBody");
    var submitButton = document.getElementById("mt5BrokerRefreshSubmit");
    var pollTimer = null;

    function slugifyVmId(vmId) {
        return String(vmId || "")
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/-+/g, "-")
            .replace(/^-|-$/g, "");
    }

    function updatePreview() {
        var vmId = vmSelect ? vmSelect.value : "";
        var accountOption = accountSelect && accountSelect.selectedOptions[0];
        var terminalPath = accountOption ? accountOption.getAttribute("data-terminal-path") : "";
        var accountVmId = accountOption ? accountOption.getAttribute("data-vm-id") : "";
        if (!vmId) {
            preview.textContent = "Queue preview: choose a VM to see scoped setup queue routing.";
            return;
        }
        var slug = slugifyVmId(vmId) || "unknown";
        var queueName = slug === "unknown" ? "mt5_setup" : "mt5_setup." + slug;
        var lines = [
            "Target VM: " + vmId,
            "Queue: " + queueName,
        ];
        if (terminalPath) {
            lines.push("Terminal path: " + terminalPath);
        }
        if (accountVmId && accountVmId !== vmId) {
            lines.push("Note: selected account vm_id (" + accountVmId + ") differs from target VM.");
        }
        preview.textContent = lines.join(" · ");
    }

    function renderResult(payload) {
        resultPanel.hidden = false;
        resultBody.textContent = JSON.stringify(payload, null, 2);
    }

    function pollJob(pollUrl) {
        if (!pollUrl) {
            return;
        }
        fetch(pollUrl, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        })
            .then(function (response) {
                return response.json().then(function (body) {
                    return { status: response.status, body: body };
                });
            })
            .then(function (payload) {
                if (!payload.body || !payload.body.ok) {
                    renderResult(payload.body || { error: "poll failed" });
                    submitButton.disabled = false;
                    return;
                }
                if (!payload.body.ready) {
                    renderResult({ status: "pending", job_id: payload.body.job_id });
                    pollTimer = window.setTimeout(function () {
                        pollJob(pollUrl);
                    }, 2000);
                    return;
                }
                renderResult(payload.body.result || payload.body);
                submitButton.disabled = false;
            })
            .catch(function (error) {
                renderResult({ error: String(error) });
                submitButton.disabled = false;
            });
    }

    if (vmSelect) {
        vmSelect.addEventListener("change", updatePreview);
    }
    if (accountSelect) {
        accountSelect.addEventListener("change", function () {
            var option = accountSelect.selectedOptions[0];
            var accountVmId = option ? option.getAttribute("data-vm-id") : "";
            if (accountVmId && vmSelect && !vmSelect.value) {
                vmSelect.value = accountVmId;
            }
            updatePreview();
        });
    }
    updatePreview();

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (pollTimer) {
            window.clearTimeout(pollTimer);
            pollTimer = null;
        }
        submitButton.disabled = true;
        resultPanel.hidden = false;
        resultBody.textContent = "Dispatching job…";

        var formData = new FormData(form);
        fetch(form.action, {
            method: "POST",
            body: formData,
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        })
            .then(function (response) {
                return response.json().then(function (body) {
                    return { status: response.status, body: body };
                });
            })
            .then(function (payload) {
                if (!payload.body || !payload.body.ok) {
                    renderResult(payload.body || { error: "dispatch failed", status: payload.status });
                    submitButton.disabled = false;
                    return;
                }
                renderResult(payload.body);
                pollJob(payload.body.poll_url);
            })
            .catch(function (error) {
                renderResult({ error: String(error) });
                submitButton.disabled = false;
            });
    });
})();
