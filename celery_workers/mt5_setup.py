import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
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
    os.environ.get("APPDATA", ""),
    "MetaQuotes",
    "Terminal",
)
IGNORED_APPDATA_FOLDERS = {"Common", "Community"}


class PermanentSetupError(RuntimeError):
    """Raised when setup is running on the wrong host or missing required local config."""


def calculate_appdata_hash(terminal_path: str) -> str:
    """
    Calculate the MT5 AppData hash for a terminal install directory.

    IMPORTANT: This formula is treated as unverified until runtime.
    The task verifies whether the calculated folder actually exists
    after launch and falls back to the folder MT5 created if needed.
    """
    path = os.path.abspath(terminal_path).upper().rstrip("\\") + "\\"
    return hashlib.md5(path.encode("utf-16-le")).hexdigest().upper()


def _list_candidate_appdata_hashes() -> list[str]:
    if not os.path.isdir(APPDATA_TERMINAL_PATH):
        return []

    hashes = []
    for name in os.listdir(APPDATA_TERMINAL_PATH):
        if name in IGNORED_APPDATA_FOLDERS:
            continue

        full_path = os.path.join(APPDATA_TERMINAL_PATH, name)
        if not os.path.isdir(full_path):
            continue

        if len(name) == 32 and all(ch in "0123456789ABCDEFabcdef" for ch in name):
            hashes.append(name)

    return hashes


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


def _verify_terminal_login(terminal_exe: str):
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None

    if not mt5.initialize(path=terminal_exe):
        mt5.shutdown()
        return False

    try:
        return mt5.account_info() is not None
    finally:
        mt5.shutdown()


def _select_fallback_hash(*, expected_hash: str, existing_hashes: set[str]) -> str:
    all_hashes = _list_candidate_appdata_hashes()
    new_hashes = [
        name for name in all_hashes
        if name not in existing_hashes and name != expected_hash
    ]
    if new_hashes:
        return max(
            new_hashes,
            key=lambda name: os.path.getctime(os.path.join(APPDATA_TERMINAL_PATH, name)),
        )

    remaining_hashes = [name for name in all_hashes if name != expected_hash]
    if remaining_hashes:
        return max(
            remaining_hashes,
            key=lambda name: os.path.getctime(os.path.join(APPDATA_TERMINAL_PATH, name)),
        )

    raise PermanentSetupError(
        "MT5 AppData folder not created after launch — check MT5 installation"
    )


@celery.task(bind=True, max_retries=2, default_retry_delay=30)
def setup_mt5_terminal(self, mt5_account_id: int):
    from flask import current_app

    from helpers.utils import decrypt_password
    from models import MT5Account, db

    proc = None

    try:
        if os.name != "nt":
            raise PermanentSetupError("setup_mt5_terminal can only run on a Windows VM worker.")

        if not os.path.isdir(MT5_BASE_PATH):
            raise PermanentSetupError(f"MT5 base path not found: {MT5_BASE_PATH}")

        if not os.environ.get("APPDATA", "").strip():
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

        expected_hash = calculate_appdata_hash(terminal_dir)
        expected_config_dir = os.path.join(
            APPDATA_TERMINAL_PATH,
            expected_hash,
            "config",
        )
        existing_hashes = set(_list_candidate_appdata_hashes())
        _write_common_ini(
            expected_config_dir,
            account_number=account.account_number,
            investor_password=investor_password,
            server=account.server,
        )

        proc = subprocess.Popen([terminal_exe], cwd=terminal_dir)
        time.sleep(15)

        verify_result = _verify_terminal_login(terminal_exe)
        hash_verified = verify_result is not False
        actual_hash = expected_hash

        if verify_result is None:
            current_app.logger.info(
                "MT5 Python API unavailable for mt5_account_id=%s; trusting calculated AppData hash %s",
                mt5_account_id,
                expected_hash,
            )
            proc = None
        elif verify_result:
            current_app.logger.info(
                "MT5 AppData hash verified for mt5_account_id=%s: %s",
                mt5_account_id,
                expected_hash,
            )
            proc = None
        else:
            _stop_process(proc)
            proc = None

            actual_hash = _select_fallback_hash(
                expected_hash=expected_hash,
                existing_hashes=existing_hashes,
            )
            current_app.logger.warning(
                "MT5 AppData hash mismatch for mt5_account_id=%s: expected=%s actual=%s",
                mt5_account_id,
                expected_hash,
                actual_hash,
            )

            fallback_config_dir = os.path.join(
                APPDATA_TERMINAL_PATH,
                actual_hash,
                "config",
            )
            _write_common_ini(
                fallback_config_dir,
                account_number=account.account_number,
                investor_password=investor_password,
                server=account.server,
            )

            proc = subprocess.Popen([terminal_exe], cwd=terminal_dir)
            time.sleep(15)

            second_verify_result = _verify_terminal_login(terminal_exe)
            if second_verify_result is False:
                raise RuntimeError("MT5 login verification failed after fallback AppData retry")
            if second_verify_result is None:
                current_app.logger.info(
                    "MT5 Python API unavailable after fallback for mt5_account_id=%s; trusting AppData hash %s",
                    mt5_account_id,
                    actual_hash,
                )
            _stop_process(proc)
            proc = None

        account.terminal_path = terminal_exe
        account.appdata_hash = actual_hash
        account.is_active = True
        db.session.commit()

        return {
            "terminal_path": terminal_exe,
            "appdata_hash": actual_hash,
            "status": "setup complete",
            "hash_verified": hash_verified,
        }
    except PermanentSetupError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        raise self.retry(exc=exc)
    finally:
        _stop_process(proc)
