import sys
from types import SimpleNamespace

from cryptography.fernet import Fernet

import celery_workers.mt5_setup as mt5_setup_module
from helpers.utils import encrypt_password
from models import MT5Account, TradeAccount, User, db


class FakeProcess:
    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0


class RecordingMt5Module:
    def __init__(self, events, login):
        self.events = events
        self.login = login

    def initialize(self, path, **kwargs):
        self.events.append(("initialize", path, kwargs["login"]))
        return True

    def account_info(self):
        return SimpleNamespace(login=self.login)

    def shutdown(self):
        self.events.append(("shutdown",))


class FlakyInitializeMt5Module:
    def __init__(self, expected_login):
        self.expected_login = expected_login
        self.initialize_calls = []
        self.account_info_calls = 0
        self.shutdown_calls = 0

    def initialize(self, path, **kwargs):
        self.initialize_calls.append(
            {
                "path": path,
                "login": kwargs["login"],
                "server": kwargs["server"],
            }
        )
        return len(self.initialize_calls) > 1

    def last_error(self):
        return (-6, "Terminal: Authorization failed")

    def account_info(self):
        self.account_info_calls += 1
        return SimpleNamespace(login=self.expected_login)

    def shutdown(self):
        self.shutdown_calls += 1


def _create_user_with_account():
    user = User(
        username="mt5-order-user",
        email="mt5-order-user@example.com",
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Main Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()
    return user, trade_account


def _create_mt5_account(user_id, trade_account_id, account_number="12345678"):
    mt5_account = MT5Account(
        user_id=user_id,
        trade_account_id=trade_account_id,
        account_number=account_number,
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()
    return mt5_account


def test_setup_mt5_terminal_clears_charts_before_python_api_login(app_ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account()
    mt5_account = _create_mt5_account(user.id, trade_account.id)

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    terminals_root = tmp_path / "terminals"
    base_appdata = tmp_path / "base-appdata"
    new_appdata = tmp_path / "new-appdata" / ("C" * 32)
    (base_appdata / "config").mkdir(parents=True)
    (new_appdata / "config").mkdir(parents=True)

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_args, **_kwargs: None)

    def _fake_find_base_appdata(path):
        if path == str(base_dir):
            return str(base_appdata)
        if path == str(terminals_root / f"mt5_{user.id}_{trade_account.id}"):
            return str(new_appdata)
        return None

    monkeypatch.setattr(mt5_setup_module, "_find_base_appdata", _fake_find_base_appdata)
    monkeypatch.setattr(
        mt5_setup_module.subprocess,
        "Popen",
        lambda args, cwd: FakeProcess(),
    )

    events = []

    monkeypatch.setattr(
        mt5_setup_module,
        "_clear_chart_profiles",
        lambda appdata_path: events.append(("clear_charts", appdata_path)),
    )
    monkeypatch.setattr(
        mt5_setup_module,
        "_clear_market_watch_selection",
        lambda appdata_path, server: events.append(("clear_market_watch", appdata_path, server)),
    )

    fake_mt5 = RecordingMt5Module(events, int(mt5_account.account_number))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    terminal_exe = terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe"

    assert result["status"] == "setup complete"
    assert events == [
        ("clear_charts", str(new_appdata)),
        ("initialize", str(terminal_exe), int(mt5_account.account_number)),
        ("shutdown",),
        ("clear_market_watch", str(new_appdata), mt5_account.server),
    ]


def test_setup_mt5_terminal_retries_mt5_verification_within_same_task(app_ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account()
    mt5_account = _create_mt5_account(user.id, trade_account.id, account_number="87654321")

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    terminals_root = tmp_path / "terminals"
    base_appdata = tmp_path / "base-appdata"
    new_appdata = tmp_path / "new-appdata" / ("D" * 32)
    (base_appdata / "config").mkdir(parents=True)
    (new_appdata / "config").mkdir(parents=True)

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")

    sleep_calls = []
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    def _fake_find_base_appdata(path):
        if path == str(base_dir):
            return str(base_appdata)
        if path == str(terminals_root / f"mt5_{user.id}_{trade_account.id}"):
            return str(new_appdata)
        return None

    monkeypatch.setattr(mt5_setup_module, "_find_base_appdata", _fake_find_base_appdata)
    monkeypatch.setattr(
        mt5_setup_module.subprocess,
        "Popen",
        lambda args, cwd: FakeProcess(),
    )

    fake_mt5 = FlakyInitializeMt5Module(expected_login=int(mt5_account.account_number))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    terminal_exe = terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe"

    assert result["status"] == "setup complete"
    assert fake_mt5.initialize_calls == [
        {
            "path": str(terminal_exe),
            "login": int(mt5_account.account_number),
            "server": mt5_account.server,
        },
        {
            "path": str(terminal_exe),
            "login": int(mt5_account.account_number),
            "server": mt5_account.server,
        },
    ]
    assert fake_mt5.account_info_calls == 1
    assert fake_mt5.shutdown_calls == 2
    assert sleep_calls == [mt5_setup_module.MT5_SETUP_VERIFY_RETRY_DELAY_SECONDS]
