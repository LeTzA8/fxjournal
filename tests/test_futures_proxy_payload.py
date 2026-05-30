"""Tests for universal weekly payload — proxy replay suppression.

Verifies:
- Proxy trades get replay_accuracy='approximate' in the serialized entry
- Bar-derived numeric fields (mfe_r, mae_r, tp_capture_pct, etc.) are suppressed
  for proxy trades
- Normal CFD/MT5 trades are unaffected
"""

import pytest

from helpers.universal_weekly_payload import _build_universal_trade


def _make_trade_dict(**overrides):
    """Minimal trade dict matching what ai_service.py builds."""
    base = {
        "review_ref": "T1",
        "trade_id": 1,
        "symbol": "EURUSD",
        "contract_code": None,
        "strategy_name": None,
        "strategy_version": None,
        "strategy_description": None,
        "side": "BUY",
        "entry_price": 1.09,
        "exit_price": 1.095,
        "stop_loss": 1.085,
        "take_profit": 1.10,
        "lot_size": 1.0,
        "pnl": 500.0,
        "planned_risk_dollars": None,
        "trade_risk_pct": None,
        "entry_session": "London",
        "exit_session": "London",
        "session": "London",
        "duration_minutes": 60,
        "opened_at": "2026-03-10 09:00:00",
        "closed_at": "2026-03-10 10:00:00",
        "trade_sequence_number": 1,
        "trade_number_in_session": 1,
        "prev_trade_pnl": None,
        "minutes_since_prev_close": None,
        "prev_symbol_trade_pnl": None,
        "minutes_since_prev_symbol_close": None,
        "loss_streak_before_trade": None,
        "is_post_loss_trade": False,
        "same_symbol_reentry": False,
        "is_post_loss_same_symbol_trade": False,
        "same_trade_idea_reentry": False,
        "is_potential_revenge": False,
        "is_potential_reactive": False,
        "trade_note": None,
        "is_revenge": False,
        "is_reactive": False,
        "is_corrective": False,
        "bundle_pubkey": None,
        "is_bundle": False,
        "bundle_trade_count": 1,
        "planned_rr": 2.0,
        "realized_rr": 1.0,
        "tp_capture_pct": 50.0,
        "closed_before_tp": True,
        "closed_before_sl": False,
        "outlier_size": False,
        "outlier_lot_spike": False,
        "possible_split_order": False,
        "split_group_size": 1,
        "split_group_index": 1,
        "split_group_role": "solo",
        "is_likely_corrective": False,
        "market_context": {
            "bars_status": "ready",
            "mfe_r": 1.2,
            "mae_r": 0.3,
            "post_exit_direction": "continued",
            "post_exit_tp_reached": False,
            "entry_in_session_overlap": False,
            "entry_active_sessions": ["London"],
        },
        "proxy_replay_symbol": None,
        "proxy_replay_status": None,
    }
    base.update(overrides)
    return base


def test_proxy_trade_payload_has_replay_accuracy(app_ctx):
    trade = _make_trade_dict(
        symbol="NQ",
        contract_code="NQH6",
        proxy_replay_symbol="NAS100",
        proxy_replay_status="pending",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    assert entry.get("replay_accuracy") == "approximate"


def test_proxy_trade_payload_has_chart_price_source(app_ctx):
    trade = _make_trade_dict(
        proxy_replay_symbol="NAS100",
        proxy_replay_status="pending",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    assert entry.get("chart_price_source") == "cfd_proxy"


def test_proxy_trade_payload_suppresses_tp_capture_pct(app_ctx):
    trade = _make_trade_dict(
        proxy_replay_symbol="NAS100",
        proxy_replay_status="pending",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    assert "tp_capture_pct" not in entry


def test_proxy_trade_payload_suppresses_closed_before_tp(app_ctx):
    trade = _make_trade_dict(
        proxy_replay_symbol="NAS100",
        proxy_replay_status="pending",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    assert "closed_before_tp" not in entry


def test_proxy_trade_payload_suppresses_closed_before_sl(app_ctx):
    trade = _make_trade_dict(
        proxy_replay_symbol="NAS100",
        proxy_replay_status="pending",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    assert "closed_before_sl" not in entry


def test_proxy_trade_payload_suppresses_mfe_mae_from_market_context(app_ctx):
    """mfe_r and mae_r must be stripped from market_context_summary for proxy trades."""
    trade = _make_trade_dict(
        proxy_replay_symbol="NAS100",
        proxy_replay_status="pending",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    mcs = entry.get("market_context_summary") or {}
    assert "mfe_r" not in mcs
    assert "mae_r" not in mcs


def test_proxy_trade_payload_suppresses_post_exit_direction(app_ctx):
    trade = _make_trade_dict(
        proxy_replay_symbol="NAS100",
        proxy_replay_status="pending",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    mcs = entry.get("market_context_summary") or {}
    assert "post_exit_direction" not in mcs
    assert "post_exit_tp_reached" not in mcs


def test_native_cfd_trade_payload_unaffected(app_ctx):
    """Normal CFD/MT5 trade must still carry all bar-derived fields."""
    trade = _make_trade_dict()  # no proxy fields
    entry = _build_universal_trade(trade, strategy_ref=None)

    assert "replay_accuracy" not in entry
    assert entry.get("tp_capture_pct") == 50.0
    assert entry.get("closed_before_tp") is True
    mcs = entry.get("market_context_summary") or {}
    assert mcs.get("mfe_r") == 1.2
    assert mcs.get("mae_r") == 0.3


def test_proxy_trade_no_proxy_symbol_still_gets_replay_accuracy(app_ctx):
    """Status set but proxy_symbol is None (e.g. unavailable_no_mapping) still gets annotation."""
    trade = _make_trade_dict(
        proxy_replay_symbol=None,
        proxy_replay_status="unavailable_no_mapping",
    )
    entry = _build_universal_trade(trade, strategy_ref=None)
    assert entry.get("replay_accuracy") == "approximate"
    # chart_price_source only set when proxy_symbol is present
    assert "chart_price_source" not in entry
