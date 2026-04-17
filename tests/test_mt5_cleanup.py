import os
from types import SimpleNamespace

import celery_workers.mt5_setup_tasks as mt5_setup_module
import pytest
from helpers.utils import utcnow_naive
from models import MT5Account, db


def _set_missing_psutil(monkeypatch):
    original_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "psutil":
            raise ModuleNotFoundError("No module named 'psutil'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fake_import)


def test_cleanup_mt5_terminal_falls_back_without_psutil_and_uses_appdata_hash(monkeypatch, tmp_path):
    terminal_dir = tmp_path / "terminals" / "mt5_1_1"
    terminal_dir.mkdir(parents=True)
    terminal_exe = terminal_dir / "terminal64.exe"
    terminal_exe.write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_hash = "A" * 32
    appdata_folder = appdata_root / appdata_hash
    appdata_folder.mkdir(parents=True)

    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    _set_missing_psutil(monkeypatch)

    calls = []

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mt5_setup_module.subprocess, "run", _fake_run)

    result = mt5_setup_module.cleanup_mt5_terminal.run(str(terminal_exe), appdata_hash)

    assert result["status"] == "cleanup complete"
    assert not terminal_dir.exists()
    assert not appdata_folder.exists()
    assert len(calls) == 1
    assert calls[0][0][:3] == ["powershell.exe", "-NoProfile", "-Command"]
    assert os.path.normcase(calls[0][1]["env"]["FXJ_TERMINAL_EXE"]) == os.path.normcase(
        str(terminal_exe)
    )


def test_cleanup_mt5_terminal_skips_mismatched_hash_folder(monkeypatch, tmp_path):
    terminal_dir = tmp_path / "terminals" / "mt5_2_2"
    terminal_dir.mkdir(parents=True)
    terminal_exe = terminal_dir / "terminal64.exe"
    terminal_exe.write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    wrong_hash = "B" * 32
    right_hash = "C" * 32
    wrong_folder = appdata_root / wrong_hash
    right_folder = appdata_root / right_hash
    wrong_folder.mkdir(parents=True)
    right_folder.mkdir(parents=True)

    (wrong_folder / "origin.txt").write_text(str(tmp_path / "other-terminal"), encoding="utf-16")
    (right_folder / "origin.txt").write_text(str(terminal_dir), encoding="utf-16")

    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    _set_missing_psutil(monkeypatch)
    monkeypatch.setattr(
        mt5_setup_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    result = mt5_setup_module.cleanup_mt5_terminal.run(str(terminal_exe), wrong_hash)

    assert result["status"] == "cleanup complete"
    assert not terminal_dir.exists()
    assert wrong_folder.exists()
    assert not right_folder.exists()


def test_cleanup_mt5_terminal_deletes_db_row_on_success(app_ctx, monkeypatch, tmp_path):
    """When mt5_account_id is provided, the DB row is deleted after successful cleanup."""
    terminal_dir = tmp_path / "terminals" / "mt5_db_del"
    terminal_dir.mkdir(parents=True)
    terminal_exe = terminal_dir / "terminal64.exe"
    terminal_exe.write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_hash = "D" * 32
    appdata_folder = appdata_root / appdata_hash
    appdata_folder.mkdir(parents=True)

    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    _set_missing_psutil(monkeypatch)
    monkeypatch.setattr(
        mt5_setup_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    mt5_account = MT5Account(
        user_id=None,
        trade_account_id=None,
        account_number="CLEANUP_99999999",
        server="TestServer",
        terminal_path=str(terminal_exe),
        appdata_hash=appdata_hash,
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()
    mt5_account_id = mt5_account.id
    assert db.session.get(MT5Account, mt5_account_id) is not None

    result = mt5_setup_module.cleanup_mt5_terminal.run(
        str(terminal_exe), appdata_hash, mt5_account_id=mt5_account_id,
    )

    assert result["status"] == "cleanup complete"
    assert result["db_deleted"] is True
    assert db.session.get(MT5Account, mt5_account_id) is None


def test_cleanup_mt5_terminal_clears_cleanup_mark_without_deleting_row(app_ctx, monkeypatch, tmp_path):
    terminal_dir = tmp_path / "terminals" / "mt5_clear_mark"
    terminal_dir.mkdir(parents=True)
    terminal_exe = terminal_dir / "terminal64.exe"
    terminal_exe.write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_hash = "F" * 32
    appdata_folder = appdata_root / appdata_hash
    appdata_folder.mkdir(parents=True)

    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    _set_missing_psutil(monkeypatch)
    monkeypatch.setattr(
        mt5_setup_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    mt5_account = MT5Account(
        user_id=None,
        trade_account_id=None,
        account_number="CLEANUP_88888888",
        server="TestServer",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
        cleanup_marked_at=utcnow_naive(),
    )
    db.session.add(mt5_account)
    db.session.commit()
    mt5_account_id = mt5_account.id
    mark_token = db.session.get(MT5Account, mt5_account_id).cleanup_marked_at.isoformat()

    result = mt5_setup_module.cleanup_mt5_terminal.run(
        str(terminal_exe),
        appdata_hash,
        mt5_account_id=mt5_account_id,
        delete_account_row=False,
        clear_cleanup_mark=True,
        cleanup_marked_at=mark_token,
    )

    refreshed = db.session.get(MT5Account, mt5_account_id)
    assert result["status"] == "cleanup complete"
    assert result["db_deleted"] is False
    assert result["cleanup_mark_cleared"] is True
    assert refreshed is not None
    assert refreshed.cleanup_marked_at is None


def test_cleanup_mt5_terminal_keeps_cleanup_mark_when_terminal_dir_survives(
    app_ctx, monkeypatch, tmp_path
):
    terminal_dir = tmp_path / "terminals" / "mt5_cleanup_survives"
    terminal_dir.mkdir(parents=True)
    terminal_exe = terminal_dir / "terminal64.exe"
    terminal_exe.write_text("", encoding="ascii")

    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_hash = "G" * 32
    appdata_folder = appdata_root / appdata_hash
    appdata_folder.mkdir(parents=True)

    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    _set_missing_psutil(monkeypatch)
    monkeypatch.setattr(
        mt5_setup_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    original_rmtree = mt5_setup_module.shutil.rmtree

    def _fake_rmtree(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(str(terminal_dir))):
            return None
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(mt5_setup_module.shutil, "rmtree", _fake_rmtree)

    mt5_account = MT5Account(
        user_id=None,
        trade_account_id=None,
        account_number="CLEANUP_77777777",
        server="TestServer",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
        cleanup_marked_at=utcnow_naive(),
    )
    db.session.add(mt5_account)
    db.session.commit()
    mt5_account_id = mt5_account.id
    mark_token = db.session.get(MT5Account, mt5_account_id).cleanup_marked_at.isoformat()

    with pytest.raises(OSError, match="terminal dir still exists after cleanup"):
        mt5_setup_module.cleanup_mt5_terminal.run(
            str(terminal_exe),
            appdata_hash,
            mt5_account_id=mt5_account_id,
            delete_account_row=False,
            clear_cleanup_mark=True,
            cleanup_marked_at=mark_token,
        )

    refreshed = db.session.get(MT5Account, mt5_account_id)
    assert refreshed is not None
    assert refreshed.cleanup_marked_at is not None
    assert terminal_dir.exists()


def test_cleanup_mt5_terminal_without_account_id_skips_db_delete(monkeypatch, tmp_path):
    """When mt5_account_id is None, no DB deletion is attempted."""
    terminal_dir = tmp_path / "terminals" / "mt5_no_id"
    terminal_dir.mkdir(parents=True)
    terminal_exe = terminal_dir / "terminal64.exe"
    terminal_exe.write_text("", encoding="ascii")

    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    _set_missing_psutil(monkeypatch)
    monkeypatch.setattr(
        mt5_setup_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    result = mt5_setup_module.cleanup_mt5_terminal.run(str(terminal_exe), "")

    assert result["status"] == "cleanup complete"
    assert result["db_deleted"] is False


def test_cleanup_mt5_terminal_removes_hashed_appdata_without_terminal_path(monkeypatch, tmp_path):
    appdata_root = tmp_path / "appdata" / "MetaQuotes" / "Terminal"
    appdata_hash = "E" * 32
    appdata_folder = appdata_root / appdata_hash
    appdata_folder.mkdir(parents=True)
    (appdata_folder / "origin.txt").write_text(str(tmp_path / "some-terminal"), encoding="utf-16")

    monkeypatch.setattr(mt5_setup_module, "APPDATA_TERMINAL_PATH", str(appdata_root))
    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    _set_missing_psutil(monkeypatch)

    calls = []

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mt5_setup_module.subprocess, "run", _fake_run)

    result = mt5_setup_module.cleanup_mt5_terminal.run("", appdata_hash)

    assert result["status"] == "cleanup complete"
    assert not appdata_folder.exists()
    assert calls == []
