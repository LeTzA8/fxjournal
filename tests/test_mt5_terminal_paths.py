import os
import sys
import uuid

from helpers.mt5_terminal_paths import (
    build_legacy_mt5_terminal_folder_name,
    build_mt5_terminal_exe_path,
    build_mt5_terminal_folder_name,
    is_windows_safe_terminal_folder_name,
    resolve_mt5_terminal_dir,
)
import celery_workers.mt5_setup_tasks as mt5_setup_module
from cryptography.fernet import Fernet
from helpers.utils import encrypt_password
from models import MT5Account, TradeAccount, User, db


def test_build_mt5_terminal_folder_name_returns_labeled_format():
    assert build_mt5_terminal_folder_name(12, 34) == "mt5_uid12_taid34"
    assert build_mt5_terminal_folder_name(12, 34, mt5_login="12345678") == "mt5_uid12_taid34"


def test_build_mt5_terminal_folder_name_is_windows_safe():
    folder_name = build_mt5_terminal_folder_name(99, 1001)
    assert is_windows_safe_terminal_folder_name(folder_name)
    assert " " not in folder_name
    assert folder_name.isascii()


def test_build_mt5_terminal_folder_name_ignores_mt5_login_for_privacy():
    folder_name = build_mt5_terminal_folder_name(5, 6, mt5_login="87654321")
    assert "87654321" not in folder_name
    assert "investor" not in folder_name.lower()
    assert "password" not in folder_name.lower()


def test_resolve_mt5_terminal_dir_prefers_persisted_terminal_path(tmp_path):
    legacy_dir = tmp_path / "terminals" / "mt5_1_2"
    legacy_dir.mkdir(parents=True)
    persisted_exe = legacy_dir / "terminal64.exe"
    persisted_exe.write_text("", encoding="ascii")

    resolved = resolve_mt5_terminal_dir(
        terminals_root=str(tmp_path / "terminals"),
        user_id=1,
        trade_account_id=2,
        terminal_path=str(persisted_exe),
    )

    assert resolved == str(legacy_dir)


def test_resolve_mt5_terminal_dir_falls_back_to_legacy_folder_on_disk(tmp_path):
    terminals_root = tmp_path / "terminals"
    legacy_dir = terminals_root / "mt5_3_4"
    legacy_dir.mkdir(parents=True)

    resolved = resolve_mt5_terminal_dir(
        terminals_root=str(terminals_root),
        user_id=3,
        trade_account_id=4,
        terminal_path=None,
    )

    assert resolved == str(legacy_dir)


def test_resolve_mt5_terminal_dir_prefers_labeled_folder_when_both_exist(tmp_path):
    terminals_root = tmp_path / "terminals"
    labeled_dir = terminals_root / build_mt5_terminal_folder_name(7, 8)
    legacy_dir = terminals_root / build_legacy_mt5_terminal_folder_name(7, 8)
    labeled_dir.mkdir(parents=True)
    legacy_dir.mkdir(parents=True)

    resolved = resolve_mt5_terminal_dir(
        terminals_root=str(terminals_root),
        user_id=7,
        trade_account_id=8,
        terminal_path=None,
    )

    assert resolved == str(labeled_dir)


def test_resolve_mt5_terminal_dir_defaults_to_labeled_folder_for_new_setup(tmp_path):
    terminals_root = tmp_path / "terminals"
    terminals_root.mkdir(parents=True)

    resolved = resolve_mt5_terminal_dir(
        terminals_root=str(terminals_root),
        user_id=9,
        trade_account_id=10,
        terminal_path=None,
    )

    assert resolved == str(terminals_root / "mt5_uid9_taid10")


def test_build_mt5_terminal_exe_path_joins_terminal64():
    assert build_mt5_terminal_exe_path(r"C:\MT5Terminals\mt5_uid1_taid2") == (
        r"C:\MT5Terminals\mt5_uid1_taid2\terminal64.exe"
    )


class _DummyPopen:
    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0


def _create_user_with_account():
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"mt5-path-user-{suffix}",
        email=f"mt5-path-user-{suffix}@example.com",
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


def _bypass_wrong_vm_guard(monkeypatch):
    monkeypatch.setattr(
        "helpers.mt5_dispatch.guard_wrong_vm_task",
        lambda *args, **kwargs: None,
    )


def test_setup_mt5_terminal_uses_labeled_folder_for_new_setup(app_ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _bypass_wrong_vm_guard(monkeypatch)

    user, trade_account = _create_user_with_account()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12345678",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    terminals_root = tmp_path / "terminals"
    base_appdata = tmp_path / "base-appdata"
    new_appdata = tmp_path / "new-appdata" / ("E" * 32)
    (base_appdata / "config").mkdir(parents=True)
    (new_appdata / "config").mkdir(parents=True)

    expected_dir = terminals_root / build_mt5_terminal_folder_name(user.id, trade_account.id)

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", lambda *args, **kwargs: _DummyPopen())

    def _fake_find_base_appdata(path):
        if path == str(base_dir):
            return str(base_appdata)
        if path == str(expected_dir):
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

    class _FakeMt5:
        def initialize(self, path=None, **kwargs):
            return True

        def login(self, login, password=None, server=None):
            return True

        def account_info(self):
            from types import SimpleNamespace

            return SimpleNamespace(login=int(mt5_account.account_number), trade_allowed=False)

        def shutdown(self):
            return None

        def last_error(self):
            return (0, "OK")

        def symbol_select(self, symbol, select):
            return True

    monkeypatch.setitem(sys.modules, "MetaTrader5", _FakeMt5())

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    refreshed = db.session.get(MT5Account, mt5_account.id)
    terminal_exe = build_mt5_terminal_exe_path(str(expected_dir))

    assert result["status"] == "setup complete"
    assert refreshed.terminal_path == terminal_exe
    assert "investor-pass" not in terminal_exe
    assert mt5_account.account_number not in os.path.basename(terminal_exe)


def test_setup_mt5_terminal_retry_reuses_persisted_legacy_path(app_ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _bypass_wrong_vm_guard(monkeypatch)

    user, trade_account = _create_user_with_account()
    legacy_dir = tmp_path / "terminals" / build_legacy_mt5_terminal_folder_name(user.id, trade_account.id)
    legacy_dir.mkdir(parents=True)
    legacy_exe = legacy_dir / "terminal64.exe"
    legacy_exe.write_text("", encoding="ascii")

    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12345678",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        terminal_path=str(legacy_exe),
        appdata_hash=None,
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()

    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_text("", encoding="ascii")

    terminals_root = tmp_path / "terminals"
    base_appdata = tmp_path / "base-appdata"
    new_appdata = tmp_path / "new-appdata" / ("F" * 32)
    (base_appdata / "config").mkdir(parents=True)
    (new_appdata / "config").mkdir(parents=True)

    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))
    monkeypatch.setattr(mt5_setup_module, "MT5_TERMINALS_ROOT", str(terminals_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(mt5_setup_module.subprocess, "Popen", lambda *args, **kwargs: _DummyPopen())

    def _fake_find_base_appdata(path):
        if path == str(base_dir):
            return str(base_appdata)
        if path == str(legacy_dir):
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

    class _FakeMt5:
        def initialize(self, path=None, **kwargs):
            return True

        def login(self, login, password=None, server=None):
            return True

        def account_info(self):
            from types import SimpleNamespace

            return SimpleNamespace(login=int(mt5_account.account_number), trade_allowed=False)

        def shutdown(self):
            return None

        def last_error(self):
            return (0, "OK")

        def symbol_select(self, symbol, select):
            return True

    monkeypatch.setitem(sys.modules, "MetaTrader5", _FakeMt5())

    labeled_dir = terminals_root / build_mt5_terminal_folder_name(user.id, trade_account.id)
    assert not labeled_dir.exists()

    result = mt5_setup_module.setup_mt5_terminal.run(mt5_account.id)

    refreshed = db.session.get(MT5Account, mt5_account.id)

    assert result["status"] == "setup complete"
    assert refreshed.terminal_path == str(legacy_exe)
    assert not labeled_dir.exists()
