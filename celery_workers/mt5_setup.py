import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shutil

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


@celery.task(bind=True, max_retries=2, default_retry_delay=30)
def setup_mt5_terminal(self, mt5_account_id: int):
    """
    Set up a new MT5 terminal for a user account.
    Copies base MT5 installation and verifies login via Python API.
    Windows only - fails permanently on other platforms.
    """
    from helpers.utils import decrypt_password
    from models import MT5Account, db

    try:
        if os.name != "nt":
            raise PermanentSetupError("setup_mt5_terminal requires Windows")

        if not os.path.exists(MT5_BASE_PATH):
            raise PermanentSetupError(
                f"MT5_BASE_PATH not found: {MT5_BASE_PATH}"
            )

        account = db.session.get(MT5Account, mt5_account_id)
        if account is None:
            return {"error": "MT5Account not found"}

        user_id = account.user_id
        trade_account_id = account.trade_account_id
        login = int(account.account_number)
        investor_password = decrypt_password(account.investor_password_encrypted)
        server = account.server

        terminal_dir = os.path.join(
            MT5_TERMINALS_ROOT,
            f"mt5_{user_id}_{trade_account_id}",
        )
        terminal_exe = os.path.join(terminal_dir, "terminal64.exe")

        if not os.path.exists(terminal_dir):
            shutil.copytree(MT5_BASE_PATH, terminal_dir)

        if not os.path.exists(terminal_exe):
            raise PermanentSetupError(
                f"terminal64.exe not found after copy: {terminal_exe}"
            )

        import MetaTrader5 as mt5

        try:
            result = mt5.initialize(
                path=terminal_exe,
                login=login,
                password=investor_password,
                server=server,
                portable=True,
            )

            if not result:
                error = mt5.last_error()
                raise RuntimeError(f"mt5.initialize() failed: {error}")

            account_info = mt5.account_info()
            if account_info is None:
                error = mt5.last_error()
                raise RuntimeError(f"mt5.account_info() returned None: {error}")

            if account_info.login != login:
                raise RuntimeError(
                    f"Wrong account logged in: expected {login}, got {account_info.login}"
                )
        finally:
            mt5.shutdown()

        account.terminal_path = terminal_exe
        account.is_active = True
        db.session.commit()

        return {
            "terminal_path": terminal_exe,
            "status": "setup complete",
            "account": login,
        }
    except PermanentSetupError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        raise self.retry(exc=exc)


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
