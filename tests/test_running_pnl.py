"""Tests for helpers/running_pnl.py — Running P&L event builder."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from helpers.running_pnl import build_running_pnl_events, summarize_running_pnl


def _trade(*, id=1, pnl=100.0, commission=None, swap=None, closed_at=None, symbol="EURUSD", side="BUY"):
    return SimpleNamespace(
        id=id,
        pnl=pnl,
        commission=commission,
        swap=swap,
        closed_at=closed_at or datetime(2026, 4, 1, 12, 0),
        symbol=symbol,
        side=side,
    )


def _cf(*, id=1, flow_type="deposit", amount=1000.0, occurred_at=None, note=""):
    return SimpleNamespace(
        id=id,
        flow_type=flow_type,
        amount=amount,
        occurred_at=occurred_at or datetime(2026, 4, 1, 8, 0),
        note=note,
    )


class TestSimpleCumulativePnl:
    def test_single_winning_trade(self):
        events = build_running_pnl_events([_trade(pnl=250.0)], [])
        assert len(events) == 1
        assert events[0]["running_realized_pnl"] == 250.0
        assert events[0]["running_cash_flow"] == 0.0
        assert events[0]["running_net_result"] == 250.0

    def test_multiple_trades_accumulate(self):
        trades = [
            _trade(id=1, pnl=100.0, closed_at=datetime(2026, 4, 1, 10, 0)),
            _trade(id=2, pnl=-50.0, closed_at=datetime(2026, 4, 1, 11, 0)),
            _trade(id=3, pnl=200.0, closed_at=datetime(2026, 4, 1, 12, 0)),
        ]
        events = build_running_pnl_events(trades, [])
        assert [e["running_realized_pnl"] for e in events] == [100.0, 50.0, 250.0]

    def test_commission_and_swap_deducted(self):
        trades = [_trade(pnl=500.0, commission=10.0, swap=-3.0)]
        events = build_running_pnl_events(trades, [])
        assert events[0]["amount"] == pytest.approx(487.0)  # 500 - 10 + (-3)

    def test_net_pnl_resolver_overrides_default_math(self):
        trades = [_trade(pnl=500.0, commission=10.0)]
        events = build_running_pnl_events(
            trades, [],
            resolve_trade_pnl=lambda t: 42.0,
        )
        assert events[0]["amount"] == 42.0


class TestDepositsDoNotAffectTradingPnl:
    def test_deposit_only(self):
        events = build_running_pnl_events([], [_cf(flow_type="deposit", amount=5000)])
        assert len(events) == 1
        assert events[0]["running_realized_pnl"] == 0.0
        assert events[0]["running_cash_flow"] == 5000.0
        assert events[0]["running_net_result"] == 5000.0
        assert events[0]["event_type"] == "deposit"

    def test_deposit_does_not_inflate_trading_pnl(self):
        trades = [_trade(pnl=100.0, closed_at=datetime(2026, 4, 2))]
        cfs = [_cf(flow_type="deposit", amount=10000, occurred_at=datetime(2026, 4, 1))]
        events = build_running_pnl_events(trades, cfs)
        assert events[0]["event_type"] == "deposit"
        assert events[0]["running_realized_pnl"] == 0.0
        assert events[1]["event_type"] == "trade_close"
        assert events[1]["running_realized_pnl"] == 100.0
        assert events[1]["running_cash_flow"] == 10000.0


class TestWithdrawalsDoNotAffectTradingPnl:
    def test_withdrawal_only(self):
        events = build_running_pnl_events([], [_cf(flow_type="withdrawal", amount=2000)])
        assert events[0]["running_realized_pnl"] == 0.0
        assert events[0]["running_cash_flow"] == -2000.0
        assert events[0]["amount"] == -2000.0

    def test_withdrawal_does_not_deflate_trading_pnl(self):
        trades = [_trade(pnl=300.0, closed_at=datetime(2026, 4, 1))]
        cfs = [_cf(flow_type="withdrawal", amount=1000, occurred_at=datetime(2026, 4, 2))]
        events = build_running_pnl_events(trades, cfs)
        assert events[1]["running_realized_pnl"] == 300.0
        assert events[1]["running_cash_flow"] == -1000.0


class TestMixedEventOrdering:
    def test_chronological_order_across_types(self):
        trades = [
            _trade(id=1, pnl=50, closed_at=datetime(2026, 4, 2, 10, 0)),
            _trade(id=2, pnl=75, closed_at=datetime(2026, 4, 4, 10, 0)),
        ]
        cfs = [
            _cf(id=1, flow_type="deposit", amount=1000, occurred_at=datetime(2026, 4, 1)),
            _cf(id=2, flow_type="withdrawal", amount=200, occurred_at=datetime(2026, 4, 3)),
        ]
        events = build_running_pnl_events(trades, cfs)
        types = [e["event_type"] for e in events]
        assert types == ["deposit", "trade_close", "withdrawal", "trade_close"]

    def test_same_timestamp_deterministic_order(self):
        ts = datetime(2026, 4, 1, 12, 0)
        trades = [
            _trade(id=10, pnl=100, closed_at=ts),
            _trade(id=20, pnl=200, closed_at=ts),
        ]
        cfs = [
            _cf(id=5, flow_type="deposit", amount=500, occurred_at=ts),
        ]
        events = build_running_pnl_events(trades, cfs)
        assert len(events) == 3
        assert events[0]["event_type"] == "deposit"
        assert events[1]["event_type"] == "trade_close"
        assert events[1]["amount"] == 100.0
        assert events[2]["event_type"] == "trade_close"
        assert events[2]["amount"] == 200.0


class TestOpenTradesExcluded:
    def test_open_trade_not_in_events(self):
        open_trade = SimpleNamespace(
            id=99, pnl=1000.0, commission=None, swap=None,
            closed_at=None, symbol="GBPUSD", side="SELL",
        )
        closed_trade = _trade(id=1, pnl=50.0)
        events = build_running_pnl_events([open_trade, closed_trade], [])
        assert len(events) == 1
        assert events[0]["amount"] == 50.0


class TestIdenticalTimestamps:
    def test_two_trades_same_instant(self):
        ts = datetime(2026, 4, 1, 12, 0, 0)
        trades = [
            _trade(id=1, pnl=100, closed_at=ts),
            _trade(id=2, pnl=-30, closed_at=ts),
        ]
        events = build_running_pnl_events(trades, [])
        assert len(events) == 2
        assert events[0]["amount"] == 100.0
        assert events[1]["amount"] == -30.0
        assert events[1]["running_realized_pnl"] == 70.0

    def test_deposit_and_trade_same_instant(self):
        ts = datetime(2026, 4, 1, 12, 0, 0)
        trades = [_trade(id=1, pnl=50, closed_at=ts)]
        cfs = [_cf(id=1, flow_type="deposit", amount=1000, occurred_at=ts)]
        events = build_running_pnl_events(trades, cfs)
        assert events[0]["event_type"] == "deposit"
        assert events[1]["event_type"] == "trade_close"


class TestEdgeCases:
    def test_no_trades_no_cash_flows(self):
        events = build_running_pnl_events([], [])
        assert events == []

    def test_null_pnl_trade_skipped(self):
        trade = SimpleNamespace(
            id=1, pnl=None, commission=None, swap=None,
            closed_at=datetime(2026, 4, 1), symbol="EURUSD", side="BUY",
        )
        events = build_running_pnl_events([trade], [])
        assert events == []

    def test_adjustment_flow_type(self):
        cfs = [_cf(flow_type="adjustment", amount=-150, occurred_at=datetime(2026, 4, 1))]
        events = build_running_pnl_events([], cfs)
        assert events[0]["event_type"] == "adjustment"
        assert events[0]["amount"] == -150.0
        assert events[0]["running_cash_flow"] == -150.0

    def test_date_range_filtering(self):
        trades = [
            _trade(id=1, pnl=100, closed_at=datetime(2026, 3, 15)),
            _trade(id=2, pnl=200, closed_at=datetime(2026, 4, 5)),
            _trade(id=3, pnl=300, closed_at=datetime(2026, 4, 20)),
        ]
        events = build_running_pnl_events(
            trades, [],
            date_from=datetime(2026, 4, 1),
            date_to=datetime(2026, 4, 10),
        )
        assert len(events) == 1
        assert events[0]["amount"] == 200.0

    def test_invalid_flow_type_ignored(self):
        cf = SimpleNamespace(
            id=1, flow_type="bonus", amount=500,
            occurred_at=datetime(2026, 4, 1), note="",
        )
        events = build_running_pnl_events([], [cf])
        assert events == []


class TestSummarize:
    def test_empty_summary(self):
        summary = summarize_running_pnl([])
        assert summary["total_realized_pnl"] == 0.0
        assert summary["event_count"] == 0

    def test_mixed_summary(self):
        trades = [
            _trade(id=1, pnl=100, closed_at=datetime(2026, 4, 1)),
            _trade(id=2, pnl=-30, closed_at=datetime(2026, 4, 2)),
        ]
        cfs = [
            _cf(id=1, flow_type="deposit", amount=5000, occurred_at=datetime(2026, 3, 30)),
            _cf(id=2, flow_type="withdrawal", amount=1000, occurred_at=datetime(2026, 4, 3)),
        ]
        events = build_running_pnl_events(trades, cfs)
        summary = summarize_running_pnl(events)
        assert summary["total_realized_pnl"] == pytest.approx(70.0)
        assert summary["total_cash_flow"] == pytest.approx(4000.0)
        assert summary["total_net_result"] == pytest.approx(4070.0)
        assert summary["event_count"] == 4
        assert summary["trade_close_count"] == 2
        assert summary["deposit_count"] == 1
        assert summary["withdrawal_count"] == 1
