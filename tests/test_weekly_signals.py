import pytest

from helpers.weekly_signals import (
    build_confidence_envelope,
    build_execution_outcome_archetype,
    build_post_loss_response,
    build_revenge_evidence,
    build_risk_authority,
    build_session_concentration,
    build_single_trade_dominance,
    build_surface_facts,
    build_tp_capture_shortfalls,
    build_weekly_signals,
)
from helpers.weekly_coaching_hypotheses import _hypothesis, build_coaching_hypotheses


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
    risk_pct_vs_prev=None,
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
        "risk_pct_vs_prev": risk_pct_vs_prev,
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


def test_revenge_evidence_repeated_only_when_multiple_strong_or_confirmed_sequences():
    isolated = [
        _trade(
            ref="T1",
            seq=1,
            pnl=-10.0,
            is_potential_revenge=True,
            prev_trade_pnl=-50.0,
            minutes_since_prev_close=20.0,
            risk_pct_vs_prev="larger",
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
            risk_pct_vs_prev="larger",
        )
    ]
    assert build_revenge_evidence(repeated)["pattern_class"] == "repeated"

    confirmed = [_trade(ref="T1", seq=1, pnl=-10.0, is_revenge=True)]
    assert build_revenge_evidence(confirmed)["pattern_class"] == "isolated"

    repeated_confirmed = confirmed + [_trade(ref="T2", seq=2, pnl=-15.0, is_revenge=True)]
    assert build_revenge_evidence(repeated_confirmed)["pattern_class"] == "repeated"


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


def _archetype(
    trades,
    *,
    net_pnl,
    risk_authority=None,
    post_loss_response=None,
    single_trade_dominance=None,
    tp_capture_shortfalls=None,
    revenge_evidence=None,
):
    return build_execution_outcome_archetype(
        trades,
        summary={"net_pnl": net_pnl},
        risk_authority=risk_authority or {"risk_judgment_allowed": True, "stable": True},
        post_loss_response=post_loss_response or {"repeated_increased_risk": False},
        single_trade_dominance=single_trade_dominance,
        tp_capture_shortfalls=tp_capture_shortfalls or {"recurring": False},
        revenge_evidence=revenge_evidence or {"pattern_class": "none"},
    )


def test_execution_outcome_full_praise_when_process_and_result_are_clean():
    out = _archetype(
        [_trade(ref="T1", seq=1, pnl=120.0), _trade(ref="T2", seq=2, pnl=80.0)],
        net_pnl=200.0,
    )

    assert out["outcome_class"] == "good"
    assert out["execution_class"] == "good"
    assert out["week_archetype"] == "good_execution_good_outcome"
    assert out["coaching_stance"] == "full_praise"


def test_execution_outcome_protects_confidence_when_clean_process_loses():
    out = _archetype(
        [_trade(ref="T1", seq=1, pnl=-60.0), _trade(ref="T2", seq=2, pnl=-40.0)],
        net_pnl=-100.0,
    )

    assert out["execution_class"] == "good"
    assert out["week_archetype"] == "good_execution_bad_outcome"
    assert out["coaching_stance"] == "protect_confidence"


def test_execution_outcome_treats_clean_flat_week_as_neutral():
    out = _archetype(
        [_trade(ref="T1", seq=1, pnl=60.0), _trade(ref="T2", seq=2, pnl=-60.0)],
        net_pnl=0.0,
    )

    assert out["outcome_class"] == "flat"
    assert out["execution_class"] == "good"
    assert out["week_archetype"] == "good_execution_flat_outcome"
    assert out["coaching_stance"] == "steady_neutral"
    assert out["issue_evidence_level"] == "none"


def test_execution_outcome_warns_when_profitable_week_has_leaks():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=-40.0),
            _trade(ref="T2", seq=2, pnl=180.0, prev_trade_pnl=-40.0),
        ],
        net_pnl=140.0,
        revenge_evidence={"pattern_class": "isolated"},
    )

    assert out["outcome_class"] == "good"
    assert out["execution_class"] == "leaky"
    assert out["week_archetype"] == "leaky_execution_good_outcome"
    assert out["coaching_stance"] == "good_week_but_habits_are_leaking"
    assert out["issue_evidence_level"] == "isolated"


def test_execution_outcome_keeps_isolated_revenge_as_light_correction():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=-40.0),
            _trade(ref="T2", seq=2, pnl=-50.0, prev_trade_pnl=-40.0),
        ],
        net_pnl=-90.0,
        revenge_evidence={"pattern_class": "isolated"},
    )

    assert out["execution_class"] == "leaky"
    assert out["week_archetype"] == "leaky_execution_bad_outcome"
    assert out["coaching_stance"] == "light_correction"
    assert out["issue_evidence_level"] == "isolated"
    assert out["primary_issue"] == "isolated_revenge_evidence"


def test_execution_outcome_corrects_leaky_flat_week_without_calling_it_a_loss():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=-60.0),
            _trade(ref="T2", seq=2, pnl=60.0, prev_trade_pnl=-60.0),
        ],
        net_pnl=0.0,
        revenge_evidence={"pattern_class": "isolated"},
    )

    assert out["outcome_class"] == "flat"
    assert out["execution_class"] == "leaky"
    assert out["week_archetype"] == "leaky_execution_flat_outcome"
    assert out["coaching_stance"] == "light_correction"
    assert out["issue_evidence_level"] == "isolated"


def test_execution_outcome_counts_same_symbol_and_same_idea_reentries_when_present():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=-40.0),
            {
                **_trade(ref="T2", seq=2, pnl=-20.0),
                "is_post_loss_same_symbol_trade": True,
                "same_trade_idea_reentry": True,
            },
            {
                **_trade(ref="T3", seq=3, pnl=25.0),
                "is_post_loss_same_symbol_trade": True,
            },
        ],
        net_pnl=-35.0,
    )

    assert out["same_symbol_after_loss_count"] == 2
    assert out["same_trade_idea_reentry_count"] == 1
    assert out["primary_issue"] == "repeated_same_symbol_after_loss"
    assert out["ranked_issues"][0]["reason"] == "repeated_same_symbol_after_loss"
    assert "repeated_same_symbol_after_loss" in out["issue_reasons"]
    assert "single_same_trade_idea_reentry" in out["issue_reasons"]


def test_execution_outcome_direct_correction_when_bad_process_loses():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=-40.0),
            _trade(ref="T2", seq=2, pnl=-70.0, prev_trade_pnl=-40.0),
        ],
        net_pnl=-110.0,
        revenge_evidence={"pattern_class": "repeated"},
    )

    assert out["execution_class"] == "bad"
    assert out["week_archetype"] == "bad_execution_bad_outcome"
    assert out["coaching_stance"] == "direct_correction"
    assert out["issue_evidence_level"] == "strong"
    assert out["primary_issue"] == "repeated_revenge_evidence"


def test_execution_outcome_ranks_primary_issue_above_secondary_leaks():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=-40.0),
            {
                **_trade(ref="T2", seq=2, pnl=-30.0),
                "is_post_loss_same_symbol_trade": True,
            },
            {
                **_trade(ref="T3", seq=3, pnl=-25.0),
                "is_post_loss_same_symbol_trade": True,
            },
            _trade(ref="T4", seq=4, pnl=20.0),
        ],
        net_pnl=-75.0,
        revenge_evidence={"pattern_class": "repeated"},
        tp_capture_shortfalls={"recurring": True},
    )

    ranked_reasons = [issue["reason"] for issue in out["ranked_issues"]]

    assert out["primary_issue"] == "repeated_revenge_evidence"
    assert ranked_reasons[:3] == [
        "repeated_revenge_evidence",
        "repeated_same_symbol_after_loss",
        "recurring_winner_exited_before_target",
    ]
    assert out["primary_issue_hint"].startswith("Lead with repeated revenge")


def test_execution_outcome_treats_clean_outlier_concentration_as_unclear_not_bad():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=300.0),
            _trade(ref="T2", seq=2, pnl=-20.0),
            _trade(ref="T3", seq=3, pnl=10.0),
        ],
        net_pnl=290.0,
        single_trade_dominance={
            "dominant_symbol": "EURUSD",
            "dominant_ref": "T1",
            "abs_pnl_share_pct": 90.0,
        },
    )

    assert out["execution_class"] == "unclear"
    assert out["week_archetype"] == "random_or_unclear_execution"
    assert out["coaching_stance"] == "measure_first"
    assert out["primary_issue"] == "single_trade_dominance"
    assert out["do_not_lead_with"] == []


def test_execution_outcome_does_not_hide_process_leak_behind_outlier_concentration():
    out = _archetype(
        [
            _trade(ref="T1", seq=1, pnl=300.0),
            _trade(ref="T2", seq=2, pnl=-20.0),
            _trade(ref="T3", seq=3, pnl=-80.0, is_revenge=True),
            _trade(ref="T4", seq=4, pnl=-70.0, is_revenge=True),
        ],
        net_pnl=130.0,
        single_trade_dominance={
            "dominant_symbol": "EURUSD",
            "dominant_ref": "T1",
            "abs_pnl_share_pct": 63.8,
        },
        revenge_evidence={"pattern_class": "repeated"},
    )

    assert out["outcome_concentrated"] is True
    assert out["execution_class"] == "bad"
    assert out["week_archetype"] == "bad_execution_good_outcome"
    assert out["coaching_stance"] == "good_week_but_habits_are_leaking"
    assert out["primary_issue"] == "repeated_revenge_evidence"
    assert out["do_not_lead_with"] == ["single_trade_dominance"]


def test_coaching_hypothesis_detects_outcome_disguised_habit():
    trades = [
        _trade(ref="T1", seq=1, pnl=-80.0),
        {
            **_trade(
                ref="T2",
                seq=2,
                pnl=120.0,
                minutes_since_prev_close=32.93,
                is_revenge=True,
            ),
            "is_post_loss_same_symbol_trade": True,
            "same_trade_idea_reentry": True,
        },
        {
            **_trade(
                ref="T3",
                seq=3,
                pnl=-140.0,
                minutes_since_prev_close=13.33,
                is_revenge=True,
            ),
            "is_post_loss_same_symbol_trade": True,
            "same_trade_idea_reentry": True,
        },
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": -100.0},
        current_week_breakdowns={},
        execution_outcome={
            "issue_reasons": [
                "repeated_revenge_evidence",
                "repeated_same_symbol_after_loss",
                "repeated_same_trade_idea_reentry",
            ],
            "same_symbol_after_loss_count": 2,
            "same_trade_idea_reentry_count": 2,
        },
        revenge_evidence={"pattern_class": "repeated"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": False},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    assert out[0]["type"] == "outcome_disguised_habit"
    assert out[0]["confidence"] == "high"
    assert out[0]["evidence_refs"] == ["T2", "T3"]
    assert out[0]["facts"]["winning_retry_refs"] == ["T2"]
    assert out[0]["facts"]["failed_retry_refs"] == ["T3"]
    assert out[0]["facts"]["retry_timing_range_minutes"] == [13.33, 32.93]
    assert out[0]["habit_rewarded_by_ref"] == "T2"
    assert out[0]["habit_rewarded_by_symbol"] == "EURUSD"
    assert out[0]["habit_exposed_by_ref"] == "T3"
    assert out[0]["habit_exposed_by_symbol"] == "EURUSD"
    assert "reinforced the same post-loss behavior" in out[0]["mechanism_hint"]
    assert "may treat the retry habit as valid" in out[0]["what_the_trader_may_have_mislearned"]
    assert "rewarded the habit" in out[0]["contrast_instruction"]
    assert out[0]["writing_shape"] == "reward -> cost -> mislesson -> better_lesson"
    assert "winning retry" in out[0]["false_lesson_hint"]


def test_coaching_hypothesis_uses_biggest_retry_win_and_worst_retry_loss():
    trades = [
        _trade(ref="T1", seq=1, pnl=-80.0),
        _trade(
            ref="T2",
            seq=2,
            pnl=30.0,
            minutes_since_prev_close=12.0,
            is_revenge=True,
            symbol="EURUSD",
        ),
        _trade(
            ref="T3",
            seq=3,
            pnl=-45.0,
            minutes_since_prev_close=18.0,
            is_revenge=True,
            symbol="GBPJPY",
        ),
        _trade(
            ref="T4",
            seq=4,
            pnl=220.0,
            minutes_since_prev_close=25.0,
            is_revenge=True,
            symbol="NAS100",
        ),
        _trade(
            ref="T5",
            seq=5,
            pnl=-180.0,
            minutes_since_prev_close=9.0,
            is_revenge=True,
            symbol="USDCAD",
        ),
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": -55.0},
        current_week_breakdowns={},
        execution_outcome={
            "issue_reasons": ["repeated_revenge_evidence"],
            "same_symbol_after_loss_count": 4,
            "same_trade_idea_reentry_count": 0,
        },
        revenge_evidence={"pattern_class": "repeated"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": False},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    hypothesis = next(item for item in out if item["type"] == "outcome_disguised_habit")
    assert hypothesis["evidence_refs"] == ["T4", "T5"]
    assert hypothesis["habit_rewarded_by_ref"] == "T4"
    assert hypothesis["habit_rewarded_by_symbol"] == "NAS100"
    assert hypothesis["habit_exposed_by_ref"] == "T5"
    assert hypothesis["habit_exposed_by_symbol"] == "USDCAD"
    assert hypothesis["facts"]["winning_retry_refs"] == ["T2", "T4"]
    assert hypothesis["facts"]["failed_retry_refs"] == ["T3", "T5"]


def test_hypothesis_rejects_extra_field_core_key_conflicts():
    with pytest.raises(ValueError, match="severity"):
        _hypothesis(
            hypothesis_type="outcome_disguised_habit",
            severity=95,
            confidence="high",
            evidence_refs=["T1"],
            facts={},
            human_trap_hint="hint",
            false_lesson_hint="false",
            better_lesson_hint="better",
            extra_fields={"severity": 10},
        )


def test_coaching_hypothesis_blocks_outcome_disguised_habit_without_support():
    trades = [
        _trade(ref="T1", seq=1, pnl=-80.0),
        _trade(
            ref="T2",
            seq=2,
            pnl=120.0,
            minutes_since_prev_close=32.93,
            is_revenge=True,
        ),
        _trade(
            ref="T3",
            seq=3,
            pnl=-140.0,
            minutes_since_prev_close=13.33,
            is_revenge=True,
        ),
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": -100.0},
        current_week_breakdowns={},
        execution_outcome={"issue_reasons": []},
        revenge_evidence={},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": False},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    assert not any(item["type"] == "outcome_disguised_habit" for item in out)


def test_coaching_hypothesis_allows_outcome_disguised_habit_with_isolated_pattern():
    trades = [
        _trade(
            ref="T1",
            seq=1,
            pnl=120.0,
            minutes_since_prev_close=32.93,
            is_revenge=True,
        ),
        _trade(
            ref="T2",
            seq=2,
            pnl=-140.0,
            minutes_since_prev_close=13.33,
            is_revenge=True,
        ),
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": -20.0},
        current_week_breakdowns={},
        execution_outcome={"issue_reasons": []},
        revenge_evidence={"pattern_class": "isolated"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": False},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    hypothesis = next(item for item in out if item["type"] == "outcome_disguised_habit")
    assert hypothesis["confidence"] == "moderate"


def test_coaching_hypothesis_detects_post_loss_decision_shift():
    trades = [
        _trade(ref="T1", seq=1, pnl=-40.0),
        _trade(ref="T2", seq=2, pnl=-20.0, minutes_since_prev_close=10.0),
        _trade(ref="T3", seq=3, pnl=-30.0, minutes_since_prev_close=18.0),
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": -90.0},
        current_week_breakdowns={},
        execution_outcome={"issue_reasons": ["repeated_revenge_evidence"]},
        revenge_evidence={"pattern_class": "repeated"},
        post_loss_response={
            "sequences": [
                {"loss_ref": "T1", "next_ref": "T2", "next_outcome": "loss"},
                {"loss_ref": "T2", "next_ref": "T3", "next_outcome": "loss"},
            ],
            "biggest_loss": {"loss_ref": "T1", "next_ref": "T2"},
        },
        risk_authority={"risk_judgment_allowed": False},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    hypothesis = next(item for item in out if item["type"] == "post_loss_decision_shift")
    assert hypothesis["facts"]["post_loss_sequence_count"] == 2
    assert hypothesis["facts"]["next_trade_refs"] == ["T2", "T3"]
    assert hypothesis["facts"]["retry_timing_range_minutes"] == [10.0, 18.0]


def test_coaching_hypothesis_detects_single_trade_masked_week():
    trades = [
        _trade(ref="T1", seq=1, pnl=300.0, symbol="GBPJPY"),
        _trade(ref="T2", seq=2, pnl=-80.0),
        _trade(ref="T3", seq=3, pnl=-70.0),
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": 150.0},
        current_week_breakdowns={},
        execution_outcome={"do_not_lead_with": []},
        revenge_evidence={"pattern_class": "none"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": True},
        single_trade_dominance={
            "dominant_ref": "T1",
            "dominant_symbol": "GBPJPY",
            "abs_pnl_share_pct": 66.7,
        },
        tp_capture_shortfalls={"recurring": False},
    )

    hypothesis = next(item for item in out if item["type"] == "single_trade_masked_week")
    assert hypothesis["facts"]["dominant_ref"] == "T1"
    assert hypothesis["facts"]["rest_of_week_pnl"] == -150.0
    assert hypothesis["facts"]["rest_of_week_flips_result"] is True
    assert hypothesis["confidence"] == "high"


def test_coaching_hypothesis_single_trade_fallback_keeps_ref_symbol_consistent():
    trades = [
        _trade(ref="T1", seq=1, pnl=50.0, symbol="EURUSD"),
        _trade(ref="T2", seq=2, pnl=-300.0, symbol="NAS100"),
        _trade(ref="T3", seq=3, pnl=20.0, symbol="USDCAD"),
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": -230.0},
        current_week_breakdowns={},
        execution_outcome={"do_not_lead_with": []},
        revenge_evidence={"pattern_class": "none"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": True},
        single_trade_dominance={
            "dominant_ref": "MISSING",
            "dominant_symbol": "GBPJPY",
            "abs_pnl_share_pct": 81.0,
        },
        tp_capture_shortfalls={"recurring": False},
    )

    hypothesis = next(item for item in out if item["type"] == "single_trade_masked_week")
    assert hypothesis["facts"]["dominant_ref"] == "T2"
    assert hypothesis["facts"]["dominant_symbol"] == "NAS100"


def test_coaching_hypothesis_detects_session_edge_disguised_as_skill():
    trades = [
        _trade(ref="T1", seq=1, pnl=60.0, session="London / New York"),
        _trade(ref="T2", seq=2, pnl=20.0, session="London / New York"),
        _trade(ref="T3", seq=3, pnl=-35.0, session="London"),
        _trade(ref="T4", seq=4, pnl=-25.0, session="London"),
    ]
    out = build_coaching_hypotheses(
        serialized_trades=trades,
        summary={"net_pnl": 20.0},
        current_week_breakdowns={
            "sessions": [
                {"name": "London / New York", "count": 2, "win_rate": 100.0, "net_pnl": 80.0},
                {"name": "London", "count": 2, "win_rate": 0.0, "net_pnl": -60.0},
            ]
        },
        execution_outcome={"issue_reasons": []},
        revenge_evidence={"pattern_class": "none"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": True},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    hypothesis = next(item for item in out if item["type"] == "session_edge_disguised_as_skill")
    assert hypothesis["facts"]["best_session"] == "London / New York"
    assert hypothesis["facts"]["weak_session"] == "London"
    assert hypothesis["evidence_refs"] == ["T1", "T3"]


def test_coaching_hypotheses_generalize_without_forcing_mechanisms():
    clean_trend_week = [
        _trade(ref="T1", seq=1, pnl=80.0, symbol="EURUSD"),
        _trade(ref="T2", seq=2, pnl=120.0, symbol="GBPUSD"),
        _trade(ref="T3", seq=3, pnl=95.0, symbol="NAS100"),
    ]
    clean_out = build_coaching_hypotheses(
        serialized_trades=clean_trend_week,
        summary={"net_pnl": 295.0},
        current_week_breakdowns={},
        execution_outcome={"issue_reasons": []},
        revenge_evidence={"pattern_class": "none"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": True},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    messy_chop_week = [
        _trade(ref="T1", seq=1, pnl=30.0, symbol="EURUSD"),
        _trade(ref="T2", seq=2, pnl=-25.0, symbol="GBPUSD"),
        _trade(ref="T3", seq=3, pnl=20.0, symbol="NAS100"),
        _trade(ref="T4", seq=4, pnl=-35.0, symbol="USDCAD"),
    ]
    messy_out = build_coaching_hypotheses(
        serialized_trades=messy_chop_week,
        summary={"net_pnl": -10.0},
        current_week_breakdowns={},
        execution_outcome={"issue_reasons": []},
        revenge_evidence={"pattern_class": "none"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": True},
        single_trade_dominance=None,
        tp_capture_shortfalls={"recurring": False},
    )

    single_big_winner_week = [
        _trade(ref="T1", seq=1, pnl=500.0, symbol="GBPJPY"),
        _trade(ref="T2", seq=2, pnl=-70.0, symbol="EURUSD"),
        _trade(ref="T3", seq=3, pnl=-60.0, symbol="USDCAD"),
        _trade(ref="T4", seq=4, pnl=-50.0, symbol="NAS100"),
    ]
    single_big_out = build_coaching_hypotheses(
        serialized_trades=single_big_winner_week,
        summary={"net_pnl": 320.0},
        current_week_breakdowns={},
        execution_outcome={"issue_reasons": [], "do_not_lead_with": []},
        revenge_evidence={"pattern_class": "none"},
        post_loss_response={"sequences": []},
        risk_authority={"risk_judgment_allowed": True},
        single_trade_dominance={
            "dominant_ref": "T1",
            "dominant_symbol": "GBPJPY",
            "abs_pnl_share_pct": 73.5,
        },
        tp_capture_shortfalls={"recurring": False},
    )

    assert clean_out == []
    assert messy_out == []
    assert [item["type"] for item in single_big_out] == ["single_trade_masked_week"]


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
        "execution_outcome",
        "coaching_hypotheses",
        "surface_facts",
        "confidence_envelope",
    }
    assert out["risk_authority"]["basis"] == "pct_of_account"
    assert out["confidence_envelope"]["level"] == "strong"
