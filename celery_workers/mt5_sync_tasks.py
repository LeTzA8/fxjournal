import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import threading
import uuid

import requests
from sqlalchemy import func

from celery_app import celery
from celery_workers.logging_utils import duration_label, log_ascii_table
from celery_workers.mt5_market_watch import MT5_MARKET_WATCH_CRYPTO_SEED_SYMBOLS
from helpers.trade_bars import (
    TRADE_CHART_M5_SECONDS,
    TRADE_CHART_POST_EXIT_M5_BARS,
    TRADE_CHART_PRE_ENTRY_M5_BARS,
    has_complete_m5_chart_coverage,
)
from trading import MT5_DEFAULT_SOURCE_TIMEZONE_NAME

logger = logging.getLogger(__name__)

MT5_SYNC_QUEUE_NAME = "mt5_sync"
MT5_PRIORITY_QUEUE_NAME = "mt5_priority"
MT5_SYNC_BEAT_EXPIRES_SECONDS = 28
MT5_SYNC_BEAT_MAX_QUEUE_DEPTH = 150
MT5_SERVER_OFFSET_STALE_TICK_SECONDS = 6 * 3600
MT5_SERVER_OFFSET_HOUR_TOLERANCE_SECONDS = 5 * 60

# MetaTrader5 keeps process-global session state, so sync tasks must not
# overlap MT5 API calls inside the same worker process.
_MT5_API_SESSION_LOCK = threading.Lock()


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


_PROBE_ALWAYS_ON_SYMBOLS = list(MT5_MARKET_WATCH_CRYPTO_SEED_SYMBOLS)


def _mt5_probe_symbol_candidates(*, preferred_symbol=None):
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
    return symbol_candidates


def _probe_mt5_server_delta_result(mt5, *, preferred_symbol=None, symbol_candidates=None):
    """
    Estimate MT5 server clock drift relative to UTC using latest tick timestamp.
    Returns minute delta where positive means MT5 clock appears ahead of UTC,
    plus the symbol that supplied a fresh tick.

    Symbol priority:
      1. preferred_symbol candidates (trade-specific, e.g. XAUUSD)
      2. EURUSD (liquid FX baseline; stale on weekends)
      3. Common 24/7 crypto aliases — reliable during forex market closure

    The 6-hour sanity guard filters ticks that are too stale to give a useful
    reading regardless of which symbol supplied them. The Redis cache in
    _resolve_mt5_server_offset_minutes is a final safety net for brokers that
    offer no crypto instruments at all.
    """
    symbol_info_tick = getattr(mt5, "symbol_info_tick", None)
    if not callable(symbol_info_tick):
        return {"offset_minutes": 0, "symbol": None}
    if symbol_candidates is None:
        symbol_candidates = _mt5_probe_symbol_candidates(preferred_symbol=preferred_symbol)
    for symbol in symbol_candidates:
        if not symbol:
            continue
        tick = symbol_info_tick(symbol)
        tick_time = getattr(tick, "time", None) if tick is not None else None
        if not tick_time:
            continue
        now_utc = int(datetime.now(timezone.utc).timestamp())
        delta_seconds = int(tick_time) - now_utc
        if abs(delta_seconds) > MT5_SERVER_OFFSET_STALE_TICK_SECONDS:
            continue
        nearest_hour_minutes = int(round(delta_seconds / 3600) * 60)
        nearest_hour_seconds = nearest_hour_minutes * 60
        if abs(delta_seconds - nearest_hour_seconds) > MT5_SERVER_OFFSET_HOUR_TOLERANCE_SECONDS:
            logger.warning(
                "MT5 server offset probe rejected non-hour delta symbol=%s delta_seconds=%s nearest_hour_minutes=%s",
                symbol,
                delta_seconds,
                nearest_hour_minutes,
            )
            continue
        tick_hour_remainder = int(tick_time) % 3600
        now_hour_remainder = now_utc % 3600
        remainder_distance = abs(tick_hour_remainder - now_hour_remainder)
        remainder_distance = min(remainder_distance, 3600 - remainder_distance)
        if remainder_distance > MT5_SERVER_OFFSET_HOUR_TOLERANCE_SECONDS:
            logger.warning(
                "MT5 server offset probe rejected stale minute mismatch symbol=%s remainder_distance_seconds=%s",
                symbol,
                remainder_distance,
            )
            continue
        return {"offset_minutes": nearest_hour_minutes, "symbol": symbol}
    return {"offset_minutes": 0, "symbol": None}


def _probe_mt5_server_delta_minutes(mt5, *, preferred_symbol=None):
    result = _probe_mt5_server_delta_result(mt5, preferred_symbol=preferred_symbol)
    return result["offset_minutes"]


def _seed_market_watch_probe_symbol(mt5, symbol_candidates):
    """Best-effort select one 24/7 probe alias into Market Watch."""
    symbol_select = getattr(mt5, "symbol_select", None)
    if not callable(symbol_select):
        return None
    for symbol in symbol_candidates:
        if symbol not in _PROBE_ALWAYS_ON_SYMBOLS:
            continue
        try:
            if symbol_select(symbol, True):
                return symbol
        except Exception:
            continue
    return None


def _mt5_offset_cache_key(mt5_account_id):
    return f"mt5_server_offset_minutes:{mt5_account_id}"


def _mt5_server_offset_key(server_name):
    normalized = str(server_name or "").strip().lower()
    return normalized or None


def _load_db_server_offset_minutes(server_name):
    server_key = _mt5_server_offset_key(server_name)
    if not server_key:
        return None
    try:
        from models import MT5BrokerServerOffset  # noqa: PLC0415

        row = MT5BrokerServerOffset.query.filter_by(server_key=server_key).one_or_none()
        if row is None:
            return None
        return int(row.offset_minutes)
    except Exception as exc:
        logger.warning("MT5 server offset DB read failed server=%s error=%s", server_name, exc)
        return None


def _store_db_server_offset_minutes(server_name, offset_minutes, *, probe_symbol=None):
    server_key = _mt5_server_offset_key(server_name)
    if not server_key:
        return
    try:
        from models import MT5BrokerServerOffset, db, utcnow_naive  # noqa: PLC0415

        now = utcnow_naive()
        row = MT5BrokerServerOffset.query.filter_by(server_key=server_key).one_or_none()
        if row is None:
            row = MT5BrokerServerOffset(
                server_name=str(server_name or "").strip(),
                server_key=server_key,
                offset_minutes=int(offset_minutes or 0),
                probe_symbol=probe_symbol,
                probed_at=now,
                created_at=now,
                updated_at=now,
            )
            db.session.add(row)
        else:
            row.server_name = str(server_name or "").strip() or row.server_name
            row.offset_minutes = int(offset_minutes or 0)
            row.probe_symbol = probe_symbol
            row.probed_at = now
            row.updated_at = now
        db.session.commit()
    except Exception as exc:
        try:
            from models import db  # noqa: PLC0415

            db.session.rollback()
        except Exception:
            pass
        logger.warning("MT5 server offset DB write failed server=%s error=%s", server_name, exc)


def _resolve_mt5_server_offset_minutes(mt5, mt5_account_id, *, preferred_symbol=None, server_name=None):
    """
    Return the broker UTC offset in minutes.

    During market hours the probe compares the latest tick timestamp to UTC now
    and gives an accurate reading. When market is closed the last tick is stale
    and the probe's sanity guard returns 0. We cache every non-zero measurement
    in Redis (7-day TTL) so off-hours syncs and bar-fetches still use the correct
    offset rather than falling back to 0 and storing raw broker-server-time.

    Live probe wins when available. If the broker server/feed is down, use the
    last known good server-level DB offset before falling back to the older
    per-account Redis cache and finally 0.
    """
    symbol_candidates = _mt5_probe_symbol_candidates(preferred_symbol=preferred_symbol)
    probe_result = _probe_mt5_server_delta_result(
        mt5,
        preferred_symbol=preferred_symbol,
        symbol_candidates=symbol_candidates,
    )
    probed = probe_result["offset_minutes"]
    if probe_result["symbol"] is None:
        seeded_symbol = _seed_market_watch_probe_symbol(mt5, symbol_candidates)
        if seeded_symbol:
            probe_result = _probe_mt5_server_delta_result(
                mt5,
                preferred_symbol=preferred_symbol,
                symbol_candidates=symbol_candidates,
            )
            probed = probe_result["offset_minutes"]

    if probe_result["symbol"] is not None:
        _store_db_server_offset_minutes(
            server_name,
            probed,
            probe_symbol=probe_result["symbol"],
        )
        redis_client = None
        try:
            from celery_workers.cache import _client as _cache_client  # noqa: PLC0415
            redis_client = _cache_client()
        except Exception:
            pass
        if redis_client is not None and probed != 0:
            try:
                redis_client.set(_mt5_offset_cache_key(mt5_account_id), str(probed), ex=60 * 60 * 24 * 7)
            except Exception:
                pass
        return probed

    db_offset = _load_db_server_offset_minutes(server_name)
    if db_offset is not None:
        return db_offset

    redis_client = None
    try:
        from celery_workers.cache import _client as _cache_client  # noqa: PLC0415
        redis_client = _cache_client()
    except Exception:
        pass

    cache_key = _mt5_offset_cache_key(mt5_account_id)

    if redis_client is not None:
        try:
            cached = redis_client.get(cache_key)
            if cached is not None:
                return int(cached)
        except Exception:
            pass

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


def _trade_has_complete_m5_bars_for_worker(trade, *, db, TradeBars):
    if trade is None or trade.opened_at is None or trade.closed_at is None:
        return False
    coverage = (
        db.session.query(
            func.count(TradeBars.id),
            func.min(TradeBars.bar_time),
            func.max(TradeBars.bar_time),
        )
        .filter(TradeBars.trade_id == trade.id, TradeBars.timeframe == "M5")
        .one()
    )
    bar_count = int(coverage[0] or 0)
    min_bar_time = coverage[1]
    max_bar_time = coverage[2]
    return has_complete_m5_chart_coverage(
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
        bar_count=bar_count,
        min_bar_time=min_bar_time,
        max_bar_time=max_bar_time,
    )


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
    sync_mode = "rolling_7d"
    trigger_label = str(trigger_source or "unknown").strip() or "unknown"
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
        used_full_history_window = full_history or account.last_full_history_sync_at is None
        sync_mode = "full_history" if used_full_history_window else "rolling_7d"
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

                if used_full_history_window:
                    from_date = datetime(2000, 1, 1, tzinfo=timezone.utc)
                else:
                    from_date = datetime.now(timezone.utc) - timedelta(days=7)
                to_date = datetime.now(timezone.utc)
                probe_symbol = None
                try:
                    positions_probe = mt5.positions_get() or []
                    if positions_probe:
                        probe_symbol = getattr(positions_probe[0], "symbol", None)
                except Exception:
                    probe_symbol = None
                mt5_server_delta_minutes = _resolve_mt5_server_offset_minutes(
                    mt5,
                    mt5_account_id,
                    preferred_symbol=probe_symbol,
                    server_name=server,
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
                log_ascii_table(
                    logger,
                    "MT5 Sync Context",
                    [
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
                        ("MT5-UTC Delta (min)", mt5_server_delta_minutes),
                        ("Applied Time Offset (min)", applied_offset_minutes),
                        ("Window (UTC)", f"{from_date.isoformat()} -> {to_date.isoformat()}"),
                        ("MT5 Request Window", f"{mt5_from_date.isoformat()} -> {mt5_to_date.isoformat()}"),
                    ],
                )
                # MT5 history filters follow broker/server clock semantics in
                # practice, so shift the request window into server time while
                # continuing to normalize returned timestamps back to UTC.
                deals = mt5.history_deals_get(mt5_from_date, mt5_to_date) or []
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
            "include_skip_debug": True,
            "history_scope": "full" if used_full_history_window else "rolling",
        }
        if recalibrate_trade_timestamps:
            sync_payload["refresh_closed_trade_timestamps"] = True
        response = requests.post(
            f"{base_url}/api/internal/mt5/sync",
            json=sync_payload,
            headers={
                "X-Sync-Secret": sync_secret,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        sync_finished_at = datetime.now(timezone.utc)
        worker_completion_stamp = sync_finished_at.astimezone(timezone.utc).replace(tzinfo=None)
        worker_stamp_saved = False
        try:
            account.last_synced_at = worker_completion_stamp
            if used_full_history_window:
                account.last_full_history_sync_at = worker_completion_stamp
            db.session.commit()
            worker_stamp_saved = True
        except Exception as stamp_exc:
            db.session.rollback()
            logger.warning(
                "MT5 worker completion stamp failed mt5_account_id=%s trigger=%s: %s",
                mt5_account_id,
                trigger_label,
                stamp_exc,
            )
        log_ascii_table(
            logger,
            "MT5 Sync Result",
            [
                ("Finished", sync_finished_at),
                ("Duration", duration_label(sync_started_at, sync_finished_at)),
                ("Worker Last Synced Stamp", worker_completion_stamp if worker_stamp_saved else "failed"),
                ("Trigger", trigger_label),
                ("Mode", sync_mode),
                ("Raw Deals", raw_deal_count),
                ("Trade Rows", aggregated_trade_count),
                ("Open Rows", open_trade_count),
                ("Closed Rows", closed_trade_count),
                ("Saved New", result.get("saved")),
                ("Updated", result.get("updated")),
                ("Skipped", result.get("skipped")),
                ("Errors", result.get("errors")),
                ("Timestamp Refreshes", result.get("timestamp_refreshes")),
                ("Auto Bar Tasks", result.get("auto_bar_sync_queued")),
                ("Auto Bar Scan", result.get("auto_bar_sync")),
                ("Skip Reasons", result.get("skip_reasons")),
            ],
        )
        if int(result.get("skipped") or 0) > 0 and int(result.get("saved") or 0) == 0 and int(result.get("updated") or 0) == 0:
            log_ascii_table(
                logger,
                "MT5 Sync Warning",
                [
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("Trade Account ID", trade_account_id),
                    ("Trigger", trigger_label),
                    ("Mode", sync_mode),
                    ("Raw Deals", raw_deal_count),
                    ("Trade Rows", aggregated_trade_count),
                    ("Skipped", result.get("skipped")),
                    ("Reason", "sync produced only skipped rows"),
                ],
                level=logging.WARNING,
            )
            skip_debug_rows = result.get("skip_debug") or []
            if skip_debug_rows:
                log_ascii_table(
                    logger,
                    "MT5 Skip Debug",
                    [
                        ("Task ID", task_id),
                        ("MT5 Account ID", mt5_account_id),
                        ("Rows Logged", len(skip_debug_rows)),
                        ("Sample", skip_debug_rows),
                    ],
                    level=logging.WARNING,
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
                ("Mode", sync_mode),
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

    from helpers.utils import decrypt_password
    from models import MT5Account, Trade, TradeBars, db
    from trading import cfd_mt5_symbol_name_candidates, mt5_timeframe_constant

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

    if _trade_has_complete_m5_bars_for_worker(trade, db=db, TradeBars=TradeBars):
        log_ascii_table(
            logger,
            "Bar Fetch Skipped",
            [
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Trade ID", trade_id),
                ("Reason", "complete M5 bars already stored"),
            ],
        )
        return {"saved": 0, "timeframe": "M5", "skipped_existing": 1}

    symbol = trade.symbol
    symbol_candidates = cfd_mt5_symbol_name_candidates(symbol) or (symbol,)
    opened_at_utc = _naive_utc_to_aware(trade.opened_at)
    closed_at_utc = _naive_utc_to_aware(trade.closed_at)
    # Always fetch M5; the window must be expressed in M5 bars. Using
    # select_chart_timeframe bar size here capped short trades at ~20×5m (~100m)
    # before entry — not enough context to see a typical setup.
    # Wider window for chart context (esp. structure before entry). ~3× prior pre-window, 2× post.
    pre_entry_m5_bars = TRADE_CHART_PRE_ENTRY_M5_BARS  # 36h of M5 before open (was 144 / 12h)
    post_exit_m5_bars = TRADE_CHART_POST_EXIT_M5_BARS  # 12h of M5 after close (was 72 / 6h)
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
    start_dt = opened_at_utc - timedelta(seconds=pre_entry_m5_bars * TRADE_CHART_M5_SECONDS)
    end_dt = closed_at_utc + timedelta(seconds=post_exit_m5_bars * TRADE_CHART_M5_SECONDS)
    if end_dt > now_utc:
        end_dt = now_utc

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 not installed on this worker.") from exc

    bars = []
    mt5_server_delta_minutes = 0
    with _MT5_API_SESSION_LOCK:
        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(f"MT5 init failed during bar fetch: {mt5.last_error()}")
        try:
            if not mt5.login(int(account_number), password=investor_password, server=server):
                raise RuntimeError(f"MT5 login failed during bar fetch: {mt5.last_error()}")

            mt5_server_delta_minutes = _resolve_mt5_server_offset_minutes(
                mt5,
                mt5_account_id,
                preferred_symbol=symbol,
                server_name=server,
            )
            start_dt_shifted = _shift_datetime_by_minutes(start_dt, minutes=mt5_server_delta_minutes)
            end_dt_shifted = _shift_datetime_by_minutes(end_dt, minutes=mt5_server_delta_minutes)
            tf_constant = mt5_timeframe_constant("M5", mt5)

            bar_normalization_offset = mt5_server_delta_minutes
            fetch_strategy = "broker_time"
            selected_symbol = None
            symbol_select = getattr(mt5, "symbol_select", None)
            last_bar_error = None
            for candidate_symbol in symbol_candidates:
                if callable(symbol_select):
                    try:
                        symbol_select(candidate_symbol, True)
                    except Exception:
                        pass
                raw_bars = mt5.copy_rates_range(
                    candidate_symbol,
                    tf_constant,
                    start_dt_shifted,
                    end_dt_shifted,
                )
                last_bar_error = mt5.last_error()
                if raw_bars is None or len(raw_bars) == 0:
                    continue
                selected_symbol = candidate_symbol
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
                break

            if not bars:
                logger.warning(
                    "Bar fetch no rates mt5_account_id=%s trade_id=%s symbol=%s candidates=%s "
                    "delta_min=%s broker_window=%s..%s utc_window=%s..%s last_error=%s",
                    mt5_account_id,
                    trade_id,
                    symbol,
                    list(symbol_candidates)[:12],
                    mt5_server_delta_minutes,
                    start_dt_shifted.isoformat(),
                    end_dt_shifted.isoformat(),
                    start_dt.isoformat(),
                    end_dt.isoformat(),
                    last_bar_error,
                )
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
            ("Broker Symbol", selected_symbol or "none"),
            ("Candidates Tried", min(len(symbol_candidates), 12)),
            ("M5 context", f"{pre_entry_m5_bars} pre / {post_exit_m5_bars} post bars"),
            ("Stored TF", "M5"),
            ("Bars Fetched", len(bars)),
            ("MT5-UTC Delta (min)", mt5_server_delta_minutes),
            ("Bar Epoch Offset Applied (min)", bar_normalization_offset),
            ("Fetch Strategy", fetch_strategy),
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


@celery.task(
    bind=True,
    max_retries=1,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def fetch_trade_bars_batch(self, mt5_account_id, trade_ids):
    """Fetch OHLC bars for several closed trades with one MT5 session and one API POST."""
    task_id = getattr(getattr(self, "request", None), "id", None)
    fetch_started_at = datetime.now(timezone.utc)

    from helpers.utils import decrypt_password
    from models import MT5Account, Trade, TradeBars, db
    from trading import cfd_mt5_symbol_name_candidates, mt5_timeframe_constant

    account = db.session.get(MT5Account, mt5_account_id)
    if account is None or not account.is_active or account.is_orphaned:
        log_ascii_table(
            logger,
            "Bar Fetch Batch Skipped",
            [
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Reason", "account missing, inactive, or orphaned"),
            ],
            level=logging.WARNING,
        )
        return {"skipped": "account unavailable"}

    normalized_trade_ids = []
    for raw_trade_id in trade_ids or []:
        try:
            trade_id = int(raw_trade_id)
        except (TypeError, ValueError):
            continue
        if trade_id not in normalized_trade_ids:
            normalized_trade_ids.append(trade_id)
    if not normalized_trade_ids:
        return {"saved": 0, "items": [], "skipped": "no valid trade ids"}

    trades_by_id = {
        trade.id: trade
        for trade in Trade.query.filter(Trade.id.in_(normalized_trade_ids)).all()
    }
    candidate_trades = [
        trades_by_id[trade_id]
        for trade_id in normalized_trade_ids
        if trade_id in trades_by_id
        and trades_by_id[trade_id].closed_at is not None
        and trades_by_id[trade_id].user_id == account.user_id
        and trades_by_id[trade_id].trade_account_id == account.trade_account_id
    ]
    trades_to_fetch = [
        trade
        for trade in candidate_trades
        if not _trade_has_complete_m5_bars_for_worker(trade, db=db, TradeBars=TradeBars)
    ]
    skipped_existing = max(len(candidate_trades) - len(trades_to_fetch), 0)
    if not trades_to_fetch:
        log_ascii_table(
            logger,
            "Bar Fetch Batch Skipped",
            [
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Requested Trades", len(normalized_trade_ids)),
                ("Eligible Closed Trades", len(candidate_trades)),
                ("Skipped Existing", skipped_existing),
                ("Reason", "complete M5 bars already stored"),
            ],
        )
        return {"saved": 0, "items": [], "skipped_existing": skipped_existing}

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 not installed on this worker.") from exc

    investor_password = decrypt_password(account.investor_password_encrypted)
    init_kwargs = {}
    if account.terminal_path:
        init_kwargs["path"] = account.terminal_path

    pre_entry_m5_bars = TRADE_CHART_PRE_ENTRY_M5_BARS
    post_exit_m5_bars = TRADE_CHART_POST_EXIT_M5_BARS
    now_utc = datetime.now(timezone.utc)
    fetched_items = []
    no_rate_count = 0
    sample_broker_symbol = None
    mt5_server_delta_minutes = 0
    with _MT5_API_SESSION_LOCK:
        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(f"MT5 init failed during bar fetch batch: {mt5.last_error()}")
        try:
            if not mt5.login(int(account.account_number), password=investor_password, server=account.server):
                raise RuntimeError(f"MT5 login failed during bar fetch batch: {mt5.last_error()}")
            mt5_server_delta_minutes = _resolve_mt5_server_offset_minutes(
                mt5,
                mt5_account_id,
                preferred_symbol=trades_to_fetch[0].symbol,
                server_name=account.server,
            )
            tf_constant = mt5_timeframe_constant("M5", mt5)
            symbol_select = getattr(mt5, "symbol_select", None)
            for trade in trades_to_fetch:
                opened_at_utc = _naive_utc_to_aware(trade.opened_at)
                closed_at_utc = _naive_utc_to_aware(trade.closed_at)
                start_dt = opened_at_utc - timedelta(seconds=pre_entry_m5_bars * TRADE_CHART_M5_SECONDS)
                end_dt = closed_at_utc + timedelta(seconds=post_exit_m5_bars * TRADE_CHART_M5_SECONDS)
                if end_dt > now_utc:
                    end_dt = now_utc
                start_dt_shifted = _shift_datetime_by_minutes(start_dt, minutes=mt5_server_delta_minutes)
                end_dt_shifted = _shift_datetime_by_minutes(end_dt, minutes=mt5_server_delta_minutes)
                bars = []
                selected_symbol = None
                symbol_candidates = cfd_mt5_symbol_name_candidates(trade.symbol) or (trade.symbol,)
                last_bar_error = None
                for candidate_symbol in symbol_candidates:
                    if callable(symbol_select):
                        try:
                            symbol_select(candidate_symbol, True)
                        except Exception:
                            pass
                    raw_bars = mt5.copy_rates_range(
                        candidate_symbol,
                        tf_constant,
                        start_dt_shifted,
                        end_dt_shifted,
                    )
                    last_bar_error = mt5.last_error()
                    if raw_bars is None or len(raw_bars) == 0:
                        continue
                    selected_symbol = candidate_symbol
                    sample_broker_symbol = sample_broker_symbol or selected_symbol
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
                    break
                if not bars:
                    no_rate_count += 1
                    logger.warning(
                        "Bar fetch batch no rates mt5_account_id=%s trade_id=%s symbol=%s candidates=%s "
                        "delta_min=%s broker_window=%s..%s utc_window=%s..%s last_error=%s",
                        mt5_account_id,
                        trade.id,
                        trade.symbol,
                        list(symbol_candidates)[:12],
                        mt5_server_delta_minutes,
                        start_dt_shifted.isoformat(),
                        end_dt_shifted.isoformat(),
                        start_dt.isoformat(),
                        end_dt.isoformat(),
                        last_bar_error,
                    )
                    continue
                fetched_items.append({"trade_id": trade.id, "timeframe": "M5", "bars": bars})
        finally:
            mt5.shutdown()

    fetch_finished_at = datetime.now(timezone.utc)
    total_bars = sum(len(item["bars"]) for item in fetched_items)
    log_ascii_table(
        logger,
        "Bar Fetch Batch Result",
        [
            ("Task ID", task_id),
            ("MT5 Account ID", mt5_account_id),
            ("Requested Trades", len(normalized_trade_ids)),
            ("Eligible Closed Trades", len(candidate_trades)),
            ("Skipped Existing", skipped_existing),
            ("Fetched Trades", len(fetched_items)),
            ("No Rate Trades", no_rate_count),
            ("Bars Fetched", total_bars),
            ("Sample Broker Symbol", sample_broker_symbol or "none"),
            ("MT5-UTC Delta (min)", mt5_server_delta_minutes),
            ("Duration", duration_label(fetch_started_at, fetch_finished_at)),
        ],
    )

    if not fetched_items:
        return {"saved": 0, "items": [], "skipped_existing": skipped_existing}

    base_url = os.environ.get("FLASK_API_URL", "https://myfxjournal.com").strip() or "https://myfxjournal.com"
    sync_secret = os.environ.get("MT5_SYNC_SECRET", "").strip()
    if not sync_secret:
        raise RuntimeError("MT5_SYNC_SECRET is required for bar fetch.")

    response = requests.post(
        f"{base_url}/api/internal/mt5/trade-bars/batch",
        json={"mt5_account_id": mt5_account_id, "timeframe": "M5", "items": fetched_items},
        headers={
            "X-Sync-Secret": sync_secret,
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


@celery.task
def sync_all_active_mt5_accounts():
    from models import MT5Account
    from celery_workers.cache import CacheUnavailableError, get_queue_depth

    try:
        sync_queue_depth = get_queue_depth(MT5_SYNC_QUEUE_NAME)
        priority_queue_depth = get_queue_depth(MT5_PRIORITY_QUEUE_NAME)
        total_queue_depth = sync_queue_depth + priority_queue_depth
        if total_queue_depth > MT5_SYNC_BEAT_MAX_QUEUE_DEPTH:
            logger.warning(
                "MT5 beat skipped queue_depth_total=%s sync_queue_depth=%s priority_queue_depth=%s max=%s",
                total_queue_depth,
                sync_queue_depth,
                priority_queue_depth,
                MT5_SYNC_BEAT_MAX_QUEUE_DEPTH,
            )
            return
    except CacheUnavailableError as exc:
        logger.warning("MT5 beat queue-depth guard unavailable: %s", exc)

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
            queue=MT5_SYNC_QUEUE_NAME,
            expires=MT5_SYNC_BEAT_EXPIRES_SECONDS,
        )
