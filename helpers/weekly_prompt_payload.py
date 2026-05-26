"""Compressed model-facing weekly review payload.

The full audit payload from ``build_trade_payload`` is stored unchanged in
``AIGeneratedResponse.payload_json``. This module builds a leaner structure
for pass-1 prompt input only.
"""

from __future__ import annotations

import json

_STRATEGY_DESCRIPTION_MAX_LEN = 1200
_MARKET_CONTEXT_ISSUES = frozenset(
    {
        "recurring_winner_exited_before_target",
        "single_trade_dominance",
        "losing_outlier_risk",
    }
)
_REENTRY_EXPERIMENT_KEYWORDS = (
    "post-loss",
    "post loss",
    "same symbol",
    "re-entry",
    "reentry",
    "re entry",
    "wait",
    "one trade per symbol",
    "repair",
    "retry",
)
_NARROW_INSTRUCTION = (
    "Do not dismiss the week as low value. Review the available trades directly. "
    "Avoid broad recurring-pattern claims unless supported by prior weeks."
)
_EVIDENCE_NARROW_INSTRUCTION = (
    "Make a bounded coaching read from the available trades. "
    "Do not make a strong personality claim or repeated-pattern claim."
)


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


def _outcome(trade):
    pnl = _safe_float(trade.get("pnl"))
    if pnl is None:
        return None
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "breakeven"


def _closed_trades(trades):
    return [t for t in (trades or []) if t.get("closed_at")]


def _strategy_dedupe_key(trade):
    version_id = trade.get("trade_profile_version_id") or trade.get("strategy_version_id")
    if version_id not in (None, ""):
        return ("id", int(version_id))
    return (
        "tuple",
        trade.get("strategy_name"),
        trade.get("strategy_version"),
        trade.get("strategy_description"),
    )


def _build_strategy_context(trades):
    strategies_used = []
    ref_by_key = {}
    for trade in trades:
        key = _strategy_dedupe_key(trade)
        if key in ref_by_key:
            continue
        if not any(
            [
                trade.get("strategy_name"),
                trade.get("strategy_version") is not None,
                trade.get("strategy_description"),
            ]
        ):
            continue
        strategy_ref = f"S{len(strategies_used) + 1}"
        ref_by_key[key] = strategy_ref
        description = (trade.get("strategy_description") or "").strip()
        entry = {
            "strategy_ref": strategy_ref,
            "name": trade.get("strategy_name"),
            "version": trade.get("strategy_version"),
            "description": description or None,
        }
        if len(description) > _STRATEGY_DESCRIPTION_MAX_LEN:
            entry["description"] = description[:_STRATEGY_DESCRIPTION_MAX_LEN].rstrip() + "..."
            entry["description_truncated"] = True
        strategies_used.append(entry)

    trade_strategy_refs = {}
    for trade in trades:
        key = _strategy_dedupe_key(trade)
        trade_strategy_refs[id(trade)] = ref_by_key.get(key)
    return strategies_used, trade_strategy_refs


def _trade_ref(trade):
    return trade.get("review_ref")


def _trade_by_ref(trades):
    return {_trade_ref(t): t for t in trades if _trade_ref(t)}


def _size_change_label(trade):
    size_vs = trade.get("size_vs_prev_symbol_trade") or trade.get("size_vs_prev_trade")
    if size_vs == "larger":
        return "increased"
    if size_vs == "smaller":
        return "decreased"
    if size_vs == "same":
        return "same"
    return None


def _build_post_loss_context(trade, trades_by_ref):
    if not (
        trade.get("is_post_loss_same_symbol_trade")
        or (
            trade.get("same_symbol_reentry")
            and (_safe_float(trade.get("prev_symbol_trade_pnl")) or 0) < 0
        )
    ):
        return None
    minutes = trade.get("minutes_since_prev_symbol_close") or trade.get("minutes_since_prev_close")
    prev_pnl = trade.get("prev_symbol_trade_pnl") if trade.get("prev_symbol_trade_pnl") is not None else trade.get("prev_trade_pnl")
    loss_ref = None
    for candidate in trades_by_ref.values():
        if candidate is trade:
            continue
        if candidate.get("symbol") != trade.get("symbol"):
            continue
        if _safe_float(candidate.get("pnl")) == _safe_float(prev_pnl):
            loss_ref = _trade_ref(candidate)
            break
    size_change = _size_change_label(trade)
    context = {
        "minutes_after_loss": _round(minutes),
        "same_symbol": True,
        "same_size": size_change == "same",
        "size_change": size_change,
        "previous_loss_pnl": _round(prev_pnl),
    }
    if trade.get("same_trade_idea_reentry"):
        context["same_side"] = True
    if loss_ref:
        context["previous_loss_ref"] = loss_ref
    return context


def _compress_market_context(trade, *, include_summary=True):
    market_context = trade.get("market_context") or {}
    if not market_context or not include_summary:
        return None
    summary = {
        "bars_status": market_context.get("bars_status"),
        "entry_bar_aligned": market_context.get("entry_bar_closes_in_trade_direction"),
        "mfe_r": _round(market_context.get("mfe_r")),
        "mae_r": _round(market_context.get("mae_r")),
        "post_exit_direction": market_context.get("post_exit_direction"),
        "post_exit_tp_reached": market_context.get("post_exit_tp_reached"),
    }
    if not any(value is not None for value in summary.values()):
        return None
    return summary


def _market_context_relevant(primary_issue):
    if not primary_issue:
        return False
    if primary_issue in _MARKET_CONTEXT_ISSUES:
        return True
    return any(
        token in (primary_issue or "")
        for token in ("exit", "target", "stop", "entry", "tp", "capture")
    )


def _build_high_signal_trade(trade, *, strategy_ref, include_market_summary, trades_by_ref):
    ref = _trade_ref(trade)
    entry = {
        "ref": ref,
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "outcome": _outcome(trade),
        "pnl": _round(trade.get("pnl")),
        "realized_rr": _round(trade.get("realized_rr")),
        "duration_minutes": _round(trade.get("duration_minutes")),
        "entry_session": trade.get("entry_session") or trade.get("session"),
        "exit_session": trade.get("exit_session") or trade.get("session"),
        "is_bundle": bool(trade.get("is_bundle")),
    }
    if strategy_ref:
        entry["strategy_ref"] = strategy_ref
    if trade.get("planned_rr") is not None:
        entry["planned_rr"] = _round(trade.get("planned_rr"))
    if trade.get("is_bundle"):
        bundle_count = trade.get("bundle_trade_count")
        if bundle_count:
            entry["bundle_trade_count"] = int(bundle_count)
            entry["bundle_summary"] = f"{bundle_count} orders merged into one trade idea"
    if trade.get("loss_streak_before_trade") is not None:
        entry["loss_streak_before_trade"] = trade.get("loss_streak_before_trade")
    if trade.get("same_symbol_reentry"):
        entry["same_symbol_reentry"] = True
    if trade.get("same_trade_idea_reentry"):
        entry["same_trade_idea_reentry"] = True
    if trade.get("size_vs_prev_trade"):
        entry["size_vs_prev_trade"] = trade.get("size_vs_prev_trade")
    if trade.get("size_vs_prev_symbol_trade"):
        entry["size_vs_prev_symbol_trade"] = trade.get("size_vs_prev_symbol_trade")
    if trade.get("trade_note"):
        entry["trade_note"] = trade.get("trade_note")
    post_loss_context = _build_post_loss_context(trade, trades_by_ref)
    if post_loss_context:
        entry["same_symbol_after_loss"] = True
        entry["post_loss_context"] = post_loss_context
    market_summary = _compress_market_context(trade, include_summary=include_market_summary)
    if market_summary:
        entry["market_context_summary"] = market_summary
    return entry


def _build_primary_sequences(trades, post_loss_response):
    sequences = []
    trades_by_ref = _trade_by_ref(trades)
    seen = set()

    def add_sequence(*, loss_ref, next_ref, loss_trade, next_trade, minutes_after_loss):
        key = (loss_ref, next_ref)
        if key in seen:
            return
        seen.add(key)
        size_change = _size_change_label(next_trade)
        seq = {
            "type": "same_symbol_after_loss",
            "loss_ref": loss_ref,
            "next_ref": next_ref,
            "symbol": next_trade.get("symbol"),
            "side": next_trade.get("side"),
            "minutes_after_loss": _round(minutes_after_loss),
            "same_size": size_change == "same",
            "size_change": size_change,
            "loss_pnl": _round(loss_trade.get("pnl")),
            "next_pnl": _round(next_trade.get("pnl")),
            "next_outcome": _outcome(next_trade),
        }
        if next_trade.get("same_trade_idea_reentry"):
            seq["same_side"] = True
        if _outcome(next_trade) == "win":
            seq["sequence_outcome"] = "winning_retry"
        sequences.append(seq)

    for trade in trades:
        if not (
            trade.get("is_post_loss_same_symbol_trade")
            or (
                trade.get("same_symbol_reentry")
                and (_safe_float(trade.get("prev_symbol_trade_pnl")) or 0) < 0
            )
        ):
            continue
        next_ref = _trade_ref(trade)
        minutes = trade.get("minutes_since_prev_symbol_close") or trade.get("minutes_since_prev_close")
        loss_ref = None
        loss_trade = None
        for seq in (post_loss_response or {}).get("sequences") or []:
            if seq.get("next_ref") == next_ref:
                loss_ref = seq.get("loss_ref")
                loss_trade = trades_by_ref.get(loss_ref)
                break
        if loss_trade is None:
            prev_pnl = trade.get("prev_symbol_trade_pnl")
            for candidate in trades:
                if candidate is trade:
                    continue
                if candidate.get("symbol") != trade.get("symbol"):
                    continue
                if _safe_float(candidate.get("pnl")) == _safe_float(prev_pnl):
                    loss_trade = candidate
                    loss_ref = _trade_ref(candidate)
                    break
        if loss_trade is None or not loss_ref:
            continue
        add_sequence(
            loss_ref=loss_ref,
            next_ref=next_ref,
            loss_trade=loss_trade,
            next_trade=trade,
            minutes_after_loss=minutes,
        )

    for seq in (post_loss_response or {}).get("sequences") or []:
        loss_ref = seq.get("loss_ref")
        next_ref = seq.get("next_ref")
        loss_trade = trades_by_ref.get(loss_ref)
        next_trade = trades_by_ref.get(next_ref)
        if not loss_trade or not next_trade:
            continue
        if loss_trade.get("symbol") != next_trade.get("symbol"):
            continue
        if not (
            next_trade.get("is_post_loss_same_symbol_trade")
            or next_trade.get("same_symbol_reentry")
        ):
            continue
        minutes = next_trade.get("minutes_since_prev_symbol_close") or next_trade.get(
            "minutes_since_prev_close"
        )
        add_sequence(
            loss_ref=loss_ref,
            next_ref=next_ref,
            loss_trade=loss_trade,
            next_trade=next_trade,
            minutes_after_loss=minutes,
        )
    return sequences


def _confirmed_revenge_count(summary):
    return int((summary or {}).get("confirmed_revenge_trade_count") or 0)


def _build_coaching_frame_triggers(
    *,
    trades,
    primary_sequences,
    summary,
    execution_outcome,
    risk_authority,
    single_trade_dominance,
    revenge_evidence,
):
    triggers = []
    trades_by_ref = _trade_by_ref(trades)
    confirmed_count = _confirmed_revenge_count(summary)

    for seq in primary_sequences:
        if seq.get("sequence_outcome") != "winning_retry":
            continue
        next_trade = trades_by_ref.get(seq.get("next_ref"))
        if not next_trade:
            continue
        if next_trade.get("is_revenge"):
            continue
        size_change = seq.get("size_change")
        if size_change == "increased":
            continue
        triggers.append(
            {
                "frame": "reward_cost_mislesson",
                "trigger": "winning_same_symbol_retry_after_loss",
                "refs": [seq.get("loss_ref"), seq.get("next_ref")],
                "instruction": (
                    "Discuss how the winning retry may reward a risky habit without calling it confirmed revenge."
                ),
                "suggested_lesson": "The win should not become proof that post-loss retries are safe.",
            }
        )
        break

    reentry_trades = [
        t
        for t in trades
        if t.get("is_post_loss_same_symbol_trade")
        or t.get("same_trade_idea_reentry")
        or t.get("same_symbol_reentry")
    ]
    if len(reentry_trades) >= 2:
        size_changes = [_size_change_label(t) for t in reentry_trades]
        net_reentry = sum(_safe_float(t.get("pnl")) or 0.0 for t in reentry_trades)
        stable_size = all(change in (None, "same") for change in size_changes)
        if stable_size and net_reentry <= 0 and confirmed_count == 0:
            triggers.append(
                {
                    "frame": "calm_decision_leak",
                    "trigger": "controlled_repetition_after_loss",
                    "refs": [_trade_ref(t) for t in reentry_trades[:3] if _trade_ref(t)],
                    "instruction": (
                        "This was not panic. It was controlled repetition. Coach the habit without calling it revenge."
                    ),
                }
            )

    net_pnl = _safe_float((summary or {}).get("net_pnl"))
    closed_count = int((summary or {}).get("closed_trades") or len(_closed_trades(trades)))
    largest_share = _safe_float((summary or {}).get("largest_trade_abs_pnl_share_pct"))
    if net_pnl is not None and net_pnl > 0 and closed_count >= 3:
        masked = False
        if single_trade_dominance:
            masked = True
        elif largest_share is not None and largest_share >= 60:
            masked = True
        else:
            winners = sorted(
                (_safe_float(t.get("pnl")) or 0.0 for t in trades),
                reverse=True,
            )
            if winners and (net_pnl - winners[0]) < 0:
                masked = True
        if masked:
            dominant_ref = (single_trade_dominance or {}).get("dominant_ref")
            triggers.append(
                {
                    "frame": "single_trade_masked_week",
                    "trigger": "one_winner_changed_week_result",
                    "refs": [dominant_ref] if dominant_ref else [],
                    "instruction": (
                        "One trade changed the result; review the rest of the distribution separately."
                    ),
                }
            )

    if (execution_outcome or {}).get("coaching_stance") == "good_week_but_habits_are_leaking":
        primary_issue = (execution_outcome or {}).get("primary_issue")
        if primary_issue and not any(item.get("frame") == "reward_cost_mislesson" for item in triggers):
            refs = []
            for seq in primary_sequences[:1]:
                refs.extend([seq.get("loss_ref"), seq.get("next_ref")])
            triggers.append(
                {
                    "frame": "acknowledge_profit_focus_leak",
                    "trigger": primary_issue,
                    "refs": [ref for ref in refs if ref],
                    "instruction": "Acknowledge profit, but do not let outcome validate the leak.",
                }
            )

    revenge_class = (revenge_evidence or {}).get("pattern_class")
    if revenge_class == "isolated" and confirmed_count == 0:
        pass

    return triggers


def _build_constraints(
    *,
    closed_trades,
    summary,
    execution_outcome,
    risk_authority,
    weekly_checkin,
    notes_confidence,
):
    do_not_claim = []
    do_not_focus_on = [
        "Do not make raw PnL the main story.",
        "Do not over-focus on entry/exit prices.",
    ]
    confirmed_count = _confirmed_revenge_count(summary)
    if confirmed_count == 0:
        do_not_claim.append("Do not call this confirmed revenge.")
    risk_authority = risk_authority or {}
    if risk_authority.get("stable") is True or _safe_float(risk_authority.get("dispersion_pct")) == 0:
        do_not_claim.append("Do not claim size escalation.")
    if closed_trades < 3:
        do_not_claim.extend(
            [
                "Do not call one sequence a repeated pattern.",
                "Do not overstate broad improvement from a 1-2 trade sample.",
                "Do not dismiss the week as low-value or low-confidence.",
            ]
        )
    coaching_stance = (execution_outcome or {}).get("coaching_stance")
    if coaching_stance == "good_week_but_habits_are_leaking":
        do_not_claim.append("Do not let a profitable week validate the process leak.")
    checkin_present = bool((weekly_checkin or {}).get("emotional_state"))
    if not checkin_present and (notes_confidence or "").lower() in {"low", ""}:
        do_not_claim.append("Do not make claims about emotional intent.")
    return {"do_not_claim": do_not_claim, "do_not_focus_on": do_not_focus_on}


def _human_reasons(full_payload, closed_trades):
    reasons = []
    if closed_trades < 3:
        reasons.append(f"only {closed_trades} closed trade{'s' if closed_trades != 1 else ''}")
    notes_confidence = (full_payload.get("notes_confidence") or "").lower()
    if notes_confidence == "low":
        reasons.append("low notes coverage")
    elif notes_confidence == "medium":
        reasons.append("partial notes coverage")
    if not (full_payload.get("weekly_checkin") or {}).get("emotional_state"):
        reasons.append("no weekly check-in")
    if not reasons:
        envelope = full_payload.get("confidence_envelope") or {}
        for reason in envelope.get("reasons") or []:
            if reason == "account_age_days_below_30":
                reasons.append("account under 30 days old")
            elif reason == "closed_trade_count_below_3":
                reasons.append("few closed trades")
            elif reason == "notes_confidence_low":
                reasons.append("low notes coverage")
    return reasons


def _build_relevant_continuity(full_payload, primary_issue, primary_sequences):
    recent_experiments = full_payload.get("recent_experiments") or []
    issue_text = " ".join(
        [
            primary_issue or "",
            " ".join(seq.get("type") or "" for seq in primary_sequences),
        ]
    ).lower()
    if not any(token in issue_text for token in ("loss", "reentry", "re-entry", "same_symbol", "retry", "symbol")):
        if not primary_sequences:
            return {}

    matched_rules = []
    for item in recent_experiments:
        text = (item.get("text") or "").lower()
        if any(keyword in text for keyword in _REENTRY_EXPERIMENT_KEYWORDS):
            matched_rules.append(item.get("text"))

    if not matched_rules:
        return {}

    relevance = "This week included a same-symbol retry after a loss."
    if primary_sequences:
        seq = primary_sequences[0]
        relevance = (
            f"This week included a {seq.get('symbol') or 'same-symbol'} sequence after a loss "
            f"({seq.get('loss_ref')} then {seq.get('next_ref')})."
        )
    return {
        "recent_experiment_theme": "post-loss re-entry control",
        "prior_rules": matched_rules[:3],
        "relevance": relevance,
    }


def _build_conditional_context(full_payload):
    context = {}
    user_profile = full_payload.get("user_profile") or {}
    if any(user_profile.get(key) for key in ("trading_style", "instruments", "experience_level")):
        context["user_profile"] = {
            key: user_profile.get(key)
            for key in ("trading_style", "instruments", "experience_level")
            if user_profile.get(key)
        }
    weekly_checkin = full_payload.get("weekly_checkin") or {}
    if any(
        weekly_checkin.get(key)
        for key in ("emotional_state", "plan_adherence", "execution_quality", "additional_context")
    ):
        context["weekly_checkin"] = {
            key: weekly_checkin.get(key)
            for key in (
                "emotional_state",
                "plan_adherence",
                "execution_quality",
                "additional_context",
            )
            if weekly_checkin.get(key)
        }
    tone_context = full_payload.get("tone_context") or {}
    if tone_context:
        context["tone_context"] = {
            key: tone_context.get(key)
            for key in ("mode", "reasons", "checkin_submitted")
            if tone_context.get(key) is not None
        }
    experiment_context = full_payload.get("experiment_context") or {}
    if experiment_context:
        context["experiment_context"] = experiment_context
    emotional_index = full_payload.get("emotional_index") or {}
    if emotional_index.get("label"):
        context["emotional_index"] = {
            "label": emotional_index.get("label"),
            "self_report_mismatch": emotional_index.get("self_report_mismatch"),
        }
    performance_trends = full_payload.get("performance_trends") or {}
    if performance_trends.get("ei_trend"):
        context["performance_trends"] = {"ei_trend": performance_trends.get("ei_trend")}
    return context


def build_weekly_prompt_payload(full_payload: dict) -> dict:
    trades = list(full_payload.get("trades") or [])
    closed = _closed_trades(trades)
    closed_trades = int((full_payload.get("summary") or {}).get("closed_trades") or len(closed))
    trade_idea_count = sum(1 for trade in trades if trade.get("closed_at")) or closed_trades
    breakdowns = full_payload.get("current_week_breakdowns") or {}
    execution_outcome = breakdowns.get("execution_outcome") or {}
    risk_authority = breakdowns.get("risk_authority") or {}
    post_loss_response = breakdowns.get("post_loss_response") or {}
    single_trade_dominance = breakdowns.get("single_trade_dominance")
    revenge_evidence = breakdowns.get("revenge_evidence") or {}
    summary = full_payload.get("summary") or {}
    primary_issue = execution_outcome.get("primary_issue")

    if closed_trades < 3:
        review_mode = "specific_trade_sequence"
        claim_scope = "trade_specific_not_pattern_level"
        sample_instruction = _NARROW_INSTRUCTION
    else:
        review_mode = "weekly_pattern_review"
        claim_scope = "weekly_pattern_level_allowed"
        sample_instruction = (
            "Review the week for repeatable patterns when the evidence supports them."
        )

    evidence_claim_scope = "narrow" if closed_trades < 3 else "moderate"
    if (full_payload.get("confidence_envelope") or {}).get("level") == "strong" and closed_trades >= 5:
        evidence_claim_scope = "broad"

    strategies_used, strategy_ref_map = _build_strategy_context(trades)
    trades_by_ref = _trade_by_ref(trades)
    high_signal_trades = []
    for trade in closed or trades:
        high_signal_trades.append(
            _build_high_signal_trade(
                trade,
                strategy_ref=strategy_ref_map.get(id(trade)),
                include_market_summary=True,
                trades_by_ref=trades_by_ref,
            )
        )

    primary_sequences = _build_primary_sequences(trades, post_loss_response)
    coaching_frame_triggers = _build_coaching_frame_triggers(
        trades=trades,
        primary_sequences=primary_sequences,
        summary=summary,
        execution_outcome=execution_outcome,
        risk_authority=risk_authority,
        single_trade_dominance=single_trade_dominance,
        revenge_evidence=revenge_evidence,
    )
    constraints = _build_constraints(
        closed_trades=closed_trades,
        summary=summary,
        execution_outcome=execution_outcome,
        risk_authority=risk_authority,
        weekly_checkin=full_payload.get("weekly_checkin"),
        notes_confidence=full_payload.get("notes_confidence"),
    )
    relevant_continuity = _build_relevant_continuity(
        full_payload,
        primary_issue,
        primary_sequences,
    )
    conditional_context = _build_conditional_context(full_payload)

    week_summary = {
        "net_pnl": _round(summary.get("net_pnl")),
        "win_rate": _round(summary.get("win_rate")),
        "outcome_class": execution_outcome.get("outcome_class"),
        "execution_class": execution_outcome.get("execution_class"),
        "week_archetype": execution_outcome.get("week_archetype"),
        "coaching_stance": execution_outcome.get("coaching_stance"),
        "primary_issue": primary_issue,
        "primary_issue_evidence_level": execution_outcome.get("issue_evidence_level"),
    }

    payload = {
        "review_scope": {
            "period_label": "current review week",
            "closed_trades": closed_trades,
            "trade_idea_count": trade_idea_count,
        },
        "sample_context": {
            "trade_count": closed_trades,
            "review_mode": review_mode,
            "claim_scope": claim_scope,
            "instruction": sample_instruction,
        },
        "evidence_boundary": {
            "claim_scope": evidence_claim_scope,
            "reasons": _human_reasons(full_payload, closed_trades),
            "instruction": _EVIDENCE_NARROW_INSTRUCTION if closed_trades < 3 else (
                "Stay within what the week's evidence supports."
            ),
        },
        "week_summary": week_summary,
        "strategy_context": {"strategies_used": strategies_used},
        "primary_sequences": primary_sequences,
        "high_signal_trades": high_signal_trades,
        "coaching_frame_triggers": coaching_frame_triggers,
        "constraints": constraints,
        "relevant_continuity": relevant_continuity,
        "conditional_context": conditional_context,
    }
    return payload


def _format_json_block(value):
    return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)


def format_weekly_prompt_payload(prompt_payload: dict) -> str:
    lines = [
        "WEEKLY REVIEW DATA (compressed)",
        "Use this structure for the review. Strategy descriptions appear once in strategy_context.",
        "Trade refs (T1, B1) are citation anchors only — never mention them in prose.",
        "",
        "REVIEW_SCOPE",
        _format_json_block(prompt_payload.get("review_scope") or {}),
        "",
        "SAMPLE_CONTEXT",
        _format_json_block(prompt_payload.get("sample_context") or {}),
        "",
        "EVIDENCE_BOUNDARY",
        _format_json_block(prompt_payload.get("evidence_boundary") or {}),
        "",
        "WEEK_SUMMARY",
        _format_json_block(prompt_payload.get("week_summary") or {}),
    ]

    strategies = (prompt_payload.get("strategy_context") or {}).get("strategies_used") or []
    if strategies:
        lines.extend(["", "STRATEGY_CONTEXT", _format_json_block({"strategies_used": strategies})])

    sequences = prompt_payload.get("primary_sequences") or []
    if sequences:
        lines.extend(["", "PRIMARY_SEQUENCES", _format_json_block(sequences)])

    triggers = prompt_payload.get("coaching_frame_triggers") or []
    if triggers:
        lines.extend(["", "COACHING_FRAME_TRIGGERS", _format_json_block(triggers)])

    constraints = prompt_payload.get("constraints") or {}
    if constraints:
        lines.extend(["", "CONSTRAINTS", _format_json_block(constraints)])

    continuity = prompt_payload.get("relevant_continuity") or {}
    if continuity:
        lines.extend(["", "RELEVANT_CONTINUITY", _format_json_block(continuity)])

    conditional = prompt_payload.get("conditional_context") or {}
    if conditional:
        lines.extend(["", "CONDITIONAL_CONTEXT", _format_json_block(conditional)])

    trades = prompt_payload.get("high_signal_trades") or []
    lines.extend(["", f"HIGH_SIGNAL_TRADES ({len(trades)})"])
    if trades:
        lines.append(_format_json_block(trades))
    else:
        lines.append("- no closed trades")

    return "\n".join(lines)
