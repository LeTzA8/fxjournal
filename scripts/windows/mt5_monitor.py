"""
FX Journal MT5 Monitor — standalone terminal dashboard.

Shows live status for both sync and setup workers plus all MT5 accounts.
Reads directly from Redis (queue/worker state) and Postgres (accounts).

Usage:
    python scripts/windows/mt5_monitor.py [--interval N]

Designed for 20-30 accounts on a single VM. Ctrl+C to exit.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import celery_app as _celery_app_module
_celery_app_module._load_runtime_env()

from celery_workers.cache import (
    CacheUnavailableError,
    get_queue_depth,
    get_queue_monitor_state,
    list_worker_states,
)

# ── ANSI ──────────────────────────────────────────────────────────────────────
R    = "\033[0m"
BOLD = "\033[1m"
DIM  = "\033[2m"
RED  = "\033[31m"
GRN  = "\033[32m"
YLW  = "\033[33m"
CYN  = "\033[36m"
WHT  = "\033[97m"

def _c(code, s):
    return f"{code}{s}{R}"

def _b(s):
    return f"{BOLD}{s}{R}"

def _ansi_strip(s):
    return re.sub(r"\033\[[0-9;]*m", "", s)

def _pad(s, width, right=False):
    """Pad an ANSI-colored string to `width` visible characters."""
    gap = max(0, width - len(_ansi_strip(s))) * " "
    return (gap + s) if right else (s + gap)

def _cell(value, width, right=False):
    """Truncate plain value to width, optionally right-align."""
    s = str(value if value is not None else "—")
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s.rjust(width) if right else s.ljust(width)

# ── Time ──────────────────────────────────────────────────────────────────────

def _utcnow():
    return datetime.now(timezone.utc)

def _parse_ts(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None

def _ago(dt, now):
    if dt is None:
        return "never"
    secs = max(int((now - dt).total_seconds()), 0)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"

# ── VM stats ──────────────────────────────────────────────────────────────────

def _count_terminals():
    """Count running terminal64.exe processes on this VM."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.lower().count("terminal64.exe")
    except Exception:
        return None

# ── DB ────────────────────────────────────────────────────────────────────────

_engine = None

def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    from sqlalchemy import create_engine
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL not configured")
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    # Use psycopg3 driver explicitly — project uses psycopg[binary], not psycopg2
    if db_url.startswith("postgresql://") and "+psycopg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    _engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    return _engine

def _fetch_accounts():
    from sqlalchemy import text
    q = text("""
        SELECT
            m.id,
            m.user_id,
            m.account_number,
            m.server,
            m.last_synced_at,
            m.is_active,
            m.archived_at,
            m.archive_reason,
            m.vm_id,
            t.name        AS account_name,
            t.account_size
        FROM mt5_account m
        LEFT JOIN trade_accounts t ON t.id = m.trade_account_id
        WHERE m.user_id IS NOT NULL
          AND m.trade_account_id IS NOT NULL
        ORDER BY
            m.archived_at ASC NULLS FIRST,
            m.is_active DESC,
            m.id
    """)
    with _get_engine().connect() as conn:
        return conn.execute(q).fetchall()

# ── Redis ─────────────────────────────────────────────────────────────────────

def _queue_snapshot(queue_name, worker_kind, now):
    try:
        depth   = get_queue_depth(queue_name)
        state   = get_queue_monitor_state(queue_name)
        list_worker_states(worker_kind)
    except CacheUnavailableError as exc:
        return {"ok": False, "error": str(exc)}

    last_p    = _parse_ts(state.get("last_task_processed_at"))
    last_s    = _parse_ts(state.get("last_task_started_at"))
    last_r    = _parse_ts(state.get("last_task_received_at"))
    running   = last_s is not None and (last_p is None or last_s > last_p)

    return {
        "ok":         True,
        "depth":      depth,
        "last_p":     last_p,
        "last_r":     last_r,
        "last_state": (state.get("last_finished_state") or "").upper(),
        "running":    running,
        "hostname":   state.get("last_processed_worker_hostname") or "—",
    }

# ── Rendering ────────────────────────────────────────────────────────────────

W       = 112
STALE_S = 5 * 60
BAR     = "─" * W
BAR2    = "═" * W

# Account table column widths
CW_ID    =  4
CW_USER  =  6
CW_LOGIN = 10
CW_SVR   = 22
CW_NAME  = 22
CW_SYNC  =  9
CW_STAT  =  6
CW_BAL   =  9


def _worker_block(now):
    lines = []
    lines.append(_b("  WORKERS"))
    lines.append(_c(DIM, "  " + BAR))

    snapshots = {}
    for queue_name, worker_kind, label in (
        ("mt5_sync",  "mt5_sync",  "mt5_sync "),
        ("mt5_setup", "mt5_setup", "mt5_setup"),
    ):
        info = _queue_snapshot(queue_name, worker_kind, now)
        snapshots[queue_name] = info

        if not info["ok"]:
            lines.append(f"  {_b(label)}  {_c(RED, '✗ REDIS ERROR')}  {info['error'][:60]}")
            continue

        depth    = info["depth"]
        last_p   = info["last_p"]
        ago_str  = _ago(last_p, now)
        state    = info["last_state"] or "—"
        hostname = info["hostname"]

        if info["running"]:
            dot = _c(CYN, "● RUNNING")
        elif last_p is None:
            dot = _c(YLW, "◌ NO DATA")
        elif (now - last_p).total_seconds() > STALE_S:
            dot = _c(RED, "✗ STALE  ")
        else:
            dot = _c(GRN, "● ACTIVE ")

        state_c = (
            _c(GRN, state) if state == "SUCCESS"
            else _c(YLW, state) if state in ("—", "PENDING", "")
            else _c(RED, state)
        )

        depth_c = _c(WHT, str(depth)) if depth > 0 else _c(DIM, "0")
        lines.append(
            f"  {_b(label)}  {dot}  "
            f"queue={depth_c}  last={_b(ago_str)}  "
            f"state={state_c}  {_c(DIM, hostname)}"
        )

    return lines, snapshots


def _stats_block(now, snapshots, accounts):
    lines = []
    lines.append(_b("  STATS"))
    lines.append(_c(DIM, "  " + BAR))

    # Terminal count
    terminal_count = _count_terminals()
    if terminal_count is None:
        term_str = _c(YLW, "unknown")
    elif terminal_count == 0:
        term_str = _c(YLW, "0")
    else:
        term_str = _c(GRN, str(terminal_count))
    lines.append(f"  MT5 terminals running    {term_str}")

    # Account counts
    if accounts is not None:
        active_n   = sum(1 for r in accounts if r.is_active and not r.archived_at)
        inactive_n = sum(1 for r in accounts if not r.is_active and not r.archived_at)
        archived_n = sum(1 for r in accounts if r.archived_at)
        total_n    = len(accounts)
        parts = [
            _c(GRN, f"{active_n} active"),
            _c(DIM,  f"{total_n} total"),
        ]
        if inactive_n:
            parts.append(_c(YLW, f"{inactive_n} inactive"))
        if archived_n:
            parts.append(_c(DIM, f"{archived_n} archived"))
        lines.append(f"  Accounts                 {'  ·  '.join(parts)}")

    # Beat / last dispatch
    sync_info = snapshots.get("mt5_sync", {})
    if sync_info.get("ok"):
        last_r = sync_info.get("last_r")
        beat_str = _b(_ago(last_r, now)) if last_r else _c(YLW, "never")
        lines.append(f"  Last sync beat           {beat_str} ago")

    # Setup queue
    setup_info = snapshots.get("mt5_setup", {})
    if setup_info.get("ok"):
        last_p = setup_info.get("last_p")
        setup_str = _b(_ago(last_p, now)) if last_p else _c(YLW, "never")
        lines.append(f"  Last setup task          {setup_str} ago")

    return lines


def _accounts_block(now, accounts, max_rows=None):
    lines = []

    if accounts is None:
        lines.append(_c(RED, "  DB ERROR — accounts unavailable"))
        return lines

    active_n   = sum(1 for r in accounts if r.is_active and not r.archived_at)
    archived_n = sum(1 for r in accounts if r.archived_at)
    inactive_n = sum(1 for r in accounts if not r.is_active and not r.archived_at)

    summary = f"{active_n} active"
    if inactive_n:
        summary += f"  {inactive_n} inactive"
    if archived_n:
        summary += f"  {archived_n} archived"

    lines.append(_b(f"  ACCOUNTS  ·  {summary}"))
    lines.append(_c(DIM, "  " + BAR))

    hdr = (
        f"  {_c(DIM, _cell('ID',          CW_ID))}  "
        f"{_c(DIM,   _cell('User',        CW_USER))}  "
        f"{_c(DIM,   _cell('Login',       CW_LOGIN))}  "
        f"{_c(DIM,   _cell('Server',      CW_SVR))}  "
        f"{_c(DIM,   _cell('Account',     CW_NAME))}  "
        f"{_c(DIM,   _cell('Sync',        CW_SYNC, right=True))}  "
        f"{_c(DIM,   _cell('Status',      CW_STAT))}  "
        f"{_c(DIM,   _cell('Balance',     CW_BAL,  right=True))}"
    )
    lines.append(hdr)

    visible   = accounts if max_rows is None else accounts[:max_rows]
    hidden_n  = len(accounts) - len(visible)

    for r in visible:
        last_sync = r.last_synced_at
        if last_sync is not None and last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        sync_ago = _ago(last_sync, now) if last_sync else "never"

        if r.archived_at:
            stat_str = _c(DIM, "ARCHVD")
            sync_col = _c(DIM, _cell(sync_ago, CW_SYNC, right=True))
        elif not r.is_active:
            stat_str = _c(YLW, "INACTV")
            sync_col = _c(YLW, _cell(sync_ago, CW_SYNC, right=True))
        elif last_sync and (now - last_sync).total_seconds() > STALE_S:
            stat_str = _c(RED, "STALE ")
            sync_col = _c(RED, _cell(sync_ago, CW_SYNC, right=True))
        else:
            stat_str = _c(GRN, "OK    ")
            sync_col = _c(GRN, _cell(sync_ago, CW_SYNC, right=True))

        try:
            bal_str = f"{float(r.account_size):,.0f}" if r.account_size else "—"
        except (TypeError, ValueError):
            bal_str = "—"

        lines.append(
            f"  {_cell(r.id,            CW_ID)}  "
            f"{_cell(r.user_id,         CW_USER)}  "
            f"{_cell(r.account_number,  CW_LOGIN)}  "
            f"{_cell(r.server,          CW_SVR)}  "
            f"{_cell(r.account_name,    CW_NAME)}  "
            f"{_pad(sync_col,           CW_SYNC, right=True)}  "
            f"{stat_str}  "
            f"{_c(DIM, _cell(bal_str,   CW_BAL, right=True))}"
        )

    if hidden_n > 0:
        lines.append(
            _c(DIM, f"  ... {hidden_n} more account(s) — resize window taller to see all")
        )

    return lines


def _render(interval):
    now   = _utcnow()
    vm_id = os.getenv("COMPUTERNAME") or os.getenv("VM_ID") or "VM"
    ts    = now.strftime("%Y-%m-%d  %H:%M:%S UTC")

    # Detect terminal height for account table truncation.
    # Fixed overhead: 3 header + 1 blank + 4 workers + 1 blank + 6 stats +
    #                 1 blank + 3 accounts-header + 1 blank + 2 footer = 22
    _FIXED_LINES = 22
    try:
        term_h   = os.get_terminal_size().lines
        max_rows = max(1, term_h - _FIXED_LINES)
    except OSError:
        max_rows = None  # unknown height — show all

    # Fetch accounts once — shared between stats and accounts blocks
    try:
        accounts = _fetch_accounts()
    except Exception as exc:
        accounts     = None
        _accounts_err = str(exc)

    out = []
    out.append(_c(CYN, BAR2))
    out.append(_b(_c(WHT,
        f"  FX Journal MT5 Monitor  │  {vm_id}  │  {ts}  │  refresh {interval}s"
    )))
    out.append(_c(CYN, BAR2))
    out.append("")

    worker_lines, snapshots = _worker_block(now)
    out.extend(worker_lines)
    out.append("")

    out.extend(_stats_block(now, snapshots, accounts))
    out.append("")

    if accounts is None:
        out.append(_c(RED, f"  DB ERROR: {_accounts_err}"))
    else:
        out.extend(_accounts_block(now, accounts, max_rows=max_rows))

    out.append("")
    out.append(_c(CYN, BAR2))
    out.append(_c(DIM, "  Ctrl+C to exit"))
    return "\n".join(out)


# ── Entry ─────────────────────────────────────────────────────────────────────

def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def main():
    parser = argparse.ArgumentParser(description="FX Journal MT5 Monitor")
    parser.add_argument(
        "--interval", type=int, default=5, metavar="N",
        help="Refresh interval in seconds (default: 5)",
    )
    args = parser.parse_args()

    try:
        while True:
            _clear()
            try:
                print(_render(args.interval))
            except Exception as exc:
                print(_c(RED, f"  Render error: {exc}"))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _clear()
        print("Monitor stopped.")


if __name__ == "__main__":
    main()
