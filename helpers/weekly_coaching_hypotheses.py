"""Bounded coaching hypotheses for weekly AI reviews.

These objects sit between deterministic signals and final review prose. They
name the human trap the data can support, but deliberately stop short of
writing the review or prescribing the final rule.
"""

from __future__ import annotations


def _safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _truthy(value):
    return bool(value)


def _closed_trades(serialized_trades):
    trades = [t for t in (serialized_trades or []) if t.get("closed_at")]
    return sorted(
        trades,
        key=lambda t: (
            t.get("trade_sequence_number") is None,
            t.get("trade_sequence_number") or 0,
            t.get("review_ref") or "",
        ),
    )


def _review_ref(trade):
    return str((trade or {}).get("review_ref") or "").strip() or None


def _unique_refs(refs, limit=None):
    out = []
    for ref in refs:
        if not ref or ref in out:
            continue
        out.append(ref)
        if limit is not None and len(out) >= limit:
            break
    return out


def _retry_timing_range_minutes(trades):
    values = [
        _safe_float(trade.get("minutes_since_prev_close"))
        for trade in (trades or [])
    ]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return [_round(min(values), 2), _round(max(values), 2)]


def _outcome(trade):
    pnl = _safe_float((trade or {}).get("pnl"))
    if pnl is None:
        return None
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "breakeven"


def _hypothesis(
    *,
    hypothesis_type,
    severity,
    confidence,
    evidence_refs,
    facts,
    human_trap_hint,
    false_lesson_hint,
    better_lesson_hint,
    extra_fields=None,
):
    out = {
        "type": hypothesis_type,
        "eligible": True,
        "rank": None,
        "severity": severity,
        "confidence": confidence,
        "evidence_refs": _unique_refs(evidence_refs, limit=4),
        "facts": facts,
        "human_trap_hint": human_trap_hint,
        "false_lesson_hint": false_lesson_hint,
        "better_lesson_hint": better_lesson_hint,
        "prompt_instruction": "Use this only if supported by the named refs. Use it as framing, not as a script.",
    }
    if extra_fields:
        conflicts = set(extra_fields).intersection(out)
        if conflicts:
            raise ValueError(f"extra_fields conflict: {sorted(conflicts)}")
        out.update(extra_fields)
    return out


def _reactionary_retry_trades(closed):
    return [
        trade
        for trade in closed
        if _truthy(trade.get("is_post_loss_same_symbol_trade"))
        or _truthy(trade.get("same_trade_idea_reentry"))
        or _truthy(trade.get("is_revenge"))
        or _truthy(trade.get("is_potential_revenge"))
    ]


def _build_outcome_disguised_habit(
    *,
    closed,
    execution_outcome,
    revenge_evidence,
):
    retry_trades = _reactionary_retry_trades(closed)
    winning_retries = [trade for trade in retry_trades if _outcome(trade) == "win"]
    losing_retries = [trade for trade in retry_trades if _outcome(trade) == "loss"]
    if not winning_retries or not losing_retries:
        return None

    issue_reasons = set((execution_outcome or {}).get("issue_reasons") or [])
    supported_by_issue = bool(
        issue_reasons.intersection(
            {
                "repeated_revenge_evidence",
                "repeated_same_symbol_after_loss",
                "repeated_same_trade_idea_reentry",
                "single_same_symbol_after_loss",
                "single_same_trade_idea_reentry",
                "isolated_revenge_evidence",
            }
        )
    )
    pattern_class = (revenge_evidence or {}).get("pattern_class")
    if not supported_by_issue and pattern_class not in {"isolated", "repeated"}:
        return None

    confidence = (
        "high"
        if pattern_class == "repeated"
        and len(retry_trades) >= 2
        else "moderate"
    )
    rewarded_trade = max(
        winning_retries,
        key=lambda trade: _safe_float(trade.get("pnl")) or 0.0,
    )
    exposed_trade = min(
        losing_retries,
        key=lambda trade: _safe_float(trade.get("pnl")) or 0.0,
    )
    rewarded_ref = _review_ref(rewarded_trade)
    exposed_ref = _review_ref(exposed_trade)
    evidence_refs = _unique_refs(
        [rewarded_ref, exposed_ref],
        limit=3,
    )
    return _hypothesis(
        hypothesis_type="outcome_disguised_habit",
        severity=95,
        confidence=confidence,
        evidence_refs=evidence_refs,
        facts={
            "winning_retry_refs": _unique_refs(_review_ref(t) for t in winning_retries),
            "failed_retry_refs": _unique_refs(_review_ref(t) for t in losing_retries),
            "retry_timing_range_minutes": _retry_timing_range_minutes(retry_trades),
            "post_loss_retry_count": len(retry_trades),
            "same_symbol_after_loss_count": (execution_outcome or {}).get(
                "same_symbol_after_loss_count", 0
            ),
            "same_trade_idea_reentry_count": (execution_outcome or {}).get(
                "same_trade_idea_reentry_count", 0
            ),
        },
        human_trap_hint="A winning retry can make the same post-loss habit feel justified.",
        false_lesson_hint="A winning retry may make the post-loss behavior look safe.",
        better_lesson_hint="Judge the post-loss decision separately from whether that one trade won.",
        extra_fields={
            "habit_rewarded_by_ref": rewarded_ref,
            "habit_rewarded_by_symbol": rewarded_trade.get("symbol"),
            "habit_exposed_by_ref": exposed_ref,
            "habit_exposed_by_symbol": exposed_trade.get("symbol"),
            "mechanism_hint": "The winning retry reinforced the same post-loss behavior that later caused damage.",
            "what_the_trader_may_have_mislearned": "Because the retry won, the trader may treat the retry habit as valid.",
            "contrast_instruction": "Contrast the trade that rewarded the habit with the trade that exposed it.",
        },
    )


def _build_post_loss_decision_shift(*, closed, execution_outcome, post_loss_response):
    sequences = list((post_loss_response or {}).get("sequences") or [])
    if len(sequences) < 2:
        return None

    issue_reasons = set((execution_outcome or {}).get("issue_reasons") or [])
    if not issue_reasons.intersection(
        {
            "repeated_revenge_evidence",
            "repeated_same_symbol_after_loss",
            "repeated_same_trade_idea_reentry",
            "repeated_increased_risk_after_loss",
            "isolated_revenge_evidence",
        }
    ):
        return None

    next_refs = _unique_refs(seq.get("next_ref") for seq in sequences)
    next_trades = [trade for trade in closed if _review_ref(trade) in next_refs]
    confidence = "high" if len(sequences) >= 3 else "moderate"
    return _hypothesis(
        hypothesis_type="post_loss_decision_shift",
        severity=85,
        confidence=confidence,
        evidence_refs=next_refs[:4],
        facts={
            "post_loss_sequence_count": len(sequences),
            "next_trade_refs": next_refs,
            "losing_followup_count": sum(
                1 for seq in sequences if seq.get("next_outcome") == "loss"
            ),
            "winning_followup_count": sum(
                1 for seq in sequences if seq.get("next_outcome") == "win"
            ),
            "retry_timing_range_minutes": _retry_timing_range_minutes(next_trades),
            "biggest_loss_ref": ((post_loss_response or {}).get("biggest_loss") or {}).get(
                "loss_ref"
            ),
            "biggest_loss_followup_ref": (
                (post_loss_response or {}).get("biggest_loss") or {}
            ).get("next_ref"),
        },
        human_trap_hint="The first loss may not be the main failure point; the next decision is where the week can change shape.",
        false_lesson_hint="The strategy may look broken when the bigger issue is the decision immediately after being wrong.",
        better_lesson_hint="Review the post-loss decision as its own setup, not as a repair of the previous trade.",
    )


def _build_single_trade_masked_week(*, closed, summary, single_trade_dominance, execution_outcome):
    if not single_trade_dominance:
        return None
    dominant_ref = single_trade_dominance.get("dominant_ref")
    dominant = next((trade for trade in closed if _review_ref(trade) == dominant_ref), None)
    used_fallback_dominant = False
    if dominant is None:
        dominant = max(closed, key=lambda trade: abs(_safe_float(trade.get("pnl")) or 0), default=None)
        dominant_ref = _review_ref(dominant)
        used_fallback_dominant = True
    if dominant is None:
        return None

    rest = [trade for trade in closed if _review_ref(trade) != dominant_ref]
    rest_net = sum(_safe_float(trade.get("pnl")) or 0.0 for trade in rest)
    rest_wins = sum(1 for trade in rest if (_safe_float(trade.get("pnl")) or 0) > 0)
    rest_win_rate = (rest_wins / len(rest) * 100.0) if rest else None
    total_net = _safe_float((summary or {}).get("net_pnl"))
    dominant_pnl = _safe_float(dominant.get("pnl"))
    sign_flip = total_net is not None and rest_net is not None and (
        (total_net >= 0 and rest_net < 0) or (total_net <= 0 and rest_net > 0)
    )
    do_not_lead = set((execution_outcome or {}).get("do_not_lead_with") or [])
    severity = 35 if "single_trade_dominance" in do_not_lead else 70
    return _hypothesis(
        hypothesis_type="single_trade_masked_week",
        severity=severity,
        confidence="high" if sign_flip else "moderate",
        evidence_refs=[dominant_ref],
        facts={
            "dominant_ref": dominant_ref,
            "dominant_symbol": dominant.get("symbol")
            if used_fallback_dominant
            else single_trade_dominance.get("dominant_symbol") or dominant.get("symbol"),
            "dominant_trade_pnl": _round(dominant_pnl),
            "dominant_abs_pnl_share_pct": single_trade_dominance.get("abs_pnl_share_pct"),
            "total_net_pnl": _round(total_net),
            "rest_of_week_pnl": _round(rest_net),
            "rest_of_week_trade_count": len(rest),
            "rest_of_week_win_rate": _round(rest_win_rate),
            "rest_of_week_flips_result": sign_flip,
        },
        human_trap_hint="One standout trade can make the week look cleaner than the rest of the decisions were.",
        false_lesson_hint="The headline result may make the whole week look better than the rest of the trades show.",
        better_lesson_hint="Separate the dominant trade from the rest of the week before judging process quality.",
    )


def _session_rows_from_breakdowns(current_week_breakdowns):
    rows = []
    for item in (current_week_breakdowns or {}).get("sessions") or []:
        rows.append(
            {
                "name": item.get("name"),
                "count": int(item.get("count") or 0),
                "win_rate": _safe_float(item.get("win_rate")),
                "net_pnl": _safe_float(item.get("net_pnl")),
            }
        )
    return [row for row in rows if row["name"] and row["count"] > 0 and row["net_pnl"] is not None]


def _largest_abs_ref_for_session(closed, session_name):
    session_trades = [trade for trade in closed if trade.get("session") == session_name]
    trade = max(
        session_trades,
        key=lambda t: abs(_safe_float(t.get("pnl")) or 0),
        default=None,
    )
    return _review_ref(trade)


def _build_session_edge_disguised_as_skill(*, closed, current_week_breakdowns):
    sessions = _session_rows_from_breakdowns(current_week_breakdowns)
    if len(sessions) < 2:
        return None
    positive_sessions = [row for row in sessions if (row.get("net_pnl") or 0) > 0]
    negative_sessions = [row for row in sessions if (row.get("net_pnl") or 0) < 0]
    if not positive_sessions or not negative_sessions:
        return None

    best = max(positive_sessions, key=lambda row: (row["net_pnl"], row["count"]))
    weakest = min(negative_sessions, key=lambda row: (row["net_pnl"], -row["count"]))
    if best["count"] < 2 or weakest["count"] < 2:
        return None

    confidence = "high" if best["count"] >= 5 and weakest["count"] >= 5 else "moderate"
    best_ref = _largest_abs_ref_for_session(closed, best["name"])
    weak_ref = _largest_abs_ref_for_session(closed, weakest["name"])
    return _hypothesis(
        hypothesis_type="session_edge_disguised_as_skill",
        severity=55,
        confidence=confidence,
        evidence_refs=[best_ref, weak_ref],
        facts={
            "best_session": best["name"],
            "best_session_trade_count": best["count"],
            "best_session_net_pnl": _round(best["net_pnl"]),
            "best_session_win_rate": _round(best["win_rate"]),
            "weak_session": weakest["name"],
            "weak_session_trade_count": weakest["count"],
            "weak_session_net_pnl": _round(weakest["net_pnl"]),
            "weak_session_win_rate": _round(weakest["win_rate"]),
        },
        human_trap_hint="A good session context can make execution look broadly stronger than it was.",
        false_lesson_hint="A profitable window may make the edge look portable across sessions.",
        better_lesson_hint="Separate session context from setup quality before assuming the same approach works everywhere.",
    )


def build_coaching_hypotheses(
    *,
    serialized_trades,
    summary,
    current_week_breakdowns,
    execution_outcome,
    revenge_evidence,
    post_loss_response,
    risk_authority,
    single_trade_dominance,
    tp_capture_shortfalls,
):
    """Return ranked, eligible coaching hypotheses for the prompt.

    `risk_authority` and `tp_capture_shortfalls` are accepted even when a
    starter hypothesis does not use them yet. Keeping the interface explicit
    makes later hypothesis additions less likely to bypass existing guards.
    """
    _ = risk_authority, tp_capture_shortfalls
    closed = _closed_trades(serialized_trades)
    candidates = [
        _build_outcome_disguised_habit(
            closed=closed,
            execution_outcome=execution_outcome,
            revenge_evidence=revenge_evidence,
        ),
        _build_post_loss_decision_shift(
            closed=closed,
            execution_outcome=execution_outcome,
            post_loss_response=post_loss_response,
        ),
        _build_single_trade_masked_week(
            closed=closed,
            summary=summary,
            single_trade_dominance=single_trade_dominance,
            execution_outcome=execution_outcome,
        ),
        _build_session_edge_disguised_as_skill(
            closed=closed,
            current_week_breakdowns=current_week_breakdowns,
        ),
    ]
    hypotheses = [candidate for candidate in candidates if candidate]
    hypotheses.sort(
        key=lambda item: (
            -int(item.get("severity", 0) or 0),
            str(item.get("type") or ""),
        )
    )
    for index, item in enumerate(hypotheses, start=1):
        item["rank"] = index
    return hypotheses
