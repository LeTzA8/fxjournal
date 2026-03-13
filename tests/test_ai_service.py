from datetime import datetime

import ai_service
from ai_service import (
    build_trade_payload,
    format_payload_for_prompt,
    load_prompt_text,
    maybe_generate_weekly_dashboard_advice,
)
from models import AIGeneratedResponse, AIPromptHistory, Trade, TradeAccount, User, db


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


def test_format_payload_for_prompt_includes_trade_fields_and_signed_drawdown():
    payload = {
        "generated_at": "2026-03-12 10:00:00 UTC",
        "period_start_utc": "2026-03-03 00:00:00 UTC",
        "period_end_utc": "2026-03-10 00:00:00 UTC",
        "notes_coverage": 1.0,
        "account_age_days": 45,
        "summary": {
            "total_trades": 1,
            "closed_trades": 1,
            "open_trades": 0,
            "win_rate": 100.0,
            "net_pnl": 140.0,
            "weekly_pnl": 140.0,
            "monthly_pnl": 140.0,
            "best_trade_pnl": 140.0,
            "worst_trade_pnl": 140.0,
            "max_drawdown": -45.75,
        },
        "historical_context": {
            "window_start_utc": "2025-12-12 00:00:00 UTC",
            "window_end_utc": "2026-03-10 00:00:00 UTC",
            "window_days": 90,
            "summary": {
                "total_trades": 8,
                "closed_trades": 8,
                "win_rate": 50.0,
                "net_pnl": 320.5,
                "max_drawdown": -88.2,
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
                "session": "New York",
                "duration_minutes": 84.0,
                "opened_at": "2026-03-10 14:00:00 UTC",
                "closed_at": "2026-03-10 15:24:00 UTC",
                "outlier_size": False,
                "possible_split_order": False,
                "is_likely_corrective": False,
                "trade_note": "Held through the close.",
            }
        ],
    }

    prompt_text = format_payload_for_prompt(payload)

    assert "- max_drawdown: -45.75" in prompt_text
    assert "- historical_max_drawdown: -88.20" in prompt_text
    assert "- notes_coverage: 1.00" in prompt_text
    assert "- account_age_days: 45" in prompt_text
    assert "contract_code: MESM26" in prompt_text
    assert "stop_loss: 4998.75000" in prompt_text
    assert "take_profit: 5004.00000" in prompt_text
    assert "session: New York" in prompt_text
    assert "duration_minutes: 84.00" in prompt_text
    assert "outlier_size: false" in prompt_text
    assert "possible_split_order: false" in prompt_text
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
    assert payload["trades"][0]["session"] == "New York"
    assert payload["trades"][0]["duration_minutes"] == 45.0


def test_format_payload_for_prompt_handles_missing_trade_session():
    prompt_text = format_payload_for_prompt(
        {
            "generated_at": "2026-03-12 10:00:00 UTC",
            "period_start_utc": "2026-03-03 00:00:00 UTC",
            "period_end_utc": "2026-03-10 00:00:00 UTC",
            "notes_coverage": 0.0,
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
                    "session": None,
                    "duration_minutes": None,
                    "opened_at": None,
                    "closed_at": "2026-03-10 14:45:00 UTC",
                    "outlier_size": True,
                    "possible_split_order": False,
                    "is_likely_corrective": True,
                    "trade_note": None,
                }
            ],
        }
    )

    assert "session: -" in prompt_text
    assert "outlier_size: true" in prompt_text
    assert "is_likely_corrective: true" in prompt_text


def test_dashboard_prompt_uses_exit_price_language():
    prompt_text = load_prompt_text("dashboard_advice.txt")["prompt_text"]

    assert "close_price" not in prompt_text
    assert prompt_text.count("exit_price") >= 3
    assert "stop_loss" in prompt_text
    assert "take_profit" in prompt_text
    assert "session" in prompt_text
    assert 'End the response with one final bullet prefixed exactly with "→ Rule:"' in prompt_text
    assert "Do not use paragraph prose anywhere in the response." in prompt_text
    assert "Keep the response between 100 and 150 words." not in prompt_text
    assert "notes_coverage" in prompt_text
    assert "possible_split_order" in prompt_text


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
    assert payload["account_age_days"] == 18

    eur_trades = [trade for trade in payload["trades"] if trade["symbol"] == "EURUSD"]
    gbp_trade = next(trade for trade in payload["trades"] if trade["symbol"] == "GBPUSD")

    assert len(eur_trades) == 2
    assert all(trade["possible_split_order"] is True for trade in eur_trades)
    assert gbp_trade["outlier_size"] is True
    assert gbp_trade["is_likely_corrective"] is True


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
            "output_text": "EDGE\n- Supported insight.\n→ Rule: Keep risk fixed.",
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
        lambda payload, prompt_filename=None: (prompt_history, [{"role": "user", "content": []}], '{"payload":"new"}'),
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
    assert len(rows) == 2
    assert rows[0].response_text == "Existing weekly advice"
    assert rows[1].response_text == "Fresh weekly advice"
