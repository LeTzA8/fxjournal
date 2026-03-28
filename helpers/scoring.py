from helpers.trade_analysis import build_trade_annotations, get_trade_identity
from trading import merge_bundled_trades


def _get_value(record, key):
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _subjective_score(weekly_checkin):
    emotional_state_points = {
        "calm": 0.0,
        "slightly_off": 0.5,
        "stressed": 1.0,
    }
    plan_adherence_points = {
        "consistent": 0.0,
        "some_deviations": 0.5,
        "impulsive": 1.0,
    }
    execution_quality_points = {
        "sharp": 0.0,
        "average": 0.0,
        "poor": 0.5,
    }
    return (
        emotional_state_points.get(str(_get_value(weekly_checkin, "emotional_state") or "").strip().lower(), 0.0)
        + plan_adherence_points.get(str(_get_value(weekly_checkin, "plan_adherence") or "").strip().lower(), 0.0)
        + execution_quality_points.get(str(_get_value(weekly_checkin, "execution_quality") or "").strip().lower(), 0.0)
    )


def _score_label(score):
    if score >= 7.5:
        return "very_high"
    if score >= 5.0:
        return "high"
    if score >= 2.5:
        return "moderate"
    return "low"


def compute_emotional_index(*, trades, weekly_checkin):
    merged_trades = merge_bundled_trades(trades)
    closed_trades = [
        trade
        for trade in merged_trades
        if getattr(trade, "closed_at", None) is not None
    ]
    if weekly_checkin is None and not closed_trades:
        return None

    total_closed_trades = len(closed_trades)
    reactive_trade_count = sum(1 for trade in closed_trades if bool(getattr(trade, "is_reactive", False)))
    corrective_trade_count = sum(1 for trade in closed_trades if bool(getattr(trade, "is_corrective", False)))
    annotations = build_trade_annotations(closed_trades)
    revenge_trade_count = sum(
        1
        for trade in closed_trades
        if bool(annotations.get(get_trade_identity(trade), {}).get("is_potential_revenge"))
    )

    subjective_points = _subjective_score(weekly_checkin)
    objective_reactive_points = (
        min((reactive_trade_count / total_closed_trades) * 4.0, 3.0)
        if total_closed_trades
        else 0.0
    )
    objective_corrective_points = (
        min((corrective_trade_count / total_closed_trades) * 3.0, 2.5)
        if total_closed_trades
        else 0.0
    )
    objective_revenge_points = min(revenge_trade_count * 0.75, 2.0)

    score = min(
        subjective_points
        + objective_reactive_points
        + objective_corrective_points
        + objective_revenge_points,
        10.0,
    )
    return {
        "score": round(score, 2),
        "label": _score_label(score),
        "signals": {
            "reactive_trade_count": reactive_trade_count,
            "corrective_trade_count": corrective_trade_count,
            "revenge_trade_count": revenge_trade_count,
            "total_closed_trades": total_closed_trades,
        },
    }
