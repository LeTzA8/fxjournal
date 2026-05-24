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

    document.querySelectorAll("[data-mt5-account-row]").forEach(function (row) {
        var select = row.querySelector("[data-mt5-row-vm-select]");
        if (!select) {
            return;
        }
        select.addEventListener("change", function () {
            syncRowTargetVm(row);
        });
        syncRowTargetVm(row);
    });
})();
