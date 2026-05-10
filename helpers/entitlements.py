"""
Central entitlement helpers for plan/access checks.

All gating decisions go through these helpers — never scattered across routes
or templates. All constants live here; change once, affects everywhere.

BILLING_LAUNCH_DATE controls whether MT5 trial expiry is enforced.
While None (pre-billing), trial expiry is not enforced; explicit sync pauses
are still respected so paused terminals do not consume VM resources.
Set to a naive UTC datetime when Stripe billing goes live.
"""

from helpers.utils import utcnow_naive
from helpers.schema_compat import mt5_trial_columns_available, user_entitlement_columns_available

# ── MT5 trial config ─────────────────────────────────────────────────────────

MT5_TRIAL_DAYS = 14

# Set to a naive UTC datetime when Stripe billing goes live.
# While None, trial expiry is never enforced; explicit sync pauses still block.
BILLING_LAUNCH_DATE = None

# ── Replay entitlement config ────────────────────────────────────────────────

FREE_REPLAY_TIMEFRAMES = frozenset({"M5", "M15"})
TRADER_REPLAY_TIMEFRAMES = frozenset({"M1", "M5", "M15"})
PRO_REPLAY_TIMEFRAMES = frozenset({"M1", "M5", "M15"})  # Reserved for future deeper timeframes

FREE_REPLAY_MAX_TRADE_AGE_DAYS = 90    # Auto-bar-sync only for trades within 90 days for Free
TRADER_REPLAY_MAX_TRADE_AGE_DAYS = 365
PRO_REPLAY_MAX_TRADE_AGE_DAYS = None   # Unlimited

_PLAN_TIER_RANK = {"free": 0, "trader": 1, "pro": 2}


# ── Internal helpers ─────────────────────────────────────────────────────────

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


def _is_admin(user) -> bool:
    from auth_account import user_has_admin_access
    return user_has_admin_access(user)


# ── Public API ───────────────────────────────────────────────────────────────

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


def get_mt5_trial_state(user, mt5_account) -> dict:
    """
    Returns the MT5 trial state for one MT5 account.

    Possible states:
      "grandfathered"  — existing beta user, no expiry applies
      "not_started"    — non-grandfathered user, trial clock not yet started
      "active"         — trial running, days_remaining > 0
      "expired"        — trial ran out (billing not yet live so sync still works)
      "paused"         — sync explicitly paused after billing went live

    show_trial_ui is False for grandfathered users so no badge is shown.
    """
    if not mt5_trial_columns_available():
        return {
            "state": "grandfathered",
            "days_remaining": None,
            "started_at": None,
            "show_trial_ui": False,
        }

    paused_at = getattr(mt5_account, "sync_paused_at", None)
    if paused_at is not None:
        return {
            "state": "paused",
            "days_remaining": 0,
            "started_at": getattr(mt5_account, "mt5_trial_started_at", None),
            "show_trial_ui": True,
        }

    if _is_admin(user) or _user_is_grandfathered(user):
        return {
            "state": "grandfathered",
            "days_remaining": None,
            "started_at": None,
            "show_trial_ui": False,
        }

    started_at = getattr(mt5_account, "mt5_trial_started_at", None)
    if started_at is None:
        return {
            "state": "not_started",
            "days_remaining": MT5_TRIAL_DAYS,
            "started_at": None,
            "show_trial_ui": True,
        }

    now = utcnow_naive()
    elapsed_days = (now - started_at).days
    days_remaining = max(MT5_TRIAL_DAYS - elapsed_days, 0)

    if days_remaining <= 0:
        return {
            "state": "expired",
            "days_remaining": 0,
            "started_at": started_at,
            "show_trial_ui": True,
        }

    return {
        "state": "active",
        "days_remaining": days_remaining,
        "started_at": started_at,
        "show_trial_ui": True,
    }


def can_use_mt5_sync(user, mt5_account) -> dict:
    """
    Returns {allowed: bool, reason: str}.

    reason values:
      admin                — admin user, always allowed
      grandfathered        — existing beta user, always allowed
      billing_not_launched — BILLING_LAUNCH_DATE is None, no enforcement yet
      plan_paid            — Trader or Pro plan
      trial_active         — trial running
      trial_not_started    — trial not yet started (first sync stamps it)
      trial_expired        — trial ended but billing not live (allowed)
      paused               — sync explicitly paused (blocked)
    """
    if is_mt5_sync_paused(mt5_account):
        return {"allowed": False, "reason": "paused"}

    if _is_admin(user):
        return {"allowed": True, "reason": "admin"}

    if not user_entitlement_columns_available() or not mt5_trial_columns_available():
        return {"allowed": True, "reason": "schema_compat"}

    if _user_is_grandfathered(user):
        return {"allowed": True, "reason": "grandfathered"}

    tier = _user_plan_tier(user)
    if _PLAN_TIER_RANK.get(tier, 0) >= _PLAN_TIER_RANK["trader"]:
        return {"allowed": True, "reason": "plan_paid"}

    if BILLING_LAUNCH_DATE is None:
        return {"allowed": True, "reason": "billing_not_launched"}

    trial_state = get_mt5_trial_state(user, mt5_account)
    state = trial_state["state"]

    if state in ("active", "not_started"):
        return {"allowed": True, "reason": f"trial_{state}"}

    return {"allowed": False, "reason": state}


def can_access_replay_timeframe(user, timeframe: str) -> dict:
    """
    Returns {allowed: bool, required_tier: str | None, upgrade_url: str | None}.

    Free: M5 and M15 only.
    Trader / Pro / Grandfathered: M1 unlocked (and any future timeframes).
    Admin: always allowed.
    """
    if _is_admin(user):
        return {"allowed": True, "required_tier": None, "upgrade_url": None}

    if not user_entitlement_columns_available():
        return {"allowed": True, "required_tier": None, "upgrade_url": None}

    tf = str(timeframe or "").strip().upper()
    tier = _user_plan_tier(user)
    grandfathered = _user_is_grandfathered(user)

    if tier in ("trader", "pro") or grandfathered:
        allowed_tfs = TRADER_REPLAY_TIMEFRAMES
    else:
        allowed_tfs = FREE_REPLAY_TIMEFRAMES

    if tf in allowed_tfs:
        return {"allowed": True, "required_tier": None, "upgrade_url": None}

    return {
        "allowed": False,
        "required_tier": "trader",
        "upgrade_url": "/pricing",
    }


def can_generate_replay_bars(user, trade) -> dict:
    """
    Returns {allowed: bool, reason: str}.

    Free: bars generated only for trades closed within FREE_REPLAY_MAX_TRADE_AGE_DAYS.
    Trader / Pro / Grandfathered: no age limit.
    Admin: always allowed.
    """
    if _is_admin(user):
        return {"allowed": True, "reason": "admin"}

    if not user_entitlement_columns_available():
        return {"allowed": True, "reason": "schema_compat"}

    tier = _user_plan_tier(user)
    grandfathered = _user_is_grandfathered(user)

    if tier in ("trader", "pro") or grandfathered:
        return {"allowed": True, "reason": "plan_allows"}

    closed_at = getattr(trade, "closed_at", None)
    if closed_at is None:
        return {"allowed": False, "reason": "trade_not_closed"}

    now = utcnow_naive()
    age_days = (now - closed_at).days
    if age_days > FREE_REPLAY_MAX_TRADE_AGE_DAYS:
        return {"allowed": False, "reason": "trade_too_old"}

    return {"allowed": True, "reason": "within_free_age_limit"}


def get_replay_entitlement(user) -> dict:
    """
    Returns {allowed_timeframes: frozenset, max_trade_age_days: int | None, label: str}.
    """
    if _is_admin(user):
        return {
            "allowed_timeframes": PRO_REPLAY_TIMEFRAMES,
            "max_trade_age_days": None,
            "label": "Multi-timeframe Replay",
        }

    if not user_entitlement_columns_available():
        return {
            "allowed_timeframes": TRADER_REPLAY_TIMEFRAMES,
            "max_trade_age_days": TRADER_REPLAY_MAX_TRADE_AGE_DAYS,
            "label": "Advanced Replay",
        }

    tier = _user_plan_tier(user)
    grandfathered = _user_is_grandfathered(user)

    if tier == "pro":
        return {
            "allowed_timeframes": PRO_REPLAY_TIMEFRAMES,
            "max_trade_age_days": PRO_REPLAY_MAX_TRADE_AGE_DAYS,
            "label": "Multi-timeframe Replay",
        }
    if tier == "trader" or grandfathered:
        return {
            "allowed_timeframes": TRADER_REPLAY_TIMEFRAMES,
            "max_trade_age_days": TRADER_REPLAY_MAX_TRADE_AGE_DAYS,
            "label": "Advanced Replay",
        }
    return {
        "allowed_timeframes": FREE_REPLAY_TIMEFRAMES,
        "max_trade_age_days": FREE_REPLAY_MAX_TRADE_AGE_DAYS,
        "label": "Standard Replay",
    }
