"""Shared weekly review citation ref rewrite (T1/B1 → labels) for dashboard and chat context."""

from __future__ import annotations

import json
import re
from datetime import datetime

from trading import to_display_timezone


def deserialize_datetime_iso(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_json_blob(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def build_weekly_review_citation_lookup(payload_json, timezone_name):
    payload = parse_json_blob(payload_json) or {}
    trades = payload.get("trades") if isinstance(payload.get("trades"), list) else []
    lookup = {}

    for trade in trades:
        if not isinstance(trade, dict):
            continue
        review_ref = str(trade.get("ref") or trade.get("review_ref") or "").strip().upper()
        if not review_ref:
            continue

        symbol = str(trade.get("symbol") or "-").strip() or "-"
        date_label = str(trade.get("trade_date_label") or "").strip() or None
        if not date_label:
            opened_at = deserialize_datetime_iso(trade.get("opened_at"))
            opened_local = to_display_timezone(opened_at, timezone_name)
            date_label = opened_local.strftime("%d %b %Y (%a)") if opened_local is not None else None
        bundle_key = str(trade.get("bundle_pubkey") or "").strip()
        trade_pubkey = str(trade.get("trade_pubkey") or trade.get("pubkey") or "").strip()
        trade_id = trade.get("trade_id")
        try:
            normalized_trade_id = int(trade_id)
        except (TypeError, ValueError):
            normalized_trade_id = None
        try:
            pnl_value = float(trade.get("pnl"))
        except (TypeError, ValueError):
            pnl_value = None
        tone = "good" if pnl_value is not None and pnl_value > 0 else "bad" if pnl_value is not None and pnl_value < 0 else "neutral"

        if bool(trade.get("is_bundle")):
            label_suffix = f" | {date_label}" if date_label else ""
            lookup[review_ref] = {
                "ref": review_ref,
                "type": "bundle",
                "trade_id": normalized_trade_id,
                "trade_pubkey": trade_pubkey or None,
                "bundle_key": bundle_key or None,
                "inline_label": f"{symbol} bundle",
                "label": f"{symbol} bundle{label_suffix}",
                "tone": tone,
            }
            continue

        label_suffix = f" | {date_label}" if date_label else ""
        lookup[review_ref] = {
            "ref": review_ref,
            "type": "trade",
            "trade_id": normalized_trade_id,
            "trade_pubkey": trade_pubkey or None,
            "bundle_key": bundle_key or None,
            "inline_label": symbol,
            "label": f"{symbol}{label_suffix}" if label_suffix else symbol,
            "tone": tone,
        }

    return lookup


_STRAY_CLITIC_KEYWORD_LOOKAHEAD = (
    r"(?:trade|trades|loss|losses|win|wins|winner|losers?|idea|ideas|setup|setups|"
    r"entry|entries|exit|exits|position|positions|scalp|runner)\b"
)

_STRAY_CLITIC_AFTER_LABEL_SUFFIX = (
    rf"\s+[ds]\s+(?={_STRAY_CLITIC_KEYWORD_LOOKAHEAD})"
)

_GLUED_CLITIC_AFTER_LABEL = (
    rf"['\u2019']?[ds](?=\s*(?:{_STRAY_CLITIC_KEYWORD_LOOKAHEAD}))"
)


def _strip_stray_possessive_after_review_labels(text, citation_lookup):
    normalized = str(text or "")
    if not normalized or not citation_lookup:
        return normalized

    seen = set()
    for citation in citation_lookup.values():
        if not isinstance(citation, dict):
            continue
        for raw in (
            str(citation.get("label") or "").strip(),
            str(citation.get("inline_label") or "").strip(),
        ):
            if not raw:
                continue
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)

            normalized = re.sub(
                rf"(?i){re.escape(raw)}{_STRAY_CLITIC_AFTER_LABEL_SUFFIX}",
                f"{raw} ",
                normalized,
            )
            normalized = re.sub(
                rf"(?i){re.escape(raw)}{_GLUED_CLITIC_AFTER_LABEL}",
                f"{raw} ",
                normalized,
            )
    return normalized


def unwrap_bracketed_citation_labels(text, citation_lookup):
    normalized = str(text or "")
    if not normalized or not citation_lookup:
        return normalized

    labels = []
    seen = set()
    for citation in citation_lookup.values():
        if not isinstance(citation, dict):
            continue
        for raw in (
            str(citation.get("label") or "").strip(),
            str(citation.get("inline_label") or "").strip(),
        ):
            if not raw:
                continue
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)
            labels.append(raw)

    for label in sorted(labels, key=len, reverse=True):
        normalized = re.sub(
            rf"\[\s*{re.escape(label)}\s*\]",
            label,
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def rewrite_review_text_refs(text, citation_lookup):
    normalized = str(text or "").strip()
    if not normalized or not citation_lookup:
        return normalized

    ref_codes = [ref for ref in citation_lookup.keys() if ref]
    if not ref_codes:
        return normalized

    ref_pattern = "|".join(re.escape(ref) for ref in sorted(ref_codes, key=len, reverse=True))
    bracket_close = (
        rf"\s*[\)\]\}}](?:['\u2019']?[ds](?=\s|[,.;:!?]|$))?"
    )
    normalized = re.sub(
        rf"\s*[\(\[\{{]\s*(?:{ref_pattern})(?:\s*,\s*(?:{ref_pattern}))*{bracket_close}",
        "",
        normalized,
        flags=re.IGNORECASE,
    )

    def replace_ref(match):
        ref = str(match.group(0) or "").strip().upper()
        citation = citation_lookup.get(ref)
        if citation is None:
            return ""
        return str(citation.get("inline_label") or citation.get("label") or "").strip()

    normalized = re.sub(
        rf"\b(?:{ref_pattern})\b",
        replace_ref,
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = _strip_stray_possessive_after_review_labels(normalized, citation_lookup)
    normalized = re.sub(r"\s{2,}", " ", normalized)
    normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
    return normalized.strip()
