import pytest

from models import CFDSymbol, User, db


def _login_session(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


def test_admin_cfd_symbols_root_get_and_update(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root-cfd-admin@example.com")

    user = User(
        username="rootcfd",
        email="root-cfd-admin@example.com",
        password="x",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.add(
        CFDSymbol(
            symbol="XAUUSD",
            aliases="GOLD",
            contract_size=100.0,
            pip_size=0.01,
            sort_order=10,
            is_active=True,
        )
    )
    db.session.add(
        CFDSymbol(
            symbol="EURUSD",
            aliases="EU",
            contract_size=100000.0,
            pip_size=0.0001,
            sort_order=20,
            is_active=True,
        )
    )
    db.session.commit()
    eur = CFDSymbol.query.filter_by(symbol="EURUSD").first()

    _login_session(client, user)
    get_resp = client.get("/dashboard/admin/access/cfd-symbols")
    assert get_resp.status_code == 200
    assert b"XAUUSD" in get_resp.data
    assert b"CFD broker aliases" in get_resp.data

    post_conflict = client.post(
        f"/dashboard/admin/access/cfd-symbols/{eur.id}/aliases",
        data={"aliases": "GOLD"},
        follow_redirects=True,
    )
    assert post_conflict.status_code == 200
    db.session.refresh(eur)
    assert eur.aliases == "EU"

    post_ok = client.post(
        f"/dashboard/admin/access/cfd-symbols/{eur.id}/aliases",
        data={"aliases": "EU, EURUSDM"},
        follow_redirects=True,
    )
    assert post_ok.status_code == 200
    db.session.refresh(eur)
    assert eur.aliases == "EU,EURUSDM"


def test_admin_cfd_symbols_inactive_skips_cross_symbol_validation(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root-cfd2@example.com")

    user = User(
        username="rootcfd2",
        email="root-cfd2@example.com",
        password="x",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.add(
        CFDSymbol(
            symbol="ZZ_CFD_ACTIVE",
            aliases="ZZSHARED",
            contract_size=100.0,
            pip_size=0.01,
            sort_order=10,
            is_active=True,
        )
    )
    db.session.add(
        CFDSymbol(
            symbol="ZZ_CFD_INACTIVE",
            aliases="",
            contract_size=100000.0,
            pip_size=0.0001,
            sort_order=20,
            is_active=False,
        )
    )
    db.session.commit()
    inactive_row = CFDSymbol.query.filter_by(symbol="ZZ_CFD_INACTIVE").first()

    _login_session(client, user)
    post_ok = client.post(
        f"/dashboard/admin/access/cfd-symbols/{inactive_row.id}/aliases",
        data={"aliases": "ZZSHARED"},
        follow_redirects=True,
    )
    assert post_ok.status_code == 200
    db.session.refresh(inactive_row)
    assert inactive_row.aliases == "ZZSHARED"
