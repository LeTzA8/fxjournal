import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import uuid

try:
    import ntplib
except Exception:  # pragma: no cover - fallback path is exercised when unavailable.
    ntplib = None

import requests

from celery_app import celery
from celery_workers.logging_utils import duration_label, log_ascii_table
from trading import MT5_DEFAULT_SOURCE_TIMEZONE_NAME

logger = logging.getLogger(__name__)


# MetaTrader5 keeps process-global session state, so sync tasks must not
# overlap MT5 API calls inside the same worker process.
_MT5_API_SESSION_LOCK = threading.Lock()
_NTP_CACHE_LOCK = threading.Lock()
_NTP_REFERENCE_CACHE = {
    "expires_at": 0.0,
    "unix_seconds": None,
    "source": "vm_fallback",
    "server": None,
    "vm_skew_seconds": None,
}


def _retry_with_backoff(task, exc, *, base_delay=30, max_delay=300):
    retry_number = getattr(getattr(task, "request", None), "retries", 0)
    countdown = min(base_delay * (2 ** retry_number), max_delay)
    raise task.retry(exc=exc, countdown=countdown)


def _adjust_mt5_unix_epoch(timestamp_value, *, offset_minutes=0):
    """Subtract broker/server-ahead delta from raw MT5 Unix epoch (deal times)."""
    if timestamp_value is None:
        return None
    return float(timestamp_value) - (int(offset_minutes or 0) * 60)


def _to_utc_iso(timestamp_value, *, offset_minutes=0):
    adjusted_timestamp = _adjust_mt5_unix_epoch(timestamp_value, offset_minutes=offset_minutes)
    if adjusted_timestamp is None:
        return None
    return datetime.fromtimestamp(adjusted_timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _shift_datetime_by_minutes(value, *, minutes=0):
    if value is None:
        return None
    return value + timedelta(minutes=int(minutes or 0))


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


def _ntp_servers():
    raw = os.environ.get("FXJ_NTP_SERVERS", "").strip()
    if not raw:
        return ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]
    servers = [part.strip() for part in raw.split(",") if part.strip()]
    return servers or ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]


def _reference_utc_unix_seconds():
    vm_utc_now = datetime.now(timezone.utc).timestamp()
    reference_mode = str(os.environ.get("FXJ_MT5_TIME_REFERENCE", "ntp")).strip().lower()
    cache_seconds_raw = os.environ.get("FXJ_NTP_CACHE_SECONDS", "45").strip()
    timeout_raw = os.environ.get("FXJ_NTP_TIMEOUT_SECONDS", "2.5").strip()
    try:
        cache_seconds = max(float(cache_seconds_raw), 0.0)
    except (TypeError, ValueError):
        cache_seconds = 45.0
    try:
        timeout_seconds = max(float(timeout_raw), 0.25)
    except (TypeError, ValueError):
        timeout_seconds = 2.5

    with _NTP_CACHE_LOCK:
        now_ts = datetime.now(timezone.utc).timestamp()
        cached_value = _NTP_REFERENCE_CACHE.get("unix_seconds")
        if (
            cache_seconds > 0
            and cached_value is not None
            and now_ts < float(_NTP_REFERENCE_CACHE.get("expires_at") or 0.0)
        ):
            return (
                float(cached_value),
                str(_NTP_REFERENCE_CACHE.get("source") or "vm_fallback"),
                _NTP_REFERENCE_CACHE.get("server"),
                _NTP_REFERENCE_CACHE.get("vm_skew_seconds"),
            )

    if reference_mode == "vm":
        return vm_utc_now, "vm_forced", None, 0.0

    if ntplib is not None:
        client = ntplib.NTPClient()
        for server in _ntp_servers():
            try:
                response = client.request(server, version=3, timeout=timeout_seconds)
                ntp_utc_now = float(getattr(response, "tx_time", None) or getattr(response, "recv_time", 0.0))
                if ntp_utc_now <= 0:
                    continue
                vm_skew_seconds = ntp_utc_now - vm_utc_now
                with _NTP_CACHE_LOCK:
                    _NTP_REFERENCE_CACHE.update(
                        {
                            "expires_at": datetime.now(timezone.utc).timestamp() + cache_seconds,
                            "unix_seconds": ntp_utc_now,
                            "source": "ntp",
                            "server": server,
                            "vm_skew_seconds": vm_skew_seconds,
                        }
                    )
                return ntp_utc_now, "ntp", server, vm_skew_seconds
            except Exception:
                continue

    with _NTP_CACHE_LOCK:
        _NTP_REFERENCE_CACHE.update(
            {
                "expires_at": datetime.now(timezone.utc).timestamp() + cache_seconds,
                "unix_seconds": vm_utc_now,
                "source": "vm_fallback",
                "server": None,
                "vm_skew_seconds": 0.0,
            }
        )
    return vm_utc_now, "vm_fallback", None, 0.0


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
                "pnl": None,
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


def _probe_mt5_server_delta_minutes(mt5, *, preferred_symbol=None, reference_utc_unix_seconds=None):
    """
    Estimate MT5 server clock drift relative to reference UTC using latest tick timestamp.
    Returns minute delta where positive means MT5 clock appears ahead of UTC.
    """
    symbol_info_tick = getattr(mt5, "symbol_info_tick", None)
    if not callable(symbol_info_tick):
        return 0
    if reference_utc_unix_seconds is None:
        reference_utc_unix_seconds, _, _, _ = _reference_utc_unix_seconds()
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
    for symbol in symbol_candidates:
        if not symbol:
            continue
        tick = symbol_info_tick(symbol)
        tick_time = getattr(tick, "time", None) if tick is not None else None
        if not tick_time:
            continue
        delta_seconds = int(tick_time) - int(reference_utc_unix_seconds)
        if abs(delta_seconds) > 6 * 3600:
            continue
        return int(round(delta_seconds / 60))
    return 0


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
)
def sync_mt5_account(
    self,
    mt5_account_id,
    full_history=False,
    trigger_source="unknown",
    recalibrate_trade_timestamps=False,
):
    from celery_workers.cache import CacheUnavailableError, claim_lock, release_lock

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

                if full_history or is_first_sync:
                    from_date = datetime(2000, 1, 1, tzinfo=timezone.utc)
                else:
                    from_date = datetime.now(timezone.utc) - timedelta(days=rolling_days)
                to_date = datetime.now(timezone.utc)
                reference_utc_unix, reference_time_source, ntp_server_used, ntp_vm_skew_seconds = (
                    _reference_utc_unix_seconds()
                )
                probe_symbol = None
                try:
                    positions_probe = mt5.positions_get() or []
                    if positions_probe:
                        probe_symbol = getattr(positions_probe[0], "symbol", None)
                except Exception:
                    probe_symbol = None
                mt5_server_delta_minutes = _probe_mt5_server_delta_minutes(
                    mt5,
                    preferred_symbol=probe_symbol,
                    reference_utc_unix_seconds=reference_utc_unix,
                )
                applied_offset_minutes = mt5_server_delta_minutes
                mt5_from_date = _shift_datetime_by_minutes(
                    from_date,
                    minutes=applied_offset_minutes,
                )
                mt5_to_date = _shift_datetime_by_minutes(
                    to_date,
                    minutes=applied_offset_minutes,
                )
                vm_timing_context = _vm_timezone_context()
                vm_timing_context.update(
                    {
                        "reference_time_source": reference_time_source,
                        "ntp_server_used": ntp_server_used,
                        "ntp_vm_skew_seconds": ntp_vm_skew_seconds,
                    }
                )
                # One summary table is logged after the internal API returns (see below).
                # MT5 history filters follow broker/server clock semantics in
                # practice, so shift the request window into server time while
                # continuing to normalize returned timestamps back to UTC.
                deals, history_chunks_fetched = _chunked_history_deals_get(
                    mt5,
                    mt5_from_date,
                    mt5_to_date,
                    log_chunk_merges=verbose_mt5_sync_logs,
                )
                raw_deal_count = len(deals)

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
                position_trades = _positions_to_open_trades(
                    open_positions,
                    position_type_buy=deal_type_buy,
                    offset_minutes=applied_offset_minutes,
                )
                deals_positions = {t["mt5_position"] for t in trades if t.get("mt5_position")}
                trades = trades + [t for t in position_trades if t.get("mt5_position") not in deals_positions]
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
        response.raise_for_status()
        result = response.json()
        sync_finished_at = datetime.now(timezone.utc)
        skipped_n = int(result.get("skipped") or 0)
        saved_n = int(result.get("saved") or 0)
        updated_n = int(result.get("updated") or 0)
        errors_n = int(result.get("errors") or 0)
        skip_reasons = result.get("skip_reasons") or {}
        worrisome_skip_n = _worrisome_skip_total(skip_reasons)
        # Beat/incremental runs often re-post the same closed positions; every row
        # skips with existing_already_closed_or_no_state_change — not an error.
        idle_noop = _mt5_sync_idle_noop(result)
        worrisome_no_save = (
            skipped_n > 0 and saved_n == 0 and updated_n == 0 and not idle_noop
        )
        skip_debug_rows = result.get("skip_debug") or []

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
                ("Trade Rows", aggregated_trade_count),
                ("Open Rows", open_trade_count),
                ("Closed Rows", closed_trade_count),
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
                level=logging.WARNING if worrisome_no_save else logging.INFO,
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
            logger.info(
                "MT5 sync noop mt5_account_id=%s trade_account_id=%s login=%s server=%s "
                "trigger=%s skipped=%s duration=%s mode=%s",
                mt5_account_id,
                trade_account_id,
                mt5_login,
                server,
                trigger_label,
                skipped_n,
                duration_label(sync_started_at, sync_finished_at),
                sync_mode,
            )
            _log_mt5_sync_api_payload(
                mt5_account_id, task_id, trigger_label, result, detail="skip"
            )
        else:
            tsr = result.get("timestamp_refreshes")
            logger.info(
                "MT5 sync mt5_account_id=%s trade_account_id=%s login=%s trigger=%s "
                "saved=%s updated=%s skipped=%s errors=%s timestamp_refreshes=%s duration=%s mode=%s",
                mt5_account_id,
                trade_account_id,
                mt5_login,
                trigger_label,
                saved_n,
                updated_n,
                skipped_n,
                errors_n,
                tsr if tsr is not None else "-",
                duration_label(sync_started_at, sync_finished_at),
                sync_mode,
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
                ("Trade Account", f"{trade_account_name} [ID: {trade_account_id}]"),
                ("MT5 Account", f"DB {mt5_account_id} / Login {mt5_login or account_suffix}"),
                ("Raw Deals", raw_deal_count),
                ("Trade Rows", aggregated_trade_count),
                ("Error", exc),
            ],
        )
        logger.exception(
            "MT5 sync failed and will retry. task_id=%s mt5_account_id=%s user_id=%s trade_account_id=%s account_suffix=%s raw_deals=%s aggregated_trades=%s",
            task_id,
            mt5_account_id,
            user_id,
            trade_account_id,
            account_suffix,
            raw_deal_count,
            aggregated_trade_count,
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

            reference_utc_unix, _, _, _ = _reference_utc_unix_seconds()
            mt5_server_delta_minutes = _probe_mt5_server_delta_minutes(
                mt5,
                preferred_symbol=mt5_symbol_names,
                reference_utc_unix_seconds=reference_utc_unix,
            )
            start_dt_shifted = _shift_datetime_by_minutes(start_dt, minutes=mt5_server_delta_minutes)
            end_dt_shifted = _shift_datetime_by_minutes(end_dt, minutes=mt5_server_delta_minutes)
            tf_constant = mt5_timeframe_constant("M5", mt5)
            raw_bars = []
            for sym in mt5_symbol_names:
                chunk = mt5.copy_rates_range(sym, tf_constant, start_dt_shifted, end_dt_shifted)
                if chunk is not None and len(chunk) > 0:
                    raw_bars = chunk
                    symbol = sym
                    break
            if not raw_bars:
                logger.warning(
                    "Bar fetch no rates mt5_account_id=%s trade_id=%s trade_symbol=%s "
                    "candidates=%s window_utc=%s..%s last_error=%s",
                    mt5_account_id,
                    trade_id,
                    trade.symbol,
                    list(mt5_symbol_names),
                    start_dt.isoformat(),
                    end_dt.isoformat(),
                    mt5.last_error(),
                )
            if raw_bars:
                for bar in raw_bars:
                    bar_open_utc = _adjust_mt5_unix_epoch(
                        int(bar["time"]),
                        offset_minutes=mt5_server_delta_minutes,
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
    response.raise_for_status()
    return response.json()


@celery.task
def sync_all_active_mt5_accounts():
    from models import MT5Account
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
        )
