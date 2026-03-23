import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from celery_app import celery


def _to_utc_iso(timestamp_value):
    if timestamp_value is None:
        return None
    return datetime.fromtimestamp(timestamp_value, tz=timezone.utc).isoformat(timespec="seconds")


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


def aggregate_deals_to_trades(
    deals,
    *,
    entry_in=0,
    entry_out=1,
    extra_exit_entries=(),
    deal_type_buy=0,
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

        if not entry_deals:
            continue

        entry_deal = entry_deals[0]
        entry_price = _weighted_average_price(entry_deals)
        entry_volume = sum(_deal_volume(deal) for deal in entry_deals)
        if entry_price is None or entry_volume <= 0:
            continue

        exit_price = _weighted_average_price(exit_deals)
        exit_volume = sum(_deal_volume(deal) for deal in exit_deals)
        latest_exit_deal = exit_deals[-1] if exit_deals else None
        side = "BUY" if getattr(entry_deal, "type", None) == deal_type_buy else "SELL"
        total_commission = sum(float(getattr(deal, "commission", 0.0) or 0.0) for deal in position_deals)
        total_swap = sum(float(getattr(deal, "swap", 0.0) or 0.0) for deal in position_deals)
        total_profit = sum(float(getattr(deal, "profit", 0.0) or 0.0) for deal in position_deals)

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
                    "opened_at": _to_utc_iso(getattr(entry_deal, "time", None)),
                    "closed_at": _to_utc_iso(getattr(latest_exit_deal, "time", None)),
                    "mt5_position": str(position_id),
                    "trade_note": str(getattr(latest_exit_deal, "comment", "") or "").strip() or None,
                    "source_timezone": "UTC",
                    "is_open": False,
                }
            )
        elif not exit_deals:
            trades.append(
                {
                    "symbol": getattr(entry_deal, "symbol", None),
                    "side": side,
                    "entry_price": entry_price,
                    "exit_price": None,
                    "lot_size": entry_volume,
                    "pnl": None,
                    "commission": total_commission,
                    "swap": 0.0,
                    "stop_loss": None,
                    "take_profit": None,
                    "opened_at": _to_utc_iso(getattr(entry_deal, "time", None)),
                    "closed_at": None,
                    "mt5_position": str(position_id),
                    "trade_note": str(getattr(entry_deal, "comment", "") or "").strip() or None,
                    "source_timezone": "UTC",
                    "is_open": True,
                }
            )

    return trades


@celery.task(bind=True, max_retries=2, default_retry_delay=30)
def sync_mt5_account(self, mt5_account_id):
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 not installed on this worker.") from exc

    from helpers.utils import decrypt_password
    from models import MT5Account, db

    account = db.session.get(MT5Account, mt5_account_id)
    if account is None or not account.is_active:
        return {"error": "MT5Account not found or inactive"}
    if account.is_orphaned:
        return {"error": "MT5Account is orphaned"}

    investor_password = decrypt_password(account.investor_password_encrypted)
    account_number = account.account_number
    server = account.server
    terminal_path = account.terminal_path
    is_first_sync = account.last_synced_at is None

    init_kwargs = {}
    if terminal_path:
        init_kwargs["path"] = terminal_path

    if not mt5.initialize(**init_kwargs):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    try:
        if not mt5.login(int(account_number), password=investor_password, server=server):
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

        if is_first_sync:
            from_date = datetime(2000, 1, 1, tzinfo=timezone.utc)
        else:
            from_date = datetime.now(timezone.utc) - timedelta(days=7)
        to_date = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(from_date, to_date) or []

        trades = aggregate_deals_to_trades(
            deals,
            entry_in=getattr(mt5, "DEAL_ENTRY_IN", 0),
            entry_out=getattr(mt5, "DEAL_ENTRY_OUT", 1),
            extra_exit_entries=(
                getattr(mt5, "DEAL_ENTRY_INOUT", None),
                getattr(mt5, "DEAL_ENTRY_OUT_BY", None),
            ),
            deal_type_buy=getattr(mt5, "DEAL_TYPE_BUY", 0),
        )

        base_url = os.environ.get("FLASK_API_URL", "https://myfxjournal.com").strip() or "https://myfxjournal.com"
        sync_secret = os.environ.get("MT5_SYNC_SECRET", "").strip()
        if not sync_secret:
            raise RuntimeError("MT5_SYNC_SECRET is required for MT5 sync.")

        response = requests.post(
            f"{base_url}/api/internal/mt5/sync",
            json={
                "mt5_account_id": mt5_account_id,
                "trades": trades,
            },
            headers={
                "X-Sync-Secret": sync_secret,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise self.retry(exc=exc)
    finally:
        mt5.shutdown()


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
            queue="mt5_sync",
        )
