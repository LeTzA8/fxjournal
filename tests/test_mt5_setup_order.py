import sys
import uuid
from types import SimpleNamespace

from cryptography.fernet import Fernet

import celery_workers.mt5_setup_tasks as mt5_setup_module
from helpers.utils import encrypt_password
from models import MT5Account, TradeAccount, User, db


class RecordingMt5Module:
    """Fake MT5 module that records API call events for ordering assertions.

    Updated for the ensure_mt5_terminal_ready pipeline: path-only initialize,
    separate login call, account_info includes trade_allowed.
    """

    def __init__(self, events, login):
        self.events = events
        self._login = login

    def initialize(self, path=None, **kwargs):
        self.events.append(("initialize", path))
        return True

    def login(self, login, password=None, server=None):
        self.events.append(("login", login, server))
        return True

    def account_info(self):
        return SimpleNamespace(login=self._login, trade_allowed=False)

    def shutdown(self):
        self.events.append(("shutdown",))

    def last_error(self):
        return (0, "OK")

    def symbol_select(self, symbol, select):
        pass


class FlakyInitializeMt5Module:
    """First verify initialize fails once; second attempt succeeds."""

    def __init__(self, expected_login):
        self.expected_login = expected_login
        self.initialize_calls = []
        self.login_calls = []
        self.account_info_calls = 0
        self.shutdown_calls = 0

    def initialize(self, path=None, **kwargs):
        self.initialize_calls.append({"path": path})
        # Call 1 fails, call 2 succeeds.
        return len(self.initialize_calls) != 1

    def last_error(self):
        return (-6, "Terminal: Authorization failed")

    def login(self, login, password=None, server=None):
        self.login_calls.append({"login": login, "server": server})
        return True

    def account_info(self):
        self.account_info_calls += 1
        return SimpleNamespace(login=self.expected_login, trade_allowed=False)

    def shutdown(self):
        self.shutdown_calls += 1

    def symbol_select(self, symbol, select):
        pass


class SeedRecordingMt5Module:
    def __init__(self, selection_results):
        self.selection_results = selection_results
        self.events = []

    def initialize(self, path=None, **kwargs):
        self.events.append(("initialize", path))
        return True

    def login(self, login, password=None, server=None):
        self.events.append(("login", login, server))
        return True

    def shutdown(self):
        self.events.append(("shutdown",))

    def last_error(self):
        return (0, "OK")

    def symbol_select(self, symbol, select):
        self.events.append(("symbol_select", symbol, select))
        return self.selection_results.get(symbol, False)


def _create_user_with_account():
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"mt5-order-user-{suffix}",
        email=f"mt5-order-user-{suffix}@example.com",
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


class DummyPopen:
    def __init__(self, *_args, **_kwargs):
        self.terminated = False
        self.wait_calls = []

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


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
    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", lambda *args, **kwargs: DummyPopen(*args, **kwargs))

    def _fake_find_base_appdata(path):
        if path == str(base_dir):
            return str(base_appdata)
        if path == str(terminals_root / f"mt5_{user.id}_{trade_account.id}"):
            return str(new_appdata)
        return None

    monkeypatch.setattr(mt5_setup_module, "_find_base_appdata", _fake_find_base_appdata)

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
    monkeypatch.setattr(
        mt5_setup_module,
        "_seed_market_watch_symbols",
        lambda *_a, **_kw: (
            events.append(("seed_market_watch",)),
            {
                "selected_symbols": ["BTCUSD"],
                "attempted_symbols": [],
                "used_fallback": False,
                "symbol_select_available": True,
            },
        )[1],
    )

    fake_mt5 = RecordingMt5Module(events, int(mt5_account.account_number))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    terminal_exe = terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe"

    assert result["status"] == "setup complete"

    # Bootstrap: initialize → shutdown; then charts; ensure: initialize → login; final shutdown
    assert events == [
        ("clear_charts", str(new_appdata)),
        ("initialize", str(terminal_exe)),
        ("shutdown",),
        ("clear_market_watch", str(new_appdata), mt5_account.server),
        ("seed_market_watch",),
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
    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", lambda *args, **kwargs: DummyPopen(*args, **kwargs))

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
        mt5_setup_module,
        "_seed_market_watch_symbols",
        lambda *_a, **_kw: {
            "selected_symbols": ["BTCUSD"],
            "attempted_symbols": [],
            "used_fallback": False,
            "symbol_select_available": True,
        },
    )

    fake_mt5 = FlakyInitializeMt5Module(expected_login=int(mt5_account.account_number))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    terminal_exe = terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe"

    assert result["status"] == "setup complete"

    assert fake_mt5.initialize_calls == [
        {"path": str(terminal_exe)},
        {"path": str(terminal_exe)},
    ]
    assert len(fake_mt5.login_calls) == 0
    assert fake_mt5.account_info_calls == 1
    assert fake_mt5.shutdown_calls == 2
    assert mt5_setup_module.MT5_SETUP_VERIFY_RETRY_DELAY_SECONDS in sleep_calls


def test_seed_market_watch_symbols_prefers_crypto_before_fallback():
    fake_mt5 = SeedRecordingMt5Module(
        {
            "BTCUSD": True,
            "BTCUSD.m": True,
            "BTCUSDT": False,
            "XBTUSD": True,
            "ETHUSD.m": True,
            "XAUUSD": True,
            "EURUSD": True,
        }
    )

    result = mt5_setup_module._seed_market_watch_symbols(
        fake_mt5,
        terminal_exe=r"C:\MT5Terminals\seed\terminal64.exe",
        login=12345678,
        investor_password="investor-pass",
        server="Broker-Server",
    )

    assert result["selected_symbols"] == ["BTCUSD", "BTCUSD.m", "XBTUSD", "ETHUSD.m"]
    assert result["used_fallback"] is False
    selected_attempts = [event[1] for event in fake_mt5.events if event[0] == "symbol_select"]
    assert selected_attempts[0:3] == ["BTCUSD", "BTCUSD.m", "BTCUSD.r"]
    assert "BTCUSD.m" in selected_attempts
    assert "ETHUSD.m" in selected_attempts
    assert "XAUUSD" not in selected_attempts
    assert fake_mt5.events[0:2] == [
        ("initialize", r"C:\MT5Terminals\seed\terminal64.exe"),
        ("login", 12345678, "Broker-Server"),
    ]
    assert fake_mt5.events[-1] == ("shutdown",)


def test_seed_market_watch_symbols_falls_back_when_no_crypto_symbol_exists():
    fake_mt5 = SeedRecordingMt5Module(
        {
            "BTCUSD": False,
            "BTCUSDT": False,
            "XBTUSD": False,
            "XAUUSD": True,
            "EURUSD": True,
        }
    )

    result = mt5_setup_module._seed_market_watch_symbols(
        fake_mt5,
        terminal_exe=r"C:\MT5Terminals\seed\terminal64.exe",
        login=12345678,
        investor_password="investor-pass",
        server="Broker-Server",
    )

    assert result["selected_symbols"] == ["XAUUSD", "EURUSD"]
    assert result["used_fallback"] is True
    selected_attempts = [event[1] for event in fake_mt5.events if event[0] == "symbol_select"]
    assert selected_attempts[0:3] == ["BTCUSD", "BTCUSD.m", "BTCUSD.r"]
    assert "ETHUSD" in selected_attempts
    xau_index = selected_attempts.index("XAUUSD")
    assert selected_attempts[xau_index:xau_index + 3] == ["XAUUSD", "XAUUSD.m", "XAUUSD.r"]
    assert "EURUSD" in selected_attempts[xau_index:]
    assert fake_mt5.events[0:2] == [
        ("initialize", r"C:\MT5Terminals\seed\terminal64.exe"),
        ("login", 12345678, "Broker-Server"),
    ]
    assert fake_mt5.events[-1] == ("shutdown",)
