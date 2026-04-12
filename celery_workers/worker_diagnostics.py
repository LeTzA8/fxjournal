"""
Copy-paste-friendly diagnostic blocks for VM Celery workers.

Logs are bracketed with FXJ_WORKER_DIAGNOSTIC_BEGIN/END so you can select one block
from the console or logs/workers/<hostname>/celery.log.

Disable: FXJ_WORKER_DIAGNOSTIC=0
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import socket
import traceback
from datetime import datetime, timezone

_DIAG_BEGIN = "======== FXJ_WORKER_DIAGNOSTIC_BEGIN ========"
_DIAG_END = "======== FXJ_WORKER_DIAGNOSTIC_END ========"


def worker_diagnostic_enabled() -> bool:
    raw = os.environ.get("FXJ_WORKER_DIAGNOSTIC", "").strip().lower()
    if raw in {"0", "false", "no", "off", "disable"}:
        return False
    return True


def _safe_jsonish(value, *, max_len: int = 2000) -> str:
    def default(o):
        return f"<{type(o).__name__}>"

    try:
        s = json.dumps(value, default=default, ensure_ascii=True)
    except TypeError:
        s = repr(value)
    if len(s) > max_len:
        return f"{s[: max_len - 40]}... [truncated total_len={len(s)}]"
    return s


def _mask_url_credentials(url: str) -> str:
    if not url:
        return ""
    # redis://:password@host or redis://user:password@host
    return re.sub(r"://([^:@/]*):([^@]+)@", r"://\1:***@", url)


def _env_snapshot_lines() -> list[str]:
    lines = []
    flask = os.environ.get("FLASK_API_URL", "").strip()
    lines.append(f"FLASK_API_URL={flask or '(default https://myfxjournal.com)'}")
    sec = os.environ.get("MT5_SYNC_SECRET", "").strip()
    if sec:
        lines.append(f"MT5_SYNC_SECRET=set (length={len(sec)})")
    else:
        lines.append("MT5_SYNC_SECRET=MISSING")
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        lines.append(f"REDIS_URL={_mask_url_credentials(redis_url)}")
    else:
        lines.append("REDIS_URL=(empty — memory broker per celery_app fallback)")
    env_file = os.environ.get("FXJ_ENV_FILE", "").strip()
    if env_file:
        lines.append(f"FXJ_ENV_FILE={env_file}")
    lines.append(f"cwd={os.getcwd()}")
    lines.append(f"python={platform.python_version()} platform={platform.system()}")
    return lines


def build_traceback_text(
    exception: BaseException | None,
    *,
    traceback_obj=None,
    einfo=None,
) -> str:
    if einfo is not None:
        tb_attr = getattr(einfo, "traceback", None)
        if tb_attr:
            return str(tb_attr).rstrip()
    if exception is None:
        return "(no exception)"
    if traceback_obj is not None:
        parts = traceback.format_tb(traceback_obj)
        parts.extend(traceback.format_exception_only(type(exception), exception))
        return "".join(parts).rstrip()
    return "".join(
        traceback.format_exception(type(exception), exception, exception.__traceback__)
    ).rstrip()


def build_worker_diagnostic_text(
    *,
    phase: str,
    task_name: str | None,
    task_id: str | None,
    args,
    kwargs,
    exception: BaseException | None,
    retries: int | None = None,
    traceback_obj=None,
    einfo=None,
    extra_lines: list[str] | None = None,
) -> str:
    lines = [_DIAG_BEGIN]
    lines.append(f"ts_utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append(f"phase={phase}")
    lines.append(
        f"host={socket.gethostname()} pc={os.environ.get('COMPUTERNAME') or '-'}"
    )
    lines.append(f"task_name={task_name or '-'}")
    lines.append(f"task_id={task_id or '-'}")
    if retries is not None:
        lines.append(f"celery_retries={retries}")
    lines.append(f"args={_safe_jsonish(args)}")
    lines.append(f"kwargs={_safe_jsonish(kwargs)}")
    if exception is not None:
        lines.append(f"exception_type={type(exception).__name__}")
        msg = str(exception).replace("\r", " ").replace("\n", " | ")
        lines.append(f"exception_message={msg[:4000]}")
    else:
        lines.append("exception_type=-")
        lines.append("exception_message=-")
    lines.append("--- environment (no secrets) ---")
    lines.extend(_env_snapshot_lines())
    if extra_lines:
        lines.append("--- extra ---")
        lines.extend(extra_lines)
    lines.append("--- traceback ---")
    lines.append(
        build_traceback_text(exception, traceback_obj=traceback_obj, einfo=einfo)
        or "(no traceback)"
    )
    lines.append(_DIAG_END)
    return "\n".join(lines)


def log_worker_task_diagnostic(
    log: logging.Logger,
    *,
    phase: str,
    task_name: str | None,
    task_id: str | None,
    args,
    kwargs,
    exception: BaseException | None,
    retries: int | None = None,
    traceback_obj=None,
    einfo=None,
    extra_lines: list[str] | None = None,
) -> None:
    if not worker_diagnostic_enabled():
        return
    text = build_worker_diagnostic_text(
        phase=phase,
        task_name=task_name,
        task_id=task_id,
        args=args,
        kwargs=kwargs,
        exception=exception,
        retries=retries,
        traceback_obj=traceback_obj,
        einfo=einfo,
        extra_lines=extra_lines,
    )
    log.error("%s", text)


def log_requests_http_error(
    log: logging.Logger,
    exc: BaseException,
    *,
    title: str,
    rows: list[tuple[str, object]],
) -> None:
    """Log status, URL, and response body preview for requests.HTTPError."""
    from celery_workers.logging_utils import log_ascii_table

    out_rows = list(rows)
    response = getattr(exc, "response", None)
    if response is not None:
        out_rows.append(("HTTP status", getattr(response, "status_code", "?")))
        out_rows.append(("Response URL", getattr(response, "url", "?")))
        try:
            body = (response.text or "")[:2000]
        except Exception as body_exc:
            body = f"<could not read body: {body_exc}>"
        out_rows.append(("Response body (first 2000 chars)", body))
    else:
        out_rows.append(("HTTP response", "(none on this exception)"))
    log_ascii_table(log, title, out_rows, level=logging.ERROR)
    log.error(
        "%s\n%s\n%s",
        "======== FXJ_HTTP_ERROR_SUMMARY_BEGIN ========",
        f"exception_type={type(exc).__name__} message={str(exc)[:800]}",
        "======== FXJ_HTTP_ERROR_SUMMARY_END ========",
    )
