"""Pre-activation cohort helpers (zero-data users) for dashboard and admin."""

from datetime import timedelta

from sqlalchemy import and_, exists, not_

from helpers.utils import utcnow_naive
from models import MT5AccessRequest, MT5Account, Trade, TradeAccount, User, db

_MT5_REQUEST_ACTIVE_STATUSES = (
    MT5AccessRequest.STATUS_PENDING,
    MT5AccessRequest.STATUS_APPROVED,
)


def dashboard_row_has_mt5_submission(mt5_selected_row):
    """True when the active CFD row reflects MT5 intent beyond a blank requestable slot."""
    if not mt5_selected_row:
        return False
    return bool(
        mt5_selected_row.get("mt5_account")
        or mt5_selected_row.get("pending_request")
        or mt5_selected_row.get("approved_request")
    )


def user_has_mt5_activation_signal(user_id):
    """User submitted MT5 details or has an MT5 account row on any trade account."""
    trade_account_ids = [
        row[0]
        for row in db.session.query(TradeAccount.id).filter(TradeAccount.user_id == user_id).all()
    ]
    if not trade_account_ids:
        return (
            MT5AccessRequest.query.filter(
                MT5AccessRequest.user_id == user_id,
                MT5AccessRequest.status.in_(_MT5_REQUEST_ACTIVE_STATUSES),
            )
            .limit(1)
            .first()
            is not None
        )

    if (
        MT5Account.query.filter(MT5Account.trade_account_id.in_(trade_account_ids))
        .limit(1)
        .first()
        is not None
    ):
        return True

    return (
        MT5AccessRequest.query.filter(
            MT5AccessRequest.user_id == user_id,
            MT5AccessRequest.status.in_(_MT5_REQUEST_ACTIVE_STATUSES),
        )
        .limit(1)
        .first()
        is not None
    )


def zero_data_user_filter_clause():
    """SQLAlchemy filter: no trades and no MT5 activation signal."""
    has_trade = exists().where(Trade.user_id == User.id)
    has_mt5_account = exists().where(
        TradeAccount.user_id == User.id,
        MT5Account.trade_account_id == TradeAccount.id,
    )
    has_mt5_request = exists().where(
        MT5AccessRequest.user_id == User.id,
        MT5AccessRequest.status.in_(_MT5_REQUEST_ACTIVE_STATUSES),
    )
    return and_(not_(has_trade), not_(has_mt5_account), not_(has_mt5_request))


def count_pre_activation_users():
    """Return recent (<=7d) and stuck (>7d) zero-data user counts."""
    cutoff = utcnow_naive() - timedelta(days=7)
    base = User.query.filter(zero_data_user_filter_clause())
    recent = base.filter(User.created_at >= cutoff).count()
    stuck = base.filter(User.created_at < cutoff).count()
    return {
        "pre_activation_recent": recent,
        "pre_activation_stuck": stuck,
    }
