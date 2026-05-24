import pytest

from helpers.mt5_server_seed_shortlist import (
    is_possible_missing_servers_dat_failure,
    list_active_mt5_server_seed_shortlist,
    mark_mt5_server_seed_shortlist_resolved,
    mark_mt5_server_seed_shortlist_seeded,
    normalize_mt5_server_key,
    record_possible_servers_dat_shortlist,
    resolve_mt5_server_seed_shortlist_on_success,
)
from models import MT5ServerSeedShortlist, User, db


@pytest.mark.parametrize(
    ("error_message", "post_bootstrap", "expected"),
    [
        ("mt5.initialize() failed: (-10005, 'IPC timeout')", False, False),
        ("mt5.initialize() failed: (-10005, 'IPC timeout')", True, True),
        ("Invalid account authorization failed", True, False),
        ("trading/master password detected", True, False),
        ("Invalid server res_x unknown", True, True),
        ("", True, False),
    ],
)
def test_is_possible_missing_servers_dat_failure(error_message, post_bootstrap, expected):
    assert (
        is_possible_missing_servers_dat_failure(
            error_message,
            post_bootstrap=post_bootstrap,
        )
        is expected
    )


def test_normalize_mt5_server_key():
    assert normalize_mt5_server_key(" Exness-MT5Real ") == "exness-mt5real"
    assert normalize_mt5_server_key("") is None


def test_record_possible_servers_dat_shortlist_creates_and_updates(app_ctx):
    first = record_possible_servers_dat_shortlist(
        server_name="Exness-MT5Real",
        error_message="IPC timeout",
    )
    assert first is not None
    assert first.status == MT5ServerSeedShortlist.STATUS_OPEN
    assert first.failure_count == 1

    second = record_possible_servers_dat_shortlist(
        server_name=" exness-mt5real ",
        error_message="still timing out",
    )
    assert second.id == first.id
    assert second.failure_count == 2


def test_record_possible_servers_dat_shortlist_reopens_seeded_row(app_ctx):
    row = record_possible_servers_dat_shortlist(
        server_name="ICMarketsSC-Live",
        error_message="IPC timeout",
    )
    mark_mt5_server_seed_shortlist_seeded(entry_id=row.id, admin_user_id=None)
    reopened = record_possible_servers_dat_shortlist(
        server_name="ICMarketsSC-Live",
        error_message="IPC timeout again",
    )
    assert reopened.status == MT5ServerSeedShortlist.STATUS_OPEN
    assert reopened.seeded_at is None


def test_resolve_mt5_server_seed_shortlist_on_success(app_ctx):
    record_possible_servers_dat_shortlist(
        server_name="Pepperstone-Live",
        error_message="IPC timeout",
    )
    resolve_mt5_server_seed_shortlist_on_success(server_name="Pepperstone-Live")
    row = MT5ServerSeedShortlist.query.filter_by(server_key="pepperstone-live").one()
    assert row.status == MT5ServerSeedShortlist.STATUS_RESOLVED
    assert row.resolved_at is not None


def test_list_active_mt5_server_seed_shortlist_excludes_resolved(app_ctx):
    open_row = record_possible_servers_dat_shortlist(
        server_name="Broker-A",
        error_message="IPC timeout",
    )
    seeded_row = record_possible_servers_dat_shortlist(
        server_name="Broker-B",
        error_message="IPC timeout",
    )
    mark_mt5_server_seed_shortlist_seeded(entry_id=seeded_row.id, admin_user_id=None)
    resolved_row = record_possible_servers_dat_shortlist(
        server_name="Broker-C",
        error_message="IPC timeout",
    )
    mark_mt5_server_seed_shortlist_resolved(entry_id=resolved_row.id)

    active = list_active_mt5_server_seed_shortlist()
    active_ids = {row.id for row in active}
    assert open_row.id in active_ids
    assert seeded_row.id in active_ids
    assert resolved_row.id not in active_ids


def test_admin_mt5_page_shows_server_seed_shortlist(app_ctx, client, monkeypatch):
    root_email = "mt5-server-seed-root@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root = User(
        username="mt5-server-seed-root",
        email=root_email,
        password="hashed",
        email_verified=True,
        signup_status="approved",
        is_admin=True,
    )
    db.session.add(root)
    db.session.commit()

    record_possible_servers_dat_shortlist(
        server_name="NeedsSeed-Live",
        error_message="IPC timeout",
    )

    with client.session_transaction() as sess:
        sess["user_id"] = root.id

    response = client.get("/dashboard/admin/access/mt5")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Server seed shortlist" in body
    assert "NeedsSeed-Live" in body


def test_admin_mt5_server_seed_shortlist_confirm_escapes_server_name(app_ctx, client, monkeypatch):
    root_email = "mt5-server-seed-xss-root@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root = User(
        username="mt5-server-seed-xss-root",
        email=root_email,
        password="hashed",
        email_verified=True,
        signup_status="approved",
        is_admin=True,
    )
    db.session.add(root)
    db.session.commit()

    malicious = "');alert(1)//"
    record_possible_servers_dat_shortlist(
        server_name=malicious,
        error_message="IPC timeout",
    )

    with client.session_transaction() as sess:
        sess["user_id"] = root.id

    response = client.get("/dashboard/admin/access/mt5")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "alert(1)" in body
    assert "confirm('Mark" not in body
    assert "confirm(" in body
