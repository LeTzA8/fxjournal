import json

import pytest

from ai_service import build_dashboard_advice_messages, serialize_payload
from helpers.universal_weekly_payload import build_universal_weekly_payload


def _nas100_two_trade_internal_payload():
    strategy_description = (
        "Multi-timeframe ICT strategy: wait for liquidity sweep, "
        "enter on displacement, target opposing liquidity."
    )
    return {
        "notes_confidence": "low",
        "notes_coverage": 0.5,
        "weekly_checkin": {},
        "confidence_envelope": {
            "level": "limited",
            "reasons": ["closed_trade_count_below_3", "notes_confidence_low"],
        },
        "surface_facts": ["Net PnL +530", "Win rate 50%"],
        "summary": {
            "closed_trades": 2,
            "win_rate": 50.0,
            "net_pnl": 530.0,
            "best_trade_pnl": 1004.75,
            "worst_trade_pnl": -474.75,
            "max_drawdown": 474.75,
            "largest_trade_symbol": "NAS100",
            "largest_trade_abs_pnl_share_pct": 65.0,
            "top_symbol_by_trade_count": "NAS100",
            "top_symbol_trade_share_pct": 100.0,
            "bundle_count": 0,
            "confirmed_revenge_trade_count": 0,
        },
        "current_week_breakdowns": {
            "risk_authority": {
                "basis": "pct_of_account",
                "stable": True,
                "dispersion_pct": 0.0,
                "risk_judgment_allowed": True,
            },
            "post_loss_response": {
                "sequences": [
                    {
                        "loss_ref": "T2",
                        "next_ref": "T1",
                        "risk_change": "same",
                        "next_outcome": "win",
                        "loss_pnl": -474.75,
                    }
                ],
                "pattern_count": 1,
                "repeated_increased_risk": False,
                "biggest_loss": {"loss_ref": "T2", "next_ref": "T1"},
            },
            "revenge_evidence": {"pattern_class": "isolated", "confirmed_count": 0, "heuristic_count": 1},
            "execution_outcome": {
                "outcome_class": "good",
                "execution_class": "bad",
                "week_archetype": "bad_execution_good_outcome",
                "coaching_stance": "good_week_but_habits_are_leaking",
                "primary_issue": "single_same_symbol_after_loss",
                "issue_evidence_level": "isolated",
            },
        },
        "recent_experiments": [
            {
                "period_start_utc": "2026-05-05T21:30:00Z",
                "text": "Wait after a loss before the next entry and write what is different.",
            },
        ],
        "experiment_context": {"eligible": False, "trade_idea_count": 2, "min_required": 1},
        "trades": [
            {
                "review_ref": "T1",
                "trade_id": 101,
                "symbol": "NAS100",
                "side": "BUY",
                "entry_price": 18200.0,
                "exit_price": 18300.0,
                "stop_loss": 18150.0,
                "take_profit": 18400.0,
                "lot_size": 1.0,
                "planned_risk_dollars": 50.0,
                "trade_risk_pct": 0.5,
                "pnl": 1004.75,
                "closed_at": "2026-05-15T14:26:00Z",
                "opened_at": "2026-05-15T14:00:00Z",
                "duration_minutes": 26.0,
                "entry_session": "London",
                "exit_session": "London",
                "session": "London",
                "strategy_name": "Multi Timeframe ICT Strategy",
                "strategy_version": 4,
                "strategy_description": strategy_description,
                "trade_note": "Liquidity sweep then displacement entry.",
                "is_post_loss_same_symbol_trade": True,
                "same_symbol_reentry": True,
                "same_trade_idea_reentry": True,
                "is_post_loss_trade": True,
                "minutes_since_prev_symbol_close": 26.0,
                "prev_symbol_trade_pnl": -474.75,
                "risk_pct_vs_prev_symbol": "same",
                "market_context": {
                    "bars_status": "ready",
                    "mfe_r": 2.2,
                    "mae_r": 0.87,
                    "post_exit_direction": "continued",
                    "post_exit_tp_reached": True,
                    "entry_active_sessions": ["London", "New York"],
                    "entry_in_session_overlap": True,
                },
            },
            {
                "review_ref": "T2",
                "trade_id": 102,
                "symbol": "NAS100",
                "side": "SELL",
                "pnl": -474.75,
                "closed_at": "2026-05-15T13:30:00Z",
                "opened_at": "2026-05-15T13:00:00Z",
                "duration_minutes": 30.0,
                "entry_session": "London",
                "exit_session": "London",
                "session": "London",
                "strategy_name": "Multi Timeframe ICT Strategy",
                "strategy_version": 4,
                "strategy_description": strategy_description,
            },
        ],
    }


def test_universal_payload_removes_note_metadata():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    serialized = json.dumps(payload)
    assert "notes_coverage" not in serialized
    assert "notes_confidence" not in serialized
    assert "notes_basis" not in serialized
    assert "generated_at" not in serialized
    assert "period_start_utc" not in serialized
    assert "surface_facts" not in serialized


def test_universal_payload_strategy_dedupe():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    strategies = payload["strategy_context"]["strategies_used"]
    assert len(strategies) == 1
    assert strategies[0]["strategy_ref"] == "S1"
    assert all(trade.get("strategy_ref") == "S1" for trade in payload["trades"])
    assert "strategy_description" not in json.dumps(payload["trades"])


def test_universal_payload_trade_cleanup():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    trade = payload["trades"][0]
    forbidden = {
        "trade_id",
        "contract_code",
        "opened_at",
        "closed_at",
        "entry_price",
        "exit_price",
        "stop_loss",
        "take_profit",
        "lot_size",
        "planned_risk_dollars",
        "prev_trade_pnl",
        "prev_symbol_trade_pnl",
        "minutes_since_prev_close",
        "minutes_since_prev_symbol_close",
        "outlier_lot_spike",
        "market_context",
        "review_ref",
        "post_loss_context",
        "outlier_size",
    }
    for key in forbidden:
        assert key not in trade
    assert trade["ref"] == "T1"
    assert trade["symbol"] == "NAS100"
    assert trade["trade_note"]
    assert trade["trade_date_label"] == "15 May 2026 (Fri)"
    assert trade["market_context_summary"]["mfe_r"] == 2.2


def test_universal_payload_evidence_boundary_merged():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    boundary = payload["evidence_boundary"]
    assert boundary["claim_scope"] == "narrow"
    assert boundary["review_mode"] == "focused_sequence"
    assert "notes" not in boundary["instruction"].lower()
    assert "review_scope" not in payload
    assert "sample_context" not in payload


def test_universal_payload_post_loss_canonicalization():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    serialized = json.dumps(payload)
    sequences = payload["post_loss_sequences"]
    assert len(sequences) >= 1
    t2_t1 = next(s for s in sequences if s["loss_ref"] == "T2" and s["next_ref"] == "T1")
    assert t2_t1["minutes_after_loss"] == 26.0
    assert t2_t1["sequence_outcome"] == "winning_retry"
    assert t2_t1["previous_loss_pnl"] == -474.75
    assert payload["biggest_loss_ref"] == "T2"
    assert "primary_sequences" not in serialized
    assert "post_loss_response" not in serialized
    assert "post_loss_context" not in serialized
    assert "strong_sequences" not in serialized


def test_universal_payload_no_prose_in_triggers():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    for trigger in payload.get("coaching_frame_triggers") or []:
        assert "instruction" not in trigger
        assert "suggested_lesson" not in trigger
    assert "phrase" not in json.dumps(payload)


def test_universal_payload_issue_scope_replaces_evidence_level():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    assert payload["week_summary"]["issue_scope"] == "isolated"
    assert "primary_issue_evidence_level" not in payload["week_summary"]
    assert "outcome_class" not in payload["week_summary"]
    assert "bundle_count" not in payload["week_summary"]
    assert payload["week_summary"]["trade_idea_count"] == 2


def test_universal_payload_slim_risk_authority():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    risk = payload["risk_authority"]
    assert risk["basis"] == "pct_of_account"
    assert "dispersion_pct" not in risk
    assert "value" not in risk


def test_universal_payload_lot_only_risk_constraint():
    internal = _nas100_two_trade_internal_payload()
    internal["current_week_breakdowns"]["risk_authority"] = {
        "basis": "lots",
        "risk_judgment_allowed": False,
    }
    payload = build_universal_weekly_payload(internal)
    claims = payload["constraints"]["do_not_claim"]
    assert any("lot size" in item.lower() for item in claims)


def test_nas100_scenario_reward_cost_frame():
    payload = build_universal_weekly_payload(_nas100_two_trade_internal_payload())
    frames = [t.get("frame") for t in payload.get("coaching_frame_triggers") or []]
    assert "reward_cost_mislesson" in frames
    t2_t1 = next(
        s for s in payload["post_loss_sequences"] if s["loss_ref"] == "T2" and s["next_ref"] == "T1"
    )
    assert "reward_cost_mislesson" in (t2_t1.get("frame_triggers") or [])


def test_build_dashboard_advice_messages_uses_same_universal_payload(app_ctx):
    internal = _nas100_two_trade_internal_payload()
    universal = build_universal_weekly_payload(internal)
    _, messages, payload_json = build_dashboard_advice_messages(universal)

    user_text = messages[2]["content"][0]["text"]
    assert user_text.startswith("WEEKLY REVIEW DATA\n\n")
    assert json.loads(payload_json) == universal
    assert '"ref": "T1"' in user_text or '"ref":"T1"' in user_text.replace(" ", "")
    assert "notes_coverage" not in user_text
    assert "post_loss_sequences" in user_text
