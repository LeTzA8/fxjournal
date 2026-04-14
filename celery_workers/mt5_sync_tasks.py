import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import uuid

import requests

from celery_app import celery
from celery_workers.logging_utils import duration_label, log_ascii_table
from celery_workers.worker_monitor import get_vm_id
from trading import MT5_DEFAULT_SOURCE_TIMEZONE_NAME

logger = logging.getLogger(__name__)

# ── DEBUG: raw MT5 snapshot logger ──────────────────────────────────────────
# Set FXJ_MT5_DEBUG_ACCOUNT_ID=<TradeAccount.id> in the VM env to enable.
# This is the Trade Account ID visible in the UI — not the internal MT5Account row id.
# Leave unset (or empty) to disable entirely — zero overhead for all others.
_raw = os.getenv("FXJ_MT5_DEBUG_ACCOUNT_ID", "").strip()
_MT5_DEBUG_TARGET_ACCOUNT_ID = int(_raw) if _raw.isdigit() else None
del _raw
# ────────────────────────────────────────────────────────────────────────────


# MetaTrader5 keeps process-global session state, so sync tasks must not
# overlap MT5 API calls inside the same worker process.
_MT5_API_SESSION_LOCK = threading.Lock()


def _retry_with_backoff(task, exc, *, base_delay=30, max_delay=300):
    retry_number = getattr(getattr(task, "request", None), "retries", 0)
    countdown = min(base_delay * (2 ** retry_number), max_delay)
    raise task.retry(exc=exc, countdown=countdown)


def _adjust_mt5_unix_epoch(timestamp_value, *, offset_minutes=0):
    """Subtract broker/server-ahead delta from raw MT5 Unix epoch (deal/bar times)."""
    if timestamp_value is None:
        return None
    return float(timestamp_value) - (int(offset_minutes or 0) * 60)


def _to_utc_iso(timestamp_value, *, offset_minutes=0):
    adjusted_timestamp = _adjust_mt5_unix_epoch(timestamp_value, offset_minutes=offset_minutes)
    if adjusted_timestamp is None:
        return None
    return datetime.fromtimestamp(adjusted_timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _naive_utc_to_aware(value):
    """Trade datetimes are naive UTC in DB; MT5 Python treats naive datetimes as *local* time."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _vm_timezone_context():
    now_local = datetime.now().astimezone()
    tz_value = now_local.tzinfo
    tz_name = getattr(tz_value, "key", None) or str(tz_value or "").strip() or "unknown"
    offset = now_local.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset is not None else None
    return {
        "vm_timezone_name": tz_name,
        "vm_utc_offset_minutes": offset_minutes,
    }


def _shift_datetime_by_minutes(value, *, minutes=0):
    if value is None:
        return None
    return value + timedelta(minutes=int(minutes or 0))


def _debug_use_broker_offset_end_time():
    """TEST ONLY: shift end_time forward by the broker offset before passing to
    history_deals_get. Set FXJ_MT5_DEBUG_BROKER_OFFSET_END_TIME=1 to enable.
    Remove / unset after the experiment."""
    raw = os.getenv("FXJ_MT5_DEBUG_BROKER_OFFSET_END_TIME", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _mt5_debug_resolve_log_dir():
    """Mirror the same log-dir resolution used by celery_app._configure_worker_file_logging."""
    import re as _re
    configured = os.getenv("FXJ_WORKER_LOG_DIR", "").strip()
    if configured:
        log_root = os.path.abspath(configured)
    else:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_root = os.path.join(repo_root, "logs", "workers")
    computername = os.getenv("COMPUTERNAME", "vm").strip() or "vm"
    raw_hostname = f"mt5-sync@{computername}"
    safe_name = raw_hostname.replace("@", "_at_")
    safe_name = _re.sub(r'[:*?"<>|]+', "_", safe_name)
    safe_name = _re.sub(r"\s+", "_", safe_name).strip("._")[:120] or "celery"
    return os.path.join(log_root, safe_name)


def _mt5_debug_log_raw_snapshot(mt5_account_id, trade_account_id, open_positions, deals,
                                latest_deal_utc, now_utc, mt5, from_date, to_date,
                                utc_to_date=None):
    """
    Write one raw MT5 broker snapshot to a new per-sync file in the worker log dir.
    Also runs a group="*" comparison call to test whether the terminal's per-symbol
    cache returns deals that the unfiltered history_deals_get missed.
    Only called when trade_account_id == _MT5_DEBUG_TARGET_ACCOUNT_ID.
    All exceptions are silently swallowed — this must never interrupt a sync.
    """
    try:
        ref = now_utc if now_utc else datetime.now(timezone.utc)
        ts = ref.strftime("%Y%m%d_%H%M%SZ")

        log_dir = _mt5_debug_resolve_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"mt5_debug_trade{trade_account_id}_mt5acct{mt5_account_id}_{ts}.log")

        def _ser(obj):
            if obj is None:
                return None
            if hasattr(obj, "_asdict"):
                return dict(obj._asdict())
            if hasattr(obj, "__dict__"):
                return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
            return repr(obj)

        now_iso = ref.isoformat(timespec="seconds")

        delta_seconds = None
        if latest_deal_utc and latest_deal_utc != "-":
            try:
                deal_dt = datetime.fromisoformat(latest_deal_utc)
                if deal_dt.tzinfo is None:
                    deal_dt = deal_dt.replace(tzinfo=timezone.utc)
                delta_seconds = int((ref - deal_dt).total_seconds())
            except Exception:
                pass

        # --- group="*" comparison probe ---
        # Ask the terminal for the same window but with an explicit wildcard group
        # filter. This uses a different internal code path and can surface deals
        # that the unfiltered call misses due to terminal cache divergence.
        grouped_deals = []
        grouped_error = None
        grouped_extra_tickets = []
        try:
            grouped_deals = list(mt5.history_deals_get(from_date, to_date, group="*") or [])
            original_tickets = {d.get("ticket") if isinstance(d, dict) else getattr(d, "ticket", None) for d in deals}
            grouped_extra_tickets = [
                _ser(d) for d in grouped_deals
                if (d.get("ticket") if isinstance(d, dict) else getattr(d, "ticket", None))
                not in original_tickets
            ]
        except Exception as exc:
            grouped_error = repr(exc)

        # --- end_time diagnostics ---
        utc_now_at_log  = datetime.now(timezone.utc)
        _effective_end  = to_date                          # what was actually passed to history_deals_get
        _original_utc   = utc_to_date if utc_to_date is not None else to_date
        _offset_applied = _effective_end != _original_utc

        end_time_repr       = repr(_effective_end)
        end_time_iso        = _effective_end.isoformat() if hasattr(_effective_end, "isoformat") else str(_effective_end)
        end_time_tzinfo     = str(getattr(_effective_end, "tzinfo", "N/A"))
        end_time_utcoffset  = str(_effective_end.utcoffset()) if hasattr(_effective_end, "utcoffset") and _effective_end.utcoffset() is not None else "None (naive)"
        end_time_timestamp  = _effective_end.timestamp() if hasattr(_effective_end, "timestamp") else "N/A"
        end_time_gap_sec    = round((utc_now_at_log - _effective_end).total_seconds(), 1) if hasattr(_effective_end, "utcoffset") else "N/A"
        utc_end_iso         = _original_utc.isoformat() if hasattr(_original_utc, "isoformat") else str(_original_utc)
        broker_shift_sec    = round((_effective_end - _original_utc).total_seconds()) if _offset_applied else 0

        block = "\n".join([
            "==== SYNC START ====",
            f"time: {now_iso}",
            f"trade_account_id: {trade_account_id}",
            f"mt5_account_id: {mt5_account_id}",
            f"query_from: {from_date.isoformat() if hasattr(from_date, 'isoformat') else str(from_date)}",
            f"query_to: {to_date.isoformat() if hasattr(to_date, 'isoformat') else str(to_date)}",
            "",
            "--- end_time diagnostics ---",
            f"end_time (used):      {end_time_iso}",
            f"end_time (pure UTC):  {utc_end_iso}",
            f"broker_offset_applied:{_offset_applied}  (shift: {broker_shift_sec}s)",
            f"end_time repr:        {end_time_repr}",
            f"end_time tzinfo:      {end_time_tzinfo}",
            f"end_time utcoffset:   {end_time_utcoffset}",
            f"end_time timestamp:   {end_time_timestamp}",
            f"utc_now at log time:  {utc_now_at_log.isoformat(timespec='seconds')}",
            f"end_time gap vs now:  {end_time_gap_sec}s  (positive = end_time is in the past)",
            "",
            f"open_position_count: {len(open_positions)}",
            f"deal_count (unfiltered): {len(deals)}",
            f"deal_count (group=*): {len(grouped_deals)}",
            f"deals_only_in_grouped: {len(grouped_extra_tickets)}",
            "",
            "positions:",
            json.dumps([_ser(p) for p in open_positions], indent=2, default=str),
            "",
            "deals (unfiltered):",
            json.dumps([_ser(d) for d in deals], indent=2, default=str),
            "",
            "deals only in group=* (missing from unfiltered):",
            json.dumps(grouped_extra_tickets, indent=2, default=str)
            if grouped_error is None else f"ERROR: {grouped_error}",
            "",
            "--- summary ---",
            f"latest_deal_time: {latest_deal_utc}",
            f"current_system_time: {now_iso}",
            f"delta_seconds: {delta_seconds}",
            "==== SYNC END ====",
            "",
        ])

        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(block + "\n")
    except Exception:
        pass  # Never break sync


_PROBE_ALWAYS_ON_SYMBOLS = [
    "BTCUSD",
    "BTCUSDT",
    "XBTUSD",
    "BTCUSD.r",
    "BTC/USD",
]


def _probe_mt5_server_delta_minutes(mt5, *, preferred_symbol=None):
    """
    Estimate MT5 server clock drift relative to UTC using latest tick timestamp.
    Returns minute delta where positive means MT5 clock appears ahead of UTC.

    Symbol priority:
      1. preferred_symbol candidates (trade-specific, e.g. XAUUSD)
      2. EURUSD (liquid FX baseline; stale on weekends)
      3. Common BTC/USD names (24/7 crypto — reliable during forex market closure)

    The 6-hour sanity guard filters ticks that are too stale to give a useful
    reading regardless of which symbol supplied them.  The Redis cache in
    _resolve_mt5_server_offset_minutes is a final safety net for brokers that
    offer no crypto instruments at all.
    """
    symbol_info_tick = getattr(mt5, "symbol_info_tick", None)
    if not callable(symbol_info_tick):
        return 0
    symbol_candidates = []
    if preferred_symbol is not None:
        if isinstance(preferred_symbol, (list, tuple)):
            symbol_candidates.extend(
                str(s).strip() for s in preferred_symbol if s and str(s).strip()
            )
        else:
            s = str(preferred_symbol).strip()
            if s:
                symbol_candidates.append(s)
    if "EURUSD" not in symbol_candidates:
        symbol_candidates.append("EURUSD")
    for sym in _PROBE_ALWAYS_ON_SYMBOLS:
        if sym not in symbol_candidates:
            symbol_candidates.append(sym)
    for symbol in symbol_candidates:
        if not symbol:
            continue
        tick = symbol_info_tick(symbol)
        tick_time = getattr(tick, "time", None) if tick is not None else None
        if not tick_time:
            continue
        now_utc = int(datetime.now(timezone.utc).timestamp())
        delta_seconds = int(tick_time) - now_utc
        if abs(delta_seconds) > 6 * 3600:
            continue
        return int(round(delta_seconds / 60))
    return 0


def _mt5_offset_cache_key(mt5_account_id):
    return f"mt5_server_offset_minutes:{mt5_account_id}"


def _resolve_mt5_server_offset_minutes(mt5, mt5_account_id, *, preferred_symbol=None):
    """
    Return the broker UTC offset in minutes.

    During market hours the probe compares the latest tick timestamp to UTC now
    and gives an accurate reading.  When market is closed the last tick is stale
    and the probe's sanity guard returns 0.  We cache every non-zero measurement
    in Redis (7-day TTL) so off-hours syncs and bar-fetches still use the correct
    offset rather than falling back to 0 and storing raw broker-server-time.

    Returns 0 if the probe fires AND no cached value exists (UTC broker, or first
    run before a market-hours measurement has been stored).
    """
    probed = _probe_mt5_server_delta_minutes(mt5, preferred_symbol=preferred_symbol)

    redis_client = None
    try:
        from celery_workers.cache import _client as _cache_client  # noqa: PLC0415
        redis_client = _cache_client()
    except Exception:
        pass

    cache_key = _mt5_offset_cache_key(mt5_account_id)

    if probed != 0:
        # Good live measurement — persist it so off-hours runs can reuse it.
        if redis_client is not None:
            try:
                redis_client.set(cache_key, str(probed), ex=60 * 60 * 24 * 7)
            except Exception:
                pass
        return probed

    # Probe returned 0 — market may be closed; try the cached offset.
    if redis_client is not None:
        try:
            cached = redis_client.get(cache_key)
            if cached is not None:
                return int(cached)
        except Exception:
            pass

    return 0


def _deal_float_value(deal, field_name, default=0.0):
    try:
        return float(getattr(deal, field_name, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _deal_volume(deal):
    return max(_deal_float_value(deal, "volume", 0.0), 0.0)


def _weighted_average_price(deals):
    weighted_total = 0.0
    total_volume = 0.0
    for deal in deals:
        volume = _deal_volume(deal)
        price = _deal_float_value(deal, "price", 0.0)
        if volume <= 0 or price <= 0:
            continue
        weighted_total += price * volume
        total_volume += volume
    if total_volume <= 0:
        return None
    return weighted_total / total_volume


def _positions_to_open_trades(positions, *, position_type_buy=0, offset_minutes=0):
    """Convert MT5 position objects (from positions_get) into open trade row dicts."""
    trades = []
    for pos in positions:
        pos_id = getattr(pos, "identifier", None) or getattr(pos, "ticket", None)
        if not pos_id:
            continue
        pos_type = getattr(pos, "type", None)
        side = "BUY" if pos_type == position_type_buy else "SELL"
        entry_price = _deal_float_value(pos, "price_open") or None
        lot_size = _deal_float_value(pos, "volume") or None
        sl = _deal_float_value(pos, "sl") or None
        tp = _deal_float_value(pos, "tp") or None
        trades.append(
            {
                "symbol": getattr(pos, "symbol", None),
                "side": side,
                "entry_price": entry_price,
                "exit_price": None,
                "lot_size": lot_size,
                "pnl": _deal_float_value(pos, "profit"),
                "commission": _deal_float_value(pos, "commission"),
                "swap": _deal_float_value(pos, "swap"),
                "stop_loss": sl,
                "take_profit": tp,
                "opened_at": _to_utc_iso(getattr(pos, "time", None), offset_minutes=offset_minutes),
                "closed_at": None,
                "mt5_position": str(pos_id),
                "trade_note": str(getattr(pos, "comment", "") or "").strip() or None,
                "source_timezone": MT5_DEFAULT_SOURCE_TIMEZONE_NAME,
                "timestamp_interpretation": "mt5_epoch_utc",
                "is_open": True,
            }
        )
    return trades


def _latest_deal_context(deals, *, offset_minutes=0):
    latest_deal = None
    latest_time = None
    for deal in deals or []:
        deal_time = getattr(deal, "time", None)
        if not deal_time:
            continue
        if latest_time is None or deal_time > latest_time:
            latest_time = deal_time
            latest_deal = deal
    if latest_deal is None:
        return "-", "-"
    return (
        _to_utc_iso(latest_time, offset_minutes=offset_minutes) or "-",
        str(getattr(latest_deal, "position_id", "") or "-"),
    )


def _open_position_ids_preview(positions, *, limit=5):
    position_ids = []
    for pos in positions or []:
        pos_id = getattr(pos, "identifier", None) or getattr(pos, "ticket", None)
        if pos_id in {None, ""}:
            continue
        position_ids.append(str(pos_id))
    if not position_ids:
        return "-"
    if len(position_ids) <= limit:
        return ",".join(position_ids)
    return f"{','.join(position_ids[:limit])},+{len(position_ids) - limit}_more"


def _history_stale_threshold_minutes():
    raw_value = os.getenv("FXJ_MT5_HISTORY_STALE_THRESHOLD_MINUTES", "").strip()
    if not raw_value:
        return 30
    try:
        return max(int(raw_value), 1)
    except (TypeError, ValueError):
        return 30


def _mt5_soft_reconnect_on_stale_enabled():
    """One extra shutdown→initialize→login→refetch when broker view looks stale vs DB."""
    raw = os.getenv("FXJ_MT5_SOFT_RECONNECT_ON_STALE", "1").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _stale_reconnect_wait_seconds():
    """Seconds to wait after mt5.shutdown() before reinitializing on a stale reconnect.
    Gives the terminal process time to pull fresh history from the broker before we
    reconnect and query it. Override with FXJ_MT5_STALE_RECONNECT_WAIT_SECONDS."""
    raw = os.getenv("FXJ_MT5_STALE_RECONNECT_WAIT_SECONDS", "").strip()
    if not raw:
        return 15
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 15


def _precheck_mt5_history_stale_vs_db(
    *,
    open_position_count,
    latest_deal_utc,
    user_id,
    trade_account_id,
    now_utc,
):
    """
    Same idea as post-ingest history_stale, but before POST so we can refetch once.
    Broker: flat book + deal history missing or older than threshold, while DB still
    has MT5 rows marked open — often a stuck terminal/API view.
    """
    if int(open_position_count or 0) != 0:
        return False
    lag = _lag_minutes_from_utc_iso(latest_deal_utc, now=now_utc)
    if lag is not None and lag < _history_stale_threshold_minutes():
        return False
    return (
        _count_open_mt5_db_trades(user_id=user_id, trade_account_id=trade_account_id) > 0
    )


def _lag_minutes_from_utc_iso(utc_iso_value, *, now=None):
    text_value = str(utc_iso_value or "").strip()
    if not text_value or text_value == "-":
        return None
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    reference = now or datetime.now(timezone.utc)
    lag_seconds = max((reference - parsed).total_seconds(), 0)
    return int(lag_seconds // 60)


def _count_open_mt5_db_trades(*, user_id, trade_account_id):
    from models import Trade  # noqa: PLC0415

    return (
        Trade.query.filter_by(
            user_id=user_id,
            trade_account_id=trade_account_id,
        )
        .filter(
            Trade.mt5_position.isnot(None),
            Trade.closed_at.is_(None),
        )
        .count()
    )


def aggregate_deals_to_trades(
    deals,
    *,
    entry_in=0,
    entry_out=1,
    extra_exit_entries=(),
    deal_type_buy=0,
    offset_minutes=0,
):
    deals = [deal for deal in deals if getattr(deal, "type", 99) <= 1]
    positions = defaultdict(list)
    exit_entries = {entry_out}
    exit_entries.update(entry_value for entry_value in extra_exit_entries if entry_value is not None)

    for deal in deals:
        position_id = getattr(deal, "position_id", None)
        if not position_id:
            continue
        positions[position_id].append(deal)

    trades = []
    for position_id, position_deals in positions.items():
        position_deals = sorted(position_deals, key=lambda deal: getattr(deal, "time", 0) or 0)
        entry_deals = [deal for deal in position_deals if getattr(deal, "entry", None) == entry_in]
        exit_deals = [deal for deal in position_deals if getattr(deal, "entry", None) in exit_entries]
        exit_price = _weighted_average_price(exit_deals)
        exit_volume = sum(_deal_volume(deal) for deal in exit_deals)
        latest_exit_deal = exit_deals[-1] if exit_deals else None
        total_commission = sum(float(getattr(deal, "commission", 0.0) or 0.0) for deal in position_deals)
        total_swap = sum(float(getattr(deal, "swap", 0.0) or 0.0) for deal in position_deals)
        total_profit = sum(float(getattr(deal, "profit", 0.0) or 0.0) for deal in position_deals)

        if not entry_deals:
            if exit_deals and exit_price is not None and exit_volume > 0:
                trades.append(
                    {
                        "symbol": getattr(latest_exit_deal, "symbol", None),
                        "side": None,
                        "entry_price": None,
                        "exit_price": exit_price,
                        "lot_size": exit_volume,
                        "pnl": total_profit,
                        "commission": total_commission,
                        "swap": total_swap,
                        "stop_loss": None,
                        "take_profit": None,
                        "opened_at": None,
                        "closed_at": _to_utc_iso(
                            getattr(latest_exit_deal, "time", None),
                            offset_minutes=offset_minutes,
                        ),
                        "mt5_position": str(position_id),
                        "trade_note": str(getattr(latest_exit_deal, "comment", "") or "").strip() or None,
                        "source_timezone": MT5_DEFAULT_SOURCE_TIMEZONE_NAME,
                        "timestamp_interpretation": "mt5_epoch_utc",
                        "is_open": False,
                    }
                )
            continue

        entry_deal = entry_deals[0]
        entry_price = _weighted_average_price(entry_deals)
        entry_volume = sum(_deal_volume(deal) for deal in entry_deals)
        if entry_price is None or entry_volume <= 0:
            continue

        side = "BUY" if getattr(entry_deal, "type", None) == deal_type_buy else "SELL"

        # Keep the trade open until the full entry volume has been offset.
        if exit_deals and exit_volume + 1e-9 >= entry_volume and exit_price is not None:
            trades.append(
                {
                    "symbol": getattr(latest_exit_deal, "symbol", None) or getattr(entry_deal, "symbol", None),
                    "side": side,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "lot_size": entry_volume,
                    "pnl": total_profit,
                    "commission": total_commission,
                    "swap": total_swap,
                    "stop_loss": None,
                    "take_profit": None,
                    "opened_at": _to_utc_iso(
                        getattr(entry_deal, "time", None),
                        offset_minutes=offset_minutes,
                    ),
                    "closed_at": _to_utc_iso(
                        getattr(latest_exit_deal, "time", None),
                        offset_minutes=offset_minutes,
                    ),
                    "mt5_position": str(position_id),
                    "trade_note": str(getattr(latest_exit_deal, "comment", "") or "").strip() or None,
                    "source_timezone": MT5_DEFAULT_SOURCE_TIMEZONE_NAME,
                    "timestamp_interpretation": "mt5_epoch_utc",
                    "is_open": False,
                }
            )
        elif not exit_deals or exit_volume < entry_volume:
            # No exits at all, or partially closed — position still running.
            trades.append(
                {
                    "symbol": getattr(entry_deal, "symbol", None),
                    "side": side,
                    "entry_price": entry_price,
                    "exit_price": None,
                    "lot_size": entry_volume,
                    "pnl": None,
                    "commission": total_commission,
                    "swap": total_swap,
                    "stop_loss": None,
                    "take_profit": None,
                    "opened_at": _to_utc_iso(
                        getattr(entry_deal, "time", None),
                        offset_minutes=offset_minutes,
                    ),
                    "closed_at": None,
                    "mt5_position": str(position_id),
                    "trade_note": str(getattr(entry_deal, "comment", "") or "").strip() or None,
                    "source_timezone": MT5_DEFAULT_SOURCE_TIMEZONE_NAME,
                    "timestamp_interpretation": "mt5_epoch_utc",
                    "is_open": True,
                }
            )

    return trades


def _sync_lock_key(mt5_account_id):
    return f"mt5_sync_lock:{mt5_account_id}"


def _mask_account_number_for_log(account_number):
    text_value = str(account_number or "").strip()
    if not text_value:
        return "unknown"
    suffix = text_value[-4:] if len(text_value) >= 4 else text_value
    return f"...{suffix}"


def _summarize_trade_states(trades):
    open_trades = sum(1 for trade in trades if trade.get("is_open"))
    closed_trades = max(len(trades) - open_trades, 0)
    return open_trades, closed_trades


def _rolling_sync_window_days():
    """How far back incremental (non–full-history) syncs request MT5 deals."""
    raw = os.environ.get("FXJ_MT5_SYNC_ROLLING_DAYS", "").strip()
    if not raw:
        return 90
    try:
        parsed = int(raw)
    except ValueError:
        return 90
    return max(1, min(parsed, 366 * 5))


def _history_chunk_days():
    """Chunk size when walking MT5 deal history (wide ranges often truncate in one API call)."""
    raw = os.environ.get("FXJ_MT5_HISTORY_CHUNK_DAYS", "").strip()
    if not raw:
        return 30
    try:
        parsed = int(raw)
    except ValueError:
        return 30
    return max(1, min(parsed, 120))


def _deal_dedupe_key(deal):
    ticket = getattr(deal, "ticket", None)
    if ticket is not None:
        return ("ticket", int(ticket))
    return (
        "nt",
        getattr(deal, "time", None),
        getattr(deal, "order", None),
        getattr(deal, "position_id", None),
        getattr(deal, "entry", None),
        getattr(deal, "type", None),
    )


def _chunked_history_deals_get(mt5, mt5_from_date, mt5_to_date, *, log_chunk_merges=True):
    """
    MetaTrader 5 frequently returns only a subset of deals for very wide
    history_deals_get(from, to) windows. Walk the range in smaller slices and
    merge, deduping by deal ticket so each deal is counted once.
    """
    chunk_days = _history_chunk_days()
    span_seconds = (mt5_to_date - mt5_from_date).total_seconds()
    if span_seconds <= 0:
        return [], 0

    if span_seconds <= chunk_days * 86400:
        batch = list(mt5.history_deals_get(mt5_from_date, mt5_to_date) or [])
        return batch, 1

    seen = set()
    merged = []
    step = timedelta(days=chunk_days)
    t = mt5_from_date
    chunks = 0
    while t < mt5_to_date:
        t_end = min(t + step, mt5_to_date)
        batch = mt5.history_deals_get(t, t_end) or []
        chunks += 1
        for deal in batch:
            key = _deal_dedupe_key(deal)
            if key in seen:
                continue
            seen.add(key)
            merged.append(deal)
        t = t_end

    if chunks > 1 and log_chunk_merges:
        logger.info(
            "MT5 history fetch used %s chunks (%s day window); merged %s unique deals",
            chunks,
            chunk_days,
            len(merged),
        )
    return merged, chunks


def _worrisome_skip_total(skip_reasons):
    if not skip_reasons:
        return 0
    return (
        int(skip_reasons.get("close_only_without_existing_open") or 0)
        + int(skip_reasons.get("batch_validation_skipped") or 0)
        + int(skip_reasons.get("batch_symbol_validation_failed") or 0)
        + int(skip_reasons.get("incoming_close_validation_failed") or 0)
    )


def _mt5_sync_idle_noop(result):
    skip_reasons = result.get("skip_reasons") or {}
    skipped_n = int(result.get("skipped") or 0)
    saved_n = int(result.get("saved") or 0)
    updated_n = int(result.get("updated") or 0)
    errors_n = int(result.get("errors") or 0)
    worrisome = _worrisome_skip_total(skip_reasons)
    return (
        skipped_n > 0
        and saved_n == 0
        and updated_n == 0
        and errors_n == 0
        and worrisome == 0
        and bool(skip_reasons)
    )


def _log_mt5_sync_api_payload(
    mt5_account_id, task_id, trigger_label, result, *, detail="full"
):
    """Emit one JSON line; detail=compact omits skip_reasons, detail=skip disables."""
    if detail == "skip":
        return
    try:
        payload = {
            "kind": "mt5_sync_api_result",
            "mt5_account_id": mt5_account_id,
            "task_id": task_id,
            "trigger": trigger_label,
            "saved": result.get("saved"),
            "updated": result.get("updated"),
            "skipped": result.get("skipped"),
            "errors": result.get("errors"),
        }
        if detail == "full":
            payload["skip_reasons"] = result.get("skip_reasons")
            iv = result.get("insert_validation_reasons")
            if iv:
                payload["insert_validation_reasons"] = iv
            if result.get("timestamp_refreshes") is not None:
                payload["timestamp_refreshes"] = result.get("timestamp_refreshes")
        line = json.dumps(payload, default=str, ensure_ascii=True)
        if int(result.get("errors") or 0) > 0:
            logger.warning("MT5 sync API JSON %s", line)
        else:
            logger.info("MT5 sync API JSON %s", line)
    except Exception:
        logger.exception("MT5 sync API JSON log failed mt5_account_id=%s", mt5_account_id)


@celery.task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def sync_mt5_account(
    self,
    mt5_account_id,
    full_history=False,
    trigger_source="unknown",
    recalibrate_trade_timestamps=False,
):
    from celery_workers.cache import (
        CacheUnavailableError,
        claim_lock,
        release_lock,
        set_worker_state,
    )

    task_id = getattr(getattr(self, "request", None), "id", None)
    lock_token = uuid.uuid4().hex
    lock_acquired = False
    lock_enabled = True
    user_id = None
    trade_account_id = None
    account_suffix = "unknown"
    trade_account_name = None
    is_first_sync = None
    from_date = None
    to_date = None
    mt5_from_date = None
    mt5_to_date = None
    vm_timing_context = {}
    deals = ()
    trades = []
    raw_deal_count = 0
    aggregated_trade_count = 0
    open_trade_count = 0
    closed_trade_count = 0
    mt5_login = None
    mt5_balance = None
    mt5_equity = None
    mt5_server_delta_minutes = 0
    applied_offset_minutes = 0
    sync_started_at = None
    sync_finished_at = None
    rolling_days = _rolling_sync_window_days()
    trigger_label = str(trigger_source or "unknown").strip() or "unknown"
    history_chunks_fetched = 0
    sync_mode = None
    latest_deal_utc = "-"
    latest_deal_position = "-"
    latest_deal_lag_minutes = None
    open_position_count = 0
    open_position_ids_preview = "-"
    db_open_mt5_count = None
    history_stale = False
    mt5_soft_reconnect_done = False
    try:
        try:
            lock_acquired = claim_lock(_sync_lock_key(mt5_account_id), lock_token, ttl=600)
        except CacheUnavailableError:
            lock_enabled = False
            lock_acquired = True

        if not lock_acquired:
            log_ascii_table(
                logger,
                "MT5 Sync Skipped",
                [
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("Reason", "sync already running"),
                ],
            )
            return {"skipped": "sync already running"}

        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 not installed on this worker.") from exc

        from helpers.utils import decrypt_password
        from models import MT5Account, db

        account = db.session.get(MT5Account, mt5_account_id)
        if account is None or not account.is_active:
            log_ascii_table(
                logger,
                "MT5 Sync Skipped",
                [
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("Reason", "account missing or inactive"),
                ],
                level=logging.WARNING,
            )
            return {"error": "MT5Account not found or inactive"}
        if account.is_orphaned:
            log_ascii_table(
                logger,
                "MT5 Sync Skipped",
                [
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("User ID", account.user_id),
                    ("Trade Account ID", account.trade_account_id),
                    ("Reason", "account is orphaned"),
                ],
                level=logging.WARNING,
            )
            return {"error": "MT5Account is orphaned"}

        user_id = account.user_id
        trade_account_id = account.trade_account_id
        trade_account_name = getattr(account.trade_account, "name", None)
        investor_password = decrypt_password(account.investor_password_encrypted)
        account_number = account.account_number
        account_suffix = _mask_account_number_for_log(account_number)
        server = account.server
        terminal_path = account.terminal_path
        is_first_sync = account.last_synced_at is None
        sync_mode = "full_history" if (full_history or is_first_sync) else f"rolling_{rolling_days}d"
        base_verbose = bool(full_history) or bool(is_first_sync) or bool(recalibrate_trade_timestamps)
        verbose_mt5_sync_logs = base_verbose or (
            os.getenv("FXJ_MT5_VERBOSE_SYNC_LOGS", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        sync_started_at = datetime.now(timezone.utc)
        vm_id = get_vm_id(os.getenv("COMPUTERNAME", "").strip())

        init_kwargs = {}
        if terminal_path:
            init_kwargs["path"] = terminal_path

        with _MT5_API_SESSION_LOCK:
            if not mt5.initialize(**init_kwargs):
                raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

            try:
                expected_login = int(account_number)
                if not mt5.login(expected_login, password=investor_password, server=server):
                    raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

                account_info = mt5.account_info()
                if account_info is None:
                    raise RuntimeError(f"MT5 account_info returned None: {mt5.last_error()}")

                actual_login = getattr(account_info, "login", None)
                mt5_login = actual_login
                mt5_balance = getattr(account_info, "balance", None)
                mt5_equity = getattr(account_info, "equity", None)
                if actual_login != expected_login:
                    raise RuntimeError(
                        f"Wrong MT5 account logged in during sync: expected {expected_login}, got {actual_login}"
                    )

                def _load_broker_snapshot():
                    nonlocal from_date, to_date, mt5_from_date, mt5_to_date
                    nonlocal mt5_server_delta_minutes, applied_offset_minutes
                    nonlocal vm_timing_context
                    nonlocal deals, history_chunks_fetched, raw_deal_count
                    nonlocal latest_deal_utc, latest_deal_position
                    nonlocal trades, open_position_count, open_position_ids_preview

                    if full_history or is_first_sync:
                        from_date = datetime(2000, 1, 1, tzinfo=timezone.utc)
                    else:
                        from_date = datetime.now(timezone.utc) - timedelta(days=rolling_days)
                    to_date = datetime.now(timezone.utc)
                    mt5_server_delta_minutes = _resolve_mt5_server_offset_minutes(
                        mt5, mt5_account_id
                    )
                    applied_offset_minutes = mt5_server_delta_minutes
                    mt5_from_date = from_date
                    mt5_to_date = to_date
                    # EXPERIMENT: optionally shift end_time by broker offset
                    # to test whether timezone mismatch causes recent deals to be excluded.
                    # Enable with FXJ_MT5_DEBUG_BROKER_OFFSET_END_TIME=1. Remove when done.
                    _utc_to_date = to_date  # always keep original UTC for logging
                    if _debug_use_broker_offset_end_time() and mt5_server_delta_minutes:
                        mt5_to_date = to_date + timedelta(minutes=mt5_server_delta_minutes)
                    vm_timing_context = _vm_timezone_context()
                    vm_timing_context.update(
                        {
                            "reference_time_source": "tick_probe_cached",
                            "ntp_server_used": None,
                            "ntp_vm_skew_seconds": None,
                        }
                    )
                    # One summary table is logged after the internal API returns (see below).
                    # Use UTC-aware range boundaries directly.
                    deals, history_chunks_fetched = _chunked_history_deals_get(
                        mt5,
                        mt5_from_date,
                        mt5_to_date,
                        log_chunk_merges=verbose_mt5_sync_logs,
                    )
                    raw_deal_count = len(deals)
                    latest_deal_utc, latest_deal_position = _latest_deal_context(
                        deals,
                        offset_minutes=applied_offset_minutes,
                    )

                    deal_type_buy = getattr(mt5, "DEAL_TYPE_BUY", 0)
                    trades = aggregate_deals_to_trades(
                        deals,
                        entry_in=getattr(mt5, "DEAL_ENTRY_IN", 0),
                        entry_out=getattr(mt5, "DEAL_ENTRY_OUT", 1),
                        extra_exit_entries=(
                            getattr(mt5, "DEAL_ENTRY_INOUT", None),
                            getattr(mt5, "DEAL_ENTRY_OUT_BY", None),
                        ),
                        deal_type_buy=deal_type_buy,
                        offset_minutes=applied_offset_minutes,
                    )

                    # Supplement with currently open positions directly from the broker.
                    # positions_get() is authoritative for running trades regardless of
                    # when they were opened, so it catches trades that fall outside the
                    # history_deals_get window or whose entry deals are filtered out.
                    open_positions = mt5.positions_get() or []
                    open_position_count = len(open_positions)
                    open_position_ids_preview = _open_position_ids_preview(open_positions)
                    position_trades = _positions_to_open_trades(
                        open_positions,
                        position_type_buy=deal_type_buy,
                        offset_minutes=applied_offset_minutes,
                    )
                    deals_positions = {t["mt5_position"] for t in trades if t.get("mt5_position")}
                    trades = trades + [
                        t for t in position_trades if t.get("mt5_position") not in deals_positions
                    ]

                    # DEBUG: raw snapshot log — no-op unless _MT5_DEBUG_TARGET_ACCOUNT_ID is set
                    if (
                        _MT5_DEBUG_TARGET_ACCOUNT_ID is not None
                        and trade_account_id == _MT5_DEBUG_TARGET_ACCOUNT_ID
                    ):
                        _mt5_debug_log_raw_snapshot(
                            mt5_account_id=mt5_account_id,
                            trade_account_id=trade_account_id,
                            open_positions=open_positions,
                            deals=deals,
                            latest_deal_utc=latest_deal_utc,
                            now_utc=datetime.now(timezone.utc),
                            mt5=mt5,
                            from_date=mt5_from_date,
                            to_date=mt5_to_date,
                            utc_to_date=_utc_to_date,
                        )

                _load_broker_snapshot()
                now_probe = datetime.now(timezone.utc)
                if _mt5_soft_reconnect_on_stale_enabled() and _precheck_mt5_history_stale_vs_db(
                    open_position_count=open_position_count,
                    latest_deal_utc=latest_deal_utc,
                    user_id=user_id,
                    trade_account_id=trade_account_id,
                    now_utc=now_probe,
                ):
                    logger.warning(
                        "MT5 sync soft reconnect mt5_account_id=%s trade_account_id=%s login=%s "
                        "reason=broker_flat_history_lagging_db_open_mt5",
                        mt5_account_id,
                        trade_account_id,
                        mt5_login,
                    )
                    mt5.shutdown()
                    _wait = _stale_reconnect_wait_seconds()
                    if _wait > 0:
                        logger.info(
                            "MT5 stale reconnect: waiting %ss for terminal to refresh history "
                            "mt5_account_id=%s",
                            _wait,
                            mt5_account_id,
                        )
                        time.sleep(_wait)
                    if not mt5.initialize(**init_kwargs):
                        raise RuntimeError(
                            f"MT5 init failed after soft reconnect: {mt5.last_error()}"
                        )
                    if not mt5.login(expected_login, password=investor_password, server=server):
                        raise RuntimeError(
                            f"MT5 login failed after soft reconnect: {mt5.last_error()}"
                        )
                    account_info = mt5.account_info()
                    if account_info is None:
                        raise RuntimeError(
                            "MT5 account_info returned None after soft reconnect: "
                            f"{mt5.last_error()}"
                        )
                    actual_login = getattr(account_info, "login", None)
                    if actual_login != expected_login:
                        raise RuntimeError(
                            "Wrong MT5 account logged in during sync after soft reconnect: "
                            f"expected {expected_login}, got {actual_login}"
                        )
                    mt5_balance = getattr(account_info, "balance", None)
                    mt5_equity = getattr(account_info, "equity", None)
                    mt5_soft_reconnect_done = True
                    _load_broker_snapshot()
            finally:
                mt5.shutdown()

        aggregated_trade_count = len(trades)
        open_trade_count, closed_trade_count = _summarize_trade_states(trades)

        base_url = os.environ.get("FLASK_API_URL", "https://myfxjournal.com").strip() or "https://myfxjournal.com"
        sync_secret = os.environ.get("MT5_SYNC_SECRET", "").strip()
        if not sync_secret:
            raise RuntimeError("MT5_SYNC_SECRET is required for MT5 sync.")

        sync_payload = {
            "mt5_account_id": mt5_account_id,
            "vm_id": vm_id,
            "trades": trades,
            "timing_context": vm_timing_context,
            "mt5_server_delta_minutes": mt5_server_delta_minutes,
            "applied_time_offset_minutes": applied_offset_minutes,
            "include_skip_reasons": True,
            "skip_debug_mode": "full" if verbose_mt5_sync_logs else "worrisome",
        }
        if mt5_equity is not None:
            try:
                sync_payload["broker_equity"] = float(mt5_equity)
            except (TypeError, ValueError):
                pass
        if mt5_balance is not None:
            try:
                sync_payload["broker_balance"] = float(mt5_balance)
            except (TypeError, ValueError):
                pass
        if recalibrate_trade_timestamps:
            sync_payload["refresh_closed_trade_timestamps"] = True
        response = requests.post(
            f"{base_url}/api/internal/mt5/sync",
            json=sync_payload,
            headers={
                "X-Sync-Secret": sync_secret,
                "Content-Type": "application/json",
            },
            timeout=int(os.environ.get("FXJ_MT5_SYNC_HTTP_TIMEOUT_SECONDS", "180")),
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            from celery_workers.worker_diagnostics import log_requests_http_error

            log_requests_http_error(
                logger,
                exc,
                title="MT5 Sync HTTP Error (copy for support)",
                rows=[
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("POST URL", f"{base_url}/api/internal/mt5/sync"),
                    (
                        "Timeout seconds",
                        os.environ.get("FXJ_MT5_SYNC_HTTP_TIMEOUT_SECONDS", "180"),
                    ),
                ],
            )
            raise
        result = response.json()
        sync_finished_at = datetime.now(timezone.utc)
        skipped_n = int(result.get("skipped") or 0)
        saved_n = int(result.get("saved") or 0)
        updated_n = int(result.get("updated") or 0)
        errors_n = int(result.get("errors") or 0)
        latest_deal_lag_minutes = _lag_minutes_from_utc_iso(
            latest_deal_utc,
            now=sync_finished_at,
        )
        if open_position_count == 0 and (
            latest_deal_lag_minutes is None
            or latest_deal_lag_minutes >= _history_stale_threshold_minutes()
        ):
            db.session.expire_all()
            db_open_mt5_count = _count_open_mt5_db_trades(
                user_id=user_id,
                trade_account_id=trade_account_id,
            )
            history_stale = db_open_mt5_count > 0
        skip_reasons = result.get("skip_reasons") or {}
        worrisome_skip_n = _worrisome_skip_total(skip_reasons)
        # Beat/incremental runs often re-post the same closed positions; every row
        # skips with existing_already_closed_or_no_state_change — not an error.
        idle_noop = _mt5_sync_idle_noop(result)
        worrisome_no_save = (
            skipped_n > 0 and saved_n == 0 and updated_n == 0 and not idle_noop
        )
        skip_debug_rows = result.get("skip_debug") or []
        try:
            set_worker_state(
                "mt5_sync_diag",
                str(mt5_account_id),
                {
                    "mt5_account_id": mt5_account_id,
                    "trade_account_id": trade_account_id,
                    "latest_deal_utc": latest_deal_utc,
                    "latest_deal_position": latest_deal_position,
                    "latest_deal_lag_minutes": latest_deal_lag_minutes,
                    "open_positions": open_position_count,
                    "open_position_ids": open_position_ids_preview,
                    "db_open_mt5_count": db_open_mt5_count,
                    "history_stale": history_stale,
                    "soft_reconnect": mt5_soft_reconnect_done,
                    "updated_at": sync_finished_at,
                },
            )
        except CacheUnavailableError:
            pass
        except Exception as exc:
            logger.debug(
                "MT5 sync diag state update failed mt5_account_id=%s: %s",
                mt5_account_id,
                exc,
            )

        want_full_worker_log = (
            verbose_mt5_sync_logs
            or errors_n > 0
            or worrisome_skip_n > 0
            or worrisome_no_save
        )

        if want_full_worker_log:
            summary_rows = [
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Trade Account ID", trade_account_id),
                ("Started", sync_started_at),
                ("Trade Account", f"{trade_account_name} [ID: {trade_account_id}]"),
                ("MT5 Account", f"DB {mt5_account_id} / Login {mt5_login}"),
                ("Server", server),
                ("Balance", mt5_balance),
                ("Equity", mt5_equity),
                ("Trigger", trigger_label),
                ("Mode", sync_mode),
                ("VM ID", vm_id),
                ("VM Timezone", vm_timing_context.get("vm_timezone_name")),
                ("VM UTC Offset (min)", vm_timing_context.get("vm_utc_offset_minutes")),
                ("Reference Time Source", vm_timing_context.get("reference_time_source")),
                ("NTP Server", vm_timing_context.get("ntp_server_used")),
                ("NTP vs VM Skew (s)", vm_timing_context.get("ntp_vm_skew_seconds")),
                ("MT5-UTC Delta (min)", mt5_server_delta_minutes),
                ("Applied Time Offset (min)", applied_offset_minutes),
                ("Window (UTC)", f"{from_date.isoformat()} -> {to_date.isoformat()}"),
                ("MT5 Request Window", f"{mt5_from_date.isoformat()} -> {mt5_to_date.isoformat()}"),
                ("History chunk days", _history_chunk_days()),
                ("History API Chunks", history_chunks_fetched),
                ("Raw Deals", raw_deal_count),
                ("Latest Deal (UTC)", latest_deal_utc),
                ("Latest Deal Position", latest_deal_position),
                ("Latest Deal Lag (min)", latest_deal_lag_minutes),
                ("Trade Rows", aggregated_trade_count),
                ("Open Rows", open_trade_count),
                ("Closed Rows", closed_trade_count),
                ("Open Positions", open_position_count),
                ("Open Position IDs", open_position_ids_preview),
                ("DB Open MT5 Trades", db_open_mt5_count),
                ("History Stale", history_stale),
                ("Soft reconnect", mt5_soft_reconnect_done),
                ("Finished", sync_finished_at),
                ("Duration", duration_label(sync_started_at, sync_finished_at)),
                ("Saved New", result.get("saved")),
                ("Updated", result.get("updated")),
                ("Skipped", result.get("skipped")),
                ("Errors", result.get("errors")),
                ("Timestamp Refreshes", result.get("timestamp_refreshes")),
                ("Skip Reasons", result.get("skip_reasons")),
            ]
            if idle_noop:
                summary_rows.append(
                    (
                        "Note",
                        f"idle noop — {skipped_n} broker row(s) matched DB (benign)",
                    )
                )
                if history_stale:
                    summary_rows.append(
                        (
                            "Alert",
                            "broker positions are flat, DB still has MT5 trades open, and deal history is lagging",
                        )
                    )
            elif worrisome_no_save:
                summary_rows.extend(
                    [
                        (
                            "Alert",
                            "no saves/updates — review Skip Reasons; full skip_debug on next log line",
                        ),
                        ("Worrisome skip rows (sum)", worrisome_skip_n),
                    ]
                )
                if skip_debug_rows:
                    summary_rows.append(("Skip debug rows (count)", len(skip_debug_rows)))

            log_ascii_table(
                logger,
                "MT5 Sync",
                summary_rows,
                level=logging.WARNING if (worrisome_no_save or history_stale) else logging.INFO,
            )
            if worrisome_no_save and skip_debug_rows:
                try:
                    debug_blob = json.dumps(
                        skip_debug_rows, default=str, ensure_ascii=True
                    )
                except TypeError:
                    debug_blob = str(skip_debug_rows)
                raw_max = os.environ.get("FXJ_MT5_SKIP_DEBUG_LOG_MAX_CHARS", "16000").strip()
                if raw_max.lower() in {"0", "full", "none", "unlimited"}:
                    max_chars = None
                else:
                    try:
                        max_chars = int(raw_max)
                    except ValueError:
                        max_chars = 16000
                    max_chars = max(max_chars, 256)
                total_len = len(debug_blob)
                if max_chars is not None and total_len > max_chars:
                    debug_blob = (
                        f"{debug_blob[:max_chars]}... [truncated {total_len - max_chars} chars; "
                        "set FXJ_MT5_SKIP_DEBUG_LOG_MAX_CHARS=0 on VM for full JSON]"
                    )
                logger.warning(
                    "MT5 sync skip_debug mt5_account_id=%s task_id=%s %s",
                    mt5_account_id,
                    task_id,
                    debug_blob,
                )
            _log_mt5_sync_api_payload(
                mt5_account_id, task_id, trigger_label, result, detail="full"
            )
        elif idle_noop:
            logger.log(
                logging.WARNING if history_stale else logging.INFO,
                "MT5 sync noop mt5_account_id=%s trade_account_id=%s login=%s server=%s "
                "trigger=%s skipped=%s latest_deal_utc=%s latest_deal_position=%s "
                "open_positions=%s open_position_ids=%s latest_deal_lag_min=%s "
                "db_open_mt5=%s history_stale=%s soft_reconnect=%s duration=%s mode=%s vm_id=%s",
                mt5_account_id,
                trade_account_id,
                mt5_login,
                server,
                trigger_label,
                skipped_n,
                latest_deal_utc,
                latest_deal_position,
                open_position_count,
                open_position_ids_preview,
                latest_deal_lag_minutes if latest_deal_lag_minutes is not None else "-",
                db_open_mt5_count if db_open_mt5_count is not None else "-",
                1 if history_stale else 0,
                1 if mt5_soft_reconnect_done else 0,
                duration_label(sync_started_at, sync_finished_at),
                sync_mode,
                vm_id,
            )
            _log_mt5_sync_api_payload(
                mt5_account_id, task_id, trigger_label, result, detail="skip"
            )
        else:
            tsr = result.get("timestamp_refreshes")
            logger.log(
                logging.WARNING if history_stale else logging.INFO,
                "MT5 sync mt5_account_id=%s trade_account_id=%s login=%s trigger=%s "
                "saved=%s updated=%s skipped=%s errors=%s timestamp_refreshes=%s "
                "latest_deal_utc=%s latest_deal_position=%s open_positions=%s "
                "open_position_ids=%s latest_deal_lag_min=%s db_open_mt5=%s "
                "history_stale=%s soft_reconnect=%s duration=%s mode=%s vm_id=%s",
                mt5_account_id,
                trade_account_id,
                mt5_login,
                trigger_label,
                saved_n,
                updated_n,
                skipped_n,
                errors_n,
                tsr if tsr is not None else "-",
                latest_deal_utc,
                latest_deal_position,
                open_position_count,
                open_position_ids_preview,
                latest_deal_lag_minutes if latest_deal_lag_minutes is not None else "-",
                db_open_mt5_count if db_open_mt5_count is not None else "-",
                1 if history_stale else 0,
                1 if mt5_soft_reconnect_done else 0,
                duration_label(sync_started_at, sync_finished_at),
                sync_mode,
                vm_id,
            )
            _log_mt5_sync_api_payload(
                mt5_account_id, task_id, trigger_label, result, detail="compact"
            )
        return result
    except Exception as exc:
        sync_finished_at = sync_finished_at or datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "MT5 Sync Failed",
            [
                ("Finished", sync_finished_at),
                ("Duration", duration_label(sync_started_at, sync_finished_at)),
                ("Trigger", trigger_label),
                ("Mode", sync_mode or "unknown"),
                ("VM ID", vm_id if "vm_id" in locals() else "unknown"),
                ("Trade Account", f"{trade_account_name} [ID: {trade_account_id}]"),
                ("MT5 Account", f"DB {mt5_account_id} / Login {mt5_login or account_suffix}"),
                ("Raw Deals", raw_deal_count),
                ("Trade Rows", aggregated_trade_count),
                ("Error", exc),
            ],
        )
        logger.exception(
            "MT5 sync failed and will retry. task_id=%s mt5_account_id=%s user_id=%s trade_account_id=%s account_suffix=%s raw_deals=%s aggregated_trades=%s vm_id=%s",
            task_id,
            mt5_account_id,
            user_id,
            trade_account_id,
            account_suffix,
            raw_deal_count,
            aggregated_trade_count,
            vm_id if "vm_id" in locals() else "unknown",
            exc_info=exc,
        )
        _retry_with_backoff(self, exc, base_delay=30, max_delay=300)
    finally:
        if lock_enabled and lock_acquired:
            try:
                release_lock(_sync_lock_key(mt5_account_id), lock_token)
            except CacheUnavailableError:
                pass


@celery.task(
    bind=True,
    max_retries=1,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def fetch_trade_bars(self, mt5_account_id, trade_id):
    """Fetch OHLC bar data for a single closed trade and store via internal API."""
    from datetime import timedelta
    task_id = getattr(getattr(self, "request", None), "id", None)
    fetch_started_at = None
    fetch_finished_at = None
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 not installed on this worker.") from exc

    from helpers.utils import decrypt_password
    from models import MT5Account, Trade, db
    from trading import (
        cfd_mt5_symbol_name_candidates,
        chart_timeframe_bar_seconds,
        mt5_timeframe_constant,
        normalize_account_type,
    )

    fetch_started_at = datetime.now(timezone.utc)

    account = db.session.get(MT5Account, mt5_account_id)
    if account is None or not account.is_active or account.is_orphaned:
        log_ascii_table(
            logger,
            "Bar Fetch Skipped",
            [
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Trade ID", trade_id),
                ("Reason", "account missing, inactive, or orphaned"),
            ],
            level=logging.WARNING,
        )
        return {"skipped": "account unavailable"}

    trade = db.session.get(Trade, trade_id)
    if trade is None or trade.closed_at is None:
        log_ascii_table(
            logger,
            "Bar Fetch Skipped",
            [
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Trade ID", trade_id),
                ("Reason", "trade not found or not closed"),
            ],
            level=logging.WARNING,
        )
        return {"skipped": "trade not closed"}

    symbol = trade.symbol
    trade_account = account.trade_account
    account_type = normalize_account_type(
        getattr(trade_account, "account_type", "CFD") if trade_account else "CFD"
    )
    mt5_symbol_names = (
        cfd_mt5_symbol_name_candidates(symbol)
        if account_type == "CFD"
        else (symbol,)
    )
    opened_at_utc = _naive_utc_to_aware(trade.opened_at)
    closed_at_utc = _naive_utc_to_aware(trade.closed_at)
    # Always fetch M5; the window must be expressed in M5 bars. Using
    # select_chart_timeframe bar size here capped short trades at ~20×5m (~100m)
    # before entry — not enough context to see a typical setup.
    m5_seconds = chart_timeframe_bar_seconds("M5")
    # Wider window for chart context (esp. structure before entry). ~3× prior pre-window, 2× post.
    pre_entry_m5_bars = 432  # 36h of M5 before open (was 144 / 12h)
    post_exit_m5_bars = 144  # 12h of M5 after close (was 72 / 6h)
    now_utc = datetime.now(timezone.utc)

    investor_password = decrypt_password(account.investor_password_encrypted)
    account_number = account.account_number
    server = account.server
    terminal_path = account.terminal_path
    init_kwargs = {}
    if terminal_path:
        init_kwargs["path"] = terminal_path

    # Store M5 only; M15 (and other higher TFs) are derived in the API via OHLC aggregation.
    # copy_rates_range: naive datetimes are interpreted as *local VM time*, not UTC — use aware UTC
    # so the requested window matches trade.opened_at / closed_at (naive UTC in DB).
    start_dt = opened_at_utc - timedelta(seconds=pre_entry_m5_bars * m5_seconds)
    end_dt = closed_at_utc + timedelta(seconds=post_exit_m5_bars * m5_seconds)
    if end_dt > now_utc:
        end_dt = now_utc

    bars = []
    mt5_server_delta_minutes = 0
    with _MT5_API_SESSION_LOCK:
        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(f"MT5 init failed during bar fetch: {mt5.last_error()}")
        try:
            if not mt5.login(int(account_number), password=investor_password, server=server):
                raise RuntimeError(f"MT5 login failed during bar fetch: {mt5.last_error()}")

            mt5_server_delta_minutes = _resolve_mt5_server_offset_minutes(
                mt5, mt5_account_id, preferred_symbol=mt5_symbol_names
            )
            start_dt_shifted = _shift_datetime_by_minutes(start_dt, minutes=mt5_server_delta_minutes)
            end_dt_shifted = _shift_datetime_by_minutes(end_dt, minutes=mt5_server_delta_minutes)
            tf_constant = mt5_timeframe_constant("M5", mt5)

            def _first_rates(window_from, window_to):
                for sym in mt5_symbol_names:
                    chunk = mt5.copy_rates_range(sym, tf_constant, window_from, window_to)
                    if chunk is not None and len(chunk) > 0:
                        return sym, chunk
                return None, None

            bar_normalization_offset = 0
            fetch_strategy = "none"
            sym_used, raw_bars = _first_rates(start_dt_shifted, end_dt_shifted)
            if raw_bars is not None:
                symbol = sym_used
                bar_normalization_offset = mt5_server_delta_minutes
                fetch_strategy = "shifted"
            else:
                # Broker/API mismatch: some servers return empty for shifted windows but
                # accept UTC-aware boundaries (MetaQuotes Python docs). Retry without shift.
                sym_used, raw_bars = _first_rates(start_dt, end_dt)
                if raw_bars is not None:
                    symbol = sym_used
                    bar_normalization_offset = 0
                    fetch_strategy = "utc_fallback"

            if raw_bars is None:
                logger.warning(
                    "Bar fetch no rates mt5_account_id=%s trade_id=%s trade_symbol=%s "
                    "candidates=%s delta_min=%s window_shifted=%s..%s window_utc=%s..%s last_error=%s",
                    mt5_account_id,
                    trade_id,
                    trade.symbol,
                    list(mt5_symbol_names),
                    mt5_server_delta_minutes,
                    start_dt_shifted.isoformat(),
                    end_dt_shifted.isoformat(),
                    start_dt.isoformat(),
                    end_dt.isoformat(),
                    mt5.last_error(),
                )
            else:
                for bar in raw_bars:
                    bar_open_utc = _adjust_mt5_unix_epoch(
                        int(bar["time"]),
                        offset_minutes=bar_normalization_offset,
                    )
                    bars.append({
                        "time": int(bar_open_utc),
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "tick_volume": int(bar["tick_volume"]) if bar["tick_volume"] is not None else None,
                    })
        finally:
            mt5.shutdown()

    fetch_finished_at = datetime.now(timezone.utc)
    log_ascii_table(
        logger,
        "Bar Fetch Result",
        [
            ("Task ID", task_id),
            ("MT5 Account ID", mt5_account_id),
            ("Trade ID", trade_id),
            ("Symbol", symbol),
            ("M5 context", f"{pre_entry_m5_bars} pre / {post_exit_m5_bars} post bars"),
            ("Stored TF", "M5"),
            ("Bars Fetched", len(bars)),
            ("Fetch strategy", fetch_strategy if fetch_strategy != "none" else "none (no rates)"),
            ("MT5-UTC Delta (min)", mt5_server_delta_minutes),
            ("Window (UTC)", f"{start_dt.isoformat()} -> {end_dt.isoformat()}"),
            ("Duration", duration_label(fetch_started_at, fetch_finished_at)),
        ],
    )

    base_url = os.environ.get("FLASK_API_URL", "https://myfxjournal.com").strip() or "https://myfxjournal.com"
    sync_secret = os.environ.get("MT5_SYNC_SECRET", "").strip()
    if not sync_secret:
        raise RuntimeError("MT5_SYNC_SECRET is required for bar fetch.")

    if not bars:
        return {"saved": 0, "timeframe": "M5"}

    response = requests.post(
        f"{base_url}/api/internal/mt5/trade-bars",
        json={
            "mt5_account_id": mt5_account_id,
            "trade_id": trade_id,
            "timeframe": "M5",
            "bars": bars,
        },
        headers={
            "X-Sync-Secret": sync_secret,
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        from celery_workers.worker_diagnostics import log_requests_http_error

        log_requests_http_error(
            logger,
            exc,
            title="MT5 Trade Bars HTTP Error (copy for support)",
            rows=[
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Trade ID", trade_id),
                ("Bars in payload", len(bars)),
                ("POST URL", f"{base_url}/api/internal/mt5/trade-bars"),
            ],
        )
        raise
    return response.json()


@celery.task
def sync_all_active_mt5_accounts():
    from models import MT5Account

    from celery_workers.cache import CacheUnavailableError, get_queue_depth

    # Queue depth gate: if the mt5_sync queue is already backed up beyond
    # one full beat's worth of tasks, the VM worker is not draining — skip
    # this beat entirely rather than piling on more tasks.
    _MAX_QUEUE_DEPTH = 150
    try:
        depth = get_queue_depth("mt5_sync")
        if depth > _MAX_QUEUE_DEPTH:
            logger.warning(
                "mt5_sync queue depth %s exceeds %s — skipping beat to avoid buildup",
                depth,
                _MAX_QUEUE_DEPTH,
            )
            return
    except CacheUnavailableError:
        pass  # Redis unavailable: proceed and let the task-level expiry handle it

    accounts = (
        MT5Account.query.filter(
            MT5Account.is_active.is_(True),
            MT5Account.user_id.isnot(None),
            MT5Account.trade_account_id.isnot(None),
        ).all()
    )
    if not accounts:
        return
    for account in accounts:
        sync_mt5_account.apply_async(
            args=[account.id],
            kwargs={"trigger_source": "beat"},
            queue="mt5_sync",
            # Tasks not picked up before the next beat fires are stale — discard
            # them so a recovering worker always starts with fresh work only.
            expires=28,
        )
