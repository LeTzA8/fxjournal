import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shutil
import subprocess
import time

from celery_app import celery


MT5_BASE_PATH = (
    os.environ.get("MT5_BASE_PATH", r"C:\Program Files\MetaTrader 5").strip()
    or r"C:\Program Files\MetaTrader 5"
)
MT5_TERMINALS_ROOT = (
    os.environ.get("MT5_TERMINALS_DIR", r"C:\MT5Terminals").strip()
    or r"C:\MT5Terminals"
)


class PermanentSetupError(RuntimeError):
    """Raised when setup is running on the wrong host or missing required local config."""


def _detect_appdata_hash(terminal_dir: str) -> str:
    """
    Detect MT5 AppData hash by launching terminal briefly
    and finding the newly created AppData folder.
    MT5's internal hash formula is undocumented.
    """
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        raise PermanentSetupError("APPDATA environment variable not set")

    terminal_base = os.path.join(appdata, "MetaQuotes", "Terminal")
    os.makedirs(terminal_base, exist_ok=True)
    terminal_exe = os.path.join(terminal_dir, "terminal64.exe")

    excluded = {"Common", "Community"}

    before = set(
        folder_name
        for folder_name in os.listdir(terminal_base)
        if os.path.isdir(os.path.join(terminal_base, folder_name))
        and folder_name not in excluded
    )

    proc = subprocess.Popen([terminal_exe], cwd=terminal_dir)
    time.sleep(15)
    _stop_process(proc)
    time.sleep(3)

    after = set(
        folder_name
        for folder_name in os.listdir(terminal_base)
        if os.path.isdir(os.path.join(terminal_base, folder_name))
        and folder_name not in excluded
    )
    new_folders = after - before

    if not new_folders:
        raise PermanentSetupError(
            "MT5 AppData folder not created after launch - "
            "check MT5 installation"
        )

    return new_folders.pop()


def _stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _write_common_ini(config_dir: str, *, account_number: str, investor_password: str, server: str) -> str:
    os.makedirs(config_dir, exist_ok=True)
    ini_path = os.path.join(config_dir, "common.ini")
    ini_content = (
        "[Common]\r\n"
        f"Login={account_number}\r\n"
        f"Password={investor_password}\r\n"
        f"Server={server}\r\n"
    )
    with open(ini_path, "w", encoding="ascii", newline="") as file_obj:
        file_obj.write(ini_content)
    return ini_path


@celery.task(bind=True, max_retries=2, default_retry_delay=30)
def setup_mt5_terminal(self, mt5_account_id: int):
    from helpers.utils import decrypt_password
    from models import MT5Account, db

    proc = None

    try:
        if os.name != "nt":
            raise PermanentSetupError("setup_mt5_terminal can only run on a Windows VM worker.")

        if not os.path.isdir(MT5_BASE_PATH):
            raise PermanentSetupError(f"MT5 base path not found: {MT5_BASE_PATH}")

        appdata = os.environ.get("APPDATA", "").strip()
        if not appdata:
            raise PermanentSetupError("APPDATA is required to locate MT5 terminal config folders.")

        account = db.session.get(MT5Account, mt5_account_id)
        if account is None:
            return {"error": "MT5Account not found"}

        investor_password = decrypt_password(account.investor_password_encrypted)

        terminal_dir = os.path.join(
            MT5_TERMINALS_ROOT,
            f"mt5_{account.user_id}_{account.trade_account_id}",
        )
        terminal_exe = os.path.join(terminal_dir, "terminal64.exe")

        os.makedirs(MT5_TERMINALS_ROOT, exist_ok=True)
        if not os.path.isdir(terminal_dir):
            shutil.copytree(MT5_BASE_PATH, terminal_dir)

        if not os.path.isfile(terminal_exe):
            raise RuntimeError(f"MT5 terminal executable not found: {terminal_exe}")

        actual_hash = _detect_appdata_hash(terminal_dir)
        config_dir = os.path.join(
            appdata,
            "MetaQuotes",
            "Terminal",
            actual_hash,
            "config",
        )
        _write_common_ini(
            config_dir,
            account_number=account.account_number,
            investor_password=investor_password,
            server=account.server,
        )

        proc = subprocess.Popen([terminal_exe], cwd=terminal_dir)
        time.sleep(15)
        proc = None

        account.terminal_path = terminal_exe
        account.appdata_hash = actual_hash
        account.is_active = True
        db.session.commit()

        return {
            "terminal_path": terminal_exe,
            "appdata_hash": actual_hash,
            "status": "setup complete",
        }
    except PermanentSetupError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        raise self.retry(exc=exc)
    finally:
        _stop_process(proc)


@celery.task(bind=True, max_retries=2, default_retry_delay=10)
def cleanup_mt5_terminal(self, terminal_path: str, appdata_hash: str):
    """
    Clean up MT5 terminal files when an MT5Account is deleted.
    Runs on VM only - kills process, deletes terminal folder
    and AppData hash folder.
    """
    if os.name != "nt":
        return {"error": "not Windows, skipping cleanup"}

    import psutil

    terminal_exe = terminal_path
    terminal_dir = os.path.dirname(terminal_path)

    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            if proc.info["exe"] and os.path.normcase(proc.info["exe"]) == os.path.normcase(terminal_exe):
                proc.terminate()
                proc.wait(timeout=10)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass

    if terminal_dir and os.path.exists(terminal_dir):
        shutil.rmtree(terminal_dir, ignore_errors=True)

    appdata = os.environ.get("APPDATA", "")
    if appdata and appdata_hash:
        hash_folder = os.path.join(
            appdata,
            "MetaQuotes",
            "Terminal",
            appdata_hash,
        )
        if os.path.exists(hash_folder):
            shutil.rmtree(hash_folder, ignore_errors=True)

    return {
        "terminal_dir": terminal_dir,
        "appdata_hash": appdata_hash,
        "status": "cleanup complete",
    }
