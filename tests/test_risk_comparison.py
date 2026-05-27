from helpers.risk_comparison import (
    classify_risk_change,
    classify_risk_change_between,
    enrich_serialized_trades_with_risk_comparison,
)


def test_classify_risk_change():
    assert classify_risk_change(1.2, 1.0) == "larger"
    assert classify_risk_change(0.8, 1.0) == "smaller"
    assert classify_risk_change(1.05, 1.0) == "same"
    assert classify_risk_change(1.0, 0) is None


def test_classify_risk_change_between_prefers_pct():
    current = {"trade_risk_pct": 1.2, "planned_risk_dollars": 500}
    baseline = {"trade_risk_pct": 1.0, "planned_risk_dollars": 400}
    assert classify_risk_change_between(current, baseline) == "larger"


def test_enrich_serialized_trades_with_risk_comparison():
    trades = [
        {
            "review_ref": "T1",
            "symbol": "EURUSD",
            "opened_at": "2026-05-01T10:00:00Z",
            "trade_sequence_number": 1,
            "trade_risk_pct": 0.5,
        },
        {
            "review_ref": "T2",
            "symbol": "EURUSD",
            "opened_at": "2026-05-01T12:00:00Z",
            "trade_sequence_number": 2,
            "trade_risk_pct": 0.8,
        },
    ]
    enrich_serialized_trades_with_risk_comparison(trades)
    assert trades[1]["risk_pct_vs_prev"] == "larger"
    assert trades[1]["risk_pct_vs_prev_symbol"] == "larger"
    assert "risk_pct_vs_prev" not in trades[0]
