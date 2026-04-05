"""Prefix trades.html <style> rules with .trades-page. Run from repo root: python tools/patch_trades_page_css.py"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "templates" / "trades.html"


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    start = text.index("<style>") + len("<style>")
    end = text.index("</style>", start)
    before, chunk, after = text[:start], text[start:end], text[end:]

    old_jinja = """    {% if manage_mode %}
    th:nth-child(2),
    td:nth-child(2) {
        padding-left: 1rem;
    }
    {% endif %}
"""
    new_css = """    .trades-page.trades-page--manage th:nth-child(2),
    .trades-page.trades-page--manage td:nth-child(2) {
        padding-left: 1rem;
    }
"""
    if old_jinja not in chunk:
        raise SystemExit("Expected manage_mode Jinja block not found in trades.html <style>")
    chunk = chunk.replace(old_jinja, new_css)

    replacements = [
        ("    h2 {", "    .trades-page h2 {"),
        ("    table {", "    .trades-page table {"),
        ("    th,\n    td {", "    .trades-page th,\n    .trades-page td {"),
        (
            "    th:first-child,\n    td:first-child {",
            "    .trades-page th:first-child,\n    .trades-page td:first-child {",
        ),
        ("    th { font-weight", "    .trades-page th { font-weight"),
        ("    #applyFilters", "    .trades-page #applyFilters"),
        ("    #clearFilters", "    .trades-page #clearFilters"),
    ]
    for old, new in replacements:
        if old not in chunk:
            raise SystemExit(f"Expected snippet not found: {old!r}")
        chunk = chunk.replace(old, new)

    chunk = chunk.replace("    tbody tr", "    .trades-page tbody tr")

    norm: list[str] = []
    for line in chunk.splitlines(keepends=True):
        core = line.rstrip("\n")
        if core and core[0] not in " \t}%@" and not core.startswith("{%"):
            norm.append("    " + core + ("\n" if line.endswith("\n") else ""))
        else:
            norm.append(line)
    chunk = "".join(norm)

    prefixed: list[str] = []
    for line in chunk.splitlines(keepends=True):
        if line.startswith("    .") and not line.startswith("    .trades-page"):
            prefixed.append("    .trades-page " + line[4:])
        elif line.startswith("        .") and not line.startswith("        .trades-page"):
            prefixed.append("        .trades-page " + line[8:])
        else:
            prefixed.append(line)
    chunk = "".join(prefixed)

    while ".trades-page .trades-page " in chunk:
        chunk = chunk.replace(".trades-page .trades-page ", ".trades-page ")

    chunk = chunk.replace("        .trades-page .dash-wrap", "        .trades-page.dash-wrap")
    chunk = chunk.replace("        .trades-page .head-row", "        .app-page-hero-row")
    chunk = chunk.replace("        .trades-page .head-actions", "        .app-page-hero-actions")

    PATH.write_text(before + chunk + after, encoding="utf-8")


if __name__ == "__main__":
    main()
