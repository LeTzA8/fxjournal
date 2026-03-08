(function () {
    const importForm = document.querySelector(".import-form");
    const dropzone = document.getElementById("mt5Dropzone");
    const fileInput = document.getElementById("mt5_file");
    const fileName = document.getElementById("mt5FileName");
    const importProfile = document.getElementById("importProfile");
    const platformValue = document.getElementById("importPlatformValue");
    const marketValue = document.getElementById("importMarketValue");
    const validationValue = document.getElementById("importValidationValue");
    const submitButton = document.querySelector(".import-form .submit-btn");
    if (!importForm || !dropzone || !fileInput || !fileName || !importProfile || !platformValue || !marketValue || !validationValue || !submitButton) {
        return;
    }

    const activeMarketType = importForm.dataset.activeMarketType || "";
    const expectedUploadLabel = importForm.dataset.expectedUploadLabel || "";

    const setFileName = (file) => {
        fileName.textContent = file ? `Selected: ${file.name}` : "";
    };

    const setInvalid = (message) => {
        fileInput.setCustomValidity(message || "");
        fileInput.dispatchEvent(new Event("input", { bubbles: true }));
    };

    const tradovateHeaders = [
        "symbol",
        "buyFillId",
        "sellFillId",
        "qty",
        "buyPrice",
        "sellPrice",
        "pnl",
        "boughtTimestamp",
        "soldTimestamp",
    ];
    const parseCsvHeader = (text) => {
        const lines = String(text || "").split(/\r?\n/).filter((line) => line.trim());
        if (!lines.length) {
            return [];
        }
        return lines[0].split(",").map((part) => part.trim());
    };
    const looksLikeZipWorkbook = async (file) => {
        try {
            const bytes = new Uint8Array(await file.slice(0, 4).arrayBuffer());
            return bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B;
        } catch {
            return false;
        }
    };
    const detectProfileFromFile = async (file) => {
        if (!file) {
            return null;
        }

        try {
            const textSample = await file.slice(0, 8192).text();
            const csvHeader = parseCsvHeader(textSample);
            if (csvHeader.length && tradovateHeaders.every((header) => csvHeader.includes(header))) {
                return { platform: "Tradovate", market: "Futures" };
            }
        } catch {
            // Fall through to workbook detection.
        }

        if (await looksLikeZipWorkbook(file)) {
            return { platform: "MetaTrader 5", market: "CFD" };
        }

        return null;
    };
    const setImportProfile = (file, detectedProfile, isValid) => {
        if (!file || !detectedProfile) {
            importProfile.hidden = true;
            importProfile.classList.remove("valid", "invalid");
            platformValue.textContent = "-";
            marketValue.textContent = "-";
            validationValue.textContent = "Waiting for file";
            submitButton.disabled = true;
            return;
        }

        importProfile.hidden = false;
        importProfile.classList.toggle("valid", Boolean(isValid));
        importProfile.classList.toggle("invalid", !isValid);
        platformValue.textContent = detectedProfile.platform;
        marketValue.textContent = detectedProfile.market;
        validationValue.textContent = isValid
            ? `Matches active ${activeMarketType} account`
            : `Blocked: ${detectedProfile.market} file does not match active ${activeMarketType} account`;
        submitButton.disabled = !isValid;
    };

    fileInput.addEventListener("change", async () => {
        const file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
        if (!file) {
            setFileName(null);
            setInvalid("");
            setImportProfile(null, null, false);
            return;
        }
        submitButton.disabled = true;
        const detectedProfile = await detectProfileFromFile(file);
        if (!detectedProfile) {
            setFileName(file);
            fileInput.value = "";
            setInvalid(`Unsupported file. Please use a ${expectedUploadLabel} that matches this ${activeMarketType} account.`);
            setImportProfile(null, null, false);
            return;
        }
        const isValid = detectedProfile.market === activeMarketType;
        if (!isValid) {
            setFileName(file);
            setInvalid(`This ${detectedProfile.market} file does not match the active ${activeMarketType} account.`);
            setImportProfile(file, detectedProfile, false);
            return;
        }
        setInvalid("");
        setFileName(file);
        setImportProfile(file, detectedProfile, true);
    });

    ["dragenter", "dragover"].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
            event.preventDefault();
            event.stopPropagation();
            dropzone.classList.add("drag-over");
        });
    });

    ["dragleave", "dragend", "drop"].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
            event.preventDefault();
            event.stopPropagation();
            dropzone.classList.remove("drag-over");
        });
    });

    dropzone.addEventListener("drop", async (event) => {
        const file = event.dataTransfer && event.dataTransfer.files ? event.dataTransfer.files[0] : null;
        if (!file) {
            return;
        }
        submitButton.disabled = true;
        const detectedProfile = await detectProfileFromFile(file);
        if (!detectedProfile) {
            fileInput.value = "";
            setFileName(file);
            setInvalid(`Unsupported file. Please use a ${expectedUploadLabel} that matches this ${activeMarketType} account.`);
            setImportProfile(null, null, false);
            return;
        }
        const isValid = detectedProfile.market === activeMarketType;
        if (!isValid) {
            fileInput.value = "";
            setFileName(file);
            setInvalid(`This ${detectedProfile.market} file does not match the active ${activeMarketType} account.`);
            setImportProfile(file, detectedProfile, false);
            return;
        }
        setInvalid("");
        try {
            const transfer = new DataTransfer();
            transfer.items.add(file);
            fileInput.files = transfer.files;
        } catch {
            // Some browsers may block programmatic assignment; fallback keeps filename hint.
        }
        setFileName(file);
        setImportProfile(file, detectedProfile, true);
    });

    dropzone.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            fileInput.click();
        }
    });

    setImportProfile(null, null, false);
})();


