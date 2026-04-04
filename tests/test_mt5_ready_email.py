import os

from cryptography.fernet import Fernet

import auth_account
import celery_workers.mt5_setup as mt5_setup_module
from helpers.utils import encrypt_password
from models import MT5Account, TradeAccount, User, db


class _FakeProcess:
    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0


class _FakeMt5Module:
    def __init__(self, expected_login):
        self.expected_login = expected_login
        self.shutdown_calls = 0

    def initialize(self, **kwargs):
        return True

    def account_info(self):
        return type("AccountInfo", (), {"login": self.expected_login})()

    def shutdown(self):
        self.shutdown_calls += 1


def _set_mt5_import(monkeypatch, module):
    original_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "MetaTrader5":
            return module
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fake_import)


def _create_user_with_account():
    user = User(
        username="mt5-ready-user",
        email="mt5-ready-user@example.com",
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Ready Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()

    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="77112233",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Live",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()
    return user, trade_account, mt5_account


def test_setup_mt5_terminal_sends_ready_email_when_account_becomes_active(app_ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    user, trade_account, mt5_account = _create_user_with_account()

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    base_appdata = tmp_path / "base-appdata"
    (base_appdata / "config").mkdir(parents=True)
    (base_appdata / "config" / "servers.dat").write_text("server-data", encoding="ascii")

    new_appdata = tmp_path / "new-appdata"
    new_appdata.mkdir()

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(tmp_path / "terminals"))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mt5_setup_module,
        "_find_base_appdata",
        lambda path: str(base_appdata) if os.path.normcase(str(path)) == os.path.normcase(str(base_dir)) else str(new_appdata),
    )
    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess())

    fake_mt5 = _FakeMt5Module(expected_login=int(mt5_account.account_number))
    _set_mt5_import(monkeypatch, fake_mt5)

    captured = {}

    def _fake_send_email(to_email, subject, text_body, html_body=None):
        captured["to_email"] = to_email
        captured["subject"] = subject
        captured["text_body"] = text_body
        captured["html_body"] = html_body
        return {"sent": True, "mode": "test"}

    monkeypatch.setattr(auth_account, "send_email_placeholder", _fake_send_email)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    db.session.expire_all()
    refreshed = db.session.get(MT5Account, mt5_account.id)

    assert result["status"] == "setup complete"
    assert refreshed.is_active is True
    assert captured["to_email"] == user.email
    assert captured["subject"] == "Your MT5 sync is ready"
    assert "your MT5 sync is ready" in captured["text_body"]
