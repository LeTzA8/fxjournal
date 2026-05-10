"""
Tests for helpers/entitlements.py.

All helpers use plain SimpleNamespace mocks to avoid DB fixtures where
possible, keeping tests fast and isolated.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import helpers.entitlements as ent


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _user(plan_tier="free", plan_grandfathered=False, is_admin=False,
          email_verified=True, is_admin_field=False):
    return SimpleNamespace(
        plan_tier=plan_tier,
        plan_grandfathered=plan_grandfathered,
        is_admin=is_admin_field or is_admin,
        email_verified=email_verified,
        email="test@example.com",
    )


def _admin_user():
    return _user(is_admin_field=True, email_verified=True)


def _mt5_account(trial_started_at=None, sync_paused_at=None, sync_pause_reason=None):
    return SimpleNamespace(
        mt5_trial_started_at=trial_started_at,
        sync_paused_at=sync_paused_at,
        sync_pause_reason=sync_pause_reason,
    )


def _trade(closed_at=None):
    return SimpleNamespace(closed_at=closed_at)


# ── get_user_plan_state ───────────────────────────────────────────────────────

def test_get_user_plan_state_defaults():
    user = _user()
    state = ent.get_user_plan_state(user)
    assert state["tier"] == "free"
    assert state["grandfathered"] is False


def test_get_user_plan_state_trader():
    user = _user(plan_tier="trader")
    state = ent.get_user_plan_state(user)
    assert state["tier"] == "trader"


def test_get_user_plan_state_grandfathered():
    user = _user(plan_grandfathered=True)
    state = ent.get_user_plan_state(user)
    assert state["grandfathered"] is True


def test_is_mt5_sync_paused_reads_explicit_pause_stamp():
    assert ent.is_mt5_sync_paused(_mt5_account()) is False
    assert ent.is_mt5_sync_paused(_mt5_account(sync_paused_at=datetime.utcnow())) is True


# ── get_mt5_trial_state ───────────────────────────────────────────────────────

def test_mt5_trial_state_grandfathered_user():
    user = _user(plan_grandfathered=True)
    account = _mt5_account()
    state = ent.get_mt5_trial_state(user, account)
    assert state["state"] == "grandfathered"
    assert state["show_trial_ui"] is False
    assert state["days_remaining"] is None


def test_mt5_trial_state_not_started():
    user = _user()
    account = _mt5_account()
    state = ent.get_mt5_trial_state(user, account)
    assert state["state"] == "not_started"
    assert state["days_remaining"] == ent.MT5_TRIAL_DAYS
    assert state["show_trial_ui"] is True


def test_mt5_trial_state_active():
    user = _user()
    started = datetime.utcnow() - timedelta(days=5)
    account = _mt5_account(trial_started_at=started)
    state = ent.get_mt5_trial_state(user, account)
    assert state["state"] == "active"
    assert state["days_remaining"] == ent.MT5_TRIAL_DAYS - 5
    assert state["show_trial_ui"] is True


def test_mt5_trial_state_expired():
    user = _user()
    started = datetime.utcnow() - timedelta(days=ent.MT5_TRIAL_DAYS + 1)
    account = _mt5_account(trial_started_at=started)
    state = ent.get_mt5_trial_state(user, account)
    assert state["state"] == "expired"
    assert state["days_remaining"] == 0
    assert state["show_trial_ui"] is True


def test_mt5_trial_state_paused():
    user = _user()
    paused = datetime.utcnow()
    account = _mt5_account(sync_paused_at=paused, sync_pause_reason="trial_expired")
    state = ent.get_mt5_trial_state(user, account)
    assert state["state"] == "paused"
    assert state["days_remaining"] == 0
    assert state["show_trial_ui"] is True


def test_mt5_trial_state_admin_treated_as_grandfathered(monkeypatch):
    monkeypatch.setattr(ent, "_is_admin", lambda u: True)
    user = _user()
    account = _mt5_account()
    state = ent.get_mt5_trial_state(user, account)
    assert state["state"] == "grandfathered"
    assert state["show_trial_ui"] is False


# ── can_use_mt5_sync ──────────────────────────────────────────────────────────

def test_can_use_mt5_sync_grandfathered():
    user = _user(plan_grandfathered=True)
    account = _mt5_account()
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is True
    assert result["reason"] == "grandfathered"


def test_can_use_mt5_sync_admin(monkeypatch):
    monkeypatch.setattr(ent, "_is_admin", lambda u: True)
    user = _user()
    account = _mt5_account()
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is True
    assert result["reason"] == "admin"


def test_can_use_mt5_sync_billing_not_launched():
    """All non-grandfathered free users are allowed while BILLING_LAUNCH_DATE is None."""
    assert ent.BILLING_LAUNCH_DATE is None
    user = _user()  # free, non-grandfathered
    account = _mt5_account()
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is True
    assert result["reason"] == "billing_not_launched"


def test_can_use_mt5_sync_trader_plan():
    user = _user(plan_tier="trader")
    account = _mt5_account()
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is True
    assert result["reason"] == "plan_paid"


def test_can_use_mt5_sync_expired_trial_blocked_when_billing_live(monkeypatch):
    """Once BILLING_LAUNCH_DATE is set, expired trials are blocked."""
    monkeypatch.setattr(ent, "BILLING_LAUNCH_DATE", datetime(2026, 6, 1))
    user = _user()
    started = datetime.utcnow() - timedelta(days=ent.MT5_TRIAL_DAYS + 2)
    account = _mt5_account(trial_started_at=started)
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is False
    assert result["reason"] == "expired"


def test_can_use_mt5_sync_paused_blocked_when_billing_live(monkeypatch):
    monkeypatch.setattr(ent, "BILLING_LAUNCH_DATE", datetime(2026, 6, 1))
    user = _user()
    account = _mt5_account(sync_paused_at=datetime.utcnow())
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is False
    assert result["reason"] == "paused"


def test_can_use_mt5_sync_paused_blocked_before_billing_launch():
    assert ent.BILLING_LAUNCH_DATE is None
    user = _user()
    account = _mt5_account(sync_paused_at=datetime.utcnow())
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is False
    assert result["reason"] == "paused"


def test_can_use_mt5_sync_active_trial_allowed_when_billing_live(monkeypatch):
    monkeypatch.setattr(ent, "BILLING_LAUNCH_DATE", datetime(2026, 6, 1))
    user = _user()
    started = datetime.utcnow() - timedelta(days=3)
    account = _mt5_account(trial_started_at=started)
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is True
    assert result["reason"] == "trial_active"


# ── can_access_replay_timeframe ───────────────────────────────────────────────

def test_replay_timeframe_free_m5_allowed():
    user = _user()
    result = ent.can_access_replay_timeframe(user, "M5")
    assert result["allowed"] is True


def test_replay_timeframe_free_m15_allowed():
    user = _user()
    result = ent.can_access_replay_timeframe(user, "M15")
    assert result["allowed"] is True


def test_replay_timeframe_free_m1_blocked():
    user = _user()
    result = ent.can_access_replay_timeframe(user, "M1")
    assert result["allowed"] is False
    assert result["required_tier"] == "trader"
    assert result["upgrade_url"] == "/pricing"


def test_replay_timeframe_trader_m1_allowed():
    user = _user(plan_tier="trader")
    result = ent.can_access_replay_timeframe(user, "M1")
    assert result["allowed"] is True


def test_replay_timeframe_grandfathered_m1_allowed():
    """Grandfathered beta users get Trader replay entitlement."""
    user = _user(plan_grandfathered=True)
    result = ent.can_access_replay_timeframe(user, "M1")
    assert result["allowed"] is True


def test_replay_timeframe_admin_always_allowed(monkeypatch):
    monkeypatch.setattr(ent, "_is_admin", lambda u: True)
    user = _user()
    result = ent.can_access_replay_timeframe(user, "M1")
    assert result["allowed"] is True


def test_replay_timeframe_case_insensitive():
    user = _user()
    assert ent.can_access_replay_timeframe(user, "m5")["allowed"] is True
    assert ent.can_access_replay_timeframe(user, "m15")["allowed"] is True
    assert ent.can_access_replay_timeframe(user, "m1")["allowed"] is False


# ── can_generate_replay_bars ──────────────────────────────────────────────────

def test_replay_bars_free_within_age_limit():
    user = _user()
    trade = _trade(closed_at=datetime.utcnow() - timedelta(days=30))
    result = ent.can_generate_replay_bars(user, trade)
    assert result["allowed"] is True


def test_replay_bars_free_exceeds_age_limit():
    user = _user()
    trade = _trade(closed_at=datetime.utcnow() - timedelta(days=ent.FREE_REPLAY_MAX_TRADE_AGE_DAYS + 1))
    result = ent.can_generate_replay_bars(user, trade)
    assert result["allowed"] is False
    assert result["reason"] == "trade_too_old"


def test_replay_bars_free_exactly_at_limit():
    user = _user()
    trade = _trade(closed_at=datetime.utcnow() - timedelta(days=ent.FREE_REPLAY_MAX_TRADE_AGE_DAYS))
    result = ent.can_generate_replay_bars(user, trade)
    assert result["allowed"] is True


def test_replay_bars_trader_no_age_limit():
    user = _user(plan_tier="trader")
    trade = _trade(closed_at=datetime.utcnow() - timedelta(days=500))
    result = ent.can_generate_replay_bars(user, trade)
    assert result["allowed"] is True


def test_replay_bars_grandfathered_no_age_limit():
    """Grandfathered users have Trader entitlement — no age limit during beta."""
    user = _user(plan_grandfathered=True)
    trade = _trade(closed_at=datetime.utcnow() - timedelta(days=500))
    result = ent.can_generate_replay_bars(user, trade)
    assert result["allowed"] is True


def test_replay_bars_free_not_closed():
    user = _user()
    trade = _trade(closed_at=None)
    result = ent.can_generate_replay_bars(user, trade)
    assert result["allowed"] is False
    assert result["reason"] == "trade_not_closed"


def test_replay_bars_admin_always_allowed(monkeypatch):
    monkeypatch.setattr(ent, "_is_admin", lambda u: True)
    user = _user()
    trade = _trade(closed_at=datetime.utcnow() - timedelta(days=1000))
    result = ent.can_generate_replay_bars(user, trade)
    assert result["allowed"] is True


# ── get_replay_entitlement ───────────────────────────────────────────────────

def test_replay_entitlement_free():
    user = _user()
    e = ent.get_replay_entitlement(user)
    assert e["allowed_timeframes"] == ent.FREE_REPLAY_TIMEFRAMES
    assert e["max_trade_age_days"] == ent.FREE_REPLAY_MAX_TRADE_AGE_DAYS
    assert e["label"] == "Standard Replay"


def test_replay_entitlement_trader():
    user = _user(plan_tier="trader")
    e = ent.get_replay_entitlement(user)
    assert e["allowed_timeframes"] == ent.TRADER_REPLAY_TIMEFRAMES
    assert e["label"] == "Advanced Replay"


def test_replay_entitlement_grandfathered():
    user = _user(plan_grandfathered=True)
    e = ent.get_replay_entitlement(user)
    assert e["allowed_timeframes"] == ent.TRADER_REPLAY_TIMEFRAMES
    assert e["label"] == "Advanced Replay"


def test_replay_entitlement_pro():
    user = _user(plan_tier="pro")
    e = ent.get_replay_entitlement(user)
    assert e["allowed_timeframes"] == ent.PRO_REPLAY_TIMEFRAMES
    assert e["label"] == "Multi-timeframe Replay"


# ── Safety: no destructive path triggered by trial expiry ────────────────────

def test_trial_expiry_does_not_trigger_cleanup(monkeypatch):
    """
    When billing is live and trial is expired, can_use_mt5_sync returns
    allowed=False. Verify that no cleanup/archive path is referenced from
    the entitlement module itself.
    """
    monkeypatch.setattr(ent, "BILLING_LAUNCH_DATE", datetime(2026, 6, 1))
    user = _user()
    started = datetime.utcnow() - timedelta(days=ent.MT5_TRIAL_DAYS + 5)
    account = _mt5_account(trial_started_at=started)

    result = ent.can_use_mt5_sync(user, account)

    # Blocked, but the entitlement module itself never calls archive/cleanup.
    # The caller (sync task) handles the pause dispatch; helpers only compute state.
    assert result["allowed"] is False
    assert result["reason"] == "expired"
    # Confirm account object is untouched by this pure function
    assert account.sync_paused_at is None
    assert account.mt5_trial_started_at == started


# ── Grandfathered regression: existing sync users never blocked ───────────────

def test_grandfathered_user_never_blocked_regardless_of_billing(monkeypatch):
    """Grandfathered users pass the sync check even when billing is live."""
    monkeypatch.setattr(ent, "BILLING_LAUNCH_DATE", datetime(2025, 1, 1))
    user = _user(plan_grandfathered=True)
    account = _mt5_account()
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is True
    assert result["reason"] == "grandfathered"


def test_grandfathered_user_with_very_old_trial_still_unblocked(monkeypatch):
    monkeypatch.setattr(ent, "BILLING_LAUNCH_DATE", datetime(2025, 1, 1))
    user = _user(plan_grandfathered=True)
    very_old = datetime(2020, 1, 1)
    account = _mt5_account(trial_started_at=very_old)
    result = ent.can_use_mt5_sync(user, account)
    assert result["allowed"] is True
