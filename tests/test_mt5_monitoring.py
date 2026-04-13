from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

import auth_account
import celery_workers.mt5_monitoring as mt5_monitoring
from helpers.utils import encrypt_password
from models import MT5Account, MT5SyncVMState, TradeAccount, User, db


def _create_user_with_account(*, username, email):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Monitoring Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()

    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="88112233",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        is_active=True,
    )
    db.session.add(mt5_account)
    db.session.commit()
    return user, trade_account, mt5_account


def test_check_mt5_sync_health_sends_one_alert_per_outage_and_resets_on_recovery(app_ctx, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("ERROR_LOG_TO_EMAIL", "ops@example.com")

    for existing_account in MT5Account.query.all():
        existing_account.is_active = False
    MT5SyncVMState.query.delete()
    db.session.commit()

    sent_messages = []

    def _fake_send_email(to_email, subject, text_body, html_body=None):
        sent_messages.append(
            {
                "to_email": to_email,
                "subject": subject,
                "text_body": text_body,
            }
        )
        return {"sent": True, "mode": "test"}

    monkeypatch.setattr(auth_account, "send_email_placeholder", _fake_send_email)

    _user, _trade_account, mt5_account = _create_user_with_account(
        username="monitor-alert-user",
        email="monitor-alert@example.com",
    )
    _user2, _trade_account2, mt5_account2 = _create_user_with_account(
        username="monitor-alert-user-2",
        email="monitor-alert-2@example.com",
    )
    mt5_account.last_synced_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).replace(tzinfo=None)
    mt5_account2.last_synced_at = (datetime.now(timezone.utc) - timedelta(minutes=12)).replace(tzinfo=None)
    mt5_account.vm_id = "vm-old"
    mt5_account2.vm_id = "vm-old"
    db.session.commit()

    first = mt5_monitoring.check_mt5_sync_health.run()
    db.session.expire_all()
    vm_state = db.session.get(MT5SyncVMState, "vm-old")

    assert first["vm_alerts_sent"] == 1
    assert first["stale_vms"] == 1
    assert vm_state is not None
    assert vm_state.vm_alert_sent is True
    assert len(sent_messages) == 1
    assert "Stale accounts: 2" in sent_messages[0]["text_body"]
    assert f"MT5 account id: {mt5_account.id}" in sent_messages[0]["text_body"]
    assert f"MT5 account id: {mt5_account2.id}" in sent_messages[0]["text_body"]
    assert "VM ID: vm-old" in sent_messages[0]["text_body"]

    second = mt5_monitoring.check_mt5_sync_health.run()
    assert second["vm_alerts_sent"] == 0
    assert len(sent_messages) == 1

    refreshed = db.session.get(MT5Account, mt5_account.id)
    refreshed.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()

    third = mt5_monitoring.check_mt5_sync_health.run()
    db.session.expire_all()
    vm_state = db.session.get(MT5SyncVMState, "vm-old")

    assert third["vm_recovered"] == 0
    assert vm_state.vm_alert_sent is True

    recovered_account = db.session.get(MT5Account, mt5_account2.id)
    recovered_account.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()

    fourth = mt5_monitoring.check_mt5_sync_health.run()
    db.session.expire_all()
    recovered_vm_state = db.session.get(MT5SyncVMState, "vm-old")

    assert fourth["vm_recovered"] == 1
    assert recovered_vm_state.vm_alert_sent is False


def test_build_mt5_sync_health_snapshot_flags_stale_backlog(monkeypatch):
    now = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(mt5_monitoring, "get_queue_depth", lambda queue_name: 4)
    monkeypatch.setattr(
        mt5_monitoring,
        "get_queue_monitor_state",
        lambda queue_name: {
            "last_task_received_at": "2026-04-13T11:40:00+00:00",
            "last_task_started_at": "2026-04-13T11:41:00+00:00",
            "last_task_processed_at": "2026-04-13T11:45:00+00:00",
            "last_processed_vm_id": "vm-1",
            "last_processed_worker_hostname": "mt5-sync@VM-1",
            "last_broker_connected_at": "2026-04-13T11:00:00+00:00",
            "last_celery_heartbeat_at": "2026-04-13T11:59:30+00:00",
        },
    )
    monkeypatch.setattr(
        mt5_monitoring,
        "list_worker_states",
        lambda worker_kind: [
            {
                "worker_id": "vm-1",
                "last_task_processed_at": "2026-04-13T11:45:00+00:00",
            }
        ],
    )

    snapshot = mt5_monitoring.build_mt5_sync_health_snapshot(now=now)

    assert snapshot["stale"] is True
    assert snapshot["queue_depth"] == 4
    assert snapshot["last_processed_vm_id"] == "vm-1"
    assert snapshot["seconds_since_last_processed"] == 900


def test_build_mt5_setup_health_snapshot_uses_started_or_processed_activity(monkeypatch):
    now = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(mt5_monitoring, "get_queue_depth", lambda queue_name: 2)
    monkeypatch.setattr(
        mt5_monitoring,
        "get_queue_monitor_state",
        lambda queue_name: {
            "last_task_received_at": "2026-04-13T11:30:00+00:00",
            "last_task_started_at": "2026-04-13T11:55:00+00:00",
            "last_task_processed_at": "2026-04-13T11:20:00+00:00",
            "last_processed_vm_id": "vm-setup",
            "last_processed_worker_hostname": "mt5-setup@VM-SETUP",
            "last_broker_connected_at": "2026-04-13T11:00:00+00:00",
            "last_celery_heartbeat_at": "2026-04-13T11:59:30+00:00",
        },
    )
    monkeypatch.setattr(
        mt5_monitoring,
        "list_worker_states",
        lambda worker_kind: [
            {
                "worker_id": "vm-setup",
                "last_task_started_at": "2026-04-13T11:55:00+00:00",
            }
        ],
    )

    snapshot = mt5_monitoring.build_mt5_setup_health_snapshot(now=now)

    assert snapshot["stale"] is False
    assert snapshot["activity_basis"] == "started_or_processed"
    assert snapshot["activity_reference_at"] == "2026-04-13T11:55:00+00:00"
    assert snapshot["seconds_since_activity"] == 300


def test_check_mt5_worker_staleness_marks_stale_once(app_ctx, monkeypatch):
    captured_state = []
    monkeypatch.setattr(
        mt5_monitoring,
        "build_mt5_sync_health_snapshot",
        lambda: {
            "queue_name": "mt5_sync",
            "queue_depth": 7,
            "current_time": "2026-04-13T12:00:00+00:00",
            "stale": True,
            "stale_alert_active": False,
            "last_task_received_at": "2026-04-13T11:40:00+00:00",
            "last_task_started_at": "2026-04-13T11:41:00+00:00",
            "last_task_processed_at": "2026-04-13T11:45:00+00:00",
            "last_broker_connected_at": "2026-04-13T11:00:00+00:00",
            "last_broker_disconnect_at": None,
            "last_broker_retry_at": None,
            "last_celery_heartbeat_at": "2026-04-13T11:59:30+00:00",
            "last_processed_vm_id": "vm-1",
            "last_processed_worker_hostname": "mt5-sync@VM-1",
        },
    )
    monkeypatch.setattr(
        mt5_monitoring,
        "set_queue_monitor_state",
        lambda queue_name, mapping, ttl=86400: captured_state.append((queue_name, mapping, ttl)),
    )

    result = mt5_monitoring.check_mt5_worker_staleness.run()

    assert result["stale"] is True
    assert captured_state == [
        (
            "mt5_sync",
            {
                "stale_alert_active": True,
                "stale_detected_at": "2026-04-13T12:00:00+00:00",
            },
            86400,
        )
    ]


def test_check_mt5_setup_worker_staleness_marks_stale_once(app_ctx, monkeypatch):
    captured_state = []
    monkeypatch.setattr(
        mt5_monitoring,
        "build_mt5_setup_health_snapshot",
        lambda: {
            "queue_name": "mt5_setup",
            "queue_depth": 3,
            "current_time": "2026-04-13T12:00:00+00:00",
            "stale": True,
            "stale_alert_active": False,
            "last_task_received_at": "2026-04-13T11:20:00+00:00",
            "last_task_started_at": "2026-04-13T11:21:00+00:00",
            "last_task_processed_at": "2026-04-13T11:00:00+00:00",
            "activity_reference_at": "2026-04-13T11:21:00+00:00",
            "last_broker_connected_at": "2026-04-13T11:00:00+00:00",
            "last_broker_disconnect_at": None,
            "last_broker_retry_at": None,
            "last_celery_heartbeat_at": "2026-04-13T11:59:30+00:00",
            "last_processed_vm_id": "vm-setup",
            "last_processed_worker_hostname": "mt5-setup@VM-SETUP",
        },
    )
    monkeypatch.setattr(
        mt5_monitoring,
        "set_queue_monitor_state",
        lambda queue_name, mapping, ttl=86400: captured_state.append((queue_name, mapping, ttl)),
    )

    result = mt5_monitoring.check_mt5_setup_worker_staleness.run()

    assert result["stale"] is True
    assert captured_state == [
        (
            "mt5_setup",
            {
                "stale_alert_active": True,
                "stale_detected_at": "2026-04-13T12:00:00+00:00",
            },
            86400,
        )
    ]
