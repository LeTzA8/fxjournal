"""User/system interpretation of trades (bundles, behavior flags) — separate from raw trade facts."""

from __future__ import annotations

from helpers.utils import utcnow_naive

MISSING = object()


def _normalize_bundle_pubkey(value):
    if value is None or value is MISSING:
        return None
    text = str(value).strip()
    return text or None


def interpretation_snapshot(trade):
    """Current bundle + flags for a trade ORM object (no row => all false / no bundle)."""
    interp = getattr(trade, "interpretation", None)
    if interp is None:
        return {
            "bundle_pubkey": None,
            "is_revenge": False,
            "is_reactive": False,
            "is_corrective": False,
        }
    return {
        "bundle_pubkey": _normalize_bundle_pubkey(interp.bundle_pubkey),
        "is_revenge": bool(interp.is_revenge),
        "is_reactive": bool(interp.is_reactive),
        "is_corrective": bool(interp.is_corrective),
    }


def trade_bundle_pubkey(trade):
    return interpretation_snapshot(trade)["bundle_pubkey"]


def trade_is_revenge(trade):
    return interpretation_snapshot(trade)["is_revenge"]


def trade_is_reactive(trade):
    return interpretation_snapshot(trade)["is_reactive"]


def trade_is_corrective(trade):
    return interpretation_snapshot(trade)["is_corrective"]


def apply_interpretation(
    trade,
    *,
    bundle_pubkey=MISSING,
    is_revenge=MISSING,
    is_reactive=MISSING,
    is_corrective=MISSING,
    source="unknown",
    user_id=None,
):
    """
    Update interpretation for a persisted trade; append history when values change.
    Returns True if state changed, False if no-op.
    """
    from models import TradeInterpretation, TradeInterpretationHistory, db

    if getattr(trade, "id", None) is None:
        raise ValueError("apply_interpretation requires a persisted trade with id")

    snap = interpretation_snapshot(trade)
    new_bundle = snap["bundle_pubkey"] if bundle_pubkey is MISSING else _normalize_bundle_pubkey(bundle_pubkey)
    new_rev = snap["is_revenge"] if is_revenge is MISSING else bool(is_revenge)
    new_rea = snap["is_reactive"] if is_reactive is MISSING else bool(is_reactive)
    new_cor = snap["is_corrective"] if is_corrective is MISSING else bool(is_corrective)

    if (
        new_bundle == snap["bundle_pubkey"]
        and new_rev == snap["is_revenge"]
        and new_rea == snap["is_reactive"]
        and new_cor == snap["is_corrective"]
    ):
        return False

    now = utcnow_naive()
    interp = getattr(trade, "interpretation", None)
    if interp is None:
        sess = db.session
        interp = sess.get(TradeInterpretation, trade.id)

    history = TradeInterpretationHistory(
        trade_id=trade.id,
        bundle_pubkey=new_bundle,
        is_revenge=new_rev,
        is_reactive=new_rea,
        is_corrective=new_cor,
        source=str(source or "unknown")[:32],
        user_id=user_id,
        created_at=now,
    )
    db.session.add(history)

    is_empty = not new_bundle and not new_rev and not new_rea and not new_cor
    if is_empty:
        if interp is not None:
            db.session.delete(interp)
        return True

    if interp is None:
        interp = TradeInterpretation(
            trade_id=trade.id,
            bundle_pubkey=new_bundle,
            is_revenge=new_rev,
            is_reactive=new_rea,
            is_corrective=new_cor,
            updated_at=now,
        )
        db.session.add(interp)
        if hasattr(trade, "__mapper__"):
            trade.interpretation = interp
    else:
        interp.bundle_pubkey = new_bundle
        interp.is_revenge = new_rev
        interp.is_reactive = new_rea
        interp.is_corrective = new_cor
        interp.updated_at = now
    return True
