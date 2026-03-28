from datetime import datetime

import ai_service
from ai_service import (
    build_dashboard_advice_messages,
    build_profile_instructions,
    build_trade_payload,
    format_payload_for_prompt,
    load_prompt_text,
    maybe_generate_weekly_dashboard_advice,
    normalize_dashboard_advice_text,
)
from models import (
    AIGeneratedResponse,
    AIPromptHistory,
    Trade,
    TradeAccount,
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
                "combined_revenge_points": 1.25,
                "confirmed_reactive_points": 1.5,
                "confirmed_corrective_points": 0.0,
                "revenge_points": 1.25,
            },
            "signals": {
                "bundle_count": 0,
                "confirmed_revenge_trade_count": 1,
                "confirmed_behavior_trade_count": 1,
                "heuristic_revenge_trade_count": 1,
                "reactive_trade_count": 1,
                "corrective_trade_count": 0,
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
        "trades": [
            {
                "symbol": "MES (MESM26)",
                "contract_code": "MESM26",
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
                "outlier_size": False,
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
    assert "EMOTIONAL INDEX" in prompt_text
    assert "- label: moderate" in prompt_text
    assert "- self_report_mismatch: true" in prompt_text
    assert "- subjective_points: 0.00" in prompt_text
    assert "- confirmed_reactive_points: 1.50" in prompt_text
    assert "- revenge_points: 1.25" in prompt_text
    assert "- confirmed_revenge_trade_count: 1" in prompt_text
    assert "- heuristic_revenge_trade_count: 1" in prompt_text
    assert "- reactive_trade_count: 1" in prompt_text
    assert "- bundle_count: 0" in prompt_text
    assert "- total_closed_trades: 1" in prompt_text
    assert "- confirmed_behavior_trade_count: 1" in prompt_text
    assert "- comparison_scope: history_before_review_period_only" in prompt_text
    assert "- account_age_days: 45" in prompt_text
    assert "- top_symbol_trade_share_pct: 100.00%" in prompt_text
    assert "- largest_trade_abs_pnl_share_pct: 100.00%" in prompt_text
    assert "contract_code: MESM26" in prompt_text
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
    assert "outlier_size: false" in prompt_text
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
    assert messages[1]["content"][0]["text"].endswith("- Use plain language.")


def test_dashboard_prompt_uses_exit_price_language():
    prompt_text = load_prompt_text("dashboard_advice.txt")["prompt_text"]

    assert "close_price" not in prompt_text
    assert "entry_price, exit_price, stop_loss, take_profit" in prompt_text
    assert "stop_loss" in prompt_text
    assert "take_profit" in prompt_text
    assert "entry_session" in prompt_text
    assert "exit_session" in prompt_text
    assert "entry_session, exit_session, session, duration_minutes" in prompt_text
    assert "trade_sequence_number" in prompt_text
    assert "same_trade_idea_reentry" in prompt_text
    assert "is_potential_revenge" in prompt_text
    assert "prev_symbol_trade_pnl" in prompt_text
    assert "is_revenge" in prompt_text
    assert "planned_rr, realized_rr, tp_capture_pct" in prompt_text
    assert "closed_before_tp" in prompt_text
    assert "split_group_size" in prompt_text
    assert "Key Takeaways" in prompt_text
    assert "Start with one unlabeled summary paragraph of 2-3 sentences." in prompt_text
    assert "Under Key Takeaways, write 2-4 bullets total. Never write more than 4." in prompt_text
    assert "Do not add a weak bullet just to hit a target count." in prompt_text
    assert "Every bullet must anchor to a specific trade idea or sequence from" in prompt_text
    assert "Choose the 2-4 most informative trade ideas or sequences from the" in prompt_text
    assert "If the week has 5 or fewer trade ideas, aim for the summary plus" in prompt_text
    assert "Use split_group_size, split_group_role, and possible_split_order to" in prompt_text
    assert 'End with one final standalone line prefixed exactly with "Rule:"' in prompt_text
    assert "If TRADER PROFILE ADJUSTMENTS are provided in the input, follow them for" in prompt_text
    assert "The summary should set context, not restate the bullets line for line." in prompt_text
    assert "If the summary already states the weekly result, the first bullet" in prompt_text
    assert "If all closed trades lost, say there were no winning trades instead" in prompt_text
    assert "avoid awkward phrasing like Tokyo-related sessions." in prompt_text
    assert "Prioritize bullets in this order when the data supports it:" in prompt_text
    assert "Prefer the most concrete and teachable insight, not just the most" in prompt_text
    assert "Prefer a session, behaviour, execution, or pattern rule over a" in prompt_text
    assert "Only use a symbol-only rule when the week's issue was truly isolated" in prompt_text
    assert "The rule should almost never mention two different symbols." in prompt_text
    assert "Default to broader process language such as after a loss, after a" in prompt_text
    assert 'Bad rule example: "After the XAUUSD stop, wait one full session' in prompt_text
    assert 'Better rule example: "After a large stop-out, wait one full session' in prompt_text
    assert "Do not use paragraph prose anywhere in the response." not in prompt_text
    assert "Never reveal exact account metrics from the payload." in prompt_text
    assert "Keep the response between 100 and 150 words." not in prompt_text
    assert "notes_coverage" in prompt_text
    assert "notes_with_content / notes_missing / notes_confidence" in prompt_text
    assert "EMOTIONAL INDEX" in prompt_text
    assert "top_symbol_by_trade_count / top_symbol_trade_share_pct" in prompt_text
    assert "top_symbol_abs_pnl_share_pct" in prompt_text
    assert "largest_trade_abs_pnl_share_pct" in prompt_text
    assert "bundle_count: Structural context only." in prompt_text
    assert "confirmed_revenge_trade_count: User-confirmed revenge labels." in prompt_text
    assert "revenge_trade_count: Combined revenge count" in prompt_text
    assert "possible_split_order" in prompt_text
    assert "is_revenge: User-confirmed revenge flag." in prompt_text
    assert "is_reactive / is_corrective: User-confirmed behaviour flags." in prompt_text
    assert "self_report_mismatch: If true, the user reported calm/controlled" in prompt_text
    assert "same_trade_idea_reentry alone does not mean revenge or impulsiveness." in prompt_text
    assert "If notes_confidence is low and the relevant trade has no note" in prompt_text
    assert "Use only these optional plain-text section labels in the response:" not in prompt_text


def test_normalize_dashboard_advice_text_fixes_rule_prefix_variants():
    text = "Key Takeaways\n- Supported insight.\n\u00e2\u2020' Rule: Keep risk fixed."

    normalized = normalize_dashboard_advice_text(text)

    assert normalized.endswith("Rule: Keep risk fixed.")
    assert "\u00e2\u2020'" not in normalized


def test_build_trade_payload_adds_weekly_flags_and_account_metadata(app_ctx, monkeypatch):
    monkeypatch.setattr(ai_service, "utcnow_naive", lambda: datetime(2026, 3, 20, 0, 0, 0))
    user, trade_account = _create_user_and_account(
        username="ai-flags-user",
        email="ai-flags@example.com",
    )

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
    assert gbp_trade["is_likely_corrective"] is True


def test_build_trade_payload_uses_bundled_view_for_summary_and_emotional_index(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-bundle-user",
        email="ai-bundle@example.com",
    )

    bundle_key = "bundle123456789012345678"
    db.session.add_all(
        [
            Trade(
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
                is_reactive=True,
                bundle_pubkey=bundle_key,
                opened_at=datetime(2026, 3, 10, 10, 0, 0),
                closed_at=datetime(2026, 3, 10, 10, 30, 0),
            ),
            Trade(
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
                is_reactive=True,
                bundle_pubkey=bundle_key,
                opened_at=datetime(2026, 3, 10, 10, 5, 0),
                closed_at=datetime(2026, 3, 10, 10, 35, 0),
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
                trade_note="Standalone winner.",
                opened_at=datetime(2026, 3, 10, 12, 0, 0),
                closed_at=datetime(2026, 3, 10, 12, 40, 0),
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
    assert bundled_trade["bundle_trade_count"] == 2
    assert bundled_trade["is_reactive"] is True
    assert bundled_trade["trade_note"] == "Planned scale entry. | Added on confirmation."

    emotional_index = payload["emotional_index"]
    assert emotional_index["signals"]["bundle_count"] == 1
    assert emotional_index["signals"]["confirmed_revenge_trade_count"] == 0
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


def test_build_trade_payload_flags_calm_self_report_mismatch_when_behaviour_is_elevated(app_ctx):
    user, trade_account = _create_user_and_account(
        username="ai-mismatch-user",
        email="ai-mismatch@example.com",
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
                is_reactive=True,
                opened_at=datetime(2026, 3, 10, 10, 20, 0),
                closed_at=datetime(2026, 3, 10, 10, 40, 0),
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
        weekly_checkin={
            "emotional_state": "calm",
            "plan_adherence": "consistent",
            "execution_quality": "sharp",
        },
    )

    emotional_index = payload["emotional_index"]
    assert emotional_index["score"] == 2.5
    assert emotional_index["label"] == "moderate"
    assert emotional_index["self_report_mismatch"] is True
    assert emotional_index["signals"]["confirmed_revenge_trade_count"] == 0
    assert emotional_index["signals"]["heuristic_revenge_trade_count"] == 1
    assert emotional_index["signals"]["revenge_trade_count"] == 1
    assert emotional_index["components"]["confirmed_reactive_points"] == 1.25
    assert emotional_index["components"]["revenge_points"] == 1.25


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


def test_maybe_generate_weekly_dashboard_advice_returns_skip_reason_for_too_few_trades(app_ctx, monkeypatch):
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
            "notes_coverage": 0.5,
            "account_age_days": 40,
            "historical_context": {},
            "summary": {"closed_trades": 2},
            "trades": [{"symbol": "EURUSD"}, {"symbol": "GBPUSD"}],
        },
    )

    result = maybe_generate_weekly_dashboard_advice(
        user_id=1,
        trade_account_id=1,
        prompt_filename="dashboard_advice.txt",
    )

    assert result["generated"] is False
    assert result["skip_reason"] == "too_few_trades"


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
            "output_text": "EDGE\n- Supported insight.\n\u00e2\u2020' Rule: Keep risk fixed.",
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
    assert result["record"].response_text.endswith("Rule: Keep risk fixed.")


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
