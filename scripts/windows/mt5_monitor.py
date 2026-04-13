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
    _engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    return _engine

def _fetch_accounts():
    from sqlalchemy import text
    q = text("""
        SELECT
            m.id,
            m.account_number,
            m.server,
            m.last_synced_at,
            m.is_active,
            m.archived_at,
            m.archive_reason,
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
        list_worker_states(worker_kind)  # warms the scan; result unused for now
    except CacheUnavailableError as exc:
        return {"ok": False, "error": str(exc)}

    last_p = _parse_ts(state.get("last_task_processed_at"))
    last_s = _parse_ts(state.get("last_task_started_at"))
    running = last_s is not None and (last_p is None or last_s > last_p)

    return {
        "ok":         True,
        "depth":      depth,
        "last_p":     last_p,
        "last_state": (state.get("last_finished_state") or "").upper(),
        "running":    running,
        "hostname":   state.get("last_processed_worker_hostname") or "—",
    }

# ── Rendering ────────────────────────────────────────────────────────────────

W       = 112          # display width
STALE_S = 5 * 60       # 5 min matches monitoring threshold
BAR     = "─" * W
BAR2    = "═" * W

# Column widths for account table
CW_ID    =  4
CW_LOGIN = 12
CW_SVR   = 24
CW_NAME  = 26
CW_SYNC  =  9
CW_STAT  =  6
CW_BAL   = 10


def _worker_block(now):
    lines = []
    lines.append(_b("  WORKERS"))
    lines.append(_c(DIM, "  " + BAR))

    for queue_name, worker_kind, label in (
        ("mt5_sync",  "mt5_sync",  "mt5_sync "),
        ("mt5_setup", "mt5_setup", "mt5_setup"),
    ):
        info = _queue_snapshot(queue_name, worker_kind, now)

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

        depth_c  = _c(WHT, str(depth)) if depth > 0 else _c(DIM, "0")
        lines.append(
            f"  {_b(label)}  {dot}  "
            f"queue={depth_c}  last={_b(ago_str)}  "
            f"state={state_c}  {_c(DIM, hostname)}"
        )

    return lines


def _accounts_block(now):
    lines = []
    try:
        rows = _fetch_accounts()
    except Exception as exc:
        lines.append(_c(RED, f"  DB ERROR: {exc}"))
        return lines

    active_n   = sum(1 for r in rows if r.is_active and not r.archived_at)
    archived_n = sum(1 for r in rows if r.archived_at)
    inactive_n = sum(1 for r in rows if not r.is_active and not r.archived_at)

    summary = f"{active_n} active"
    if inactive_n:
        summary += f"  {inactive_n} inactive"
    if archived_n:
        summary += f"  {archived_n} archived"

    lines.append(_b(f"  ACCOUNTS  ·  {summary}"))
    lines.append(_c(DIM, "  " + BAR))

    # Header row
    hdr = (
        f"  {_c(DIM, _cell('ID',           CW_ID))}  "
        f"{_c(DIM,  _cell('Login',         CW_LOGIN))}  "
        f"{_c(DIM,  _cell('Server',        CW_SVR))}  "
        f"{_c(DIM,  _cell('Account Name',  CW_NAME))}  "
        f"{_c(DIM,  _cell('Sync',          CW_SYNC, right=True))}  "
        f"{_c(DIM,  _cell('Status',        CW_STAT))}  "
        f"{_c(DIM,  _cell('Balance',       CW_BAL,  right=True))}"
    )
    lines.append(hdr)

    for r in rows:
        last_sync = r.last_synced_at
        if last_sync is not None and last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        sync_ago = _ago(last_sync, now) if last_sync else "never"

        if r.archived_at:
            stat_str = _c(DIM,  "ARCHVD")
            sync_col = _c(DIM,  _cell(sync_ago, CW_SYNC, right=True))
        elif not r.is_active:
            stat_str = _c(YLW,  "INACTV")
            sync_col = _c(YLW,  _cell(sync_ago, CW_SYNC, right=True))
        elif last_sync and (now - last_sync).total_seconds() > STALE_S:
            stat_str = _c(RED,  "STALE ")
            sync_col = _c(RED,  _cell(sync_ago, CW_SYNC, right=True))
        else:
            stat_str = _c(GRN,  "OK    ")
            sync_col = _c(GRN,  _cell(sync_ago, CW_SYNC, right=True))

        try:
            bal_str = f"{float(r.account_size):,.0f}" if r.account_size else "—"
        except (TypeError, ValueError):
            bal_str = "—"

        lines.append(
            f"  {_cell(r.id,             CW_ID)}  "
            f"{_cell(r.account_number,   CW_LOGIN)}  "
            f"{_cell(r.server,           CW_SVR)}  "
            f"{_cell(r.account_name,     CW_NAME)}  "
            f"{_pad(sync_col,            CW_SYNC, right=True)}  "
            f"{stat_str}  "
            f"{_c(DIM, _cell(bal_str,    CW_BAL, right=True))}"
        )

    return lines


def _render(interval):
    now   = _utcnow()
    vm_id = os.getenv("COMPUTERNAME") or os.getenv("VM_ID") or "VM"
    ts    = now.strftime("%Y-%m-%d  %H:%M:%S UTC")

    out = []
    out.append(_c(CYN, BAR2))
    out.append(
        _b(_c(WHT,
            f"  FX Journal MT5 Monitor  │  {vm_id}  │  {ts}  │  refresh {interval}s"
        ))
    )
    out.append(_c(CYN, BAR2))
    out.append("")

    out.extend(_worker_block(now))
    out.append("")
    out.extend(_accounts_block(now))

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
