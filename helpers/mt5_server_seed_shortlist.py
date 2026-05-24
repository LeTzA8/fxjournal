"""Failure-driven shortlist of MT5 broker servers that may need VM servers.dat seeding."""

from __future__ import annotations

import logging

from models import MT5ServerSeedShortlist, db, utcnow_naive

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_LEN = 500


def normalize_mt5_server_key(server_name: str | None) -> str | None:
    normalized = str(server_name or "").strip().lower()
    return normalized or None


def is_possible_missing_servers_dat_failure(
    error_message: str | None,
    *,
    post_bootstrap: bool = False,
) -> bool:
    """
    Heuristic: setup failed in a way that might mean the broker server is absent
    from the golden-master servers.dat. Only applies after bootstrap copied
    servers.dat into the per-account terminal.
    """
    if not post_bootstrap:
        return False

    text = str(error_message or "").strip().lower()
    if not text:
        return False

    if any(
        token in text
        for token in (
            "trading/master password",
            "trading password",
            "trade_allowed",
            "wrong account",
            "auth_failed",
            "auth failed",
            "invalid password",
            "invalid account",
            "authorization failed",
        )
    ):
        return False

    if any(
        token in text
        for token in (
            "unsupported",
            "invalid server",
            "res_x",
            "server not found",
            "unknown server",
        )
    ):
        return True

    if any(
        token in text
        for token in (
            "no_ipc",
            "no ipc",
            "ipc connection",
            "timeout",
            "timed out",
            "mt5.initialize() failed",
        )
    ):
        return True

    return False


def record_possible_servers_dat_shortlist(
    *,
    server_name: str | None,
    error_message: str | None,
    mt5_account_id: int | None = None,
) -> MT5ServerSeedShortlist | None:
    server_key = normalize_mt5_server_key(server_name)
    if not server_key:
        return None

    display_name = str(server_name or "").strip() or server_key
    snippet = str(error_message or "").strip()[:LAST_ERROR_MAX_LEN] or None
    now = utcnow_naive()

    row = MT5ServerSeedShortlist.query.filter_by(server_key=server_key).one_or_none()
    if row is None:
        row = MT5ServerSeedShortlist(
            server_name=display_name,
            server_key=server_key,
            status=MT5ServerSeedShortlist.STATUS_OPEN,
            first_failed_at=now,
            last_failed_at=now,
            failure_count=1,
            last_error_snippet=snippet,
            last_mt5_account_id=mt5_account_id,
            created_at=now,
            updated_at=now,
        )
        db.session.add(row)
    else:
        row.server_name = display_name
        row.failure_count = int(row.failure_count or 0) + 1
        row.last_failed_at = now
        row.last_error_snippet = snippet
        row.last_mt5_account_id = mt5_account_id
        row.updated_at = now
        if row.status in {
            MT5ServerSeedShortlist.STATUS_SEEDED,
            MT5ServerSeedShortlist.STATUS_RESOLVED,
        }:
            row.status = MT5ServerSeedShortlist.STATUS_OPEN
            row.seeded_at = None
            row.seeded_by_user_id = None
            row.resolved_at = None

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "MT5 server seed shortlist upsert failed server_key=%s mt5_account_id=%s error=%s",
            server_key,
            mt5_account_id,
            exc,
        )
        return None

    logger.info(
        "MT5 server seed shortlist updated server=%s server_key=%s failures=%s mt5_account_id=%s",
        row.server_name,
        row.server_key,
        row.failure_count,
        mt5_account_id,
    )
    return row


def resolve_mt5_server_seed_shortlist_on_success(*, server_name: str | None) -> None:
    server_key = normalize_mt5_server_key(server_name)
    if not server_key:
        return

    row = MT5ServerSeedShortlist.query.filter_by(server_key=server_key).one_or_none()
    if row is None or row.status == MT5ServerSeedShortlist.STATUS_RESOLVED:
        return

    now = utcnow_naive()
    row.status = MT5ServerSeedShortlist.STATUS_RESOLVED
    row.resolved_at = now
    row.updated_at = now
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "MT5 server seed shortlist resolve failed server_key=%s error=%s",
            server_key,
            exc,
        )
        return

    logger.info(
        "MT5 server seed shortlist resolved after successful setup server=%s",
        row.server_name,
    )


def list_active_mt5_server_seed_shortlist():
    return (
        MT5ServerSeedShortlist.query.filter(
            MT5ServerSeedShortlist.status.in_(
                (
                    MT5ServerSeedShortlist.STATUS_OPEN,
                    MT5ServerSeedShortlist.STATUS_SEEDED,
                )
            )
        )
        .order_by(
            MT5ServerSeedShortlist.status.asc(),
            MT5ServerSeedShortlist.last_failed_at.desc(),
            MT5ServerSeedShortlist.id.desc(),
        )
        .all()
    )


def mark_mt5_server_seed_shortlist_seeded(*, entry_id: int, admin_user_id: int | None) -> MT5ServerSeedShortlist | None:
    row = db.session.get(MT5ServerSeedShortlist, entry_id)
    if row is None:
        return None

    now = utcnow_naive()
    row.status = MT5ServerSeedShortlist.STATUS_SEEDED
    row.seeded_at = now
    row.seeded_by_user_id = admin_user_id
    row.updated_at = now
    db.session.commit()
    return row


def mark_mt5_server_seed_shortlist_resolved(*, entry_id: int) -> MT5ServerSeedShortlist | None:
    row = db.session.get(MT5ServerSeedShortlist, entry_id)
    if row is None:
        return None

    now = utcnow_naive()
    row.status = MT5ServerSeedShortlist.STATUS_RESOLVED
    row.resolved_at = now
    row.updated_at = now
    db.session.commit()
    return row
