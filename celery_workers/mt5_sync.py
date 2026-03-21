import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from celery_app import celery


def _to_utc_iso(timestamp_value):
    if timestamp_value is None:
        return None
    return datetime.fromtimestamp(timestamp_value, tz=timezone.utc).isoformat(timespec="seconds")


def aggregate_deals_to_trades(deals, *, entry_in=0, entry_out=1, deal_type_buy=0):
    deals = [deal for deal in deals if getattr(deal, "type", 99) <= 1]
    positions = defaultdict(list)

    for deal in deals:
        position_id = getattr(deal, "position_id", None)
        if not position_id:
            continue
        positions[position_id].append(deal)

    trades = []
    for position_id, position_deals in positions.items():
        entry_deals = [deal for deal in position_deals if getattr(deal, "entry", None) == entry_in]
        exit_deals = [deal for deal in position_deals if getattr(deal, "entry", None) == entry_out]
        if len(entry_deals) != 1 or len(exit_deals) != 1:
            continue

        entry_deal = entry_deals[0]
        exit_deal = exit_deals[0]
        side = "BUY" if getattr(entry_deal, "type", None) == deal_type_buy else "SELL"
        total_commission = sum(float(getattr(deal, "commission", 0.0) or 0.0) for deal in position_deals)
        total_swap = sum(float(getattr(deal, "swap", 0.0) or 0.0) for deal in position_deals)
        total_profit = sum(float(getattr(deal, "profit", 0.0) or 0.0) for deal in position_deals)

        trades.append(
            {
                "symbol": getattr(exit_deal, "symbol", None) or getattr(entry_deal, "symbol", None),
                "side": side,
                "entry_price": float(getattr(entry_deal, "price", 0.0) or 0.0),
                "exit_price": float(getattr(exit_deal, "price", 0.0) or 0.0),
                "lot_size": float(getattr(entry_deal, "volume", 0.0) or 0.0),
                "pnl": total_profit,
                "commission": total_commission,
                "swap": total_swap,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": _to_utc_iso(getattr(entry_deal, "time", None)),
                "closed_at": _to_utc_iso(getattr(exit_deal, "time", None)),
                "mt5_position": str(position_id),
                "trade_note": str(getattr(exit_deal, "comment", "") or "").strip() or None,
                "source_timezone": "UTC",
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
    from models import MT5Account

    account = MT5Account.query.get(mt5_account_id)
    if account is None or not account.is_active:
        return {"error": "MT5Account not found or inactive"}

    investor_password = decrypt_password(account.investor_password_encrypted)
    account_number = account.account_number
    server = account.server
    terminal_path = account.terminal_path
    days_back = 7

    init_kwargs = {}
    if terminal_path:
        init_kwargs["path"] = terminal_path

    if not mt5.initialize(**init_kwargs):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    try:
        if not mt5.login(int(account_number), password=investor_password, server=server):
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

        from_date = datetime.now(timezone.utc) - timedelta(days=days_back)
        to_date = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(from_date, to_date) or []

        trades = aggregate_deals_to_trades(
            deals,
            entry_in=getattr(mt5, "DEAL_ENTRY_IN", 0),
            entry_out=getattr(mt5, "DEAL_ENTRY_OUT", 1),
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
    accounts = MT5Account.query.filter_by(is_active=True).all()
    if not accounts:
        return
    for account in accounts:
        sync_mt5_account.apply_async(
            args=[account.id],
            queue="mt5_sync",
        )
