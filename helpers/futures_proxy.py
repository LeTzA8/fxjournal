"""Futures CFD-proxy replay resolver — Phase 1 scaffold.

Provides resolution of futures root symbols to their CFD proxy equivalents,
status computation, and the API block shape for the chart-data endpoint.

No bar fetching, no Celery tasks, no MT5 imports.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Status values — stored in Trade.proxy_replay_status
PROXY_STATUS_UNAVAILABLE_NO_MAPPING = "unavailable_no_mapping"
PROXY_STATUS_UNAVAILABLE_NO_MT5 = "unavailable_no_mt5"
PROXY_STATUS_PENDING = "pending"
# "unavailable_broker" is reserved for Phase 2 (symbol_select failed on broker)
# "available" is reserved for Phase 2 (bars have been fetched)

_PROXY_SOURCE = "mt5_cfd"
_PROXY_ACCURACY = "approximate"

_DISCLAIMER = (
    "Proxy chart uses your broker's {proxy_symbol} CFD bars for visual context. "
    "Timing is matched to your imported futures trade; CFD prices do not represent "
    "your actual futures fills."
)


def resolve_proxy_cfd_symbol(futures_root: str) -> str | None:
    """Return the canonical CFD proxy symbol for a futures root (e.g. 'NQ' → 'NAS100').

    Queries FuturesSymbol.proxy_cfd_symbol from the DB so admin overrides take effect
    immediately without a process restart.  Returns None when no mapping exists or when
    called outside an app context.
    """
    if not futures_root:
        return None
    normalized = futures_root.strip().upper()
    try:
        from flask import has_app_context  # noqa: PLC0415
        if not has_app_context():
            return None
        from models import FuturesSymbol  # noqa: PLC0415
        row = FuturesSymbol.query.filter_by(root_symbol=normalized, is_active=True).one_or_none()
        if row is None:
            return None
        proxy = (row.proxy_cfd_symbol or "").strip() or None
        return proxy
    except Exception as exc:
        logger.warning("resolve_proxy_cfd_symbol failed root=%s: %s", futures_root, exc)
        return None


def compute_proxy_window_minutes(trade) -> dict:
    """Return {'pre': N, 'post': N} based on trade duration.

    Thresholds:
      <= 30 min   →  pre=90,  post=90
      <= 4 h      →  pre=180, post=180
      <= 24 h     →  pre=360, post=180
      > 24 h      →  pre=720, post=360

    Total (pre+post) capped to 1440 min (24 h).
    Default of 180/180 when times are unavailable.
    """
    default = {"pre": 180, "post": 180}
    try:
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        if opened_at is None or closed_at is None:
            return default
        duration_minutes = (closed_at - opened_at).total_seconds() / 60.0
        if duration_minutes <= 0:
            return default
        if duration_minutes <= 30:
            window = {"pre": 90, "post": 90}
        elif duration_minutes <= 240:
            window = {"pre": 180, "post": 180}
        elif duration_minutes <= 1440:
            window = {"pre": 360, "post": 180}
        else:
            window = {"pre": 720, "post": 360}
        # Hard cap: total <= 1440 min
        total = window["pre"] + window["post"]
        if total > 1440:
            ratio = window["pre"] / total
            window["pre"] = int(1440 * ratio)
            window["post"] = 1440 - window["pre"]
        return window
    except Exception as exc:
        logger.warning("compute_proxy_window_minutes failed: %s", exc)
        return default


def is_futures_trade(trade) -> bool:
    """True when the trade belongs to a FUTURES account and has a contract_code set."""
    try:
        contract_code = (getattr(trade, "contract_code", None) or "").strip()
        if not contract_code:
            return False
        account = getattr(trade, "trade_account", None)
        if account is not None:
            account_type = (getattr(account, "account_type", None) or "").strip().upper()
            return account_type == "FUTURES"
        # Fallback: try querying if the trade has a trade_account_id
        trade_account_id = getattr(trade, "trade_account_id", None)
        if trade_account_id is None:
            return False
        from flask import has_app_context  # noqa: PLC0415
        if not has_app_context():
            return False
        from models import TradeAccount  # noqa: PLC0415
        acct = TradeAccount.query.get(trade_account_id)
        if acct is None:
            return False
        return (acct.account_type or "").strip().upper() == "FUTURES"
    except Exception as exc:
        logger.warning("is_futures_trade check failed: %s", exc)
        return False


def _user_has_active_mt5(user) -> bool:
    """Return True if the user has at least one active, non-archived MT5 account."""
    try:
        from flask import has_app_context  # noqa: PLC0415
        if not has_app_context():
            return False
        from models import MT5Account  # noqa: PLC0415
        user_id = getattr(user, "id", None)
        if user_id is None:
            return False
        row = (
            MT5Account.query
            .filter_by(user_id=user_id, is_active=True)
            .filter(MT5Account.archived_at.is_(None))
            .first()
        )
        return row is not None
    except Exception as exc:
        logger.warning("_user_has_active_mt5 check failed: %s", exc)
        return False


def _futures_root_from_trade(trade) -> str | None:
    """Extract the futures root symbol from trade.contract_code using existing parser."""
    try:
        from trading import parse_futures_contract_code  # noqa: PLC0415
        contract_code = (getattr(trade, "contract_code", None) or "").strip()
        if not contract_code:
            return None
        parsed = parse_futures_contract_code(contract_code)
        if parsed is None:
            return None
        return parsed.get("root_symbol")
    except Exception as exc:
        logger.warning("_futures_root_from_trade failed: %s", exc)
        return None


def resolve_proxy_status(trade, user) -> dict:
    """Compute proxy replay status for a futures trade.

    Returns:
        {
            "status": PROXY_STATUS_* constant,
            "proxy_symbol": str | None,
            "window_minutes": {"pre": int, "post": int} | None,
        }

    Does NOT fetch bars. Does NOT queue any Celery task.
    """
    root = _futures_root_from_trade(trade)
    if root is None:
        return {
            "status": PROXY_STATUS_UNAVAILABLE_NO_MAPPING,
            "proxy_symbol": None,
            "window_minutes": None,
        }

    proxy_symbol = resolve_proxy_cfd_symbol(root)
    if proxy_symbol is None:
        return {
            "status": PROXY_STATUS_UNAVAILABLE_NO_MAPPING,
            "proxy_symbol": None,
            "window_minutes": None,
        }

    if not _user_has_active_mt5(user):
        return {
            "status": PROXY_STATUS_UNAVAILABLE_NO_MT5,
            "proxy_symbol": proxy_symbol,
            "window_minutes": None,
        }

    return {
        "status": PROXY_STATUS_PENDING,
        "proxy_symbol": proxy_symbol,
        "window_minutes": compute_proxy_window_minutes(trade),
    }


def proxy_replay_api_block(trade) -> dict | None:
    """Build the proxy_replay JSON block for the trade_chart_data API response.

    Returns None when the trade is not a futures trade.
    Returns a dict with status, proxy_symbol, accuracy, warning_required, and disclaimer
    when it is.  Status is read directly from the stored trade field rather than
    re-resolving at request time.
    """
    if not is_futures_trade(trade):
        return None

    status = (getattr(trade, "proxy_replay_status", None) or "").strip() or None
    proxy_symbol = (getattr(trade, "proxy_replay_symbol", None) or "").strip() or None

    disclaimer = (
        _DISCLAIMER.format(proxy_symbol=proxy_symbol)
        if proxy_symbol
        else (
            "Proxy chart uses your broker's CFD bars for visual context. "
            "Timing is matched to your imported futures trade; CFD prices do not "
            "represent your actual futures fills."
        )
    )

    return {
        "is_proxy": True,
        "status": status,
        "proxy_symbol": proxy_symbol,
        "source": _PROXY_SOURCE,
        "accuracy": _PROXY_ACCURACY,
        "warning_required": True,
        "disclaimer": disclaimer,
    }
