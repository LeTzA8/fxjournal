"""Lightweight helpers for deciding whether a trade is closed."""

from __future__ import annotations

from collections.abc import Mapping

_POSITION_REMAINING_FIELDS = (
    "remaining_size",
    "remaining_volume",
    "open_volume",
)


def _trade_field_value(trade, field_name):
    if trade is None:
        return None
    if isinstance(trade, Mapping):
        return trade.get(field_name)
    return getattr(trade, field_name, None)


def _is_zero_position_value(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def trade_is_closed(trade):
    """Return True when a trade-like object has a real close signal."""

    if trade is None:
        return False

    if _trade_field_value(trade, "is_open") is False:
        return True

    if _trade_field_value(trade, "is_closed") is True:
        return True

    if _trade_field_value(trade, "closed_at") is not None:
        return True

    for field_name in _POSITION_REMAINING_FIELDS:
        if _is_zero_position_value(_trade_field_value(trade, field_name)):
            return True

    return _trade_field_value(trade, "exit_price") is not None
