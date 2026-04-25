from helpers.weekly_signals import (
    build_confidence_envelope,
    build_post_loss_response,
    build_revenge_evidence,
    build_risk_authority,
    build_session_concentration,
    build_single_trade_dominance,
    build_surface_facts,
    build_tp_capture_shortfalls,
    build_weekly_signals,
)


def _trade(
    *,
    ref,
    seq,
    pnl,
    risk_pct=0.5,
    risk_dollars=50.0,
    lot=0.10,
    session="London",
    tp_capture=None,
    is_revenge=False,
    is_potential_revenge=False,
    minutes_since_prev_close=None,
    size_vs_prev_trade=None,
    prev_trade_pnl=None,
    symbol="EURUSD",
):
    return {
        "review_ref": ref,
        "trade_sequence_number": seq,
        "pnl": pnl,
        "trade_risk_pct": risk_pct,
        "planned_risk_dollars": risk_dollars,
        "lot_size": lot,
        "session": session,
        "tp_capture_pct": tp_capture,
        "is_revenge": is_revenge,
        "is_potential_revenge": is_potential_revenge,
        "minutes_since_prev_close": minutes_since_prev_close,
        "size_vs_prev_trade": size_vs_prev_trade,
        "prev_trade_pnl": prev_trade_pnl,
        "symbol": symbol,
        "closed_at": "2026-04-20 12:00:00 UTC",
    }


def test_risk_authority_stable_when_lots_vary_but_pct_holds():
    """Lots can wiggle from SL distance — % of account is the truth."""
    trades = [
        _trade(ref="T1", seq=1, pnl=20.0, risk_pct=0.50, lot=0.10),
        _trade(ref="T2", seq=2, pnl=-30.0, risk_pct=0.51, lot=0.25),
        _trade(ref="T3", seq=3, pnl=15.0, risk_pct=0.49, lot=0.05),
    ]
    out = build_risk_authority(
        trades,
        median_risk_pct_of_account=0.50,
        median_planned_risk_dollars=50.0,
        median_lot_size=0.10,
    )
    assert out["basis"] == "pct_of_account"
    assert out["stable"] is True
    assert out["dispersion_pct"] is not None and out["dispersion_pct"] <= 5.0


def test_risk_authority_unstable_when_pct_actually_changes():
    trades = [
        _trade(ref="T1", seq=1, pnl=20.0, risk_pct=0.50),
        _trade(ref="T2", seq=2, pnl=-30.0, risk_pct=1.20),
        _trade(ref="T3", seq=3, pnl=15.0, risk_pct=0.55),
    ]
    out = build_risk_authority(
        trades,
        median_risk_pct_of_account=0.55,
        median_planned_risk_dollars=55.0,
        median_lot_size=0.10,
    )
    assert out["stable"] is False


def test_risk_authority_falls_back_to_dollars_then_lots():
    trades = [_trade(ref="T1", seq=1, pnl=10.0, risk_pct=None, risk_dollars=42.0)]
    out_dollars = build_risk_authority(
        trades,
        median_risk_pct_of_account=None,
        median_planned_risk_dollars=42.0,
        median_lot_size=0.1,
    )
    assert out_dollars["basis"] == "dollars"
    assert out_dollars["risk_judgment_allowed"] is True

    trades_no_dollars = [
        _trade(ref="T1", seq=1, pnl=10.0, risk_pct=None, risk_dollars=None, lot=0.1)
    ]
    out_lots = build_risk_authority(
        trades_no_dollars,
        median_risk_pct_of_account=None,
        median_planned_risk_dollars=None,
        median_lot_size=0.1,
    )
    assert out_lots["basis"] == "lots"
    # R1: judgment forbidden when only lot sizes are available
    assert out_lots["risk_judgment_allowed"] is False


def test_risk_authority_judgment_allowed_when_pct_present():
    trades = [_trade(ref="T1", seq=1, pnl=10.0, risk_pct=0.5)]
    out = build_risk_authority(
        trades,
        median_risk_pct_of_account=0.5,
        median_planned_risk_dollars=50.0,
        median_lot_size=0.1,
    )
    assert out["risk_judgment_allowed"] is True


def test_risk_authority_judgment_blocked_when_no_data():
    out = build_risk_authority(
        [],
        median_risk_pct_of_account=None,
        median_planned_risk_dollars=None,
        median_lot_size=None,
    )
    assert out["basis"] is None
    assert out["risk_judgment_allowed"] is False


def test_post_loss_response_classifies_biggest_loss_followup():
    trades = [
        _trade(ref="T1", seq=1, pnl=30.0, risk_pct=0.5),
        _trade(ref="T2", seq=2, pnl=-90.0, risk_pct=0.5),
        _trade(ref="T3", seq=3, pnl=-15.0, risk_pct=0.5, prev_trade_pnl=-90.0),
    ]
    risk_authority = {"basis": "pct_of_account", "risk_judgment_allowed": True}
    out = build_post_loss_response(trades, risk_authority=risk_authority)
    assert out["biggest_loss"]["loss_ref"] == "T2"
    assert out["biggest_loss"]["next_ref"] == "T3"
    assert out["biggest_loss"]["risk_change"] == "same"
    assert out["biggest_loss"]["next_outcome"] == "loss"
    assert out["pattern_count"] >= 1


def test_post_loss_biggest_loss_phrase_is_complete_sentence_for_each_branch():
    """The phrase replaces the prompt's curly-brace template (R3)."""
    # same risk + loss outcome
    trades = [
        _trade(ref="T1", seq=1, pnl=30.0, risk_pct=0.5),
        _trade(ref="T2", seq=2, pnl=-90.0, risk_pct=0.5),
        _trade(ref="T3", seq=3, pnl=-15.0, risk_pct=0.5),
    ]
    out = build_post_loss_response(
        trades, risk_authority={"basis": "pct_of_account", "risk_judgment_allowed": True}
    )
    phrase = out["biggest_loss"]["phrase"]
    assert phrase.startswith("After your biggest loss")
    assert "{" not in phrase and "}" not in phrase
    assert "risk_change" not in phrase
    assert "kept risk roughly the same" in phrase
    assert "a loss" in phrase

    # increased risk + win outcome
    trades_more = [
        _trade(ref="T1", seq=1, pnl=20.0, risk_pct=0.5),
        _trade(ref="T2", seq=2, pnl=-80.0, risk_pct=0.5),
        _trade(ref="T3", seq=3, pnl=40.0, risk_pct=1.0),
    ]
    out_more = build_post_loss_response(
        trades_more, risk_authority={"basis": "pct_of_account", "risk_judgment_allowed": True}
    )
    assert "increased risk" in out_more["biggest_loss"]["phrase"]
    assert "a win" in out_more["biggest_loss"]["phrase"]


def test_post_loss_biggest_loss_phrase_when_no_followup_is_clean():
    trades = [
        _trade(ref="T1", seq=1, pnl=20.0, risk_pct=0.5),
        _trade(ref="T2", seq=2, pnl=-150.0, risk_pct=0.5),
    ]
    out = build_post_loss_response(
        trades, risk_authority={"basis": "pct_of_account", "risk_judgment_allowed": True}
    )
    phrase = out["biggest_loss"]["phrase"]
    assert "no follow-up" in phrase
    assert "{" not in phrase


def test_post_loss_biggest_loss_phrase_omits_risk_when_not_allowed():
    """When risk_judgment_allowed is false, phrase must not assert direction."""
    trades = [
        _trade(ref="T1", seq=1, pnl=10.0, risk_pct=None, lot=0.1),
        _trade(ref="T2", seq=2, pnl=-50.0, risk_pct=None, lot=0.5),
        _trade(ref="T3", seq=3, pnl=-10.0, risk_pct=None, lot=0.1),
    ]
    out = build_post_loss_response(
        trades, risk_authority={"basis": "lots", "risk_judgment_allowed": False}
    )
    phrase = out["biggest_loss"]["phrase"]
    assert "Risk direction cannot be compared" in phrase
    assert "increased risk" not in phrase
    assert "reduced risk" not in phrase


def test_post_loss_response_flags_repeated_increased_risk():
    trades = [
        _trade(ref="T1", seq=1, pnl=10.0, risk_pct=0.5),
        _trade(ref="T2", seq=2, pnl=-40.0, risk_pct=0.5),
        _trade(ref="T3", seq=3, pnl=-50.0, risk_pct=0.9),
        _trade(ref="T4", seq=4, pnl=-30.0, risk_pct=1.4),
    ]
    out = build_post_loss_response(trades, risk_authority={"basis": "pct_of_account"})
    assert out["repeated_increased_risk"] is True


def test_post_loss_response_no_followup_when_loss_is_last():
    trades = [
        _trade(ref="T1", seq=1, pnl=20.0),
        _trade(ref="T2", seq=2, pnl=-150.0),
    ]
    out = build_post_loss_response(trades, risk_authority={"basis": "pct_of_account"})
    assert out["biggest_loss"]["no_followup_reason"] == "biggest_loss_was_last_trade"


def test_single_trade_dominance_only_when_share_above_threshold_and_enough_trades():
    assert (
        build_single_trade_dominance(
            largest_trade_symbol="GBPUSD",
            largest_trade_abs_pnl_share_pct=55.0,
            closed_trade_count=4,
        )
        is not None
    )
    assert (
        build_single_trade_dominance(
            largest_trade_symbol="GBPUSD",
            largest_trade_abs_pnl_share_pct=55.0,
            closed_trade_count=2,
        )
        is None
    )
    assert (
        build_single_trade_dominance(
            largest_trade_symbol="GBPUSD",
            largest_trade_abs_pnl_share_pct=20.0,
            closed_trade_count=4,
        )
        is None
    )


def test_tp_capture_shortfalls_marks_recurring_only_when_two_or_more():
    trades = [
        _trade(ref="T1", seq=1, pnl=15.0, tp_capture=60.0),
        _trade(ref="T2", seq=2, pnl=10.0, tp_capture=85.0),  # not a shortfall
        _trade(ref="T3", seq=3, pnl=-20.0, tp_capture=10.0),  # losing trade ignored
    ]
    out = build_tp_capture_shortfalls(trades)
    assert len(out["trades"]) == 1
    assert out["recurring"] is False

    trades_two = trades + [_trade(ref="T4", seq=4, pnl=12.0, tp_capture=55.0)]
    out_two = build_tp_capture_shortfalls(trades_two)
    assert out_two["recurring"] is True


def test_session_concentration_flags_single_session_and_mixed_outcome():
    trades = [
        _trade(ref="T1", seq=1, pnl=20.0, session="London"),
        _trade(ref="T2", seq=2, pnl=-15.0, session="London"),
    ]
    out = build_session_concentration(trades)
    assert out["dominant_session"] == "London"
    assert out["single_session"] is True
    assert out["share_pct"] == 100.0
    assert out["mixed_outcome"] is True


def test_revenge_evidence_repeated_when_strong_sequences_or_confirmed():
    isolated = [
        _trade(
            ref="T1",
            seq=1,
            pnl=-10.0,
            is_potential_revenge=True,
            prev_trade_pnl=-50.0,
            minutes_since_prev_close=20.0,
            size_vs_prev_trade="larger",
        ),
    ]
    assert build_revenge_evidence(isolated)["pattern_class"] == "isolated"

    repeated = isolated + [
        _trade(
            ref="T2",
            seq=2,
            pnl=-10.0,
            is_potential_revenge=True,
            prev_trade_pnl=-50.0,
            minutes_since_prev_close=15.0,
            size_vs_prev_trade="larger",
        )
    ]
    assert build_revenge_evidence(repeated)["pattern_class"] == "repeated"

    confirmed = [_trade(ref="T1", seq=1, pnl=-10.0, is_revenge=True)]
    assert build_revenge_evidence(confirmed)["pattern_class"] == "repeated"


def test_surface_facts_includes_dashboard_visible_strings():
    facts = build_surface_facts(
        summary={
            "net_pnl": -120.0,
            "win_rate": 33.3,
            "closed_trades": 6,
            "top_symbol_by_trade_count": "EURUSD",
            "largest_trade_symbol": "GBPUSD",
            "largest_trade_abs_pnl_share_pct": 55.0,
        },
        current_week_breakdowns={
            "sizing": {"median_risk_pct_of_account": 0.5, "median_lot_size": 0.1},
            "frequency": {"busiest_session": "London"},
        },
    )
    assert any("net_pnl" in f for f in facts)
    assert any("largest_trade_abs_pnl_share_pct" in f for f in facts)
    assert any("median_risk_pct_of_account" in f for f in facts)


def test_confidence_envelope_levels():
    assert build_confidence_envelope(
        account_age_days=120, closed_trade_count=8, notes_confidence="high"
    )["level"] == "strong"
    assert build_confidence_envelope(
        account_age_days=10, closed_trade_count=8, notes_confidence="high"
    )["level"] == "moderate"
    assert build_confidence_envelope(
        account_age_days=10, closed_trade_count=2, notes_confidence="low"
    )["level"] == "limited"


def test_build_weekly_signals_composes_all_outputs():
    trades = [
        _trade(ref="T1", seq=1, pnl=30.0),
        _trade(ref="T2", seq=2, pnl=-50.0),
        _trade(ref="T3", seq=3, pnl=-20.0, prev_trade_pnl=-50.0, tp_capture=10.0),
    ]
    out = build_weekly_signals(
        serialized_trades=trades,
        summary={
            "net_pnl": -40.0,
            "win_rate": 33.3,
            "closed_trades": 3,
            "largest_trade_symbol": "EURUSD",
            "largest_trade_abs_pnl_share_pct": 50.0,
        },
        current_week_breakdowns={
            "sizing": {"median_risk_pct_of_account": 0.5},
            "frequency": {"busiest_session": "London"},
        },
        median_risk_pct_of_account=0.5,
        median_planned_risk_dollars=50.0,
        median_lot_size=0.1,
        account_age_days=120,
        notes_confidence="high",
    )
    assert set(out.keys()) >= {
        "risk_authority",
        "post_loss_response",
        "single_trade_dominance",
        "tp_capture_shortfalls",
        "session_concentration",
        "revenge_evidence",
        "surface_facts",
        "confidence_envelope",
    }
    assert out["risk_authority"]["basis"] == "pct_of_account"
    assert out["confidence_envelope"]["level"] == "strong"
