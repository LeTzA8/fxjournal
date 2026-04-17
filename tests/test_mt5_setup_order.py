import sys
import uuid
from types import SimpleNamespace

from cryptography.fernet import Fernet

import celery_workers.mt5_setup_tasks as mt5_setup_module
from helpers.utils import encrypt_password
from models import MT5Account, TradeAccount, User, db


class FakeProcess:
    pid = 9999

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0


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
    """Fake MT5 module where the first initialize fails and the second succeeds.

    Updated for the ensure_mt5_terminal_ready pipeline: path-only initialize,
    separate login call.
    """

    def __init__(self, expected_login):
        self.expected_login = expected_login
        self.initialize_calls = []
        self.login_calls = []
        self.account_info_calls = 0
        self.shutdown_calls = 0

    def initialize(self, path=None, **kwargs):
        self.initialize_calls.append({"path": path})
        return len(self.initialize_calls) > 1

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


def test_start_terminal_process_uses_os_startfile_on_windows(monkeypatch, tmp_path):
    terminal_dir = tmp_path / "mt5-terminal"
    terminal_dir.mkdir()
    terminal_exe = terminal_dir / "terminal64.exe"
    terminal_exe.write_text("", encoding="ascii")

    startfile_calls = []

    def _fake_startfile(path, **kwargs):
        startfile_calls.append({"path": path, **kwargs})

    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.os, "startfile", _fake_startfile)

    proc = mt5_setup_module._start_terminal_process(str(terminal_exe))

    assert proc is None
    assert len(startfile_calls) == 1
    assert startfile_calls[0]["path"] == str(terminal_exe)
    assert startfile_calls[0].get("cwd") == str(terminal_dir)


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
    monkeypatch.setattr(mt5_setup_module.os, "startfile", lambda *a, **k: None)
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
        lambda args, **kwargs: FakeProcess(),
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
    monkeypatch.setattr(
        mt5_setup_module,
        "_seed_market_watch_symbols",
        lambda *_a, **_kw: None,
    )

    fake_mt5 = RecordingMt5Module(events, int(mt5_account.account_number))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    terminal_exe = terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe"

    assert result["status"] == "setup complete"

    # ensure_mt5_terminal_ready: INIT → LOGIN → VERIFY (no subprocess launch)
    # chart clearing must happen BEFORE any MT5 API call
    assert events == [
        ("clear_charts", str(new_appdata)),
        ("initialize", str(terminal_exe)),
        ("login", int(mt5_account.account_number), mt5_account.server),
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
    monkeypatch.setattr(mt5_setup_module.os, "startfile", lambda *a, **k: None)

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
        lambda args, **kwargs: FakeProcess(),
    )

    monkeypatch.setattr(
        mt5_setup_module,
        "_seed_market_watch_symbols",
        lambda *_a, **_kw: None,
    )

    fake_mt5 = FlakyInitializeMt5Module(expected_login=int(mt5_account.account_number))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    terminal_exe = terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe"

    assert result["status"] == "setup complete"

    # ensure_mt5_terminal_ready retries initialize (path-only) inside the same task.
    # First call returns False, second returns True.
    assert fake_mt5.initialize_calls == [
        {"path": str(terminal_exe)},
        {"path": str(terminal_exe)},
    ]
    assert len(fake_mt5.login_calls) == 1
    assert fake_mt5.account_info_calls == 1
    # shutdown: 1 after failed init (ensure cleanup) + 1 after ensure success (setup)
    assert fake_mt5.shutdown_calls == 2
    # sleep: 2s after terminal launch + MT5_TERMINAL_READY_ATTEMPT_DELAY between retries
    assert mt5_setup_module.MT5_TERMINAL_READY_ATTEMPT_DELAY in sleep_calls
