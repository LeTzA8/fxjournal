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
APPDATA_TERMINAL_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"),
    "MetaQuotes",
    "Terminal",
)
IGNORED_APPDATA_FOLDERS = {"Common", "Community"}


def _get_appdata_folders() -> set:
    if not os.path.isdir(APPDATA_TERMINAL_PATH):
        return set()
    return {
        e.name for e in os.scandir(APPDATA_TERMINAL_PATH)
        if e.is_dir() and e.name not in IGNORED_APPDATA_FOLDERS
    }


def _find_base_appdata(base_path: str):
    target = os.path.normcase(os.path.abspath(base_path))
    if not os.path.isdir(APPDATA_TERMINAL_PATH):
        return None
    for entry in os.scandir(APPDATA_TERMINAL_PATH):
        if not entry.is_dir() or entry.name in IGNORED_APPDATA_FOLDERS:
            continue
        origin = os.path.join(entry.path, "origin.txt")
        try:
            content = open(origin, encoding="utf-16", errors="ignore").read().strip().rstrip("\\")
            if os.path.normcase(content) == target:
                return entry.path
        except OSError:
            pass
    return None


class PermanentSetupError(RuntimeError):
    """Raised when setup is running on the wrong host or missing required local config."""


@celery.task(bind=True, max_retries=2, default_retry_delay=30, queue="mt5_setup")
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

        # Find base terminal AppData which contains the populated servers.dat
        base_appdata = _find_base_appdata(MT5_BASE_PATH)
        if base_appdata is None:
            raise PermanentSetupError(
                "Could not find base MT5 AppData via origin.txt — "
                "ensure the base terminal has been run at least once"
            )

        # Snapshot existing AppData folders so we can detect the new one
        folders_before = _get_appdata_folders()

        # Launch the terminal briefly — this causes MT5 to create its AppData folder
        proc = subprocess.Popen([terminal_exe], cwd=terminal_dir)

        try:
            # Wait up to 30s for the new AppData folder to appear
            new_hash = None
            deadline = time.time() + 30
            while time.time() < deadline:
                new_folders = _get_appdata_folders() - folders_before
                if new_folders:
                    new_hash = new_folders.pop()
                    break
                time.sleep(2)

            if new_hash is None:
                raise PermanentSetupError(
                    "MT5 AppData folder not created after launch — check MT5 installation"
                )

            # Verify the new folder's origin.txt matches the new terminal path
            new_appdata = os.path.join(APPDATA_TERMINAL_PATH, new_hash)
            new_origin = os.path.join(new_appdata, "origin.txt")
            if os.path.exists(new_origin):
                origin_content = open(new_origin, encoding="utf-16", errors="ignore").read().strip().rstrip("\\")
                if os.path.normcase(origin_content) != os.path.normcase(os.path.abspath(terminal_dir)):
                    raise PermanentSetupError(
                        f"New AppData origin.txt mismatch: expected {terminal_dir}, got {origin_content}"
                    )

            # Copy servers.dat from base AppData so the new terminal knows
            # how to resolve the broker server address
            src_servers = os.path.join(base_appdata, "config", "servers.dat")
            dst_config = os.path.join(new_appdata, "config")
            os.makedirs(dst_config, exist_ok=True)
            dst_servers = os.path.join(dst_config, "servers.dat")
            if os.path.exists(src_servers):
                shutil.copy2(src_servers, dst_servers)

            account.appdata_hash = new_hash
        finally:
            proc.terminate()
            proc.wait(timeout=10)

        import MetaTrader5 as mt5

        try:
            result = mt5.initialize(
                terminal_exe,
                login=login,
                password=investor_password,
                server=server,
                timeout=60000,
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


@celery.task(bind=True, max_retries=2, default_retry_delay=10, queue="mt5_setup")
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
