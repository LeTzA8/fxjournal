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


def log_ascii_table(logger, title, rows, *, level=logging.INFO):
    logger.log(level, "\n%s", ascii_table(title, rows))


def duration_ms(started_at, finished_at):
    if started_at is None or finished_at is None:
        return None
    return max(int((finished_at - started_at).total_seconds() * 1000), 0)


def duration_label(started_at, finished_at):
    elapsed_ms = duration_ms(started_at, finished_at)
    if elapsed_ms is None:
        return None
    return f"{elapsed_ms / 1000:.2f}s ({elapsed_ms} ms)"
