from datetime import date, datetime, timedelta

from ai_service import build_journal_chat_messages
from helpers.journal_context import MAX_DAY_SCOPE_TRADES, build_journal_payload
import routes.admin_journal as admin_journal_routes
from models import JournalMessage, JournalSession, Trade, TradeAccount, User, db


def _create_user(username, email, *, is_admin=False):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
        is_admin=is_admin,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login_as(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


def _create_account(user, *, is_default=True):
    account = TradeAccount(
        user_id=user.id,
        name=f"{user.username} Main",
        is_default=is_default,
    )
    db.session.add(account)
    db.session.flush()
    return account


def _create_trade(user, account, *, symbol="EURUSD", pnl=25.0, opened_at=None, closed_at=None):
    opened = opened_at or datetime(2026, 5, 8, 9, 0)
    closed = closed_at or opened + timedelta(minutes=45)
    trade = Trade(
        user_id=user.id,
        trade_account_id=account.id,
        symbol=symbol,
        side="BUY",
        entry_price=1.1,
        exit_price=1.105,
        lot_size=0.1,
        pnl=pnl,
        opened_at=opened,
        closed_at=closed,
    )
    db.session.add(trade)
    db.session.flush()
    return trade


def test_admin_journal_routes_return_404_for_non_admin(app_ctx, client):
    user = _create_user("journal-plain", "journal-plain@example.com")
    db.session.commit()
    _login_as(client, user)

    assert client.get("/admin/journal").status_code == 404
    assert client.post("/admin/journal/sessions", data={"scope_type": "freeform"}).status_code == 404
    assert client.post("/admin/journal/sessions/1/chat", json={"message": "hi"}).status_code == 404


def test_admin_can_create_trade_day_and_freeform_sessions(app_ctx, client):
    admin = _create_user("journal-admin-scope", "journal-admin-scope@example.com", is_admin=True)
    account = _create_account(admin)
    trade = _create_trade(admin, account, symbol="GBPJPY", closed_at=datetime(2026, 5, 8, 10, 0))
    _create_trade(admin, account, symbol="EURUSD", closed_at=datetime(2026, 5, 8, 11, 0))
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/admin/journal")
    assert response.status_code == 200
    assert b"Journal - admin research" in response.data
    assert b"GBPJPY" in response.data

    trade_response = client.post(
        "/admin/journal/sessions",
        data={"scope_type": "trade", "scope_trade_pubkey": trade.pubkey},
        follow_redirects=True,
    )
    assert trade_response.status_code == 200
    assert b"T1 GBPJPY" in trade_response.data

    day_response = client.post(
        "/admin/journal/sessions",
        data={"scope_type": "day", "scope_date": "2026-05-08"},
        follow_redirects=True,
    )
    assert day_response.status_code == 200
    assert b"T1 GBPJPY" in day_response.data
    assert b"T2 EURUSD" in day_response.data

    freeform_response = client.post(
        "/admin/journal/sessions",
        data={"scope_type": "freeform"},
        follow_redirects=True,
    )
    assert freeform_response.status_code == 200
    assert b"freeform scope with 2 trade refs" in freeform_response.data


def test_journal_chat_persists_messages_and_feedback(app_ctx, client, monkeypatch):
    admin = _create_user("journal-admin-chat", "journal-admin-chat@example.com", is_admin=True)
    account = _create_account(admin)
    trade = _create_trade(admin, account, symbol="EURUSD")
    journal_session = JournalSession(
        user_id=admin.id,
        trade_account_id=account.id,
        scope_type=JournalSession.SCOPE_TRADE,
        scope_trade_pubkey=trade.pubkey,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    db.session.add(journal_session)
    db.session.commit()
    _login_as(client, admin)

    def fake_generate(session_row, payload, user_message, chat_history=None, **kwargs):
        assert session_row.id == journal_session.id
        assert payload["trades"][0]["ref"] == "T1"
        assert user_message == "What did I do wrong?"
        assert chat_history == []
        return "You rushed the EURUSD entry [T1]. What were you feeling before entry?", {"id": "resp_123"}, "test-model"

    monkeypatch.setattr(admin_journal_routes, "generate_journal_chat_reply", fake_generate)

    response = client.post(
        f"/admin/journal/sessions/{journal_session.id}/chat",
        json={"message": "What did I do wrong?"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["message_id"]
    assert any(segment["type"] == "citation" for segment in payload["segments"])

    messages = (
        JournalMessage.query.filter_by(session_id=journal_session.id)
        .order_by(JournalMessage.id.asc())
        .all()
    )
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[1].model_used == "test-model"
    assert messages[1].prompt_version == "journal_v1"

    feedback_response = client.post(
        f"/admin/journal/messages/{messages[1].id}/feedback",
        json={"feedback": "generic", "feedback_note": "Too broad"},
    )
    assert feedback_response.status_code == 200
    assert db.session.get(JournalMessage, messages[1].id).feedback == "generic"


def test_journal_tags_save_and_reload(app_ctx, client):
    admin = _create_user("journal-admin-tags", "journal-admin-tags@example.com", is_admin=True)
    account = _create_account(admin)
    journal_session = JournalSession(
        user_id=admin.id,
        trade_account_id=account.id,
        scope_type=JournalSession.SCOPE_FREEFORM,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    db.session.add(journal_session)
    db.session.commit()
    _login_as(client, admin)

    response = client.post(
        f"/admin/journal/sessions/{journal_session.id}/tags",
        json={
            "title": "revenge on EURUSD",
            "tags": "emotion:tilted, theme:revenge",
            "notes": "Needed screenshots.",
        },
    )
    assert response.status_code == 200

    reloaded = db.session.get(JournalSession, journal_session.id)
    assert reloaded.title == "revenge on EURUSD"
    assert "theme:revenge" in reloaded.tags_json
    assert reloaded.notes == "Needed screenshots."

    page = client.get(f"/admin/journal/sessions/{journal_session.id}")
    assert b"revenge on EURUSD" in page.data
    assert b"theme:revenge" in page.data


def test_journal_prompt_loader_is_used(monkeypatch):
    session_row = JournalSession(
        id=99,
        user_id=1,
        trade_account_id=10,
        scope_type=JournalSession.SCOPE_TRADE,
        scope_trade_pubkey="abc",
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    payload = {
        "scope_type": "trade",
        "session_id": 99,
        "trade_account_id": 10,
        "trades": [
            {
                "ref": "T1",
                "trade_pubkey": "abc",
                "symbol": "EURUSD",
                "closed_at": "2026-05-09T10:00:00",
            }
        ],
    }

    monkeypatch.setattr("ai_service.load_journal_chat_prompt_text", lambda: "scope refusal interview marker")
    messages = build_journal_chat_messages(session_row, payload, "Explain this")

    assert messages[0]["content"][0]["text"] == "scope refusal interview marker"
    assert "TRADE_LINK_REFS_AVAILABLE_FOR_OUTPUT" in messages[1]["content"][0]["text"]
    assert "T1: EURUSD closed 2026-05-09" in messages[1]["content"][0]["text"]


def test_journal_end_session_sets_ended_at(app_ctx, client):
    admin = _create_user("journal-admin-end", "journal-admin-end@example.com", is_admin=True)
    account = _create_account(admin)
    journal_session = JournalSession(
        user_id=admin.id,
        trade_account_id=account.id,
        scope_type=JournalSession.SCOPE_FREEFORM,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    db.session.add(journal_session)
    db.session.commit()
    _login_as(client, admin)

    response = client.post(f"/admin/journal/sessions/{journal_session.id}/end")
    assert response.status_code == 302
    assert db.session.get(JournalSession, journal_session.id).ended_at is not None


def test_day_scope_payload_caps_chronologically_with_truncation_note(app_ctx):
    admin = _create_user("journal-day-cap", "journal-day-cap@example.com", is_admin=True)
    account = _create_account(admin)
    day = date(2026, 5, 8)
    base = datetime(2026, 5, 8, 8, 0)
    total = MAX_DAY_SCOPE_TRADES + 5
    for i in range(total):
        _create_trade(
            admin,
            account,
            symbol="EURUSD",
            pnl=1.0,
            opened_at=base + timedelta(minutes=i),
            closed_at=base + timedelta(minutes=i, seconds=30),
        )
    db.session.commit()

    journal_session = JournalSession(
        user_id=admin.id,
        trade_account_id=account.id,
        scope_type=JournalSession.SCOPE_DAY,
        scope_date=day,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    payload = build_journal_payload(admin, journal_session)

    assert len(payload["trades"]) == MAX_DAY_SCOPE_TRADES
    assert payload["trades"][0]["ref"] == "T1"
    assert payload["trades"][-1]["ref"] == f"T{MAX_DAY_SCOPE_TRADES}"
    assert payload["summary"]["day_closed_trade_total"] == total
    assert payload["summary"]["day_trades_in_payload"] == MAX_DAY_SCOPE_TRADES
    assert payload["summary"]["trade_count"] == MAX_DAY_SCOPE_TRADES
    assert f"showing {MAX_DAY_SCOPE_TRADES} of {total}" in payload["summary"]["day_scope_truncation"]


def test_admin_a_cannot_access_admin_b_journal_session(app_ctx, client):
    admin_a = _create_user("journal-a-view", "journal-a-view@example.com", is_admin=True)
    admin_b = _create_user("journal-b-view", "journal-b-view@example.com", is_admin=True)
    account_b = _create_account(admin_b)
    journal_session_b = JournalSession(
        user_id=admin_b.id,
        trade_account_id=account_b.id,
        scope_type=JournalSession.SCOPE_FREEFORM,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    db.session.add(journal_session_b)
    db.session.commit()
    _login_as(client, admin_a)

    assert client.get(f"/admin/journal/sessions/{journal_session_b.id}").status_code == 404


def test_admin_a_cannot_post_chat_to_admin_b_session(app_ctx, client):
    admin_a = _create_user("journal-a-chat", "journal-a-chat@example.com", is_admin=True)
    admin_b = _create_user("journal-b-chat", "journal-b-chat@example.com", is_admin=True)
    account_b = _create_account(admin_b)
    journal_session_b = JournalSession(
        user_id=admin_b.id,
        trade_account_id=account_b.id,
        scope_type=JournalSession.SCOPE_FREEFORM,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    db.session.add(journal_session_b)
    db.session.commit()
    _login_as(client, admin_a)

    assert (
        client.post(
            f"/admin/journal/sessions/{journal_session_b.id}/chat",
            json={"message": "hello"},
        ).status_code
        == 404
    )


def test_admin_a_cannot_feedback_admin_b_message(app_ctx, client):
    admin_a = _create_user("journal-a-fb", "journal-a-fb@example.com", is_admin=True)
    admin_b = _create_user("journal-b-fb", "journal-b-fb@example.com", is_admin=True)
    account_b = _create_account(admin_b)
    journal_session_b = JournalSession(
        user_id=admin_b.id,
        trade_account_id=account_b.id,
        scope_type=JournalSession.SCOPE_FREEFORM,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    db.session.add(journal_session_b)
    db.session.flush()
    message_b = JournalMessage(
        session_id=journal_session_b.id,
        user_id=admin_b.id,
        role=JournalMessage.ROLE_ASSISTANT,
        content="B reply",
    )
    db.session.add(message_b)
    db.session.commit()
    _login_as(client, admin_a)

    assert (
        client.post(
            f"/admin/journal/messages/{message_b.id}/feedback",
            json={"feedback": "useful"},
        ).status_code
        == 404
    )


def test_admin_a_cannot_update_admin_b_session_tags_or_end(app_ctx, client):
    admin_a = _create_user("journal-a-tags", "journal-a-tags@example.com", is_admin=True)
    admin_b = _create_user("journal-b-tags", "journal-b-tags@example.com", is_admin=True)
    account_b = _create_account(admin_b)
    journal_session_b = JournalSession(
        user_id=admin_b.id,
        trade_account_id=account_b.id,
        scope_type=JournalSession.SCOPE_FREEFORM,
        started_at=datetime(2026, 5, 9, 9, 0),
    )
    db.session.add(journal_session_b)
    db.session.commit()
    _login_as(client, admin_a)

    assert (
        client.post(
            f"/admin/journal/sessions/{journal_session_b.id}/tags",
            json={"title": "hijack", "tags": "theme:bad"},
        ).status_code
        == 404
    )
    assert client.post(f"/admin/journal/sessions/{journal_session_b.id}/end").status_code == 404
    reloaded = db.session.get(JournalSession, journal_session_b.id)
    assert reloaded.title is None
    assert reloaded.ended_at is None


def test_admin_a_cannot_create_trade_session_with_admin_b_trade_pubkey(app_ctx, client):
    admin_a = _create_user("journal-a-pub", "journal-a-pub@example.com", is_admin=True)
    admin_b = _create_user("journal-b-pub", "journal-b-pub@example.com", is_admin=True)
    account_b = _create_account(admin_b)
    trade_b = _create_trade(admin_b, account_b, symbol="XAUUSD")
    db.session.commit()
    _login_as(client, admin_a)

    assert (
        client.post(
            "/admin/journal/sessions",
            data={"scope_type": "trade", "scope_trade_pubkey": trade_b.pubkey},
        ).status_code
        == 404
    )


def test_non_admin_cannot_hit_journal_endpoints_with_guessed_ids(app_ctx, client):
    plain = _create_user("journal-plain-guess", "journal-plain-guess@example.com", is_admin=False)
    db.session.commit()
    _login_as(client, plain)
    sid = 9_999_991
    mid = 9_999_992

    assert client.get("/admin/journal").status_code == 404
    assert client.get(f"/admin/journal/sessions/{sid}").status_code == 404
    assert client.post("/admin/journal/sessions", data={"scope_type": "freeform"}).status_code == 404
    assert (
        client.post(f"/admin/journal/sessions/{sid}/chat", json={"message": "x"}).status_code == 404
    )
    assert (
        client.post(f"/admin/journal/sessions/{sid}/tags", json={"title": "x"}).status_code == 404
    )
    assert client.post(f"/admin/journal/messages/{mid}/feedback", json={"feedback": "useful"}).status_code == 404
    assert client.post(f"/admin/journal/sessions/{sid}/end").status_code == 404
