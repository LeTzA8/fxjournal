import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import threading
import uuid

import requests

from celery_app import celery
from trading import MT5_DEFAULT_SOURCE_TIMEZONE_NAME

logger = logging.getLogger(__name__)

# MetaTrader5 keeps process-global session state, so sync tasks must not
# overlap MT5 API calls inside the same worker process.
_MT5_API_SESSION_LOCK = threading.Lock()


def _retry_with_backoff(task, exc, *, base_delay=30, max_delay=300):
    retry_number = getattr(getattr(task, "request", None), "retries", 0)
    countdown = min(base_delay * (2 ** retry_number), max_delay)
    raise task.retry(exc=exc, countdown=countdown)


def _format_log_value(value, *, default="-", max_width=72):
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


def _ascii_table(title, rows):
    normalized_rows = [
        (_format_log_value(label, default=""), _format_log_value(value))
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


def _log_ascii_table(title, rows):
    logger.info("\n%s", _ascii_table(title, rows))


def _duration_ms(started_at, finished_at):
    if started_at is None or finished_at is None:
        return None
    return max(int((finished_at - started_at).total_seconds() * 1000), 0)


def _duration_label(started_at, finished_at):
    duration_ms = _duration_ms(started_at, finished_at)
    if duration_ms is None:
        return None
    return f"{duration_ms / 1000:.2f}s ({duration_ms} ms)"


def _to_utc_iso(timestamp_value, *, offset_minutes=0):
    if timestamp_value is None:
        return None
    adjusted_timestamp = float(timestamp_value) - (int(offset_minutes or 0) * 60)
    return datetime.fromtimestamp(adjusted_timestamp, tz=timezone.utc).isoformat(timespec="seconds")


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


def _probe_mt5_server_delta_minutes(mt5, *, preferred_symbol=None):
    """
    Estimate MT5 server clock drift relative to VM UTC using latest tick timestamp.
    Returns minute delta where positive means MT5 clock appears ahead of UTC.
    """
    symbol_info_tick = getattr(mt5, "symbol_info_tick", None)
    if not callable(symbol_info_tick):
        return 0
    symbol_candidates = []
    if preferred_symbol:
        symbol_candidates.append(str(preferred_symbol).strip())
    if "EURUSD" not in symbol_candidates:
        symbol_candidates.append("EURUSD")
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


@celery.task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
    reject_on_worker_lost=True,
)
def sync_mt5_account(self, mt5_account_id, full_history=False, trigger_source="unknown"):
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
    sync_mode = "full_history" if full_history else "rolling_7d"
    trigger_label = str(trigger_source or "unknown").strip() or "unknown"
    try:
        try:
            lock_acquired = claim_lock(_sync_lock_key(mt5_account_id), lock_token, ttl=600)
        except CacheUnavailableError:
            lock_enabled = False
            lock_acquired = True

        if not lock_acquired:
            logger.info(
                "MT5 sync skipped because another task already holds the lock. task_id=%s mt5_account_id=%s",
                task_id,
                mt5_account_id,
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
            logger.warning(
                "MT5 sync skipped because account is missing or inactive. task_id=%s mt5_account_id=%s",
                task_id,
                mt5_account_id,
            )
            return {"error": "MT5Account not found or inactive"}
        if account.is_orphaned:
            logger.warning(
                "MT5 sync skipped because account is orphaned. task_id=%s mt5_account_id=%s user_id=%s trade_account_id=%s",
                task_id,
                mt5_account_id,
                account.user_id,
                account.trade_account_id,
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
                    from_date = datetime.now(timezone.utc) - timedelta(days=7)
                to_date = datetime.now(timezone.utc)
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
                )
                applied_offset_minutes = mt5_server_delta_minutes
                vm_timing_context = _vm_timezone_context()
                _log_ascii_table(
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
                        ("Window", f"{from_date.isoformat()} -> {to_date.isoformat()}"),
                    ],
                )
                deals = mt5.history_deals_get(from_date, to_date) or []
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

        response = requests.post(
            f"{base_url}/api/internal/mt5/sync",
            json={
                "mt5_account_id": mt5_account_id,
                "trades": trades,
                "timing_context": vm_timing_context,
                "mt5_server_delta_minutes": mt5_server_delta_minutes,
                "applied_time_offset_minutes": applied_offset_minutes,
                "include_skip_reasons": True,
            },
            headers={
                "X-Sync-Secret": sync_secret,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        sync_finished_at = datetime.now(timezone.utc)
        _log_ascii_table(
            "MT5 Sync Result",
            [
                ("Finished", sync_finished_at),
                ("Duration", _duration_label(sync_started_at, sync_finished_at)),
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
                ("Skip Reasons", result.get("skip_reasons")),
            ],
        )
        if int(result.get("skipped") or 0) > 0 and int(result.get("saved") or 0) == 0 and int(result.get("updated") or 0) == 0:
            logger.warning(
                (
                    "MT5 sync produced only skipped rows. task_id=%s mt5_account_id=%s trade_account_id=%s "
                    "trigger=%s mode=%s raw_deals=%s aggregated_trades=%s skipped=%s"
                ),
                task_id,
                mt5_account_id,
                trade_account_id,
                trigger_label,
                sync_mode,
                raw_deal_count,
                aggregated_trade_count,
                result.get("skipped"),
            )
        return result
    except Exception as exc:
        sync_finished_at = sync_finished_at or datetime.now(timezone.utc)
        _log_ascii_table(
            "MT5 Sync Failed",
            [
                ("Finished", sync_finished_at),
                ("Duration", _duration_label(sync_started_at, sync_finished_at)),
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
