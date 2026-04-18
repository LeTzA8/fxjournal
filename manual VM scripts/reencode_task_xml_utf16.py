"""
Rewrite Task Scheduler XML exports as UTF-16 LE with BOM (declaration encoding="UTF-16").
Uses explicit UTF-16 LE bytes — avoids Windows/Python quirks with write_text(encoding="utf-16").

  python "manual VM scripts/reencode_task_xml_utf16.py"

Accepts UTF-8 (with or without BOM) or UTF-16 LE (with BOM) source files.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_FILES = (
    "FX Journal MT5 Setup Watchdog.xml",
    "FX Journal MT5 Setup Worker Direct.xml",
    "FX Journal MT5 Sync Watchdog.xml",
    "FX Journal MT5 Sync Worker Direct.xml",
)


def read_logical_xml(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        tail = raw[2:]
        text = tail.decode("utf-16le")
        # Some broken exports mix UTF-32-style padding; UTF-16 decode yields spurious U+0000 between BMP chars.
        if "\x00" in text:
            text = "".join(c for c in text if c != "\x00")
        # Broken exports sometimes turn em dash into U+0014
        text = text.replace("\x14", "\u2014")
        return text
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    # Some editors save UTF-16 LE without BOM; `<?xml` in UTF-16 LE begins with 3c 00 3f 00 78 00 6d 00
    if len(raw) >= 8 and raw.startswith(b"<\x00?\x00x\x00m\x00"):
        return raw.decode("utf-16le")
    return raw.decode("utf-8")


def reencode(path: Path) -> None:
    text = read_logical_xml(path)
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if lines:
        lines[0] = '<?xml version="1.0" encoding="UTF-16"?>'
    body = "\r\n".join(lines) + "\r\n"
    path.write_bytes(b"\xff\xfe" + body.encode("utf-16le"))

    data = path.read_bytes()
    if data[:2] != b"\xff\xfe":
        raise SystemExit(f"{path}: missing UTF-16 LE BOM")
    if len(data) % 2 != 0:
        raise SystemExit(f"{path}: odd byte length (not UTF-16)")
    decoded = data[2:].decode("utf-16le")
    if "<Task" not in decoded:
        raise SystemExit(f"{path}: unexpected content after re-encode")
    if b"\x3c\x00\x00\x00" in data:
        raise SystemExit(f"{path}: UTF-32-style byte pattern detected; encoding is wrong")


def main() -> None:
    names = sys.argv[1:] or list(DEFAULT_FILES)
    for name in names:
        reencode(ROOT / name)
    print("OK:", len(names), "file(s)")


if __name__ == "__main__":
    main()
