from datetime import datetime
import logging
import os


def format_log_value(value, *, default="-", max_width=72):
    if value is None:
        text_value = default
    elif isinstance(value, datetime):
        text_value = value.isoformat(timespec="seconds")
    elif isinstance(value, float):
        text_value = f"{value:,.2f}"
    else:
        text_value = str(value).strip() or default
    if len(text_value) <= max_width:
        return text_value
    return f"{text_value[: max_width - 3]}..."


def _resolve_log_layout():
    """
    narrow: one field per line — readable in short (windowed) terminals.
    table: legacy +/- box ASCII table — wide but aligned in full-width logs.
    Default narrow on Windows (MT5 workers); default table elsewhere (e.g. Render Linux).
    """
    raw = os.getenv("FXJ_ASCII_LOG_LAYOUT", "").strip().lower()
    if raw in {"narrow", "stacked", "rows"}:
        return "narrow"
    if raw in {"table", "box", "ascii"}:
        return "table"
    return "narrow" if os.name == "nt" else "table"


def _log_line_max():
    """Target max line length for narrow layout (label + value on one line)."""
    raw = os.getenv("FXJ_ASCII_LOG_LINE_MAX", "").strip()
    if not raw:
        return 100
    try:
        n = int(raw)
    except ValueError:
        return 100
    return max(56, min(n, 200))


def _ascii_table_value_max_width():
    """
    Widen the Value column for long skip-reason / JSON-ish cells (default 72).
    Set FXJ_ASCII_LOG_MAX_WIDTH=0 or full on MT5 VMs for untruncated table values.
    """
    raw = os.getenv("FXJ_ASCII_LOG_MAX_WIDTH", "").strip().lower()
    if not raw:
        return 72
    if raw in {"0", "full", "none", "unlimited"}:
        return 1_000_000
    try:
        n = int(raw)
        if n <= 0:
            return 1_000_000
        return max(n, 32)
    except ValueError:
        return 72


def ascii_table(title, rows):
    value_max = _ascii_table_value_max_width()
    normalized_rows = [
        (format_log_value(label, default="", max_width=72), format_log_value(value, max_width=value_max))
        for label, value in rows
    ]
    key_header = "Metric"
    value_header = "Value"
    key_width = max([len(key_header), *(len(label) for label, _value in normalized_rows)])
    value_width = max([len(value_header), *(len(value) for _label, value in normalized_rows)])
    border = f"+-{'-' * key_width}-+-{'-' * value_width}-+"
    lines = [
        title,
        border,
        f"| {key_header.ljust(key_width)} | {value_header.ljust(value_width)} |",
        border,
    ]
    lines.extend(
        f"| {label.ljust(key_width)} | {value.ljust(value_width)} |"
        for label, value in normalized_rows
    )
    lines.append(border)
    return "\n".join(lines)


def ascii_table_narrow(title, rows, *, line_max=None, label_cols=26):
    """
    Stacked key: value lines; each line stays within line_max so windowed consoles wrap cleanly.
    """
    line_max = line_max if line_max is not None else _log_line_max()
    prefix = "  "
    sep = ": "
    value_max = max(24, line_max - len(prefix) - label_cols - len(sep))
    lines = [title]
    for label, value in rows:
        lab = format_log_value(label, default="", max_width=label_cols)
        val = format_log_value(value, max_width=value_max)
        lines.append(f"{prefix}{lab}{sep}{val}")
    return "\n".join(lines)


def log_ascii_table(logger, title, rows, *, level=logging.INFO):
    if _resolve_log_layout() == "narrow":
        body = ascii_table_narrow(title, rows)
    else:
        body = ascii_table(title, rows)
    logger.log(level, "\n%s", body)


def duration_ms(started_at, finished_at):
    if started_at is None or finished_at is None:
        return None
    return max(int((finished_at - started_at).total_seconds() * 1000), 0)


def duration_label(started_at, finished_at):
    elapsed_ms = duration_ms(started_at, finished_at)
    if elapsed_ms is None:
        return None
    return f"{elapsed_ms / 1000:.2f}s ({elapsed_ms} ms)"
