"""Admin helpers for QA waitlist CTA fixture management."""

from __future__ import annotations

import json

from flask import abort

from cli.qa_fixtures.registry import SCENARIO_REGISTRY
from cli.qa_fixtures.safety import assert_dummy_email, assert_fixture_user, assert_not_admin
from cli.qa_fixtures.scenarios import reset_fixture_users, seed_scenario
from helpers.utils import utcnow_naive
from sqlalchemy.orm import joinedload

from models import QaTestAccount, Trade, db


def parse_expected_cta(raw_json: str | None):
    if not raw_json:
        return None
    try:
        return json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None


def expected_cta_kv_rows(expected_cta) -> list[tuple[str, str]]:
    if expected_cta is None:
        return []
    items = expected_cta if isinstance(expected_cta, list) else [expected_cta]
    rows = []
    for index, item in enumerate(items, start=1):
        prefix = f"[{index}] " if len(items) > 1 else ""
        for key in ("source", "feature_interest", "cta_context", "tier_intent"):
            value = item.get(key)
            if value:
                rows.append((f"{prefix}{key}", str(value)))
    return rows


def list_fixture_rows():
    return (
        QaTestAccount.query.options(joinedload(QaTestAccount.user))
        .filter(QaTestAccount.scenario_key.in_(list(SCENARIO_REGISTRY.keys())))
        .order_by(QaTestAccount.scenario_key.asc())
        .all()
    )


def build_fixture_stats(rows: list[QaTestAccount]) -> dict:
    seeded = [row for row in rows if row.user_id is not None]
    oldest = min((row.last_seeded_at for row in seeded if row.last_seeded_at), default=None)
    drift_count = sum(1 for row in seeded if row.last_verified_result == "drift")
    return {
        "fixture_count": len(seeded),
        "expected_count": len(SCENARIO_REGISTRY),
        "oldest_last_seeded_at": oldest,
        "drift_count": drift_count,
    }


def load_sidecar_for_admin_action(sidecar_id: int) -> QaTestAccount:
    sidecar = (
        QaTestAccount.query.options(joinedload(QaTestAccount.user))
        .filter_by(id=sidecar_id)
        .one_or_none()
    )
    if sidecar is None or sidecar.user is None:
        abort(404)
    try:
        assert_fixture_user(sidecar.user)
        assert_not_admin(sidecar.user)
    except ValueError as exc:
        abort(400, description=str(exc))
    return sidecar


def update_fixture_notes(sidecar_id: int, notes: str) -> None:
    sidecar = load_sidecar_for_admin_action(sidecar_id)
    sidecar.notes = (notes or "").strip() or None
    sidecar.updated_at = utcnow_naive()
    db.session.commit()


def reseed_fixture(sidecar_id: int) -> str:
    sidecar = load_sidecar_for_admin_action(sidecar_id)
    scenario_key = sidecar.scenario_key
    if scenario_key not in SCENARIO_REGISTRY:
        abort(400, description=f"Unknown scenario key: {scenario_key}")
    reset_fixture_users([scenario_key])
    seed_scenario(scenario_key)
    db.session.commit()
    return scenario_key


def delete_fixture(sidecar_id: int) -> str:
    sidecar = load_sidecar_for_admin_action(sidecar_id)
    scenario_key = sidecar.scenario_key
    reset_fixture_users([scenario_key])
    return scenario_key


def resolve_open_test_path(sidecar: QaTestAccount) -> str | None:
    path = (sidecar.test_path or "").strip()
    if not path:
        return None
    if "{trade_pubkey}" in path or "{trade_id}" in path:
        trade = (
            Trade.query.filter_by(user_id=sidecar.user_id)
            .order_by(Trade.id.asc())
            .first()
        )
        if trade is None:
            return path
        return (
            path.replace("{trade_pubkey}", trade.pubkey).replace(
                "{trade_id}", str(trade.id)
            )
        )
    return path
