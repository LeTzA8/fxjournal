import json

import pytest

from ai_service import build_dashboard_advice_messages, serialize_payload
from helpers.weekly_prompt_payload import (
    build_weekly_prompt_payload,
    format_weekly_prompt_payload,
)


def _nas100_two_trade_full_payload():
    """Two-trade NAS100 week: loss then winning same-symbol retry."""
    strategy_description = (
        "Multi-timeframe ICT strategy: wait for liquidity sweep, "
        "enter on displacement, target opposing liquidity."
    )
    return {
        "generated_at": "2026-05-20T12:00:00Z",
        "period_start_utc": "2026-05-12T21:30:00Z",
        "period_end_utc": "2026-05-19T21:30:00Z",
        "notes_coverage": 0.0,
        "notes_confidence": "low",
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
            "confirmed_revenge_trade_count": 0,
        },
        "current_week_breakdowns": {
            "risk_authority": {
                "basis": "lots",
                "stable": True,
                "dispersion_pct": 0.0,
                "risk_judgment_allowed": False,
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
            },
            "revenge_evidence": {"pattern_class": "isolated", "confirmed_count": 0},
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
            {
                "period_start_utc": "2026-04-28T21:30:00Z",
                "text": "Only one trade per symbol per day.",
            },
        ],
        "experiment_context": {"eligible": False, "trade_idea_count": 2},
        "trades": [
            {
                "review_ref": "T1",
                "trade_id": 101,
                "symbol": "NAS100",
                "side": "BUY",
                "entry_price": 18200.0,
                "exit_price": 18280.0,
                "stop_loss": 18150.0,
                "take_profit": 18350.0,
                "pnl": 1004.75,
                "realized_rr": 2.08,
                "duration_minutes": 150.35,
                "entry_session": "London / New York",
                "exit_session": "New York",
                "strategy_name": "Multi Timeframe ICT Strategy",
                "strategy_version": 3,
                "strategy_description": strategy_description,
                "opened_at": "2026-05-15T14:26:33Z",
                "closed_at": "2026-05-15T16:56:54Z",
                "bundle_pubkey": None,
                "is_bundle": False,
                "is_revenge": False,
                "is_post_loss_same_symbol_trade": True,
                "same_symbol_reentry": True,
                "same_trade_idea_reentry": True,
                "prev_symbol_trade_pnl": -474.75,
                "minutes_since_prev_symbol_close": 26.55,
                "size_vs_prev_symbol_trade": "same",
                "market_context": {
                    "bars_status": "ready",
                    "entry_bar_closes_in_trade_direction": False,
                    "mfe_r": 2.2,
                    "mae_r": 0.87,
                    "post_exit_direction": "continued",
                    "post_exit_tp_reached": True,
                    "in_trade_bars": 30,
                    "post_exit_bars": 12,
                },
            },
            {
                "review_ref": "T2",
                "trade_id": 102,
                "symbol": "NAS100",
                "side": "BUY",
                "entry_price": 18180.0,
                "exit_price": 18140.0,
                "stop_loss": 18130.0,
                "take_profit": 18280.0,
                "pnl": -474.75,
                "realized_rr": -0.69,
                "duration_minutes": 45.0,
                "entry_session": "London",
                "exit_session": "London / New York",
                "strategy_name": "Multi Timeframe ICT Strategy",
                "strategy_version": 3,
                "strategy_description": strategy_description,
                "opened_at": "2026-05-15T14:00:00Z",
                "closed_at": "2026-05-15T14:25:00Z",
                "bundle_pubkey": None,
                "is_bundle": False,
                "is_revenge": False,
                "market_context": {"bars_status": "ready"},
            },
        ],
    }


def test_strategy_dedupe_appears_once_with_refs():
    full = _nas100_two_trade_full_payload()
    prompt_payload = build_weekly_prompt_payload(full)

    strategies = prompt_payload["strategy_context"]["strategies_used"]
    assert len(strategies) == 1
    assert strategies[0]["strategy_ref"] == "S1"
    assert "Multi Timeframe ICT Strategy" in strategies[0]["name"]
    assert strategies[0]["description"]

    trades = prompt_payload["high_signal_trades"]
    assert all("strategy_description" not in trade for trade in trades)
    assert trades[0]["strategy_ref"] == "S1"
    assert trades[1]["strategy_ref"] == "S1"


def test_strategy_dedupe_multiple_strategies():
    full = _nas100_two_trade_full_payload()
    full["trades"][1]["strategy_name"] = "London Breakout"
    full["trades"][1]["strategy_version"] = 1
    full["trades"][1]["strategy_description"] = "Break of London range."

    prompt_payload = build_weekly_prompt_payload(full)
    refs = {item["strategy_ref"] for item in prompt_payload["strategy_context"]["strategies_used"]}
    assert refs == {"S1", "S2"}


def test_prompt_payload_excludes_noisy_fields():
    full = _nas100_two_trade_full_payload()
    prompt_payload = build_weekly_prompt_payload(full)
    prompt_text = format_weekly_prompt_payload(prompt_payload)
    serialized = json.dumps(prompt_payload)

    assert "surface_facts" not in serialized
    assert "opened_at" not in serialized
    assert "closed_at" not in serialized
    assert "entry_price" not in serialized
    assert "exit_price" not in serialized
    assert "stop_loss" not in serialized
    assert "take_profit" not in serialized
    assert '"trade_id"' not in serialized
    assert "bundle_pubkey" not in serialized
    assert full["trades"][0]["strategy_description"] in serialized
    assert full["trades"][0]["strategy_description"] not in prompt_text.split("HIGH_SIGNAL_TRADES", 1)[1]


def test_bundle_semantics_preserved_in_prompt_payload():
    full = _nas100_two_trade_full_payload()
    full["trades"][0]["review_ref"] = "B1"
    full["trades"][0]["is_bundle"] = True
    full["trades"][0]["bundle_trade_count"] = 3
    full["trades"][0]["bundle_pubkey"] = "bundle-abc123"

    prompt_payload = build_weekly_prompt_payload(full)
    bundle_trade = next(t for t in prompt_payload["high_signal_trades"] if t["ref"] == "B1")

    assert bundle_trade["is_bundle"] is True
    assert bundle_trade["bundle_trade_count"] == 3
    assert "bundle_summary" in bundle_trade
    assert "bundle_pubkey" not in json.dumps(prompt_payload)
    assert full["trades"][0]["bundle_pubkey"] == "bundle-abc123"


def test_low_trade_week_sample_context_and_constraints():
    full = _nas100_two_trade_full_payload()
    prompt_payload = build_weekly_prompt_payload(full)

    sample = prompt_payload["sample_context"]
    assert sample["review_mode"] == "specific_trade_sequence"
    assert sample["claim_scope"] == "trade_specific_not_pattern_level"
    assert "low confidence" not in sample["instruction"].lower()
    assert "not enough data" not in sample["instruction"].lower()

    constraints = prompt_payload["constraints"]["do_not_claim"]
    assert any("repeated pattern" in item for item in constraints)
    assert any("1-2 trade sample" in item for item in constraints)
    assert any("low-value" in item or "low-confidence" in item for item in constraints)
    assert prompt_payload["primary_sequences"]


def test_winning_same_symbol_retry_triggers():
    full = _nas100_two_trade_full_payload()
    prompt_payload = build_weekly_prompt_payload(full)

    sequences = prompt_payload["primary_sequences"]
    assert len(sequences) >= 1
    seq = sequences[0]
    assert seq["type"] == "same_symbol_after_loss"
    assert seq["loss_ref"] == "T2"
    assert seq["next_ref"] == "T1"
    assert seq["sequence_outcome"] == "winning_retry"
    assert seq["same_side"] is True
    assert seq["minutes_after_loss"] == pytest.approx(26.55)

    frames = prompt_payload["coaching_frame_triggers"]
    reward_frame = next(item for item in frames if item["frame"] == "reward_cost_mislesson")
    assert reward_frame["trigger"] == "winning_same_symbol_retry_after_loss"
    assert reward_frame["refs"] == ["T2", "T1"]

    claims = prompt_payload["constraints"]["do_not_claim"]
    assert "Do not call this confirmed revenge." in claims
    assert "Do not claim size escalation." in claims


def test_same_symbol_reentry_after_win_is_not_labeled_after_loss():
    full = _nas100_two_trade_full_payload()
    full["summary"]["closed_trades"] = 2
    full["current_week_breakdowns"]["post_loss_response"] = {"sequences": []}
    full["current_week_breakdowns"]["execution_outcome"]["primary_issue"] = None
    full["trades"][0]["pnl"] = 1004.75
    full["trades"][0]["is_post_loss_same_symbol_trade"] = False
    full["trades"][0]["same_symbol_reentry"] = True
    full["trades"][0]["same_trade_idea_reentry"] = True
    full["trades"][0]["prev_symbol_trade_pnl"] = 240.0
    full["trades"][1]["pnl"] = 240.0

    prompt_payload = build_weekly_prompt_payload(full)
    reentry = next(t for t in prompt_payload["high_signal_trades"] if t["ref"] == "T1")

    assert reentry["same_symbol_reentry"] is True
    assert reentry["same_trade_idea_reentry"] is True
    assert "same_symbol_after_loss" not in reentry
    assert "post_loss_context" not in reentry
    assert prompt_payload["primary_sequences"] == []


def test_market_context_compression():
    full = _nas100_two_trade_full_payload()
    prompt_payload = build_weekly_prompt_payload(full)

    winner = next(t for t in prompt_payload["high_signal_trades"] if t["ref"] == "T1")
    summary = winner["market_context_summary"]
    assert summary["bars_status"] == "ready"
    assert summary["mfe_r"] == pytest.approx(2.2)
    assert "in_trade_bars" not in summary
    assert full["trades"][0]["market_context"]["in_trade_bars"] == 30


def test_relevant_continuity_matches_post_loss_experiments():
    full = _nas100_two_trade_full_payload()
    prompt_payload = build_weekly_prompt_payload(full)

    continuity = prompt_payload["relevant_continuity"]
    assert continuity["recent_experiment_theme"] == "post-loss re-entry control"
    assert len(continuity["prior_rules"]) >= 1
    assert "same-symbol" in continuity["relevance"].lower() or "NAS100" in continuity["relevance"]


def test_prompt_includes_strength_safety_rules():
    from ai_service import load_prompt_text

    prompt_text = load_prompt_text("dashboard_advice.txt")["prompt_text"]
    normalized = prompt_text.replace("\n", " ")
    assert "Do not force a strength" in prompt_text
    assert "Do not praise the behavior being flagged as risky" in prompt_text
    assert "Size stability does not validate" in prompt_text
    assert "reward_cost_mislesson" in prompt_text
    assert "narrow review, not a weak one" in prompt_text
    assert "planned re-entry vs post-hoc justification" in normalized
    assert "write the re-entry reason before entering" in normalized


NAS100_PLANNED_REENTRY_NOTE = (
    "First trade felt rushed; got swept for liquidity. HTF structure unchanged "
    "and price still rejecting from the 4H FVG. Second trade was a better "
    "planned re-entry on the same idea."
)


def test_winning_same_symbol_retry_exposes_trade_note_to_model(app_ctx):
    full = _nas100_two_trade_full_payload()
    full["trades"][0]["trade_note"] = NAS100_PLANNED_REENTRY_NOTE
    full["notes_coverage"] = 1.0
    full["notes_confidence"] = "high"

    prompt_payload = build_weekly_prompt_payload(full)
    winner = next(t for t in prompt_payload["high_signal_trades"] if t["ref"] == "T1")
    assert winner["trade_note"] == NAS100_PLANNED_REENTRY_NOTE

    prompt_text = format_weekly_prompt_payload(prompt_payload)
    assert "4H FVG" in prompt_text
    assert "swept for liquidity" in prompt_text

    _, messages, _ = build_dashboard_advice_messages(full)
    user_text = messages[2]["content"][0]["text"]
    assert "HIGH_SIGNAL_TRADES" in user_text
    assert NAS100_PLANNED_REENTRY_NOTE in user_text


def test_build_dashboard_advice_messages_uses_compressed_prompt(app_ctx):
    full = _nas100_two_trade_full_payload()
    _, messages, payload_json = build_dashboard_advice_messages(full)

    user_text = messages[2]["content"][0]["text"]
    assert "WEEKLY REVIEW DATA (compressed)" in user_text
    assert "HIGH_SIGNAL_TRADES" in user_text
    assert "SURFACE_FACTS" not in user_text
    assert "period_start_utc" not in user_text
    assert json.loads(payload_json) == full


def test_full_payload_unchanged_after_compression():
    full = _nas100_two_trade_full_payload()
    stored = serialize_payload(full)
    build_weekly_prompt_payload(full)
    assert json.loads(stored) == full
