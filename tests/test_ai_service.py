from datetime import datetime

from ai_service import build_trade_payload, format_payload_for_prompt, load_prompt_text
from models import Trade, TradeAccount, User, db


def test_format_payload_for_prompt_includes_trade_fields_and_signed_drawdown():
    payload = {
        "generated_at": "2026-03-12 10:00:00 UTC",
        "period_start_utc": "2026-03-03 00:00:00 UTC",
        "period_end_utc": "2026-03-10 00:00:00 UTC",
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
                "trade_note": "Held through the close.",
            }
        ],
    }

    prompt_text = format_payload_for_prompt(payload)

    assert "- max_drawdown: -45.75" in prompt_text
    assert "- historical_max_drawdown: -88.20" in prompt_text
    assert "contract_code: MESM26" in prompt_text
    assert "stop_loss: 4998.75000" in prompt_text
    assert "take_profit: 5004.00000" in prompt_text
    assert "session: New York" in prompt_text
    assert "duration_minutes: 84.00" in prompt_text


def test_build_trade_payload_serializes_trade_risk_fields_and_session(app_ctx):
    user = User(
        username="ai-payload-user",
        email="ai-payload@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Futures Account",
        account_type="FUTURES",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()

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
                    "trade_note": None,
                }
            ],
        }
    )

    assert "session: -" in prompt_text


def test_dashboard_prompt_uses_exit_price_language():
    prompt_text = load_prompt_text("dashboard_advice.txt")["prompt_text"]

    assert "close_price" not in prompt_text
    assert prompt_text.count("exit_price") >= 3
    assert "stop_loss" in prompt_text
    assert "take_profit" in prompt_text
    assert "session" in prompt_text
