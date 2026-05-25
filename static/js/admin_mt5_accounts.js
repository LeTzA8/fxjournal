(function () {
    function syncRowTargetVm(row) {
        var select = row.querySelector("[data-mt5-row-vm-select]");
        if (!select) {
            return;
        }
        var value = select.value || "";
        row.querySelectorAll("input.mt5-row-target-vm-input").forEach(function (input) {
            input.value = value;
        });
    }

    function ensureRowTargetVmSelected(row) {
        var select = row.querySelector("[data-mt5-row-vm-select]");
        if (!select) {
            return;
        }

        var canonicalVmId = row.getAttribute("data-mt5-account-vm-id") || "";
        if (canonicalVmId) {
            var matched = false;
            Array.prototype.forEach.call(select.options, function (option) {
                if (option.value === canonicalVmId) {
                    select.value = canonicalVmId;
                    matched = true;
                }
            });
            if (!matched && select.options.length > 1 && !select.value) {
                select.selectedIndex = 1;
            }
        }

        syncRowTargetVm(row);
    }

    document.querySelectorAll("[data-mt5-account-row]").forEach(function (row) {
        var select = row.querySelector("[data-mt5-row-vm-select]");
        if (select) {
            select.addEventListener("change", function () {
                syncRowTargetVm(row);
            });
        }
        ensureRowTargetVmSelected(row);

        row.querySelectorAll("form").forEach(function (form) {
            form.addEventListener("submit", function () {
                syncRowTargetVm(row);
            });
        });
    });
})();
