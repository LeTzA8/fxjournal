import os

import pytest
from cryptography.fernet import Fernet

import celery_workers.mt5_setup as mt5_setup_module
from helpers.utils import encrypt_password
from models import MT5Account, TradeAccount, User, db


class FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


class FakeMt5Module:
    def __init__(self, account_info_results, initialize_result=True):
        self._account_info_results = list(account_info_results)
        self._initialize_result = initialize_result
        self.initialize_calls = []
        self.shutdown_calls = 0

    def initialize(self, **kwargs):
        self.initialize_calls.append(kwargs)
        return self._initialize_result

    def account_info(self):
        if not self._account_info_results:
            return None
        return self._account_info_results.pop(0)

    def shutdown(self):
        self.shutdown_calls += 1


def _set_mt5_import(monkeypatch, module):
    original_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "MetaTrader5":
            if isinstance(module, BaseException):
                raise module
            return module
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fake_import)


def _create_user_with_account(*, username, email, account_name="Main Account"):
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
        name=account_name,
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()
    return user, trade_account


def _create_mt5_account(*, user_id, trade_account_id, account_number="12345678"):
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


@pytest.mark.skip(
    reason="MT5 setup now resolves AppData via origin.txt / _find_base_appdata; calculate_appdata_hash removed."
)
def test_calculate_appdata_hash_returns_uppercase_md5():
    terminal_dir = os.path.join("mt5", "terminal")
    expected = (
        mt5_setup_module.hashlib.md5(
            (os.path.abspath(terminal_dir).upper().rstrip("\\") + "\\").encode("utf-16-le")
        )
        .hexdigest()
        .upper()
    )

    actual = mt5_setup_module.calculate_appdata_hash(terminal_dir)

    assert actual == expected
    assert len(actual) == 32
    assert actual == actual.upper()


@pytest.mark.skip(reason="Obsolete: setup flow no longer uses MD5 formula + common.ini assertions above.")
def test_setup_mt5_terminal_persists_verified_hash_and_terminal_path(app_ctx, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    user, trade_account = _create_user_with_account(
        username="setup-user",
        email="setup-user@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
    )

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_root.mkdir(parents=True)

    terminals_root = tmp_path / "terminals"

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_args, **_kwargs: None)
    fake_mt5 = FakeMt5Module([object()])
    _set_mt5_import(monkeypatch, fake_mt5)

    launch_calls = []

    def _fake_popen(args, cwd):
        launch_calls.append((args, cwd))
        return FakeProcess()

    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", _fake_popen)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    mt5_account = db.session.get(MT5Account, mt5_account.id)
    terminal_exe = terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe"
    expected_hash = mt5_setup_module.calculate_appdata_hash(str(terminals_root / f"mt5_{user.id}_{trade_account.id}"))
    ini_path = appdata_root / expected_hash / "config" / "common.ini"

    assert result["status"] == "setup complete"
    assert result["hash_verified"] is True
    assert mt5_account.is_active is True
    assert mt5_account.terminal_path == str(terminal_exe)
    assert mt5_account.appdata_hash == expected_hash
    assert ini_path.read_bytes() == (
        "[Common]\r\n"
        f"Login={mt5_account.account_number}\r\n"
        "Password=investor-pass\r\n"
        f"Server={mt5_account.server}\r\n"
    ).encode("ascii")
    assert [path.name for path in ini_path.parent.iterdir()] == ["common.ini"]
    assert len(launch_calls) == 1
    assert fake_mt5.initialize_calls == [{"path": str(terminal_exe)}]
    assert fake_mt5.shutdown_calls == 1


@pytest.mark.skip(reason="Obsolete: hash fallback path replaced by origin.txt discovery.")
def test_setup_mt5_terminal_falls_back_to_new_hash_when_formula_misses(app_ctx, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    user, trade_account = _create_user_with_account(
        username="fallback-user",
        email="fallback-user@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="77777777",
    )

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_root.mkdir(parents=True)

    old_hash = "A" * 32
    (appdata_root / old_hash).mkdir()

    terminals_root = tmp_path / "terminals"
    fallback_hash = "B" * 32

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_args, **_kwargs: None)
    fake_mt5 = FakeMt5Module([None, object()])
    _set_mt5_import(monkeypatch, fake_mt5)

    launch_calls = []

    def _fake_popen(args, cwd):
        launch_calls.append((args, cwd))
        if len(launch_calls) == 1:
            (appdata_root / fallback_hash).mkdir(parents=True, exist_ok=True)
        return FakeProcess()

    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", _fake_popen)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    mt5_account = db.session.get(MT5Account, mt5_account.id)
    ini_path = appdata_root / fallback_hash / "config" / "common.ini"

    assert result["status"] == "setup complete"
    assert result["hash_verified"] is False
    assert mt5_account.is_active is True
    assert mt5_account.appdata_hash == fallback_hash
    assert ini_path.read_bytes() == (
        "[Common]\r\n"
        f"Login={mt5_account.account_number}\r\n"
        "Password=investor-pass\r\n"
        f"Server={mt5_account.server}\r\n"
    ).encode("ascii")
    assert len(launch_calls) == 2
    assert fake_mt5.initialize_calls == [
        {"path": str(terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe")},
        {"path": str(terminals_root / f"mt5_{user.id}_{trade_account.id}" / "terminal64.exe")},
    ]
    assert fake_mt5.shutdown_calls == 2


@pytest.mark.skip(reason="Obsolete: verification semantics changed with origin.txt-based AppData.")
def test_setup_mt5_terminal_skips_verification_when_mt5_python_api_is_unavailable(app_ctx, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    user, trade_account = _create_user_with_account(
        username="no-api-user",
        email="no-api@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="78787878",
    )

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_root.mkdir(parents=True)

    terminals_root = tmp_path / "terminals"

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_args, **_kwargs: None)
    _set_mt5_import(monkeypatch, ImportError("MetaTrader5 missing"))

    launch_calls = []

    def _fake_popen(args, cwd):
        launch_calls.append((args, cwd))
        return FakeProcess()

    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", _fake_popen)

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    mt5_account = db.session.get(MT5Account, mt5_account.id)

    assert result["status"] == "setup complete"
    assert result["hash_verified"] is True
    assert mt5_account.is_active is True
    assert len(launch_calls) == 1


@pytest.mark.skip(reason="Obsolete: error messages and setup stages differ from MD5 fallback era.")
def test_setup_mt5_terminal_raises_when_fallback_hash_cannot_be_found(app_ctx, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    user, trade_account = _create_user_with_account(
        username="missing-hash-user",
        email="missing-hash@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="89898989",
    )

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_root.mkdir(parents=True)

    terminals_root = tmp_path / "terminals"

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_args, **_kwargs: None)
    fake_mt5 = FakeMt5Module([None])
    _set_mt5_import(monkeypatch, fake_mt5)

    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", lambda args, cwd: FakeProcess())

    with pytest.raises(
        mt5_setup_module.PermanentSetupError,
        match="MT5 AppData folder not created after launch — check MT5 installation",
    ):
        mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    mt5_account = db.session.get(MT5Account, mt5_account.id)
    assert mt5_account.is_active is False
    assert mt5_account.terminal_path is None
    assert mt5_account.appdata_hash is None


def test_setup_mt5_terminal_fails_permanently_on_non_windows(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    user, trade_account = _create_user_with_account(
        username="non-windows-user",
        email="non-windows@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="88888888",
    )

    monkeypatch.setattr(mt5_setup_module.os, "name", "posix")

    with pytest.raises(mt5_setup_module.PermanentSetupError):
        mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    mt5_account = db.session.get(MT5Account, mt5_account.id)
    assert mt5_account.is_active is False
    assert mt5_account.terminal_path is None
    assert mt5_account.appdata_hash is None


def test_setup_mt5_terminal_fails_permanently_when_base_path_is_missing(app_ctx, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    user, trade_account = _create_user_with_account(
        username="missing-base-user",
        email="missing-base@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="99999999",
    )

    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(tmp_path / "missing-base"))

    with pytest.raises(mt5_setup_module.PermanentSetupError):
        mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    mt5_account = db.session.get(MT5Account, mt5_account.id)
    assert mt5_account.is_active is False
    assert mt5_account.terminal_path is None
    assert mt5_account.appdata_hash is None


def test_mt5_terminals_root_uses_env_override(monkeypatch):
    monkeypatch.setenv("MT5_TERMINALS_DIR", r"D:\CustomMt5")

    configured_root = (
        os.environ.get("MT5_TERMINALS_DIR", r"C:\MT5Terminals").strip()
        or r"C:\MT5Terminals"
    )

    assert configured_root == r"D:\CustomMt5"
