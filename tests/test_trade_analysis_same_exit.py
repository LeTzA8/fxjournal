"""Focused tests for same_exit bundle detection in detect_outliers."""

from datetime import datetime

from helpers.trade_analysis import detect_outliers
from models import Trade, TradeInterpretation


def _trade(trade_id, **overrides):
    base = {
        "user_id": 1,
        "trade_account_id": 1,
        "symbol": "NAS100",
        "side": "BUY",
        "entry_price": 18200.0,
        "exit_price": 18280.5,
        "lot_size": 0.5,
        "pnl": 40.0,
        "opened_at": datetime(2026, 3, 17, 9, 0, 0),
        "closed_at": datetime(2026, 3, 17, 10, 30, 0),
    }
    base.update(overrides)
    trade = Trade(**base)
    trade.id = trade_id
    return trade


def _trade_identities(candidate):
    return sorted(getattr(trade, "id") for trade in candidate["trades"])


def test_same_exit_bundle_detected_without_tp_sl_or_split_bucket():
    trades = [
        _trade(
            1,
            exit_price=18280.5,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 30, 0),
        ),
        _trade(
            2,
            exit_price=18281.0,
            opened_at=datetime(2026, 3, 17, 9, 6, 0),
            closed_at=datetime(2026, 3, 17, 10, 45, 0),
        ),
        _trade(
            3,
            exit_price=18280.0,
            opened_at=datetime(2026, 3, 17, 9, 12, 0),
            closed_at=datetime(2026, 3, 17, 11, 0, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert len(outliers["bundle_candidates"]) == 1
    candidate = outliers["bundle_candidates"][0]
    assert candidate["match_reason"] == "same_exit"
    assert _trade_identities(candidate) == [1, 2, 3]


def test_same_exit_does_not_override_split_bucket():
    trades = [
        _trade(
            1,
            exit_price=18280.5,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 30, 0),
        ),
        _trade(
            2,
            exit_price=18281.0,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 3, 0),
            closed_at=datetime(2026, 3, 17, 10, 45, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert len(outliers["bundle_candidates"]) == 1
    assert outliers["bundle_candidates"][0]["match_reason"] == "split_bucket"


def test_same_exit_does_not_override_same_tp_and_sl():
    trades = [
        _trade(
            1,
            exit_price=18280.5,
            take_profit=18350.0,
            stop_loss=18150.0,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 30, 0),
        ),
        _trade(
            2,
            exit_price=18281.0,
            take_profit=18350.0,
            stop_loss=18150.0,
            opened_at=datetime(2026, 3, 17, 9, 6, 0),
            closed_at=datetime(2026, 3, 17, 10, 45, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert len(outliers["bundle_candidates"]) == 1
    assert outliers["bundle_candidates"][0]["match_reason"] == "same_tp_and_sl"


def test_different_side_does_not_bundle_on_same_exit():
    trades = [
        _trade(1, side="BUY", exit_price=18280.5),
        _trade(2, side="SELL", exit_price=18281.0, opened_at=datetime(2026, 3, 17, 9, 6, 0)),
    ]

    outliers = detect_outliers(trades)

    assert outliers["bundle_candidates"] == []


def test_different_symbol_does_not_bundle_on_same_exit():
    trades = [
        _trade(1, symbol="NAS100", exit_price=18280.5),
        _trade(
            2,
            symbol="US30",
            exit_price=18281.0,
            opened_at=datetime(2026, 3, 17, 9, 6, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert outliers["bundle_candidates"] == []


def test_exit_price_outside_tolerance_does_not_bundle_on_same_exit():
    trades = [
        _trade(1, exit_price=18280.0, take_profit=None, stop_loss=None),
        _trade(
            2,
            exit_price=18500.0,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 6, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert outliers["bundle_candidates"] == []


def test_null_exit_price_does_not_trigger_same_exit():
    trades = [
        _trade(1, exit_price=18280.5, take_profit=None, stop_loss=None),
        _trade(
            2,
            exit_price=None,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 6, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert outliers["bundle_candidates"] == []


def test_same_exit_respects_representative_closed_guard():
    trades = [
        _trade(
            1,
            exit_price=18280.5,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 0, 0),
        ),
        _trade(
            2,
            exit_price=18281.0,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 11, 0, 0),
            closed_at=datetime(2026, 3, 17, 12, 0, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert outliers["bundle_candidates"] == []


def test_split_bucket_bundle_still_detected():
    trades = [
        _trade(
            1,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            take_profit=1.1050,
            stop_loss=1.0975,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 9, 20, 0),
        ),
        _trade(
            2,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1002,
            exit_price=1.1012,
            take_profit=1.1050,
            stop_loss=1.0974,
            opened_at=datetime(2026, 3, 17, 9, 3, 0),
            closed_at=datetime(2026, 3, 17, 9, 40, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert len(outliers["bundle_candidates"]) == 1
    assert outliers["bundle_candidates"][0]["match_reason"] == "split_bucket"


def test_already_bundled_trades_are_ignored():
    bundled = _trade(
        1,
        exit_price=18280.5,
        take_profit=None,
        stop_loss=None,
    )
    bundled.interpretation = TradeInterpretation(bundle_pubkey="existing-bundle")
    trades = [
        bundled,
        _trade(
            2,
            exit_price=18281.0,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 6, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert outliers["bundle_candidates"] == []


def test_revenge_flag_excludes_trade_from_same_exit_bundle():
    revenge_trade = _trade(
        1,
        exit_price=18280.5,
        take_profit=None,
        stop_loss=None,
    )
    revenge_trade.interpretation = TradeInterpretation(is_revenge=True)
    trades = [
        revenge_trade,
        _trade(
            2,
            exit_price=18281.0,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 6, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    assert outliers["bundle_candidates"] == []


def test_multiple_same_day_pairs_remain_separate_bundles():
    trades = [
        _trade(
            1,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 9, 20, 0),
        ),
        _trade(
            2,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1002,
            exit_price=1.1012,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 9, 15, 0),
            closed_at=datetime(2026, 3, 17, 9, 40, 0),
        ),
        _trade(
            3,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1010,
            exit_price=1.1020,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 13, 10, 0),
            closed_at=datetime(2026, 3, 17, 13, 35, 0),
        ),
        _trade(
            4,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1012,
            exit_price=1.1022,
            take_profit=None,
            stop_loss=None,
            opened_at=datetime(2026, 3, 17, 13, 25, 0),
            closed_at=datetime(2026, 3, 17, 13, 50, 0),
        ),
    ]

    outliers = detect_outliers(trades)

    bundle_candidates = outliers["bundle_candidates"]
    assert len(bundle_candidates) == 2
    assert all(len(candidate["trades"]) == 2 for candidate in bundle_candidates)
    assert sorted(_trade_identities(candidate) for candidate in bundle_candidates) == [
        [1, 2],
        [3, 4],
    ]
