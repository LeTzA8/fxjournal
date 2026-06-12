from helpers.scoring import build_trade_behavior_signal_map
from helpers.trade_analysis import get_trade_identity
from trading import format_trade_symbol, outlier_size_reason, resolve_net_pnl, to_display_timezone

BEHAVIOR_ORDER = ("revenge", "reactive", "corrective")
BEHAVIOR_LABELS = {
    "revenge": "Revenge",
    "reactive": "Reactive",
    "corrective": "Corrective",
}
BEHAVIOR_PRIORITY = {
    ("confirmed", "revenge"): 80,
    ("possible", "revenge"): 70,
    ("confirmed", "reactive"): 60,
    ("possible", "reactive"): 45,
    ("confirmed", "corrective"): 35,
    ("possible", "corrective"): 20,
}


def _coerce_minutes(value):
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return None
    if minutes < 0:
        return None
    return minutes


def _has_larger_size(context):
    return (
        context.get("size_vs_prev_trade") == "larger"
        or context.get("size_vs_prev_symbol_trade") == "larger"
    )


def _trade_account_type(trade):
    account = getattr(trade, "trade_account", None)
    if account is None:
        return None
    return getattr(account, "account_type", None)


def _build_reason_snippets(key, signal, *, account_type=None):
    context = signal.get("context") or {}
    reasons = []

    if key == "revenge":
        if context.get("is_post_loss_same_symbol_trade"):
            reasons.append("same-symbol re-entry after a loss")
        elif context.get("is_post_loss_trade"):
            reasons.append("post-loss re-entry")
        if context.get("same_trade_idea_reentry"):
            reasons.append("same-idea re-entry")
        quick_minutes = (
            _coerce_minutes(context.get("minutes_since_prev_symbol_close"))
            or _coerce_minutes(context.get("minutes_since_prev_close"))
        )
        if quick_minutes is not None and quick_minutes <= 30:
            reasons.append("quick turnaround")
        if _has_larger_size(context):
            reasons.append("size stepped up")
        if (context.get("loss_streak_before_trade") or 0) >= 2:
            reasons.append("loss streak pressure")

    elif key == "reactive":
        if context.get("same_trade_idea_reentry"):
            reasons.append("same-idea re-engagement")
        elif context.get("same_symbol_reentry"):
            reasons.append("same-symbol re-entry")
        quick_minutes = _coerce_minutes(context.get("minutes_since_prev_close"))
        if quick_minutes is not None and quick_minutes <= 45:
            reasons.append("fast turnaround")
        if context.get("is_post_loss_trade"):
            reasons.append("came right after a loss")
        if _has_larger_size(context):
            reasons.append("size stepped up")

    elif key == "corrective":
        if context.get("closed_before_sl") is True:
            reasons.append("cut before the stop-loss level")
        if context.get("quick_duration"):
            reasons.append("short holding time")
        if context.get("outlier_size"):
            if context.get("outlier_lot_spike"):
                reasons.append(outlier_size_reason(account_type))
            else:
                reasons.append("unusually high risk on the account vs your recent trades")
        if context.get("is_post_loss_trade") or context.get("same_trade_idea_reentry"):
            reasons.append("followed a pressured sequence")

    unique_reasons = []
    seen = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        unique_reasons.append(reason)
    return unique_reasons[:3]


def _build_confirmed_title(key):
    return f"{BEHAVIOR_LABELS[key]} Trade"


def _build_possible_title(key, signal, *, account_type=None):
    label = BEHAVIOR_LABELS[key]
    reasons = _build_reason_snippets(key, signal, account_type=account_type)
    if not reasons:
        return f"Possible {label.lower()} signal"
    return f"Possible {label.lower()} signal: {'; '.join(reasons)}"


def _build_badges_for_signal(signal, *, account_type=None):
    badges = []
    confirmed = signal.get("confirmed") or {}
    possible = signal.get("possible") or {}

    for key in BEHAVIOR_ORDER:
        if confirmed.get(key):
            badges.append(
                {
                    "key": key,
                    "state": "confirmed",
                    "label": BEHAVIOR_LABELS[key],
                    "title": _build_confirmed_title(key),
                }
            )

    for key in BEHAVIOR_ORDER:
        if possible.get(key):
            badges.append(
                {
                    "key": key,
                    "state": "possible",
                    "label": f"Possible {BEHAVIOR_LABELS[key]}",
                    "title": _build_possible_title(key, signal, account_type=account_type),
                }
            )

    return badges


def build_trade_behavior_badge_map(trades, *, signal_map=None):
    signals = signal_map or build_trade_behavior_signal_map(trades)
    badge_map = {}
    for trade in trades:
        identity = get_trade_identity(trade)
        badge_map[identity] = _build_badges_for_signal(
            signals.get(identity, {}),
            account_type=_trade_account_type(trade),
        )
    return badge_map


def build_trade_behavior_analytics(trades, *, timezone_name="UTC", signal_map=None, max_flagged_trades=6):
    signals = signal_map or build_trade_behavior_signal_map(trades)
    badge_map = build_trade_behavior_badge_map(trades, signal_map=signals)
    closed_trades = [
        trade
        for trade in trades
        if getattr(trade, "closed_at", None) is not None
    ]
    closed_trade_count = len(closed_trades)

    confirmed_counts = {key: 0 for key in BEHAVIOR_ORDER}
    possible_counts = {key: 0 for key in BEHAVIOR_ORDER}
    confirmed_trade_count = 0
    possible_trade_count = 0
    review_trade_count = 0
    flagged_trades = []

    for trade in closed_trades:
        identity = get_trade_identity(trade)
        signal = signals.get(identity, {})
        confirmed = signal.get("confirmed") or {}
        possible = signal.get("possible") or {}
        badges = badge_map.get(identity) or []

        has_confirmed = any(confirmed.values())
        has_possible = any(possible.values())
        if has_confirmed:
            confirmed_trade_count += 1
        if has_possible:
            possible_trade_count += 1
        if badges:
            review_trade_count += 1

        for key in BEHAVIOR_ORDER:
            if confirmed.get(key):
                confirmed_counts[key] += 1
            if possible.get(key):
                possible_counts[key] += 1

        if not badges:
            continue

        display_dt = to_display_timezone(
            getattr(trade, "opened_at", None) or getattr(trade, "closed_at", None),
            timezone_name,
        )
        priority = max(
            (
                BEHAVIOR_PRIORITY[(badge["state"], badge["key"])]
                + int(round((signal.get("strength") or {}).get(badge["key"], 0.0) * 10))
            )
            for badge in badges
        )
        flagged_trades.append(
            {
                "symbol": format_trade_symbol(trade),
                "date_label": display_dt.strftime("%d %b %Y (%a)") if display_dt else "-",
                "time_label": display_dt.strftime("%H:%M") if display_dt else "",
                "pnl": resolve_net_pnl(trade),
                "behavior_badges": badges,
                "bundle_pubkey": getattr(trade, "bundle_pubkey", None),
                "trade_pubkey": (getattr(trade, "pubkey", None) or "").strip(),
                "priority": priority,
                "timestamp_sort": display_dt.timestamp() if display_dt else 0.0,
            }
        )

    rows = []
    for key in BEHAVIOR_ORDER:
        total_count = confirmed_counts[key] + possible_counts[key]
        rows.append(
            {
                "key": key,
                "label": BEHAVIOR_LABELS[key],
                "confirmed_count": confirmed_counts[key],
                "possible_count": possible_counts[key],
                "total_count": total_count,
                "share_pct": (total_count / closed_trade_count * 100.0) if closed_trade_count else 0.0,
            }
        )

    top_focus = None
    ranked_rows = sorted(
        rows,
        key=lambda row: (-row["total_count"], -confirmed_counts[row["key"]], BEHAVIOR_ORDER.index(row["key"])),
    )
    if ranked_rows and ranked_rows[0]["total_count"] > 0:
        top_focus = ranked_rows[0]

    flagged_trades = sorted(
        flagged_trades,
        key=lambda row: (-row["priority"], -row["timestamp_sort"]),
    )[:max_flagged_trades]

    return {
        "closed_trade_count": closed_trade_count,
        "confirmed_trade_count": confirmed_trade_count,
        "possible_trade_count": possible_trade_count,
        "review_trade_count": review_trade_count,
        "rows": rows,
        "top_focus": top_focus,
        "flagged_trades": flagged_trades,
        "has_behavior_signals": review_trade_count > 0,
    }
