"""Seed and teardown handlers for each waitlist CTA QA scenario."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from werkzeug.security import generate_password_hash

from ai_service import WEEKLY_DASHBOARD_KIND
from helpers.entitlements import MT5_TRIAL_DAYS, WEEKLY_FOLLOWUP_TRIAL_MESSAGE_LIMIT
from helpers.utils import utcnow_naive
from models import (
    AIGeneratedResponse,
    AIPromptHistory,
    MT5Account,
    QaTestAccount,
    Trade,
    TradeAccount,
    TradeBars,
    UpgradeWaitlistEntry,
    User,
    WeeklyReviewChatMessage,
    db,
)

from cli.qa_fixtures.registry import FIXTURE_VERSION, SCENARIO_REGISTRY
from cli.qa_fixtures.safety import FIXTURE_NOTES, KNOWN_FIXTURE_PASSWORD


def _base_user(*, username: str, email: str, premium_trial_started_at=None) -> User:
    now = utcnow_naive()
    user = User(
        username=username,
        email=email,
        password=generate_password_hash(KNOWN_FIXTURE_PASSWORD),
        email_verified=True,
        signup_status="approved",
        is_admin=False,
        google_sub=None,
        plan_tier="free",
        plan_grandfathered=False,
        premium_trial_started_at=premium_trial_started_at,
        created_at=now - timedelta(days=7),
    )
    return user


def _default_trade_account(user: User, *, name_suffix: str) -> TradeAccount:
    account = TradeAccount(
        user_id=user.id,
        name=f"DUMMY CTA — {name_suffix}",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(account)
    db.session.flush()
    return account


def _closed_trade(
    user: User,
    trade_account: TradeAccount,
    *,
    position_suffix: str,
    opened_at,
    closed_at,
    pnl: float = 42.5,
) -> Trade:
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1015,
        lot_size=0.10,
        pnl=pnl,
        stop_loss=1.0980,
        take_profit=1.1040,
        opened_at=opened_at,
        closed_at=closed_at,
        mt5_position=f"CTA{position_suffix}",
    )
    db.session.add(trade)
    db.session.flush()
    return trade


def _add_m5_bars_through_post_exit(trade: Trade, *, bar_count: int = 30) -> None:
    opened_at = trade.opened_at
    closed_at = trade.closed_at or opened_at
    end_at = closed_at + timedelta(hours=12)
    total_seconds = max(int((end_at - opened_at).total_seconds()), 300)
    step = max(total_seconds // max(bar_count - 1, 1), 300)
    base_price = 1.1000
    rows = []
    for index in range(bar_count):
        bar_dt = opened_at + timedelta(seconds=step * index)
        bar_time = int(bar_dt.timestamp())
        offset = index * 0.0001
        rows.append(
            TradeBars(
                trade_id=trade.id,
                timeframe="M5",
                bar_time=bar_time,
                open=base_price + offset,
                high=base_price + offset + 0.0008,
                low=base_price + offset - 0.0004,
                close=base_price + offset + 0.0004,
                tick_volume=40 + index,
            )
        )
    db.session.add_all(rows)


def _inert_mt5_account(
    user: User,
    trade_account: TradeAccount,
    *,
    account_number: str,
    mt5_trial_started_at=None,
    sync_paused_at=None,
    sync_pause_reason=None,
) -> MT5Account:
    account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number=account_number,
        server="Fixture-Broker-Test",
        terminal_path=None,
        investor_password_encrypted=None,
        is_active=True,
        last_synced_at=utcnow_naive() - timedelta(days=2) if sync_paused_at is None else None,
        connection_status=MT5Account.CONNECTION_STATUS_PENDING,
        vm_id=None,
        mt5_trial_started_at=mt5_trial_started_at,
        sync_paused_at=sync_paused_at,
        sync_pause_reason=sync_pause_reason,
    )
    db.session.add(account)
    db.session.flush()
    return account


def _upsert_sidecar(
    *,
    user: User,
    scenario_key: str,
    test_path: str,
    expected_cta,
) -> QaTestAccount:
    meta = SCENARIO_REGISTRY[scenario_key]
    now = utcnow_naive()
    payload = json.dumps(expected_cta) if expected_cta is not None else None
    sidecar = QaTestAccount.query.filter_by(scenario_key=scenario_key).one_or_none()
    if sidecar is None:
        sidecar = QaTestAccount(
            user_id=user.id,
            scenario_key=scenario_key,
            label=meta["label"],
            fixture_version=FIXTURE_VERSION,
            notes=FIXTURE_NOTES,
            test_path=test_path,
            expected_cta_json=payload,
            last_seeded_at=now,
            created_at=now,
            updated_at=now,
        )
        db.session.add(sidecar)
    else:
        if sidecar.user_id != user.id:
            from cli.qa_fixtures.safety import assert_fixture_user, assert_not_admin

            if sidecar.user is None:
                raise ValueError(
                    f"Refusing fixture operation: scenario {scenario_key!r} has a "
                    "qa_test_accounts row without a valid user"
                )
            assert_fixture_user(sidecar.user)
            assert_not_admin(sidecar.user)
        sidecar.user_id = user.id
        sidecar.label = meta["label"]
        sidecar.fixture_version = FIXTURE_VERSION
        sidecar.notes = FIXTURE_NOTES
        sidecar.test_path = test_path
        sidecar.expected_cta_json = payload
        sidecar.last_seeded_at = now
        sidecar.updated_at = now
    db.session.flush()
    return sidecar


def _prompt_history_for(scenario_key: str) -> AIPromptHistory:
    digest = hashlib.sha256(f"cta-fixture-{scenario_key}".encode("utf-8")).hexdigest()
    existing = AIPromptHistory.query.filter_by(prompt_sha256=digest).one_or_none()
    if existing is not None:
        return existing
    row = AIPromptHistory(
        prompt_id=f"cta-fixture-{scenario_key}",
        prompt_sha256=digest,
        prompt_text="QA fixture weekly review prompt stub.",
        source_path=None,
    )
    db.session.add(row)
    db.session.flush()
    return row


def seed_replay_lock() -> dict:
    now = utcnow_naive()
    meta = SCENARIO_REGISTRY["replay_lock"]
    user = _base_user(username=meta["username"], email=meta["email"])
    db.session.add(user)
    db.session.flush()
    trade_account = _default_trade_account(user, name_suffix="Replay")
    trade = _closed_trade(
        user,
        trade_account,
        position_suffix="REPLAY",
        opened_at=now - timedelta(days=5),
        closed_at=now - timedelta(days=4),
    )
    _add_m5_bars_through_post_exit(trade, bar_count=30)
    test_path = f"/dashboard/trades/{trade.pubkey}"
    _upsert_sidecar(
        user=user,
        scenario_key="replay_lock",
        test_path=test_path,
        expected_cta=meta["expected_cta"],
    )
    return {"scenario": "replay_lock", "email": meta["email"], "test_path": test_path}


def seed_ai_followup() -> dict:
    now = utcnow_naive()
    meta = SCENARIO_REGISTRY["ai_followup"]
    trial_start = now - timedelta(days=7)
    user = _base_user(
        username=meta["username"],
        email=meta["email"],
        premium_trial_started_at=trial_start,
    )
    db.session.add(user)
    db.session.flush()
    trade_account = _default_trade_account(user, name_suffix="AI Followup")
    _closed_trade(
        user,
        trade_account,
        position_suffix="AIFU",
        opened_at=now - timedelta(days=3),
        closed_at=now - timedelta(days=2),
    )
    prompt_history = _prompt_history_for("ai_followup")
    review = AIGeneratedResponse(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_history_id=prompt_history.id,
        kind=WEEKLY_DASHBOARD_KIND,
        model="fixture",
        response_text="Fixture weekly review summary.",
        pass_1_output="Fixture pass one.",
        payload_json=json.dumps({"trades": []}),
        payload_hash="cta-fixture-ai-followup",
        trade_count_used=1,
        period_start_utc=now - timedelta(days=7),
        period_end_utc=now,
        generated_at=now - timedelta(days=1),
    )
    db.session.add(review)
    db.session.flush()
    limit = WEEKLY_FOLLOWUP_TRIAL_MESSAGE_LIMIT
    for index in range(limit):
        created_at = trial_start + timedelta(hours=index + 1)
        db.session.add(
            WeeklyReviewChatMessage(
                user_id=user.id,
                trade_account_id=trade_account.id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_USER,
                content=f"Fixture user question {index + 1}",
                created_at=created_at,
            )
        )
        db.session.add(
            WeeklyReviewChatMessage(
                user_id=user.id,
                trade_account_id=trade_account.id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_ASSISTANT,
                content=f"Fixture assistant reply {index + 1}",
                created_at=created_at + timedelta(minutes=1),
            )
        )
    test_path = meta["test_path"]
    _upsert_sidecar(
        user=user,
        scenario_key="ai_followup",
        test_path=test_path,
        expected_cta=meta["expected_cta"],
    )
    return {"scenario": "ai_followup", "email": meta["email"], "test_path": test_path}


def seed_mt5_expired() -> dict:
    now = utcnow_naive()
    meta = SCENARIO_REGISTRY["mt5_expired"]
    user = _base_user(username=meta["username"], email=meta["email"])
    db.session.add(user)
    db.session.flush()
    trade_account = _default_trade_account(user, name_suffix="MT5 Expired")
    _inert_mt5_account(
        user,
        trade_account,
        account_number="999CTA003",
        mt5_trial_started_at=now - timedelta(days=MT5_TRIAL_DAYS + 7),
        sync_paused_at=None,
        sync_pause_reason=None,
    )
    test_path = meta["test_path"]
    _upsert_sidecar(
        user=user,
        scenario_key="mt5_expired",
        test_path=test_path,
        expected_cta=meta["expected_cta"],
    )
    return {"scenario": "mt5_expired", "email": meta["email"], "test_path": test_path}


def seed_mt5_paused() -> dict:
    now = utcnow_naive()
    meta = SCENARIO_REGISTRY["mt5_paused"]
    user = _base_user(username=meta["username"], email=meta["email"])
    db.session.add(user)
    db.session.flush()
    trade_account = _default_trade_account(user, name_suffix="MT5 Paused")
    _inert_mt5_account(
        user,
        trade_account,
        account_number="999CTA004",
        mt5_trial_started_at=now - timedelta(days=MT5_TRIAL_DAYS + 7),
        sync_paused_at=now - timedelta(days=1),
        sync_pause_reason="trial_expired",
    )
    test_path = meta["test_path"]
    _upsert_sidecar(
        user=user,
        scenario_key="mt5_paused",
        test_path=test_path,
        expected_cta=meta["expected_cta"],
    )
    return {"scenario": "mt5_paused", "email": meta["email"], "test_path": test_path}


def seed_zero_data() -> dict:
    meta = SCENARIO_REGISTRY["zero_data"]
    user = _base_user(username=meta["username"], email=meta["email"])
    db.session.add(user)
    db.session.flush()
    test_path = meta["test_path"]
    _upsert_sidecar(
        user=user,
        scenario_key="zero_data",
        test_path=test_path,
        expected_cta=meta["expected_cta"],
    )
    return {"scenario": "zero_data", "email": meta["email"], "test_path": test_path}


def seed_normal_active() -> dict:
    now = utcnow_naive()
    meta = SCENARIO_REGISTRY["normal_active"]
    trial_start = now - timedelta(days=3)
    user = _base_user(
        username=meta["username"],
        email=meta["email"],
        premium_trial_started_at=trial_start,
    )
    db.session.add(user)
    db.session.flush()
    trade_account = _default_trade_account(user, name_suffix="Normal Active")
    for index in range(3):
        trade = _closed_trade(
            user,
            trade_account,
            position_suffix=f"NORM{index}",
            opened_at=now - timedelta(days=index + 2),
            closed_at=now - timedelta(days=index + 1),
            pnl=10.0 + index,
        )
        _add_m5_bars_through_post_exit(trade, bar_count=6)
    test_path = meta["test_path"]
    _upsert_sidecar(
        user=user,
        scenario_key="normal_active",
        test_path=test_path,
        expected_cta=meta["expected_cta"],
    )
    return {"scenario": "normal_active", "email": meta["email"], "test_path": test_path}


SEED_HANDLERS = {
    "replay_lock": seed_replay_lock,
    "ai_followup": seed_ai_followup,
    "mt5_expired": seed_mt5_expired,
    "mt5_paused": seed_mt5_paused,
    "zero_data": seed_zero_data,
    "normal_active": seed_normal_active,
}


def seed_all_fixture_scenarios() -> list[dict]:
    """Reset and seed every CTA fixture scenario in one transaction batch."""
    try:
        reset_fixture_users(commit=False)
        results = [seed_scenario(key) for key in SEED_HANDLERS]
        db.session.commit()
        return results
    except Exception:
        db.session.rollback()
        raise


def seed_scenario(scenario_key: str) -> dict:
    handler = SEED_HANDLERS.get(scenario_key)
    if handler is None:
        raise ValueError(f"Unknown scenario: {scenario_key}")
    return handler()


def reset_fixture_users(scenario_keys=None, *, commit: bool = True) -> int:
    from cli.qa_fixtures.safety import assert_dummy_email, assert_not_admin

    from helpers.core import delete_users_with_related_data

    db.session.rollback()

    keys = scenario_keys or list(SEED_HANDLERS.keys())
    emails = {SCENARIO_REGISTRY[key]["email"] for key in keys}
    user_ids = []
    for email in sorted(emails):
        assert_dummy_email(email)
        user = User.query.filter_by(email=email).one_or_none()
        if user is None:
            continue
        assert_not_admin(user)
        sidecar = QaTestAccount.query.filter_by(user_id=user.id).one_or_none()
        if sidecar is None:
            raise ValueError(
                f"Refusing reset for {email}: missing qa_test_accounts sidecar row"
            )
        user_ids.append(user.id)

    if not user_ids:
        return 0

    UpgradeWaitlistEntry.query.filter(
        db.or_(
            UpgradeWaitlistEntry.user_id.in_(user_ids),
            UpgradeWaitlistEntry.email.in_(emails),
        )
    ).delete(synchronize_session=False)
    QaTestAccount.query.filter(QaTestAccount.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    delete_users_with_related_data(user_ids)
    if commit:
        db.session.commit()
    return len(user_ids)
