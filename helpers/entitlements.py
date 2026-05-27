"""
Central entitlement helpers for plan/access checks.

All gating decisions go through these helpers, not scattered across routes or
templates. The shared trial starts only when a premium workflow feature is
successfully used; core imports and account creation do not start the clock.
"""

from datetime import timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError

from helpers.schema_compat import (
    mt5_trial_columns_available,
    user_entitlement_columns_available,
    user_premium_trial_column_available,
)
from helpers.utils import utcnow_naive


# Premium trial config

PREMIUM_TRIAL_DAYS = 14
MT5_TRIAL_DAYS = PREMIUM_TRIAL_DAYS
ADMIN_TRIAL_EXTEND_MIN_DAYS = 1
ADMIN_TRIAL_EXTEND_MAX_DAYS = 90
ADMIN_TRIAL_EXTEND_DEFAULT_DAYS = 14
WEEKLY_FOLLOWUP_TRIAL_MESSAGE_LIMIT = 5
WEEKLY_FOLLOWUP_MAX_USER_MESSAGES_PER_MINUTE = 3
WEEKLY_FOLLOWUP_RATE_WINDOW_SECONDS = 60
TRIAL_UPGRADE_URL = "/pricing"


# Replay entitlement config

FREE_REPLAY_TIMEFRAMES = frozenset({"M5", "M15"})
TRADER_REPLAY_TIMEFRAMES = frozenset({"M1", "M5", "M15"})
PRO_REPLAY_TIMEFRAMES = frozenset({"M1", "M5", "M15"})

FREE_REPLAY_MAX_TRADE_AGE_DAYS = 90
TRADER_REPLAY_MAX_TRADE_AGE_DAYS = 365
PRO_REPLAY_MAX_TRADE_AGE_DAYS = None

_PLAN_TIER_RANK = {"free": 0, "trader": 1, "pro": 2}


# Internal helpers

def _user_plan_tier(user) -> str:
    if not user_entitlement_columns_available():
        # Pre-0056 schema: do not enforce tier-based blocks.
        return "trader"
    return str(getattr(user, "plan_tier", None) or "free").strip().lower() or "free"


def _user_is_grandfathered(user) -> bool:
    if not user_entitlement_columns_available():
        # Pre-0056 schema: preserve pre-gating behavior.
        return True
    return bool(getattr(user, "plan_grandfathered", False))


def _user_has_paid_workflow_access(user) -> bool:
    return _PLAN_TIER_RANK.get(_user_plan_tier(user), 0) >= _PLAN_TIER_RANK["trader"]


def _is_admin(user) -> bool:
    from auth_account import user_has_admin_access

    return user_has_admin_access(user)


def _trial_cta(feature_interest="trader_workflow") -> dict:
    return {
        "label": "Join Trader waitlist",
        "url": TRIAL_UPGRADE_URL,
        "source": "trial_gate",
        "feature_interest": feature_interest,
    }


def _safe_naive_datetime(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _is_trade_account_like(account) -> bool:
    return getattr(account, "__tablename__", None) == "trade_accounts" or hasattr(account, "mt5_accounts")


def _account_mt5_trial_started_at(user, account=None):
    if not mt5_trial_columns_available():
        return None

    direct_started_at = getattr(account, "mt5_trial_started_at", None)
    if direct_started_at is not None:
        return _safe_naive_datetime(direct_started_at)

    mt5_accounts = []
    related_accounts = getattr(account, "mt5_accounts", None)
    if related_accounts is not None:
        try:
            mt5_accounts.extend(list(related_accounts))
        except TypeError:
            pass

    user_mt5_accounts = getattr(user, "mt5_accounts", None)
    if user_mt5_accounts is not None and not mt5_accounts:
        try:
            mt5_accounts.extend(list(user_mt5_accounts))
        except TypeError:
            pass

    if not mt5_accounts:
        try:
            from flask import has_app_context
            from models import MT5Account

            if has_app_context() and getattr(user, "id", None):
                query = MT5Account.query.filter(MT5Account.user_id == user.id)
                account_id = getattr(account, "id", None)
                if account_id is not None and _is_trade_account_like(account):
                    query = query.filter(MT5Account.trade_account_id == account_id)
                mt5_accounts = query.all()
        except Exception:
            mt5_accounts = []

    started_values = [
        _safe_naive_datetime(getattr(mt5_account, "mt5_trial_started_at", None))
        for mt5_account in mt5_accounts
        if getattr(mt5_account, "mt5_trial_started_at", None) is not None
    ]
    return min(started_values) if started_values else None


def _user_premium_trial_started_at(user):
    if not user_premium_trial_column_available():
        return None
    return _safe_naive_datetime(getattr(user, "premium_trial_started_at", None))


def _trial_started_at(user, account=None):
    user_started_at = _user_premium_trial_started_at(user)
    if user_started_at is not None:
        return user_started_at, "premium_trial_started_at"

    mt5_started_at = _account_mt5_trial_started_at(user, account)
    if mt5_started_at is not None:
        return mt5_started_at, "mt5_trial_started_at"

    return None, None


def _workflow_access_reason(user):
    if _is_admin(user):
        return "admin"
    if not user_entitlement_columns_available():
        return "schema_compat"
    if _user_is_grandfathered(user):
        return "grandfathered"
    if _user_has_paid_workflow_access(user):
        return "plan_paid"
    return None


# Public API

def start_premium_trial_if_needed(user, account=None, *, started_at=None):
    """
    Stamp the shared premium trial start on successful premium-feature use.

    The caller owns committing/rolling back the active DB transaction. Returns
    the effective start timestamp, or None for paid/grandfathered/admin/schema
    compatibility paths where no trial clock should be started.
    """
    if user is None:
        return None
    if _workflow_access_reason(user) is not None:
        return None

    existing_started_at, _source = _trial_started_at(user, account)
    effective_started_at = existing_started_at or _safe_naive_datetime(started_at) or utcnow_naive()

    if user_premium_trial_column_available() and getattr(user, "premium_trial_started_at", None) is None:
        try:
            user.premium_trial_started_at = effective_started_at
        except Exception:
            pass

    if (
        mt5_trial_columns_available()
        and account is not None
        and hasattr(account, "mt5_trial_started_at")
        and getattr(account, "mt5_trial_started_at", None) is None
    ):
        try:
            account.mt5_trial_started_at = effective_started_at
        except Exception:
            pass

    return effective_started_at

def get_user_plan_state(user) -> dict:
    """Returns {tier: str, grandfathered: bool}."""
    if not user_entitlement_columns_available():
        return {"tier": "trader", "grandfathered": True}
    return {
        "tier": _user_plan_tier(user),
        "grandfathered": _user_is_grandfathered(user),
    }


def is_mt5_sync_paused(mt5_account) -> bool:
    """Return True when an MT5 account has an explicit sync pause stamp."""
    if not mt5_trial_columns_available():
        return False
    return getattr(mt5_account, "sync_paused_at", None) is not None


def get_trial_state(user, account=None) -> dict:
    """
    Returns the shared premium workflow trial state.

    Possible states:
      "grandfathered" - existing beta user, no expiry applies
      "plan_paid" - Trader or Pro user, no trial limit applies
      "not_started" - trial clock not yet started
      "active" - trial running, days_remaining > 0
      "expired" - trial ran out
      "paused" - sync explicitly paused
    """
    if not user_entitlement_columns_available():
        return {
            "state": "grandfathered",
            "days_remaining": None,
            "started_at": None,
            "source": "schema_compat",
            "show_trial_ui": False,
        }

    paused_at = getattr(account, "sync_paused_at", None)
    if paused_at is not None:
        return {
            "state": "paused",
            "days_remaining": 0,
            "started_at": _safe_naive_datetime(getattr(account, "mt5_trial_started_at", None)),
            "source": "mt5_sync_pause",
            "show_trial_ui": True,
        }

    privileged_reason = _workflow_access_reason(user)
    if privileged_reason is not None:
        state = "grandfathered" if privileged_reason in ("admin", "schema_compat", "grandfathered") else privileged_reason
        return {
            "state": state,
            "days_remaining": None,
            "started_at": None,
            "source": privileged_reason,
            "show_trial_ui": False,
        }

    started_at, source = _trial_started_at(user, account)
    if started_at is None:
        return {
            "state": "not_started",
            "days_remaining": PREMIUM_TRIAL_DAYS,
            "started_at": None,
            "source": None,
            "show_trial_ui": True,
        }

    now = utcnow_naive()
    elapsed_days = (now - started_at).days
    days_remaining = max(PREMIUM_TRIAL_DAYS - elapsed_days, 0)

    if days_remaining <= 0:
        return {
            "state": "expired",
            "days_remaining": 0,
            "started_at": started_at,
            "source": source,
            "show_trial_ui": True,
        }

    return {
        "state": "active",
        "days_remaining": days_remaining,
        "started_at": started_at,
        "source": source,
        "show_trial_ui": True,
    }


def get_mt5_trial_state(user, mt5_account) -> dict:
    """Compatibility wrapper for MT5-specific call sites."""
    return get_trial_state(user, mt5_account)


def can_use_mt5_sync(user, mt5_account) -> dict:
    """
    Returns {allowed: bool, reason: str}.

    reason values:
      admin, grandfathered, plan_paid, trial_active, trial_not_started,
      expired, paused, schema_compat
    """
    if is_mt5_sync_paused(mt5_account):
        return {"allowed": False, "reason": "paused"}

    privileged_reason = _workflow_access_reason(user)
    if privileged_reason is not None:
        return {"allowed": True, "reason": privileged_reason}

    if not mt5_trial_columns_available():
        return {"allowed": True, "reason": "schema_compat"}

    trial_state = get_trial_state(user, mt5_account)
    state = trial_state["state"]

    if state in ("active", "not_started"):
        return {"allowed": True, "reason": f"trial_{state}"}

    return {"allowed": False, "reason": state}


def get_weekly_followup_message_usage(user, weekly_review=None) -> dict:
    """Count user-sent weekly review follow-up messages for the active trial."""
    limit = WEEKLY_FOLLOWUP_TRIAL_MESSAGE_LIMIT
    if not getattr(user, "id", None):
        return {"used": 0, "limit": limit, "remaining": limit}

    try:
        from models import WeeklyReviewChatMessage

        query = WeeklyReviewChatMessage.query.filter(
            WeeklyReviewChatMessage.user_id == user.id,
            WeeklyReviewChatMessage.role == WeeklyReviewChatMessage.ROLE_USER,
        )
        trial_state = get_trial_state(user, getattr(weekly_review, "trade_account", None))
        started_at = trial_state.get("started_at")
        if trial_state.get("state") == "not_started":
            return {"used": 0, "limit": limit, "remaining": limit}
        if started_at is not None:
            query = query.filter(WeeklyReviewChatMessage.created_at >= started_at)
        used = query.count()
    except Exception:
        used = 0

    return {
        "used": used,
        "limit": limit,
        "remaining": max(limit - used, 0),
    }


def can_use_weekly_followup_chat(user, weekly_review) -> dict:
    """
    Returns display-level access for weekly review follow-up chat.

    Existing generated weekly reviews and saved chat messages should still be
    shown even when this returns allowed=False.
    """
    privileged_reason = _workflow_access_reason(user)
    if privileged_reason is not None:
        return {"allowed": True, "reason": privileged_reason, "cta": None}

    trial_state = get_trial_state(user, getattr(weekly_review, "trade_account", None))
    if trial_state["state"] in ("active", "not_started"):
        return {
            "allowed": True,
            "reason": f"trial_{trial_state['state']}",
            "trial_state": trial_state,
            "cta": None,
        }

    message = (
        "Follow-up chat is part of the Trader workflow. "
        "Join the waitlist for early access."
    )
    if trial_state["state"] in ("expired", "paused"):
        message = (
            "Follow-up chat is part of the Trader workflow. "
            "Your free trial has ended. Join the waitlist for early access."
        )

    return {
        "allowed": False,
        "reason": trial_state["state"],
        "trial_state": trial_state,
        "message": message,
        "cta": _trial_cta("weekly_followup_chat"),
    }


def can_send_weekly_followup_message(user, weekly_review) -> dict:
    """Return whether a new weekly-review follow-up user message may be sent."""
    access = can_use_weekly_followup_chat(user, weekly_review)
    usage = get_weekly_followup_message_usage(user, weekly_review)

    if not access["allowed"]:
        return {
            **access,
            "error": "weekly_followup_trial_required",
            "usage": usage,
        }

    if access["reason"] in ("admin", "schema_compat", "grandfathered", "plan_paid"):
        return {**access, "usage": usage}

    if usage["used"] >= usage["limit"]:
        return {
            "allowed": False,
            "reason": "trial_message_limit_reached",
            "error": "weekly_followup_trial_limit_reached",
            "message": (
                "Your free trial includes 5 follow-up messages. "
                "Join the waitlist to unlock full review conversations."
            ),
            "cta": _trial_cta("weekly_followup_chat"),
            "usage": usage,
            "trial_state": access.get("trial_state"),
        }

    try:
        from models import WeeklyReviewChatMessage

        cutoff = utcnow_naive() - timedelta(seconds=WEEKLY_FOLLOWUP_RATE_WINDOW_SECONDS)
        recent_user_messages = (
            WeeklyReviewChatMessage.query.filter(
                WeeklyReviewChatMessage.user_id == user.id,
                WeeklyReviewChatMessage.role == WeeklyReviewChatMessage.ROLE_USER,
                WeeklyReviewChatMessage.created_at >= cutoff,
            ).count()
        )
    except SQLAlchemyError:
        recent_user_messages = 0

    if recent_user_messages >= WEEKLY_FOLLOWUP_MAX_USER_MESSAGES_PER_MINUTE:
        return {
            "allowed": False,
            "reason": "rate_limit_exceeded",
            "error": "rate_limit_exceeded",
            "message": "You're sending messages too quickly. Try again in a moment.",
            "cta": None,
            "usage": usage,
            "trial_state": access.get("trial_state"),
        }

    return {**access, "usage": usage}


def can_access_advanced_replay(user, timeframe: str, trade=None) -> dict:
    """
    Returns {allowed, required_tier, upgrade_url, reason}.

    Standard M5/M15 replay remains core. Lower-timeframe replay is a premium
    workflow feature allowed for paid/grandfathered/admin users and during an
    active trial.
    """
    tf = str(timeframe or "").strip().upper()
    if tf in FREE_REPLAY_TIMEFRAMES:
        return {
            "allowed": True,
            "required_tier": None,
            "upgrade_url": None,
            "reason": "standard_replay",
        }

    privileged_reason = _workflow_access_reason(user)
    if privileged_reason is not None:
        return {
            "allowed": True,
            "required_tier": None,
            "upgrade_url": None,
            "reason": privileged_reason,
        }

    trial_state = get_trial_state(user, getattr(trade, "trade_account", None))
    if trial_state["state"] == "active":
        return {
            "allowed": True,
            "required_tier": None,
            "upgrade_url": None,
            "reason": "trial_active",
            "trial_state": trial_state,
        }

    return {
        "allowed": False,
        "required_tier": "trader",
        "upgrade_url": TRIAL_UPGRADE_URL,
        "reason": trial_state["state"],
        "trial_state": trial_state,
    }


def can_access_replay_timeframe(user, timeframe: str) -> dict:
    """Compatibility wrapper for replay timeframe access checks."""
    result = can_access_advanced_replay(user, timeframe)
    return {
        "allowed": result["allowed"],
        "required_tier": result["required_tier"],
        "upgrade_url": result["upgrade_url"],
    }


def can_generate_replay_bars(user, trade) -> dict:
    """
    Returns {allowed: bool, reason: str}.

    Free: bars generated only for trades closed within FREE_REPLAY_MAX_TRADE_AGE_DAYS.
    Trader / Pro / Grandfathered: no age limit.
    Admin: always allowed.
    """
    privileged_reason = _workflow_access_reason(user)
    if privileged_reason is not None:
        return {"allowed": True, "reason": privileged_reason}

    closed_at = getattr(trade, "closed_at", None)
    if closed_at is None:
        return {"allowed": False, "reason": "trade_not_closed"}

    age_days = (utcnow_naive() - closed_at).days
    if age_days > FREE_REPLAY_MAX_TRADE_AGE_DAYS:
        return {"allowed": False, "reason": "trade_too_old"}

    return {"allowed": True, "reason": "within_free_age_limit"}


def get_replay_entitlement(user, trade=None) -> dict:
    """
    Returns {allowed_timeframes: frozenset, max_trade_age_days: int | None, label: str}.
    """
    privileged_reason = _workflow_access_reason(user)
    if privileged_reason == "admin":
        return {
            "allowed_timeframes": PRO_REPLAY_TIMEFRAMES,
            "max_trade_age_days": None,
            "label": "Multi-timeframe Replay",
        }
    if privileged_reason == "schema_compat":
        return {
            "allowed_timeframes": TRADER_REPLAY_TIMEFRAMES,
            "max_trade_age_days": TRADER_REPLAY_MAX_TRADE_AGE_DAYS,
            "label": "Advanced Replay",
        }
    if privileged_reason == "plan_paid":
        tier = _user_plan_tier(user)
        if tier == "pro":
            return {
                "allowed_timeframes": PRO_REPLAY_TIMEFRAMES,
                "max_trade_age_days": PRO_REPLAY_MAX_TRADE_AGE_DAYS,
                "label": "Multi-timeframe Replay",
            }
        return {
            "allowed_timeframes": TRADER_REPLAY_TIMEFRAMES,
            "max_trade_age_days": TRADER_REPLAY_MAX_TRADE_AGE_DAYS,
            "label": "Advanced Replay",
        }
    if privileged_reason == "grandfathered":
        return {
            "allowed_timeframes": TRADER_REPLAY_TIMEFRAMES,
            "max_trade_age_days": TRADER_REPLAY_MAX_TRADE_AGE_DAYS,
            "label": "Advanced Replay",
        }

    trial_state = get_trial_state(user, getattr(trade, "trade_account", None))
    if trial_state["state"] == "active":
        return {
            "allowed_timeframes": TRADER_REPLAY_TIMEFRAMES,
            "max_trade_age_days": TRADER_REPLAY_MAX_TRADE_AGE_DAYS,
            "label": "Advanced Replay Trial",
        }

    return {
        "allowed_timeframes": FREE_REPLAY_TIMEFRAMES,
        "max_trade_age_days": FREE_REPLAY_MAX_TRADE_AGE_DAYS,
        "label": "Standard Replay",
    }


def _format_admin_trial_timestamp(value):
    safe = _safe_naive_datetime(value)
    if safe is None:
        return None
    return safe.strftime("%Y-%m-%d %H:%M UTC")


def _trial_pause_reasons_to_clear():
    return frozenset({"expired", "paused", "trial_expired"})


def _user_has_trial_paused_mt5(user) -> bool:
    if not mt5_trial_columns_available() or not getattr(user, "id", None):
        return False
    try:
        from models import MT5Account

        rows = MT5Account.query.filter(
            MT5Account.user_id == user.id,
            MT5Account.sync_paused_at.isnot(None),
        ).all()
    except Exception:
        return False
    for row in rows:
        reason = str(getattr(row, "sync_pause_reason", "") or "").strip().lower()
        if reason in _trial_pause_reasons_to_clear():
            return True
    return False


def build_admin_user_trial_display(user) -> dict:
    """Compact trial summary for the admin Registered Users panel."""
    base = {
        "summary": "Unknown",
        "started_label": None,
        "ends_label": None,
        "days_remaining": None,
        "can_extend": False,
        "extend_default_days": ADMIN_TRIAL_EXTEND_DEFAULT_DAYS,
    }
    if user is None:
        return base

    privileged_reason = _workflow_access_reason(user)
    if privileged_reason == "admin":
        return {
            **base,
            "summary": "Admin — no trial limit",
            "state": "admin",
        }
    if privileged_reason == "schema_compat":
        return {
            **base,
            "summary": "Schema compat — no trial limit",
            "state": "schema_compat",
        }
    if privileged_reason == "grandfathered":
        return {
            **base,
            "summary": "Grandfathered",
            "state": "grandfathered",
        }
    if privileged_reason == "plan_paid":
        tier = _user_plan_tier(user).title()
        return {
            **base,
            "summary": f"{tier} plan — no trial limit",
            "state": "plan_paid",
        }

    trial_state = get_trial_state(user)
    state = trial_state.get("state")
    started_at = trial_state.get("started_at")
    days_remaining = trial_state.get("days_remaining")
    started_label = _format_admin_trial_timestamp(started_at)
    ends_at = (
        _safe_naive_datetime(started_at) + timedelta(days=PREMIUM_TRIAL_DAYS)
        if started_at is not None
        else None
    )
    ends_label = _format_admin_trial_timestamp(ends_at)

    if state == "not_started":
        return {
            **base,
            "summary": f"Not started ({PREMIUM_TRIAL_DAYS}-day trial available)",
            "state": state,
            "days_remaining": PREMIUM_TRIAL_DAYS,
        }

    if state == "active":
        day_word = "day" if days_remaining == 1 else "days"
        return {
            **base,
            "summary": f"Active · {days_remaining} {day_word} left",
            "state": state,
            "started_label": started_label,
            "ends_label": ends_label,
            "days_remaining": days_remaining,
        }

    if state == "paused":
        return {
            **base,
            "summary": "Paused · trial ended",
            "state": state,
            "started_label": started_label,
            "ends_label": ends_label,
            "days_remaining": 0,
            "can_extend": True,
        }

    can_extend = state == "expired" or _user_has_trial_paused_mt5(user)
    return {
        **base,
        "summary": "Expired",
        "state": state or "expired",
        "started_label": started_label,
        "ends_label": ends_label,
        "days_remaining": 0,
        "can_extend": can_extend,
    }


def extend_premium_trial(user, days):
    """
    Grant `days` full trial days remaining from now and resume trial-paused MT5 sync.

    Caller owns the surrounding DB transaction commit.
    """
    if user is None:
        raise ValueError("missing_user")

    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("invalid_days")

    if days < ADMIN_TRIAL_EXTEND_MIN_DAYS or days > ADMIN_TRIAL_EXTEND_MAX_DAYS:
        raise ValueError("invalid_days")

    if _workflow_access_reason(user) is not None:
        raise ValueError("not_extendable")

    trial_state = get_trial_state(user)
    if trial_state.get("state") not in {"expired", "paused"} and not _user_has_trial_paused_mt5(user):
        raise ValueError("trial_not_ended")

    if not user_premium_trial_column_available():
        raise ValueError("schema_unsupported")

    now = utcnow_naive()
    new_started_at = now - timedelta(days=PREMIUM_TRIAL_DAYS - days)
    user.premium_trial_started_at = new_started_at

    resumed_mt5_accounts = []
    if mt5_trial_columns_available():
        from models import MT5Account

        paused_accounts = MT5Account.query.filter(
            MT5Account.user_id == user.id,
            MT5Account.sync_paused_at.isnot(None),
        ).all()
        for mt5_account in paused_accounts:
            reason = str(getattr(mt5_account, "sync_pause_reason", "") or "").strip().lower()
            if reason and reason not in _trial_pause_reasons_to_clear():
                continue
            mt5_account.sync_paused_at = None
            mt5_account.sync_pause_reason = None
            if hasattr(mt5_account, "mt5_trial_started_at"):
                mt5_account.mt5_trial_started_at = new_started_at
            resumed_mt5_accounts.append(mt5_account)

        trade_accounts = getattr(user, "trade_accounts", None)
        if trade_accounts is not None:
            try:
                for trade_account in trade_accounts:
                    if hasattr(trade_account, "mt5_trial_started_at"):
                        trade_account.mt5_trial_started_at = new_started_at
            except TypeError:
                pass

    for mt5_account in resumed_mt5_accounts:
        if mt5_account.is_orphaned:
            continue
        if not str(getattr(mt5_account, "investor_password_encrypted", "") or "").strip():
            continue
        try:
            from celery_workers.mt5_setup_tasks import setup_mt5_terminal
            from helpers.mt5_dispatch import dispatch_mt5_setup

            dispatch_mt5_setup(
                setup_mt5_terminal,
                mt5_account.id,
                target_vm_id=getattr(mt5_account, "vm_id", None),
                allow_failover=True,
                label="admin_trial_extension",
                extra={"mt5_account_id": mt5_account.id},
            )
        except Exception:
            continue

    return {
        "trial_state": get_trial_state(user),
        "days_granted": days,
        "resumed_mt5_count": len(resumed_mt5_accounts),
    }
