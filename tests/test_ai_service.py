from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import json
import pytest

import ai_service
from ai_service import (
    WEEKLY_DASHBOARD_KIND,
    build_dashboard_advice_messages,
    build_dashboard_review_display,
    build_profile_instructions,
    build_trade_payload,
    format_payload_for_prompt,
    get_latest_trade_week_period,
    get_weekly_dashboard_period,
    load_prompt_text,
    maybe_generate_weekly_dashboard_advice,
    normalize_ai_model_name,
    normalize_dashboard_advice_text,
)
from helpers.scoring import compute_emotional_index
from helpers.trade_interpretation import apply_interpretation
import trading

from models import (
    AIGeneratedResponse,
    AIPromptHistory,
    FuturesSymbol,
    Trade,
    TradeAccount,
    TradeBars,
    TradeProfile,
    TradeProfileVersion,
    User,
    WeeklyCheckin,
    db,
)


def _create_user_and_account(*, username, email, account_name="Primary Account", account_type="CFD"):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name=account_name,
        account_type=account_type,
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()
    return user, trade_account


def test_get_ai_model_normalizes_env_value(monkeypatch):
    monkeypatch.setenv("AI_MODEL", " gpt-5.4 mini ")

    assert ai_service.get_ai_model() == "gpt-5.4-mini"


def test_normalize_ai_model_name_strips_matching_quotes():
    assert normalize_ai_model_name('"gpt-5.4-mini"') == "gpt-5.4-mini"


def test_format_payload_for_prompt_includes_trade_fields_and_clear_context():
    payload = {
        "generated_at": "2026-03-12 10:00:00 UTC",
        "period_start_utc": "2026-03-03 00:00:00 UTC",
        "period_end_utc": "2026-03-10 00:00:00 UTC",
        "notes_coverage": 1.0,
        "notes_with_content": 1,
        "notes_missing": 0,
        "notes_confidence": "high",
        "notes_basis": "Per weekly trade idea after bundle merging; counts non-empty user-authored trade_note text only.",
        "emotional_index": {
            "score": 3.15,
            "label": "moderate",
            "self_report_mismatch": True,
            "components": {
                "subjective_points": 0.0,
                "confirmed_revenge_points": 1.25,
                "heuristic_revenge_points": 0.0,
                "revenge_repetition_bonus": 0.0,
                "confirmed_reactive_points": 2.5,
                "heuristic_reactive_points": 0.0,
                "reactive_repetition_bonus": 0.0,
                "confirmed_corrective_points": 0.0,
                "heuristic_corrective_points": 0.0,
                "corrective_repetition_bonus": 0.0,
                "revenge_points": 1.25,
                "reactive_points": 2.5,
                "corrective_points": 0.0,
            },
            "signals": {
                "bundle_count": 0,
                "confirmed_revenge_trade_count": 1,
                "confirmed_behavior_trade_count": 1,
                "heuristic_revenge_trade_count": 1,
                "confirmed_reactive_trade_count": 1,
                "heuristic_reactive_trade_count": 0,
                "reactive_trade_count": 1,
                "reactive_signal_trade_count": 1,
                "confirmed_corrective_trade_count": 0,
                "heuristic_corrective_trade_count": 0,
                "corrective_trade_count": 0,
                "corrective_signal_trade_count": 0,
                "revenge_trade_count": 1,
                "total_closed_trades": 1,
            },
        },
        "account_age_days": 45,
        "summary": {
            "total_trades": 1,
            "closed_trades": 1,
            "open_trades": 0,
            "win_rate": 100.0,
            "net_pnl": 140.0,
            "weekly_pnl": 140.0,
            "monthly_pnl": 140.0,
            "pair_sample_is_diverse": False,
            "equity_has_outlier_dominance": True,
            "top_symbol_by_trade_count": "MES (MESM26)",
            "top_symbol_trade_share_pct": 100.0,
            "top_symbol_by_abs_pnl": "MES (MESM26)",
            "top_symbol_abs_pnl_share_pct": 100.0,
            "largest_trade_symbol": "MES (MESM26)",
            "largest_trade_abs_pnl_share_pct": 100.0,
            "bundle_count": 0,
            "confirmed_revenge_trade_count": 1,
            "heuristic_revenge_trade_count": 1,
            "reactive_trade_count": 1,
            "corrective_trade_count": 0,
            "revenge_trade_count": 1,
            "best_trade_pnl": 140.0,
            "worst_trade_pnl": 140.0,
            "max_drawdown": 45.75,
        },
        "historical_context": {
            "comparison_scope": "history_before_review_period_only",
            "window_start_utc": "2025-12-12 00:00:00 UTC",
            "window_end_utc": "2026-03-03 00:00:00 UTC",
            "window_days": 90,
            "summary": {
                "total_trades": 8,
                "closed_trades": 8,
                "win_rate": 50.0,
                "net_pnl": 320.5,
                "max_drawdown": 88.2,
            },
            "top_pairs": [],
            "top_sessions": [],
            "top_weekdays": [],
        },
        "tone_context": {
            "checkin_submitted": True,
            "mode": "stressed",
            "reasons": ["checkin_emotional_state:stressed", "week_result:losing"],
        },
        "current_week_breakdowns": {
            "sessions": [{"name": "London", "count": 1, "win_rate": 100.0, "net_pnl": 140.0}],
            "symbols": [{"symbol": "MES (MESM26)", "count": 1, "win_rate": 100.0, "net_pnl": 140.0}],
            "weekdays": [{"name": "Monday", "count": 1, "win_rate": 100.0, "net_pnl": 140.0}],
            "strategies": [{"strategy_name": "NY Open Sweep v1", "count": 1, "win_rate": 100.0, "net_pnl": 140.0}],
            "strategy_coverage": {"trades_with_strategy": 1, "strategy_coverage_pct": 100.0},
            "market_context": {
                "trades_with_bars": 1,
                "bar_coverage_pct": 100.0,
                "post_exit_tp_reached_count": 1,
                "protective_stop_count": 1,
                "trailing_or_breakeven_stop_count": 1,
            },
            "sizing": {
                "median_lot_size": 1.0,
                "median_planned_risk_dollars": 250.0,
                "median_risk_pct_of_account": 2.5,
                "outlier_size_count": 0,
                "outlier_size_share_pct": 0.0,
            },
            "frequency": {"trade_idea_count": 1, "active_day_count": 1, "trade_ideas_per_active_day": 1.0, "busiest_session": "London"},
            "exit_quality": {"closed_before_tp_count": 1, "closed_before_sl_count": 0, "avg_tp_capture_pct": 35.0},
        },
        "four_week_patterns": {
            "weeks_considered": 4,
            "session_patterns": [{"name": "London", "count": 3}],
            "symbol_patterns": [{"symbol": "MES (MESM26)", "count": 3}],
            "weekday_patterns": [{"name": "Monday", "count": 2}],
            "behaviour_patterns": {"weeks_with_trades": 2, "avg_post_loss_reentry_count": 1.0, "avg_larger_size_after_loss_count": 0.5},
            "weekly_series": [
                {
                    "period_start_utc": "2026-02-24T00:00:00Z",
                    "period_end_utc": "2026-03-03T00:00:00Z",
                    "trade_idea_count": 2,
                    "win_rate": 50.0,
                    "net_pnl": 40.0,
                    "top_session": "London",
                    "top_symbol": "MES (MESM26)",
                    "top_weekday": "Monday",
                }
            ],
        },
        "experiment_context": {"eligible": False, "trade_idea_count": 1, "min_required": 1},
        "recent_experiments": [{"period_start_utc": "2026-02-17T00:00:00Z", "text": "Reduce add-on entries after losses to one."}],
        "trades": [
            {
                "review_ref": "T1",
                "trade_id": 42,
                "symbol": "MES (MESM26)",
                "contract_code": "MESM26",
                "strategy_name": "NY Open Sweep v1",
                "strategy_version": 1,
                "strategy_description": "First pullback after the New York open.",
                "side": "BUY",
                "entry_price": 5000.0,
                "exit_price": 5001.4,
                "stop_loss": 4998.75,
                "take_profit": 5004.0,
                "lot_size": 1.0,
                "pnl": 140.0,
                "entry_session": "New York",
                "exit_session": "New York",
                "session": "New York",
                "duration_minutes": 84.0,
                "opened_at": "2026-03-10 14:00:00 UTC",
                "closed_at": "2026-03-10 15:24:00 UTC",
                "trade_sequence_number": 2,
                "trade_number_in_session": 2,
                "prev_trade_pnl": -80.0,
                "minutes_since_prev_close": 12.0,
                "size_vs_prev_trade": "larger",
                "prev_symbol_trade_pnl": -80.0,
                "minutes_since_prev_symbol_close": 12.0,
                "size_vs_prev_symbol_trade": "larger",
                "loss_streak_before_trade": 1,
                "is_post_loss_trade": True,
                "same_symbol_reentry": True,
                "is_post_loss_same_symbol_trade": True,
                "same_trade_idea_reentry": True,
                "is_potential_revenge": True,
                "is_revenge": True,
                "is_reactive": True,
                "is_corrective": False,
                "is_bundle": False,
                "bundle_trade_count": 1,
                "planned_rr": 3.2,
                "realized_rr": 1.12,
                "tp_capture_pct": 35.0,
                "closed_before_tp": True,
                "closed_before_sl": None,
                "market_context": {
                    "bars_status": "ready",
                    "timeframe": "M5",
                    "bars_used": 18,
                    "in_trade_bars": 6,
                    "post_exit_bars": 3,
                    "mfe_price_move": 4.0,
                    "mae_price_move": 0.5,
                    "mfe_r": 3.2,
                    "mae_r": 0.4,
                    "entry_range_vs_prior_median": 1.8,
                    "entry_location_in_prior_range_pct": 88.0,
                    "entry_near_prior_high": True,
                    "entry_near_prior_low": False,
                    "post_exit_tp_reached": True,
                    "minutes_after_exit_to_tp": 10.0,
                    "post_exit_sl_reached": False,
                    "minutes_after_exit_to_sl": None,
                    "stop_management": {
                        "stop_loss_breakeven_or_better": True,
                        "stop_loss_protects_profit": True,
                        "possible_trailing_or_breakeven_stop": True,
                        "confidence": "high",
                        "evidence": [
                            "system_note_mentions_trailing_or_moved_stop",
                            "stored_stop_loss_protects_profit",
                        ],
                    },
                },
                "outlier_size": False,
                "outlier_lot_spike": False,
                "possible_split_order": False,
                "split_group_size": 1,
                "split_group_index": 1,
                "split_group_role": "solo",
                "is_likely_corrective": False,
                "trade_note": "Held through the close.",
            }
        ],
    }

    prompt_text = format_payload_for_prompt(payload)

    assert "- max_drawdown_amount: 45.75" in prompt_text
    assert "- historical_max_drawdown_amount: 88.20" in prompt_text
    assert "- notes_coverage: 1.00 (1 of 1 weekly trade ticket has non-empty notes)" in prompt_text
    assert "- notes_confidence: high" in prompt_text
    assert "- notes_basis: Per weekly trade idea after bundle merging; counts non-empty user-authored trade_note text only." in prompt_text
    assert "- payload_scope: one completed review period for one trade account" in prompt_text
    assert "- period_bounds: period_start_utc inclusive, period_end_utc ending boundary" in prompt_text
    assert "- summary_scope: SUMMARY metrics describe the completed review period only" in prompt_text
    assert "- historical_scope: historical sections are prior account context, not this week's pair/session/weekday breakdown" in prompt_text
    assert "- count_semantics: total_trades, closed_trades, open_trades, closed_before_tp_count, closed_before_sl_count, and bundle_count are counts" in prompt_text
    assert "- percent_semantics: win_rate, historical_win_rate, tp_capture_pct, and *_share_pct fields are already percentages" in prompt_text
    assert "- currency_semantics: *_pnl fields are signed currency values for this account; *_drawdown_amount fields are drawdown magnitudes" in prompt_text
    assert "- realized_result_semantics: SUMMARY.net_pnl is the reviewed-period result; weekly_pnl/monthly_pnl are rolling calendar aggregates relative to generated_at" in prompt_text
    assert "- open_trade_semantics: total_trades includes open and closed trades; closed_trades is the realized-performance denominator" in prompt_text
    assert prompt_text.find("\n\nSUMMARY\n") < prompt_text.find("\n\nTONE_CONTEXT\n")
    assert prompt_text.find("\n\nTONE_CONTEXT\n") < prompt_text.find("\n\nEMOTIONAL INDEX\n")
    assert prompt_text.find("\n\nEMOTIONAL INDEX\n") < prompt_text.find("\n\nCURRENT_WEEK_BREAKDOWNS\n")
    assert prompt_text.find("\n\nCURRENT_WEEK_BREAKDOWNS\n") < prompt_text.find("\n\nHISTORICAL_CONTEXT\n")
    assert prompt_text.find("\n\nHISTORICAL_CONTEXT\n") < prompt_text.find("TRADES (")
    mp = prompt_text.find("median_planned_risk_dollars")
    mrp = prompt_text.find("median_risk_pct_of_account")
    ml = prompt_text.find("median_lot_size")
    assert mp < mrp < ml
    assert "EMOTIONAL INDEX" in prompt_text
    assert "- score_range: 0.00 to 10.00 (higher = stronger objective behavioural pressure)" in prompt_text
    assert "- label_interpretation: low=quiet, moderate=mild, high=elevated, very_high=strong objective behavioural pressure" in prompt_text
    assert "- component_semantics: *_points and *_repetition_bonus fields are normalized weighted components, not counts" in prompt_text
    assert "- denominator_semantics: total_closed_trades is the denominator used for normalized behaviour scoring" in prompt_text
    assert "- label: moderate" in prompt_text
    assert "- self_report_mismatch: true" in prompt_text
    assert "- subjective_points: 0.00" in prompt_text
    assert "- confirmed_reactive_points: 2.50" in prompt_text
    assert "- revenge_points: 1.25" in prompt_text
    assert "- confirmed_revenge_trade_count: 1" in prompt_text
    assert "- heuristic_revenge_trade_count: 1" in prompt_text
    assert "- reactive_trade_count: 1" in prompt_text
    assert "- bundle_count: 0" in prompt_text
    assert "- total_closed_trades: 1" in prompt_text
    assert "- confirmed_behavior_trade_count: 1" in prompt_text
    assert "- comparison_scope: history_before_review_period_only" in prompt_text
    assert "- comparison_scope_note: history sections are comparison-only context and may exclude the current review period" in prompt_text
    assert "TONE_CONTEXT" in prompt_text
    assert "- mode: stressed" in prompt_text
    assert "CURRENT_WEEK_BREAKDOWNS" in prompt_text
    assert "- sizing.median_planned_risk_dollars: 250.00" in prompt_text
    assert "- sizing.median_risk_pct_of_account: 2.50%" in prompt_text
    assert "session London: count=1, win_rate=100.00%, net_pnl=+140.00" in prompt_text
    assert "strategy NY Open Sweep v1: count=1, win_rate=100.00%, net_pnl=+140.00" in prompt_text
    assert "strategy_coverage.trades_with_strategy: 1" in prompt_text
    assert "strategy_coverage.strategy_coverage_pct: 100.00%" in prompt_text
    assert "market_context.trades_with_bars: 1" in prompt_text
    assert "market_context.bar_coverage_pct: 100.00%" in prompt_text
    assert "market_context.post_exit_tp_reached_count: 1" in prompt_text
    assert "market_context.protective_stop_count: 1" in prompt_text
    assert "market_context.trailing_or_breakeven_stop_count: 1" in prompt_text
    assert "FOUR_WEEK_PATTERNS" in prompt_text
    assert "behaviour_patterns.avg_post_loss_reentry_count: 1.00" in prompt_text
    assert "EXPERIMENT_CONTEXT" in prompt_text
    assert "- eligible: false" in prompt_text
    assert "RECENT_EXPERIMENTS" in prompt_text
    assert "Reduce add-on entries after losses to one." in prompt_text
    assert "- account_age_days: 45" in prompt_text
    assert "- pair_sample_is_diverse: false" in prompt_text
    assert "- equity_has_outlier_dominance: true" in prompt_text
    assert "- top_symbol_trade_share_pct: 100.00%" in prompt_text
    assert "- largest_trade_abs_pnl_share_pct: 100.00%" in prompt_text
    assert "review_ref: T1" in prompt_text
    assert "contract_code: MESM26" in prompt_text
    assert "strategy_name: NY Open Sweep v1" in prompt_text
    assert "strategy_version: 1" in prompt_text
    assert "strategy_description: First pullback after the New York open." in prompt_text
    assert "stop_loss: 4998.75000" in prompt_text
    assert "take_profit: 5004.00000" in prompt_text
    assert "entry_session: New York" in prompt_text
    assert "exit_session: New York" in prompt_text
    assert "session: New York" in prompt_text
    assert "duration_minutes: 84.00" in prompt_text
    assert "trade_sequence_number: 2" in prompt_text
    assert "prev_trade_pnl: -80.00" in prompt_text
    assert "minutes_since_prev_close: 12.00" in prompt_text
    assert "size_vs_prev_trade: larger" in prompt_text
    assert "prev_symbol_trade_pnl: -80.00" in prompt_text
    assert "minutes_since_prev_symbol_close: 12.00" in prompt_text
    assert "size_vs_prev_symbol_trade: larger" in prompt_text
    assert "is_post_loss_same_symbol_trade: true" in prompt_text
    assert "same_trade_idea_reentry: true" in prompt_text
    assert "is_potential_revenge: true" in prompt_text
    assert "is_revenge: true" in prompt_text
    assert "is_reactive: true" in prompt_text
    assert "is_bundle: false" in prompt_text
    assert "planned_rr: 3.20" in prompt_text
    assert "realized_rr: 1.12" in prompt_text
    assert "tp_capture_pct: 35.00%" in prompt_text
    assert "closed_before_tp: true" in prompt_text
    assert "market_context.bars_status: ready" in prompt_text
    assert "market_context.mfe_price_move: 4.00000" in prompt_text
    assert "market_context.mae_price_move: 0.50000" in prompt_text
    assert "market_context.post_exit_tp_reached: true" in prompt_text
    assert "market_context.minutes_after_exit_to_tp: 10.00" in prompt_text
    assert "stop_management.stop_loss_protects_profit: true" in prompt_text
    assert "stop_management.confidence: high" in prompt_text
    assert "system_note_mentions_trailing_or_moved_stop" in prompt_text
    assert "outlier_size: false" in prompt_text
    assert "outlier_lot_spike: false" in prompt_text
    assert "possible_split_order: false" in prompt_text
    assert "split_group_role: solo" in prompt_text
    assert "is_likely_corrective: false" in prompt_text


def test_build_trade_payload_serializes_trade_risk_fields_and_session(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-payload-user",
        email="ai-payload@example.com",
        account_name="Futures Account",
        account_type="FUTURES",
    )
    trade_account.account_size = 100_000.0

    db.session.add(
        FuturesSymbol(
            root_symbol="MES",
            tick_size=0.25,
            tick_value=1.25,
            display_name="MES",
            exchange="CME",
        )
    )
    db.session.flush()
    trading.clear_cfd_symbol_cache()

    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="MES",
        contract_code="MESM26",
        side="BUY",
        entry_price=5000.0,
        exit_price=5002.5,
        stop_loss=4998.0,
        take_profit=5006.0,
        lot_size=1.0,
        pnl=125.0,
        opened_at=datetime(2026, 3, 10, 19, 0, 0),
        closed_at=datetime(2026, 3, 10, 19, 45, 0),
        trade_note="Held to target.",
    )
    db.session.add(trade)
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
    )

    assert len(payload["trades"]) == 1
    assert payload["trades"][0]["contract_code"] == "MESM26"
    assert payload["trades"][0]["stop_loss"] == 4998.0
    assert payload["trades"][0]["take_profit"] == 5006.0
    assert payload["trades"][0]["entry_session"] == "New York"
    assert payload["trades"][0]["exit_session"] == "New York"
    assert payload["trades"][0]["session"] == "New York"
    assert payload["trades"][0]["review_ref"] == "T1"
    assert payload["trades"][0]["trade_id"] == trade.id
    assert payload["trades"][0]["duration_minutes"] == 45.0
    assert payload["trades"][0]["trade_sequence_number"] == 1
    assert payload["trades"][0]["trade_number_in_session"] == 1
    assert payload["trades"][0]["planned_rr"] == 3.0
    assert payload["trades"][0]["realized_rr"] == 1.25
    assert payload["trades"][0]["tp_capture_pct"] == 41.67
    assert payload["trades"][0]["closed_before_tp"] is True
    assert payload["trades"][0]["closed_before_sl"] is None
    assert payload["trades"][0]["split_group_size"] == 1
    assert payload["trades"][0]["split_group_role"] == "solo"
    sizing = payload["current_week_breakdowns"]["sizing"]
    assert sizing["median_planned_risk_dollars"] == 10.0
    assert sizing["median_risk_pct_of_account"] == 0.01


def test_build_trade_payload_serializes_strategy_context(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-strategy-context-user",
        email="ai-strategy-context@example.com",
    )
    profile = TradeProfile(
        user_id=user.id,
        name="NY Open Sweep",
        current_version_number=1,
    )
    db.session.add(profile)
    db.session.flush()
    version = TradeProfileVersion(
        trade_profile_id=profile.id,
        version_number=1,
        name="NY Open Sweep v1",
        short_description="Only trade the first pullback after the New York open.",
    )
    db.session.add(version)
    db.session.flush()
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            trade_profile_id=profile.id,
            trade_profile_version_id=version.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 10, 14, 0, 0),
            closed_at=datetime(2026, 3, 10, 14, 30, 0),
            trade_note="Waited for the planned pullback.",
        )
    )
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )

    trade = payload["trades"][0]
    assert trade["strategy_name"] == "NY Open Sweep v1"
    assert trade["strategy_version"] == 1
    assert trade["strategy_description"] == "Only trade the first pullback after the New York open."
    strategy_breakdown = payload["current_week_breakdowns"]["strategies"]
    assert strategy_breakdown == [
        {
            "strategy_name": "NY Open Sweep v1",
            "count": 1,
            "win_rate": 100.0,
            "net_pnl": 100.0,
        }
    ]
    assert payload["current_week_breakdowns"]["strategy_coverage"] == {
        "trades_with_strategy": 1,
        "strategy_coverage_pct": 100.0,
    }

    prompt_text = format_payload_for_prompt(payload)
    assert "strategy NY Open Sweep v1: count=1, win_rate=100.00%, net_pnl=+100.00" in prompt_text
    assert "strategy_name: NY Open Sweep v1" in prompt_text
    assert "strategy_description: Only trade the first pullback after the New York open." in prompt_text


def test_build_trade_payload_adds_market_context_and_trailing_stop_detection(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-market-context-user",
        email="ai-market-context@example.com",
    )
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="XAUUSD",
        side="BUY",
        entry_price=100.0,
        exit_price=106.0,
        stop_loss=102.0,
        take_profit=110.0,
        lot_size=1.0,
        pnl=600.0,
        opened_at=datetime(2026, 3, 10, 14, 0, 0),
        closed_at=datetime(2026, 3, 10, 14, 30, 0),
        system_trade_note="SL moved to BE, then trailing stop locked profit.",
    )
    db.session.add(trade)
    db.session.flush()

    def epoch(hour, minute):
        return int(datetime(2026, 3, 10, hour, minute, tzinfo=timezone.utc).timestamp())

    bar_rows = [
        TradeBars(trade_id=trade.id, timeframe="M5", bar_time=epoch(13, 50), open=98.0, high=99.0, low=97.0, close=98.5),
        TradeBars(trade_id=trade.id, timeframe="M5", bar_time=epoch(13, 55), open=98.5, high=99.5, low=98.0, close=99.0),
        TradeBars(trade_id=trade.id, timeframe="M5", bar_time=epoch(14, 0), open=100.0, high=103.0, low=99.0, close=102.0),
        TradeBars(trade_id=trade.id, timeframe="M5", bar_time=epoch(14, 5), open=102.0, high=105.0, low=101.0, close=104.0),
        TradeBars(trade_id=trade.id, timeframe="M5", bar_time=epoch(14, 10), open=104.0, high=108.0, low=103.0, close=107.0),
        TradeBars(trade_id=trade.id, timeframe="M5", bar_time=epoch(14, 30), open=106.0, high=107.0, low=105.0, close=106.5),
        TradeBars(trade_id=trade.id, timeframe="M5", bar_time=epoch(14, 35), open=106.5, high=111.0, low=106.0, close=110.5),
    ]
    db.session.add_all(bar_rows)
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )

    context = payload["trades"][0]["market_context"]
    assert context["bars_status"] == "ready"
    assert context["in_trade_bars"] == 4
    assert context["post_exit_bars"] == 1
    assert context["mfe_price_move"] == 8.0
    assert context["mae_price_move"] == 1.0
    assert context["mfe_r"] is None
    assert context["post_exit_tp_reached"] is True
    assert context["minutes_after_exit_to_tp"] == 5.0

    stop_context = context["stop_management"]
    assert stop_context["stop_loss_breakeven_or_better"] is True
    assert stop_context["stop_loss_protects_profit"] is True
    assert stop_context["confidence"] == "high"
    assert "stored_stop_loss_protects_profit" in stop_context["evidence"]
    assert "system_note_mentions_breakeven_stop" in stop_context["evidence"]
    assert "system_note_mentions_trailing_or_moved_stop" in stop_context["evidence"]

    weekly_market = payload["current_week_breakdowns"]["market_context"]
    assert weekly_market["trades_with_bars"] == 1
    assert weekly_market["bar_coverage_pct"] == 100.0
    assert weekly_market["post_exit_tp_reached_count"] == 1
    assert weekly_market["protective_stop_count"] == 1
    assert weekly_market["trailing_or_breakeven_stop_count"] == 1
    assert "large_candle_entry_count" in weekly_market
    assert "entry_bar_against_direction_count" in weekly_market
    assert "post_exit_continued_count" in weekly_market
    assert "post_exit_reversed_count" in weekly_market

    prompt_text = format_payload_for_prompt(payload)
    assert "market_context.post_exit_tp_reached: true" in prompt_text
    assert "stop_management.stop_loss_protects_profit: true" in prompt_text
    assert "stop_management.confidence: high" in prompt_text


def test_build_trade_payload_excludes_system_trade_notes_from_notes_coverage(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-system-note-user",
        email="ai-system-note@example.com",
    )

    db.session.add_all(
        [
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.1000,
                exit_price=1.1010,
                lot_size=1.0,
                pnl=100.0,
                opened_at=datetime(2026, 3, 10, 8, 0, 0),
                closed_at=datetime(2026, 3, 10, 9, 0, 0),
                system_trade_note="Auto-imported via MT5 sync",
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="GBPUSD",
                side="SELL",
                entry_price=1.2700,
                exit_price=1.2690,
                lot_size=1.0,
                pnl=100.0,
                opened_at=datetime(2026, 3, 10, 10, 0, 0),
                closed_at=datetime(2026, 3, 10, 11, 0, 0),
                trade_note="Faded the first spike and stuck to plan.",
                system_trade_note="Broker close comment",
            ),
        ]
    )
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )

    assert payload["notes_coverage"] == 0.5
    assert payload["notes_with_content"] == 1
    assert payload["notes_missing"] == 1
    assert payload["notes_basis"] == (
        "Per weekly trade idea after bundle merging; counts non-empty user-authored trade_note text only."
    )
    trades_by_symbol = {trade["symbol"]: trade for trade in payload["trades"]}
    assert trades_by_symbol["EURUSD"]["trade_note"] is None
    assert trades_by_symbol["GBPUSD"]["trade_note"] == "Faded the first spike and stuck to plan."


def test_format_payload_for_prompt_handles_missing_trade_session():
    prompt_text = format_payload_for_prompt(
        {
            "generated_at": "2026-03-12 10:00:00 UTC",
            "period_start_utc": "2026-03-03 00:00:00 UTC",
            "period_end_utc": "2026-03-10 00:00:00 UTC",
            "notes_coverage": 0.0,
            "notes_with_content": 0,
            "notes_missing": 1,
            "notes_confidence": "low",
            "notes_basis": "Per weekly trade idea after bundle merging; counts non-empty user-authored trade_note text only.",
            "account_age_days": None,
            "summary": {},
            "historical_context": {},
            "trades": [
                {
                    "symbol": "EURUSD",
                    "contract_code": None,
                    "side": "SELL",
                    "entry_price": 1.0825,
                    "exit_price": 1.081,
                    "stop_loss": 1.084,
                    "take_profit": 1.08,
                    "lot_size": 0.5,
                    "pnl": 75.0,
                    "entry_session": None,
                    "exit_session": "London",
                    "session": None,
                    "duration_minutes": None,
                    "opened_at": None,
                    "closed_at": "2026-03-10 14:45:00 UTC",
                    "trade_sequence_number": 1,
                    "trade_number_in_session": 1,
                    "prev_trade_pnl": None,
                    "minutes_since_prev_close": None,
                    "size_vs_prev_trade": None,
                    "prev_symbol_trade_pnl": None,
                    "minutes_since_prev_symbol_close": None,
                    "size_vs_prev_symbol_trade": None,
                    "loss_streak_before_trade": 0,
                    "is_post_loss_trade": False,
                    "same_symbol_reentry": False,
                    "is_post_loss_same_symbol_trade": False,
                    "same_trade_idea_reentry": False,
                    "is_potential_revenge": False,
                    "is_revenge": False,
                    "planned_rr": None,
                    "realized_rr": None,
                    "tp_capture_pct": None,
                    "closed_before_tp": None,
                    "closed_before_sl": None,
                    "outlier_size": True,
                    "outlier_lot_spike": True,
                    "possible_split_order": False,
                    "split_group_size": 1,
                    "split_group_index": 1,
                    "split_group_role": "solo",
                    "is_likely_corrective": True,
                    "trade_note": None,
                }
            ],
        }
    )

    assert "entry_session: -" in prompt_text
    assert "exit_session: London" in prompt_text
    assert "session: -" in prompt_text
    assert "outlier_size: true" in prompt_text
    assert "outlier_lot_spike: true" in prompt_text
    assert "is_likely_corrective: true" in prompt_text
    assert "is_revenge: false" in prompt_text
    assert "closed_before_tp: -" in prompt_text
    assert "closed_before_sl: -" in prompt_text


def test_format_payload_for_prompt_includes_profile_and_checkin_sections_when_populated():
    prompt_text = format_payload_for_prompt(
        {
            "generated_at": "2026-03-12 10:00:00 UTC",
            "period_start_utc": "2026-03-03 00:00:00 UTC",
            "period_end_utc": "2026-03-10 00:00:00 UTC",
            "notes_coverage": 0.0,
            "account_age_days": None,
            "user_profile": {
                "trading_style": "scalper",
                "instruments": "forex",
                "experience_level": "beginner",
            },
            "weekly_checkin": {
                "emotional_state": "stressed",
                "plan_adherence": "impulsive",
                "execution_quality": "poor",
                "additional_context": "Had a rough start after two early losses.",
            },
            "summary": {},
            "historical_context": {},
            "trades": [],
        }
    )

    assert "USER PROFILE" in prompt_text
    assert "- trading_style: scalper" in prompt_text
    assert "WEEKLY CHECKIN" in prompt_text
    assert "- emotional_state: stressed" in prompt_text
    assert "- additional_context: Had a rough start after two early losses." in prompt_text


def test_format_payload_for_prompt_omits_empty_profile_and_checkin_sections():
    prompt_text = format_payload_for_prompt(
        {
            "generated_at": "2026-03-12 10:00:00 UTC",
            "period_start_utc": "2026-03-03 00:00:00 UTC",
            "period_end_utc": "2026-03-10 00:00:00 UTC",
            "notes_coverage": 0.0,
            "account_age_days": None,
            "user_profile": {
                "trading_style": None,
                "instruments": None,
                "experience_level": None,
            },
            "weekly_checkin": {
                "emotional_state": None,
                "plan_adherence": None,
                "execution_quality": None,
                "additional_context": None,
            },
            "summary": {},
            "historical_context": {},
            "trades": [],
        }
    )

    assert "USER PROFILE" not in prompt_text
    assert "WEEKLY CHECKIN" not in prompt_text


def test_build_profile_instructions_returns_expected_adjustments():
    instructions = build_profile_instructions(
        "scalper",
        "beginner",
        "forex",
        "stressed",
        "impulsive",
        "poor",
    )

    assert instructions.startswith("\nTRADER PROFILE ADJUSTMENTS")
    assert "Use plain language. Explain any jargon." in instructions
    assert "Duration analysis in minutes not hours." in instructions
    assert "London/NY overlap is prime session - weight it accordingly." in instructions
    assert "Emotional week detected - prioritise BEHAVIOUR section." in instructions
    assert "Reference plan adherence directly in the Rule." in instructions
    assert "Find specific examples of poor execution in the trades." in instructions


def test_build_profile_instructions_handles_calm_self_report_mismatch():
    instructions = build_profile_instructions(
        "intraday",
        "experienced",
        "forex",
        "calm",
        "consistent",
        "sharp",
        emotional_index_label="moderate",
        emotional_index_mismatch=True,
    )

    assert "Self-report sounded calm or controlled, but observed behaviour signals were elevated" in instructions
    assert "Mild behavioural signals detected - note briefly, don't over-weight." in instructions
    assert "Treat overnight holds as style exceptions, not automatic mistakes." in instructions
    assert "Only flag overnight holding when it is repeated, clearly unplanned, or concentrated the week's risk." in instructions


def test_build_profile_instructions_can_acknowledge_controlled_emotions():
    instructions = build_profile_instructions(
        "intraday",
        "experienced",
        "forex",
        "calm",
        "consistent",
        "sharp",
        emotional_index_label="low",
        emotional_index_mismatch=False,
    )

    assert "If the trade evidence looks orderly, explicitly acknowledge that emotions looked in check this week without mentioning any internal score." in instructions


def test_build_trade_payload_serializes_user_profile_and_weekly_checkin(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-context-user",
        email="ai-context@example.com",
    )

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        user_profile={
            "trading_style": "intraday",
            "instruments": "indices",
            "experience_level": "experienced",
        },
        weekly_checkin={
            "emotional_state": "calm",
            "plan_adherence": "consistent",
            "execution_quality": "sharp",
            "additional_context": "Clean week overall.",
        },
    )

    assert payload["user_profile"]["trading_style"] == "intraday"
    assert payload["user_profile"]["instruments"] == "indices"
    assert payload["weekly_checkin"]["emotional_state"] == "calm"
    assert payload["weekly_checkin"]["additional_context"] == "Clean week overall."


def test_build_dashboard_advice_messages_appends_profile_adjustments(app_ctx):
    payload = {
        "generated_at": "2026-03-12T12:00:00Z",
        "period_start_utc": "2026-03-07T21:30:00Z",
        "period_end_utc": "2026-03-14T21:30:00Z",
        "notes_coverage": 0.0,
        "account_age_days": None,
        "user_profile": {},
        "weekly_checkin": {},
        "historical_context": {},
        "summary": {"closed_trades": 0},
        "trades": [],
    }

    prompt_history, messages, _payload_json = build_dashboard_advice_messages(
        payload,
        prompt_filename="dashboard_advice.txt",
        profile_adjustments="\nTRADER PROFILE ADJUSTMENTS\nApply all of the following:\n- Use plain language.",
    )

    assert prompt_history.prompt_id == "dashboard_advice"
    assert messages[1]["content"][0]["text"] == ai_service.REVIEW_JSON_OUTPUT_INSTRUCTIONS
    assert messages[2]["content"][0]["text"].endswith("- Use plain language.")


def test_dashboard_prompt_uses_exit_price_language():
    """Pin the structural contract of the dashboard prompt.

    The prompt was rewritten to offload deterministic logic to code and to
    enforce a strict risk-priority rule plus an insight mandate. This test
    locks in the spine (steps + output format + forbidden legacy framings)
    plus the new precomputed-signal references the prompt now consumes.
    """
    prompt_text = load_prompt_text("dashboard_advice.txt")["prompt_text"]

    # Trade fields surfaced for the model
    assert "close_price" not in prompt_text
    assert "entry_price, exit_price, stop_loss, take_profit" in prompt_text
    assert "strategy_name, strategy_version, strategy_description" in prompt_text
    assert "entry_session, exit_session, session, duration_minutes" in prompt_text
    assert "planned_risk_dollars, trade_risk_pct" in prompt_text
    assert "same_trade_idea_reentry" in prompt_text
    assert "is_potential_revenge" in prompt_text
    assert "is_potential_reactive" in prompt_text
    assert "is_revenge" in prompt_text
    assert "planned_rr" in prompt_text
    assert "realized_rr" in prompt_text
    assert "tp_capture_pct" in prompt_text
    assert "closed_before_tp" in prompt_text
    assert "market_context" in prompt_text
    assert "stop_management" in prompt_text
    assert "split_group_size" in prompt_text
    assert "review_ref" in prompt_text

    # Step spine preserved
    assert "INTERNAL WORKFLOW" in prompt_text
    assert "STEP 1 - INTERPRET THE DATA CORRECTLY" in prompt_text
    assert "STEP 2 - JUDGE DATA CONFIDENCE" in prompt_text
    assert "STEP 3 - JUDGE PERFORMANCE SHAPE" in prompt_text
    assert "STEP 4 - JUDGE BEHAVIOUR PRESSURE" in prompt_text
    assert "STEP 5 - CHOOSE TONE MODE" in prompt_text
    assert "STEP 6 - CHOOSE REVIEW MODE" in prompt_text
    assert "STEP 7 - SELECT THE BEST 1-4 INSIGHTS" in prompt_text
    assert "STEP 8 - WRITE THE RESPONSE" in prompt_text
    assert "affirm_and_refine" in prompt_text
    assert "balanced_review" in prompt_text
    assert "corrective_but_encouraging" in prompt_text
    assert "encouraging_with_limited_evidence" in prompt_text

    # Output format spine
    assert "OUTPUT FORMAT" in prompt_text
    assert "Key Takeaways" in prompt_text
    assert 'One line prefixed exactly with "Improve this week:"' in prompt_text
    assert 'One line prefixed exactly with "You\'re already strong at:"' in prompt_text

    # New precomputed-signal references
    assert "risk_authority" in prompt_text
    assert "post_loss_response" in prompt_text
    assert "single_trade_dominance" in prompt_text
    assert "tp_capture_shortfalls" in prompt_text
    assert "session_concentration" in prompt_text
    assert "revenge_evidence" in prompt_text
    assert "SURFACE_FACTS" in prompt_text
    assert "CONFIDENCE_ENVELOPE" in prompt_text or "confidence_envelope" in prompt_text

    # Risk priority rule (the headline behavioural change)
    assert "Risk authority order" in prompt_text
    assert "Lot size is descriptive" in prompt_text
    assert "risk_authority.stable" in prompt_text

    # HARD RULES block — the new top-of-prompt enforcement layer
    assert "HARD RULES" in prompt_text
    assert "R1. LOT SIZE IS NOT RISK." in prompt_text
    assert "R2. NO RECAP TAKEAWAYS." in prompt_text
    assert "R3. NO TEMPLATES IN OUTPUT." in prompt_text
    assert "R9. DIAGNOSIS BEFORE ADVICE." in prompt_text
    assert "R4. STRENGTH OVER COUNT." in prompt_text
    assert "risk_judgment_allowed" in prompt_text
    assert (
        '"Risk cannot be determined reliably from available data."'
        in prompt_text
    )

    # SELF-CHECK gate exists and references the rules by id
    assert "SELF-CHECK BEFORE EMITTING" in prompt_text

    # Insight mandate is now "every", not "at least one"
    assert "EVERY Key Takeaway" in prompt_text or "every Key Takeaway" in prompt_text
    assert "at least one Key Takeaway" not in prompt_text

    # Bullet ceiling reframing
    assert "1-3 bullets by default" in prompt_text
    assert "CEILING, not a quota" in prompt_text or "ceiling, not a quota" in prompt_text

    # Voice / interpretation tension (replaces the long tone+plain-english blocks)
    assert "VOICE" in prompt_text
    assert "INTERPRETATION TENSION" in prompt_text
    assert "DIAGNOSIS LENS" in prompt_text
    assert "likely misunderstanding" in prompt_text
    assert "STRATEGY / PLAYBOOK" in prompt_text
    assert "MARKET CONTEXT" in prompt_text
    assert "post_exit_tp_reached means stored bars show price reached the take-profit" in prompt_text
    assert "stop_management.stop_loss_protects_profit" in prompt_text
    assert "jumped back in" in prompt_text  # keep at least one plain-english anchor

    # Privacy and exact numbers (kept; section was renamed)
    assert "Use exact user numbers when they materially support a point." in prompt_text

    # Tone modes still present
    assert "stressed:" in prompt_text
    assert "calm_sharp:" in prompt_text
    assert "neutral_no_checkin:" in prompt_text

    # Examples kept
    assert "GOOD FORMAT EXAMPLE" in prompt_text
    assert "BAD FORMAT EXAMPLES" in prompt_text
    assert "One split-entry cluster should be treated as one setup, not several" in prompt_text

    # Removed verbose legacy framings (signal: rewrite actually compressed)
    assert "Do not use paragraph prose anywhere in the response." not in prompt_text
    assert "Keep the response between 100 and 150 words." not in prompt_text
    assert "Use only these optional plain-text section labels in the response:" not in prompt_text
    # The 3x outlier_size threshold prose was moved to code
    assert "more than 3x the cohort median" not in prompt_text

    # Conflicting permissions that previously undercut R1 are gone
    assert "lot size when it helps" not in prompt_text
    assert "before lot size" not in prompt_text
    # The DRAWDOWN bucket no longer contains the broken template phrasing
    # that produced "risked less and loss." Curly-brace tokens may still
    # appear inside R3 (where they are forbidden by name), so check the
    # specific broken template phrasing instead.
    assert "risked {risk_change} and" not in prompt_text
    assert "{next_outcome}." not in prompt_text


def test_normalize_dashboard_advice_text_fixes_improvement_prefix_variants():
    text = "Key Takeaways\n- Supported insight.\n\u2192 Improve this week: Keep risk fixed."

    normalized = normalize_dashboard_advice_text(text)

    assert "Improve this week: Keep risk fixed." in normalized
    assert "\u2192 Improve this week:" not in normalized


def test_build_trade_payload_adds_weekly_flags_and_account_metadata(app_ctx, monkeypatch):
    monkeypatch.setattr(ai_service, "utcnow_naive", lambda: datetime(2026, 3, 20, 0, 0, 0))
    user, trade_account = _create_user_and_account(
        username="ai-flags-user",
        email="ai-flags@example.com",
    )
    trade_account.account_size = 100_000.0

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 1, 8, 0, 0),
            closed_at=datetime(2026, 3, 1, 9, 0, 0),
            trade_note="Older account trade.",
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1020,
            exit_price=1.1030,
            stop_loss=1.1010,
            lot_size=1.0,
            pnl=80.0,
            opened_at=datetime(2026, 3, 10, 10, 1, 0),
            closed_at=datetime(2026, 3, 10, 11, 1, 0),
            trade_note="A plan with notes.",
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1030,
            exit_price=1.1040,
            stop_loss=1.1020,
            lot_size=1.0,
            pnl=90.0,
            opened_at=datetime(2026, 3, 10, 10, 4, 0),
            closed_at=datetime(2026, 3, 10, 10, 59, 0),
            trade_note="",
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.2700,
            exit_price=1.2690,
            stop_loss=1.2710,
            lot_size=4.0,
            pnl=160.0,
            opened_at=datetime(2026, 3, 10, 13, 0, 0),
            closed_at=datetime(2026, 3, 10, 13, 5, 0),
            trade_note="",
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )

    assert len(payload["trades"]) == 3
    assert payload["notes_coverage"] == 0.33
    assert payload["notes_with_content"] == 1
    assert payload["notes_missing"] == 2
    assert payload["notes_confidence"] == "low"
    assert payload["account_age_days"] == 18
    assert payload["summary"]["top_symbol_by_trade_count"] == "EURUSD"
    assert payload["summary"]["top_symbol_trade_share_pct"] == 66.67
    assert payload["summary"]["top_symbol_by_abs_pnl"] == "EURUSD"
    assert payload["summary"]["top_symbol_abs_pnl_share_pct"] == 51.52
    assert payload["summary"]["largest_trade_symbol"] == "GBPUSD"
    assert payload["summary"]["largest_trade_abs_pnl_share_pct"] == 48.48

    eur_trades = [trade for trade in payload["trades"] if trade["symbol"] == "EURUSD"]
    gbp_trade = next(trade for trade in payload["trades"] if trade["symbol"] == "GBPUSD")

    assert len(eur_trades) == 2
    assert all(trade["possible_split_order"] is True for trade in eur_trades)
    assert {trade["split_group_size"] for trade in eur_trades} == {2}
    assert {trade["split_group_role"] for trade in eur_trades} == {"lead", "add_on"}
    assert gbp_trade["outlier_size"] is True
    assert gbp_trade["outlier_lot_spike"] is True
    assert gbp_trade["is_likely_corrective"] is True


def test_build_trade_payload_uses_bundled_view_for_summary_and_emotional_index(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-bundle-user",
        email="ai-bundle@example.com",
    )

    bundle_key = "bundle123456789012345678"
    leg_a = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1010,
        lot_size=0.5,
        pnl=30.0,
        commission=-1.0,
        swap=0.0,
        trade_note="Planned scale entry.",
        opened_at=datetime(2026, 3, 10, 10, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 30, 0),
    )
    leg_b = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1005,
        exit_price=1.1015,
        lot_size=0.5,
        pnl=20.0,
        commission=-1.0,
        swap=0.0,
        trade_note="Added on confirmation.",
        opened_at=datetime(2026, 3, 10, 10, 5, 0),
        closed_at=datetime(2026, 3, 10, 10, 35, 0),
    )
    solo = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.2700,
        exit_price=1.2690,
        lot_size=1.0,
        pnl=100.0,
        trade_note="Standalone winner.",
        opened_at=datetime(2026, 3, 10, 12, 0, 0),
        closed_at=datetime(2026, 3, 10, 12, 40, 0),
    )
    db.session.add_all([leg_a, leg_b, solo])
    db.session.flush()
    for t in (leg_a, leg_b):
        apply_interpretation(
            t,
            bundle_pubkey=bundle_key,
            is_reactive=True,
            source="test",
            user_id=user.id,
        )
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
        weekly_checkin={
            "emotional_state": "slightly_off",
            "plan_adherence": "some_deviations",
            "execution_quality": "average",
        },
    )

    assert payload["summary"]["total_trades"] == 2
    assert payload["summary"]["closed_trades"] == 2
    assert payload["summary"]["bundle_count"] == 1
    assert payload["summary"]["reactive_trade_count"] == 1
    assert len(payload["trades"]) == 2

    bundled_trade = next(trade for trade in payload["trades"] if trade["is_bundle"] is True)
    solo_trade = next(trade for trade in payload["trades"] if trade["is_bundle"] is False)
    assert bundled_trade["review_ref"] == "B1"
    assert solo_trade["review_ref"] == "T1"
    assert bundled_trade["bundle_trade_count"] == 2
    assert bundled_trade["is_reactive"] is True
    assert bundled_trade["trade_note"] == "Planned scale entry. | Added on confirmation."

    emotional_index = payload["emotional_index"]
    assert emotional_index["signals"]["bundle_count"] == 1
    assert emotional_index["signals"]["confirmed_revenge_trade_count"] == 0
    assert emotional_index["signals"]["heuristic_reactive_trade_count"] == 0
    assert emotional_index["signals"]["reactive_trade_count"] == 1
    assert emotional_index["components"]["confirmed_reactive_points"] == 1.25
    assert emotional_index["signals"]["total_closed_trades"] == 2


def test_build_trade_payload_adds_sequence_and_revenge_context(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-sequence-user",
        email="ai-sequence@example.com",
    )

    db.session.add_all(
        [
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.1000,
                exit_price=1.0990,
                stop_loss=1.0980,
                take_profit=1.1040,
                lot_size=1.0,
                pnl=-100.0,
                opened_at=datetime(2026, 3, 10, 10, 0, 0),
                closed_at=datetime(2026, 3, 10, 10, 15, 0),
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.0995,
                exit_price=1.1010,
                stop_loss=1.0975,
                take_profit=1.1035,
                lot_size=1.5,
                pnl=150.0,
                opened_at=datetime(2026, 3, 10, 10, 20, 0),
                closed_at=datetime(2026, 3, 10, 10, 40, 0),
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="GBPUSD",
                side="SELL",
                entry_price=1.2700,
                exit_price=1.2690,
                stop_loss=1.2715,
                take_profit=1.2670,
                lot_size=1.0,
                pnl=100.0,
                opened_at=datetime(2026, 3, 10, 12, 0, 0),
                closed_at=datetime(2026, 3, 10, 12, 35, 0),
            ),
        ]
    )
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )

    second_trade = next(
        trade
        for trade in payload["trades"]
        if trade["symbol"] == "EURUSD" and trade["pnl"] == 150.0
    )

    assert second_trade["trade_sequence_number"] == 2
    assert second_trade["prev_trade_pnl"] == -100.0
    assert second_trade["minutes_since_prev_close"] == 5.0
    assert second_trade["size_vs_prev_trade"] == "larger"
    assert second_trade["prev_symbol_trade_pnl"] == -100.0
    assert second_trade["minutes_since_prev_symbol_close"] == 5.0
    assert second_trade["size_vs_prev_symbol_trade"] == "larger"
    assert second_trade["loss_streak_before_trade"] == 1
    assert second_trade["is_post_loss_trade"] is True
    assert second_trade["same_symbol_reentry"] is True
    assert second_trade["is_post_loss_same_symbol_trade"] is True
    assert second_trade["same_trade_idea_reentry"] is True
    assert second_trade["is_potential_revenge"] is True
    assert second_trade["is_potential_reactive"] is True


def test_build_trade_payload_flags_calm_self_report_mismatch_when_behaviour_is_elevated(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-mismatch-user",
        email="ai-mismatch@example.com",
    )

    first = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.0990,
        stop_loss=1.0980,
        take_profit=1.1040,
        lot_size=1.0,
        pnl=-100.0,
        opened_at=datetime(2026, 3, 10, 10, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 15, 0),
    )
    second = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.0995,
        exit_price=1.1010,
        stop_loss=1.0975,
        take_profit=1.1035,
        lot_size=1.5,
        pnl=150.0,
        opened_at=datetime(2026, 3, 10, 10, 20, 0),
        closed_at=datetime(2026, 3, 10, 10, 40, 0),
    )
    db.session.add_all([first, second])
    db.session.flush()
    apply_interpretation(second, is_reactive=True, source="test", user_id=user.id)
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
        weekly_checkin={
            "emotional_state": "calm",
            "plan_adherence": "consistent",
            "execution_quality": "sharp",
        },
    )

    emotional_index = payload["emotional_index"]
    assert emotional_index["score"] == pytest.approx(3.03, abs=0.02)
    assert emotional_index["label"] == "moderate"
    assert emotional_index["self_report_mismatch"] is True
    assert emotional_index["signals"]["confirmed_revenge_trade_count"] == 0
    assert emotional_index["signals"]["heuristic_revenge_trade_count"] == 1
    assert emotional_index["signals"]["revenge_trade_count"] == 1
    assert emotional_index["signals"]["heuristic_reactive_trade_count"] == 1
    assert emotional_index["signals"]["confirmed_reactive_trade_count"] == 1
    assert emotional_index["signals"]["reactive_trade_count"] == 2
    assert emotional_index["components"]["confirmed_reactive_points"] == 1.25
    assert emotional_index["components"]["revenge_points"] == pytest.approx(1.78, abs=0.02)


def test_compute_emotional_index_normalizes_repeated_confirmed_revenge_trades():
    trades = []
    base_open = datetime(2026, 3, 10, 9, 0, 0)
    symbols = [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "AUDUSD",
        "USDCAD",
        "USDCHF",
        "NZDUSD",
        "EURJPY",
        "GBPJPY",
        "AUDJPY",
    ]
    for trade_index in range(10):
        opened_at = base_open + timedelta(hours=trade_index * 6)
        closed_at = opened_at + timedelta(hours=2)
        trades.append(
            SimpleNamespace(
                symbol=symbols[trade_index],
                side="BUY",
                entry_price=1.1000,
                exit_price=1.1010,
                lot_size=1.0,
                pnl=100.0,
                is_revenge=True,
                is_reactive=False,
                is_corrective=False,
                bundle_pubkey=None,
                opened_at=opened_at,
                closed_at=closed_at,
                id=trade_index,
                pubkey=f"t{trade_index}",
            )
        )

    emotional_index = compute_emotional_index(
        trades=trades,
        weekly_checkin={
            "emotional_state": "calm",
            "plan_adherence": "consistent",
            "execution_quality": "sharp",
        },
    )

    assert emotional_index["signals"]["total_closed_trades"] == 10
    assert emotional_index["signals"]["confirmed_revenge_trade_count"] == 10
    assert emotional_index["components"]["confirmed_revenge_points"] == 4.5
    assert emotional_index["components"]["revenge_repetition_bonus"] == 1.5
    assert emotional_index["components"]["revenge_points"] == 6.0
    assert emotional_index["components"]["reactive_points"] == 0.0
    assert emotional_index["components"]["corrective_points"] == 0.0
    assert emotional_index["score"] == 6.0


def test_build_trade_payload_assigns_cross_week_trade_to_close_week(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-crossweek-user",
        email="ai-crossweek@example.com",
    )

    db.session.add_all(
        [
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.1000,
                exit_price=1.1010,
                lot_size=1.0,
                pnl=100.0,
                opened_at=datetime(2026, 3, 9, 23, 55, 0),
                closed_at=datetime(2026, 3, 10, 0, 10, 0),
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="GBPUSD",
                side="SELL",
                entry_price=1.2700,
                exit_price=1.2690,
                lot_size=1.0,
                pnl=90.0,
                opened_at=datetime(2026, 3, 10, 12, 0, 0),
                closed_at=datetime(2026, 3, 11, 0, 5, 0),
            ),
        ]
    )
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )

    assert [trade["symbol"] for trade in payload["trades"]] == ["EURUSD"]


def test_build_trade_payload_historical_context_excludes_review_period(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-history-user",
        email="ai-history@example.com",
    )

    db.session.add_all(
        [
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.1000,
                exit_price=1.1010,
                lot_size=1.0,
                pnl=100.0,
                opened_at=datetime(2026, 3, 1, 8, 0, 0),
                closed_at=datetime(2026, 3, 1, 9, 0, 0),
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="GBPUSD",
                side="SELL",
                entry_price=1.2700,
                exit_price=1.2690,
                lot_size=1.0,
                pnl=90.0,
                opened_at=datetime(2026, 3, 10, 12, 0, 0),
                closed_at=datetime(2026, 3, 10, 13, 0, 0),
            ),
        ]
    )
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )

    assert payload["historical_context"]["comparison_scope"] == "history_before_review_period_only"
    assert payload["historical_context"]["window_end_utc"] == "2026-03-10T00:00:00Z"
    assert payload["historical_context"]["summary"]["total_trades"] == 1
    assert payload["historical_context"]["summary"]["closed_trades"] == 1
    assert "expectancy" in payload["historical_context"]["summary"]
    assert payload["historical_context"]["summary"]["expectancy"] is not None

    pt = payload["performance_trends"]
    assert pt["comparison_basis"] == "reviewed_period_vs_prior_pre_period_history"
    assert pt["win_rate_current"] is not None
    assert pt["win_rate_historical"] is not None
    assert pt["expectancy_current"] is not None
    assert pt["expectancy_historical"] is not None
    assert "ei_trend" in pt


def test_build_trade_payload_ei_trend_from_prior_stored_reviews(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-ei-trend-user",
        email="ai-ei-trend@example.com",
    )
    period_old = datetime(2026, 2, 1, 0, 0, 0)
    period_new = datetime(2026, 3, 10, 0, 0, 0)
    db.session.add(
        AIGeneratedResponse(
            user_id=user.id,
            trade_account_id=trade_account.id,
            kind=WEEKLY_DASHBOARD_KIND,
            period_start_utc=period_old,
            generated_at=period_old,
            response_text="ok",
            payload_json=json.dumps({"emotional_index": {"score": 6.0}}),
        )
    )
    db.session.add_all(
        [
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.1000,
                exit_price=1.1010,
                lot_size=1.0,
                pnl=50.0,
                opened_at=datetime(2026, 3, 10, 12, 0, 0),
                closed_at=datetime(2026, 3, 10, 13, 0, 0),
            ),
        ]
    )
    db.session.commit()

    payload = build_trade_payload(
        user_id=user.id,
        trade_account_id=trade_account.id,
        period_start_utc=period_new,
        period_end_utc=datetime(2026, 3, 11, 0, 0, 0),
        closed_trades_only=True,
    )
    assert payload["performance_trends"]["ei_trend"] == "improving"


def test_format_payload_for_prompt_includes_performance_trends_section():
    payload = {
        "generated_at": "2026-03-12T12:00:00Z",
        "period_start_utc": "2026-03-10T00:00:00Z",
        "period_end_utc": "2026-03-11T00:00:00Z",
        "notes_coverage": 0.0,
        "emotional_index": None,
        "performance_trends": {
            "comparison_basis": "reviewed_period_vs_prior_pre_period_history",
            "prior_history_window_days": 90,
            "win_rate_current": 55.0,
            "win_rate_historical": 50.0,
            "expectancy_current": 12.5,
            "expectancy_historical": 10.0,
            "ei_trend": "flat",
        },
        "summary": {"total_trades": 0, "closed_trades": 0, "open_trades": 0},
        "historical_context": {},
        "trades": [],
    }
    text = format_payload_for_prompt(payload)
    assert "PERFORMANCE_TRENDS" in text
    assert "win_rate_current" in text
    assert "historical_expectancy" in text or "expectancy_historical" in text


def test_get_latest_trade_week_period_falls_back_when_all_trades_are_after_dashboard_period_end(app_ctx):
    """Mid-week onboarding: closed trades exist only after get_weekly_dashboard_period's end boundary."""
    user, trade_account = _create_user_and_account(
        username="onboard-week-user",
        email="onboard-week@example.com",
    )
    now_utc = datetime(2026, 4, 8, 18, 0, 0, tzinfo=timezone.utc)
    dash = get_weekly_dashboard_period(now_utc=now_utc)
    closed_at = dash["period_end_utc"] + timedelta(hours=6)
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=50.0,
            opened_at=closed_at - timedelta(hours=1),
            closed_at=closed_at,
        )
    )
    db.session.commit()

    period = get_latest_trade_week_period(
        user_id=user.id,
        trade_account_id=trade_account.id,
        now_utc=now_utc,
    )
    assert period is not None
    assert period["period_start_utc"] is not None
    assert period["period_end_utc"] is not None


def test_maybe_generate_weekly_dashboard_advice_skips_before_cutoff_for_returning_user(app_ctx, monkeypatch):
    """Accounts that already had a weekly AI wait until Friday 5:30 PM NY for that review week."""
    user, trade_account = _create_user_and_account(
        username="ai-returning-cutoff-user",
        email="ai-returning-cutoff@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 10, 9, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    db.session.add(
        AIGeneratedResponse(
            user_id=user.id,
            trade_account_id=trade_account.id,
            kind=ai_service.WEEKLY_DASHBOARD_KIND,
            model="gpt-5-mini",
            response_text="Prior weekly review",
            trade_count_used=1,
            period_start_utc=datetime(2026, 3, 2, 5, 0, 0),
            period_end_utc=datetime(2026, 3, 9, 5, 0, 0),
        )
    )
    db.session.commit()

    def _boom(*args, **kwargs):
        raise AssertionError("OpenAI should not run before weekly cutoff")

    monkeypatch.setattr(ai_service, "request_openai_response", _boom)

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        now_utc=datetime(2026, 3, 11, 15, 0, 0, tzinfo=timezone.utc),
    )
    assert result["generated"] is False
    assert result.get("skip_reason") == "before_weekly_cutoff"


def test_maybe_generate_weekly_dashboard_advice_first_review_may_run_before_weekly_cutoff(app_ctx, monkeypatch):
    """No prior weekly AI on the account: allow generation before Friday (onboarding path)."""
    user, trade_account = _create_user_and_account(
        username="ai-first-review-cutoff-user",
        email="ai-first-review-cutoff@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 10, 9, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    db.session.commit()

    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="first-review-cutoff-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.commit()

    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": json.dumps(
                {
                    "summary": {"text": "First review mid-week.", "refs": ["T1"]},
                    "takeaways": [{"text": "Onboarding takeaway.", "refs": ["T1"]}],
                    "improvement": {"text": "Improve this week: stay consistent.", "refs": []},
                    "strength": {"text": "", "refs": []},
                }
            ),
            "usage": {},
            "output": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        now_utc=datetime(2026, 3, 11, 15, 0, 0, tzinfo=timezone.utc),
    )
    assert result["generated"] is True
    assert result.get("skip_reason") is None
    assert result["record"] is not None


def test_maybe_generate_weekly_dashboard_advice_returns_skip_reason_for_no_trades(app_ctx, monkeypatch):
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }

    monkeypatch.setattr(ai_service, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setattr(
        ai_service,
        "build_trade_payload",
        lambda **kwargs: {
            "generated_at": "2026-03-12T12:00:00Z",
            "period_start_utc": "2026-03-07T21:30:00Z",
            "period_end_utc": "2026-03-14T21:30:00Z",
            "notes_coverage": 0.0,
            "account_age_days": None,
            "historical_context": {},
            "summary": {"closed_trades": 0},
            "trades": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=1,
        trade_account_id=1,
        prompt_filename="dashboard_advice.txt",
    )

    assert result["generated"] is False
    assert result["skip_reason"] == "no_trades"


def test_maybe_generate_weekly_dashboard_advice_generates_with_two_closed_trades(app_ctx, monkeypatch):
    """Weekly advice no longer skips on trade count alone; two closed trades can still generate."""
    user, trade_account = _create_user_and_account(
        username="ai-two-trades-user",
        email="ai-two-trades@example.com",
    )
    db.session.add_all(
        [
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.1000,
                exit_price=1.1010,
                lot_size=1.0,
                pnl=100.0,
                opened_at=datetime(2026, 3, 10, 9, 0, 0),
                closed_at=datetime(2026, 3, 10, 10, 0, 0),
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="GBPUSD",
                side="SELL",
                entry_price=1.2700,
                exit_price=1.2690,
                lot_size=1.0,
                pnl=90.0,
                opened_at=datetime(2026, 3, 11, 9, 0, 0),
                closed_at=datetime(2026, 3, 11, 10, 0, 0),
            ),
        ]
    )
    db.session.commit()

    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="two-closed-trades-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.commit()

    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": json.dumps(
                {
                    "summary": {
                        "text": "Two-trade week: early EURUSD win with GBPUSD follow-through.",
                        "refs": ["T1", "T2"],
                    },
                    "takeaways": [
                        {"text": "EURUSD set the tone for the week.", "refs": ["T1"]},
                        {"text": "GBPUSD added a second clean close.", "refs": ["T2"]},
                    ],
                    "improvement": {
                        "text": "Improve this week: keep risk consistent across both winners.",
                        "refs": [],
                    },
                    "strength": {"text": "", "refs": []},
                }
            ),
            "usage": {},
            "output": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        now_utc=datetime(2026, 3, 14, 22, 0, 0),
        force_regenerate=True,
    )

    assert result["generated"] is True
    assert result["record"] is not None
    assert result["record"].trade_count_used == 2
    assert result["record"].response_meta_json is not None


def test_maybe_generate_weekly_dashboard_advice_generates_when_three_closed_trades_exist(app_ctx, monkeypatch):
    user, trade_account = _create_user_and_account(
        username="ai-three-trades-user",
        email="ai-three-trades@example.com",
    )
    db.session.add_all(
        [
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="EURUSD",
                side="BUY",
                entry_price=1.1000,
                exit_price=1.1010,
                lot_size=1.0,
                pnl=100.0,
                opened_at=datetime(2026, 3, 10, 9, 0, 0),
                closed_at=datetime(2026, 3, 10, 10, 0, 0),
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="GBPUSD",
                side="SELL",
                entry_price=1.2700,
                exit_price=1.2690,
                lot_size=1.0,
                pnl=90.0,
                opened_at=datetime(2026, 3, 11, 9, 0, 0),
                closed_at=datetime(2026, 3, 11, 10, 0, 0),
            ),
            Trade(
                user_id=user.id,
                trade_account_id=trade_account.id,
                symbol="USDJPY",
                side="BUY",
                entry_price=149.20,
                exit_price=149.60,
                lot_size=1.0,
                pnl=110.0,
                opened_at=datetime(2026, 3, 12, 9, 0, 0),
                closed_at=datetime(2026, 3, 12, 10, 0, 0),
            ),
        ]
    )
    db.session.commit()

    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="three-closed-trades-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.commit()

    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": json.dumps(
                {
                    "summary": {
                        "text": "This was a profitable week led by clean EURUSD and USDJPY execution, with the middle trade acting more like support than the main driver.",
                        "refs": ["T1", "T3"],
                    },
                    "takeaways": [
                        {
                            "text": "EURUSD set the tone early and gave the clearest clean execution of the week.",
                            "refs": ["T1"],
                        },
                        {
                            "text": "GBPUSD contributed, but it looked more like a supporting winner than the week's defining idea.",
                            "refs": ["T2"],
                        },
                        {
                            "text": "USDJPY closed the week cleanly and reinforced that patience held into later setups.",
                            "refs": ["T3"],
                        },
                    ],
                        "improvement": {
                            "text": "Improve this week: Keep size fixed and let the first clean winner set the standard for later setups.",
                            "refs": [],
                        },
                        "strength": {"text": "", "refs": []},
                    }
                ),
            "usage": {},
            "output": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        now_utc=datetime(2026, 3, 14, 22, 0, 0),
        force_regenerate=True,
    )

    assert result["generated"] is True
    assert result["record"] is not None
    assert result["record"].trade_count_used == 3
    assert result["record"].payload_json == ai_service.serialize_payload(result["payload"])
    assert result["record"].response_meta_json is not None
    review_display = build_dashboard_review_display(
        result["record"].response_text,
        result["record"].response_meta_json,
    )
    assert review_display["summary"]["refs"] == ["T1", "T3"]
    assert [item["refs"] for item in review_display["takeaways"]] == [["T1"], ["T2"], ["T3"]]
    assert review_display["improvement"]["text"].lower().startswith("improve this week:")


def test_maybe_generate_weekly_dashboard_advice_omits_experiment_when_ineligible(app_ctx, monkeypatch):
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    user, trade_account = _create_user_and_account(
        username="ai-experiment-ineligible-user",
        email="ai-experiment-ineligible@example.com",
    )

    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 10, 9, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    db.session.commit()

    monkeypatch.setattr(ai_service, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setattr(ai_service, "should_generate_weekly_dashboard_advice", lambda **kwargs: True)
    monkeypatch.setattr(
        ai_service,
        "build_trade_payload",
        lambda **kwargs: {
            "generated_at": "2026-03-12T12:00:00Z",
            "period_start_utc": "2026-03-07T21:30:00Z",
            "period_end_utc": "2026-03-14T21:30:00Z",
            "historical_context": {},
            "summary": {"closed_trades": 1},
            "trades": [{"review_ref": "T1"}],
            "experiment_context": {"eligible": False, "trade_idea_count": 1, "min_required": 1},
        },
    )
    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": json.dumps(
                {
                    "summary": {"text": "Sample summary.", "refs": []},
                    "takeaways": [{"text": "Sample takeaway.", "refs": []}, {"text": "Second takeaway.", "refs": []}],
                    "improvement": {"text": "Improve this week: Keep risk fixed.", "refs": []},
                    "strength": {"text": "You're already strong at: Staying selective.", "refs": []},
                    "experiment": {"text": "Try one experiment.", "refs": []},
                }
            ),
            "usage": {},
            "output": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        force_regenerate=True,
    )
    assert result["generated"] is True
    meta = json.loads(result["record"].response_meta_json or "{}")
    assert "experiment" not in meta


def test_maybe_generate_weekly_dashboard_advice_keeps_experiment_when_eligible(app_ctx, monkeypatch):
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    user, trade_account = _create_user_and_account(
        username="ai-experiment-eligible-user",
        email="ai-experiment-eligible@example.com",
    )

    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 10, 9, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    db.session.commit()

    monkeypatch.setattr(ai_service, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setattr(ai_service, "should_generate_weekly_dashboard_advice", lambda **kwargs: True)
    monkeypatch.setattr(
        ai_service,
        "build_trade_payload",
        lambda **kwargs: {
            "generated_at": "2026-03-12T12:00:00Z",
            "period_start_utc": "2026-03-07T21:30:00Z",
            "period_end_utc": "2026-03-14T21:30:00Z",
            "historical_context": {},
            "summary": {"closed_trades": 5},
            "trades": [{"review_ref": "T1"}],
            "experiment_context": {"eligible": True, "trade_idea_count": 5, "min_required": 1},
        },
    )
    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": json.dumps(
                {
                    "summary": {"text": "Sample summary.", "refs": []},
                    "takeaways": [{"text": "Sample takeaway.", "refs": []}, {"text": "Second takeaway.", "refs": []}],
                    "improvement": {"text": "Improve this week: Keep risk fixed.", "refs": []},
                    "strength": {"text": "You're already strong at: Staying selective.", "refs": []},
                    "experiment": {"text": "Run one London-only execution drill.", "refs": []},
                }
            ),
            "usage": {},
            "output": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        force_regenerate=True,
    )
    assert result["generated"] is True
    meta = json.loads(result["record"].response_meta_json or "{}")
    assert meta.get("experiment", {}).get("text") == "Run one London-only execution drill."


def test_force_weekly_generation_appends_new_response_for_same_period(app_ctx, monkeypatch):
    user = User(
        username="ai-force-user",
        email="ai-force@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Primary Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()

    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.08,
        exit_price=1.0815,
        lot_size=1.0,
        pnl=150.0,
        opened_at=datetime(2026, 3, 10, 19, 0, 0),
        closed_at=datetime(2026, 3, 10, 19, 45, 0),
    )
    db.session.add(trade)
    db.session.flush()

    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="force-weekly-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.flush()

    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    db.session.add(
        AIGeneratedResponse(
            user_id=user.id,
            trade_account_id=trade_account.id,
            prompt_history_id=prompt_history.id,
            kind=ai_service.WEEKLY_DASHBOARD_KIND,
            model="gpt-5-mini",
            response_text="Existing weekly advice",
            payload_hash="existing-payload",
            trade_count_used=1,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
        )
    )
    db.session.commit()

    monkeypatch.setattr(ai_service, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setattr(ai_service, "should_generate_weekly_dashboard_advice", lambda **kwargs: True)
    monkeypatch.setattr(
        ai_service,
        "build_trade_payload",
        lambda **kwargs: {
            "generated_at": "2026-03-12T12:00:00Z",
            "period_start_utc": "2026-03-07T21:30:00Z",
            "period_end_utc": "2026-03-14T21:30:00Z",
            "historical_context": {},
            "summary": {"closed_trades": 3},
            "trades": [{"symbol": "EURUSD"}, {"symbol": "GBPUSD"}, {"symbol": "USDJPY"}],
        },
    )
    monkeypatch.setattr(
        ai_service,
        "build_dashboard_advice_messages",
        lambda payload, prompt_filename=None, profile_adjustments="": (
            prompt_history,
            [{"role": "user", "content": []}],
            '{"payload":"new"}',
        ),
    )
    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": "Fresh weekly advice",
            "usage": {},
            "output": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        force_regenerate=True,
    )

    rows = (
        AIGeneratedResponse.query.filter_by(
            user_id=user.id,
            trade_account_id=trade_account.id,
            kind=ai_service.WEEKLY_DASHBOARD_KIND,
            period_start_utc=period["period_start_utc"],
        )
        .order_by(AIGeneratedResponse.generated_at.asc(), AIGeneratedResponse.id.asc())
        .all()
    )

    assert result["generated"] is True
    assert result["record"] is not None
    assert result["record"].response_text == "Fresh weekly advice"
    assert result["record"].payload_json == '{"payload":"new"}'
    assert len(rows) == 2
    assert rows[0].response_text == "Existing weekly advice"
    assert rows[1].response_text == "Fresh weekly advice"
    assert rows[1].payload_json == '{"payload":"new"}'


def test_weekly_dashboard_advice_runs_rewrite_pass_without_trade_payload(app_ctx, monkeypatch):
    user, trade_account = _create_user_and_account(
        username="ai-two-pass-user",
        email="ai-two-pass@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 10, 9, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="two-pass-weekly-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.commit()

    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    monkeypatch.setattr(ai_service, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setattr(ai_service, "should_generate_weekly_dashboard_advice", lambda **kwargs: True)
    monkeypatch.setattr(
        ai_service,
        "build_trade_payload",
        lambda **kwargs: {
            "generated_at": "2026-03-12T12:00:00Z",
            "period_start_utc": "2026-03-07T21:30:00Z",
            "period_end_utc": "2026-03-14T21:30:00Z",
            "historical_context": {},
            "summary": {"closed_trades": 1},
            "trades": [{"review_ref": "T1", "symbol": "EURUSD"}],
        },
    )
    monkeypatch.setattr(
        ai_service,
        "build_dashboard_advice_messages",
        lambda payload, prompt_filename=None, profile_adjustments="": (
            prompt_history,
            [{"role": "user", "content": [{"type": "input_text", "text": "TRADE PAYLOAD"}]}],
            '{"payload":"two-pass"}',
        ),
    )

    calls = []

    def fake_request(messages, model=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "model": "gpt-5-mini",
                "status": "completed",
                "output_text": (
                    "Pass one review with repeated wording.\n\n"
                    "Key Takeaways\n"
                    "- Repeated point.\n"
                    "Improve this week: Keep the rule simple."
                ),
                "usage": {},
                "output": [],
            }
        return {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": (
                "Pass one review, clearer.\n\n"
                "Key Takeaways\n"
                "- Clearer point.\n"
                "Improve this week: Keep the rule simple."
            ),
            "usage": {},
            "output": [],
        }

    monkeypatch.setattr(ai_service, "request_openai_response", fake_request)

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        force_regenerate=True,
    )

    rewrite_prompt = calls[1][0]["content"][0]["text"]
    assert len(calls) == 2
    assert "TRADE PAYLOAD" not in rewrite_prompt
    assert "Pass one review with repeated wording." in rewrite_prompt
    assert "simpler does not mean shorter" in rewrite_prompt
    assert "never explain, rename, reorder, or drop references" in rewrite_prompt
    assert "London/New York idea" in rewrite_prompt
    assert result["record"].pass_1_output.startswith("Pass one review with repeated wording.")
    assert result["record"].pass_2_output.startswith("Pass one review, clearer.")
    assert result["record"].response_text.startswith("Pass one review, clearer.")
    assert result["record"].prompt_version_pass_1 == "two-pass-weekly-sha"
    assert result["record"].prompt_version_pass_2 == ai_service.hash_text(
        load_prompt_text(ai_service.DEFAULT_REWRITE_PROMPT_FILE)["prompt_text"]
    )
    assert result["record"].model_used == "gpt-5-mini"


def test_weekly_rewrite_falls_back_when_format_is_lost(app_ctx, monkeypatch):
    pass_1_output = (
        "Formatted story.\n\n"
        "Key Takeaways\n"
        "- Keep the structure.\n"
        "Improve this week: Keep the plan simple."
    )
    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": "Shorter but now just one unstructured paragraph.",
            "usage": {},
            "output": [],
        },
    )

    output, response_payload, prompt_data = ai_service.rewrite_weekly_dashboard_review_or_fallback(
        pass_1_output,
        user_id=1,
        trade_account_id=2,
        period={
            "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
            "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
        },
    )

    assert output == pass_1_output
    assert response_payload is None
    assert prompt_data is None


def test_weekly_dashboard_advice_falls_back_when_rewrite_fails(app_ctx, monkeypatch):
    user, trade_account = _create_user_and_account(
        username="ai-two-pass-fallback-user",
        email="ai-two-pass-fallback@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=100.0,
            opened_at=datetime(2026, 3, 10, 9, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="two-pass-fallback-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.commit()

    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    monkeypatch.setattr(ai_service, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setattr(ai_service, "should_generate_weekly_dashboard_advice", lambda **kwargs: True)
    monkeypatch.setattr(
        ai_service,
        "build_trade_payload",
        lambda **kwargs: {
            "generated_at": "2026-03-12T12:00:00Z",
            "period_start_utc": "2026-03-07T21:30:00Z",
            "period_end_utc": "2026-03-14T21:30:00Z",
            "historical_context": {},
            "summary": {"closed_trades": 1},
            "trades": [{"review_ref": "T1", "symbol": "EURUSD"}],
        },
    )
    monkeypatch.setattr(
        ai_service,
        "build_dashboard_advice_messages",
        lambda payload, prompt_filename=None, profile_adjustments="": (
            prompt_history,
            [{"role": "user", "content": []}],
            '{"payload":"fallback"}',
        ),
    )

    calls = []

    def fake_request(messages, model=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "model": "gpt-5-mini",
                "status": "completed",
                "output_text": "Pass one survives.",
                "usage": {},
                "output": [],
            }
        raise ai_service.AIRequestError("rewrite unavailable")

    monkeypatch.setattr(ai_service, "request_openai_response", fake_request)

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        force_regenerate=True,
    )

    assert len(calls) == 2
    assert result["generated"] is True
    assert result["record"].pass_1_output == "Pass one survives."
    assert result["record"].pass_2_output == "Pass one survives."
    assert result["record"].response_text == "Pass one survives."
    assert result["rewrite_response_payload"] is None


def test_weekly_generation_allows_thin_sample_with_cautious_review(app_ctx, monkeypatch):
    user = User(
        username="ai-thin-week-user",
        email="ai-thin-week@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Primary Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()

    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="thin-week-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.commit()

    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }

    monkeypatch.setattr(ai_service, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setattr(ai_service, "should_generate_weekly_dashboard_advice", lambda **kwargs: True)
    monkeypatch.setattr(
        ai_service,
        "build_trade_payload",
        lambda **kwargs: {
            "generated_at": "2026-03-12T12:00:00Z",
            "period_start_utc": "2026-03-07T21:30:00Z",
            "period_end_utc": "2026-03-14T21:30:00Z",
            "historical_context": {},
            "summary": {"closed_trades": 1},
            "trades": [{"review_ref": "T1", "symbol": "EURUSD"}],
        },
    )
    monkeypatch.setattr(
        ai_service,
        "build_dashboard_advice_messages",
        lambda payload, prompt_filename=None, profile_adjustments="": (
            prompt_history,
            [{"role": "user", "content": []}],
            '{"payload":"thin-week"}',
        ),
    )
    monkeypatch.setattr(
        ai_service,
        "request_openai_response",
        lambda messages, model=None: {
            "model": "gpt-5-mini",
            "status": "completed",
            "output_text": json.dumps(
                {
                    "summary": {
                        "text": "This week had limited evidence, but the single trade still offers a cautious review point.",
                        "refs": ["T1"],
                    },
                    "takeaways": [
                        {
                            "text": "EURUSD provided the only closed trade, so any pattern read should stay tentative.",
                            "refs": ["T1"],
                        },
                        {
                            "text": "The week is too thin for broad conclusions, but the trade is still worth reviewing.",
                            "refs": ["T1"],
                        },
                    ],
                    "rule": {
                        "text": "Rule: When the sample is thin, keep any adjustment modest and wait for more evidence before changing too much.",
                        "refs": [],
                    },
                }
            ),
            "usage": {},
            "output": [],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_filename="dashboard_advice.txt",
        now_utc=datetime(2026, 3, 14, 22, 0, 0),
        force_regenerate=True,
    )

    assert result["generated"] is True
    assert result["record"] is not None
    assert result["record"].trade_count_used == 1
