import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import shutil
import subprocess
import time
import logging
from datetime import datetime, timezone

from celery_app import celery
from celery_workers.logging_utils import duration_label, log_ascii_table
from celery_workers.mt5_market_watch import (
    MT5_MARKET_WATCH_CRYPTO_SEED_SYMBOLS,
    MT5_MARKET_WATCH_FALLBACK_SYMBOLS,
)
from helpers.mt5_copy import (
    EXNESS_MT5_SETUP_EMAIL_NOTE_PARAGRAPHS,
    EXNESS_MT5_SETUP_EMAIL_NOTE_TEXT,
    is_exness_mt5_account,
)


logger = logging.getLogger(__name__)


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
MT5_SETUP_VERIFY_ATTEMPTS = 2
MT5_SETUP_VERIFY_RETRY_DELAY_SECONDS = 2


def _retry_with_backoff(task, exc, *, base_delay=30, max_delay=300):
    retry_number = getattr(getattr(task, "request", None), "retries", 0)
    countdown = min(base_delay * (2 ** retry_number), max_delay)
    raise task.retry(exc=exc, countdown=countdown)



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


def _terminate_mt5_processes(terminal_exe: str):
    normalized_terminal_exe = os.path.normcase(os.path.abspath(terminal_exe))

    try:
        import psutil
    except ImportError:
        _terminate_mt5_processes_via_powershell(normalized_terminal_exe)
        return

    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            proc_exe = proc.info.get("exe")
            if proc_exe and os.path.normcase(os.path.abspath(proc_exe)) == normalized_terminal_exe:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except psutil.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except (OSError, psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass


def _terminate_mt5_processes_via_powershell(terminal_exe: str):
    script = (
        "$target = [System.IO.Path]::GetFullPath($env:FXJ_TERMINAL_EXE).ToLowerInvariant(); "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -and "
        "([System.IO.Path]::GetFullPath($_.ExecutablePath)).ToLowerInvariant() -eq $target } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    env = dict(os.environ)
    env["FXJ_TERMINAL_EXE"] = terminal_exe
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            check=False,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except OSError:
        pass


def _resolve_cleanup_appdata_folder(terminal_dir: str, appdata_hash: str):
    normalized_terminal_dir = os.path.normcase(os.path.abspath(terminal_dir))
    normalized_hash = (appdata_hash or "").strip().upper()
    if re.fullmatch(r"[A-F0-9]{32}", normalized_hash):
        candidate = os.path.join(APPDATA_TERMINAL_PATH, normalized_hash)
        if os.path.isdir(candidate):
            if not str(terminal_dir or "").strip():
                return candidate
            origin = os.path.join(candidate, "origin.txt")
            try:
                content = open(origin, encoding="utf-16", errors="ignore").read().strip().rstrip("\\")
            except OSError:
                return candidate
            if os.path.normcase(content) == normalized_terminal_dir:
                return candidate
    return _find_base_appdata(terminal_dir)


def _clear_chart_profiles(appdata_path: str):
    charts_path = os.path.join(appdata_path, "MQL5", "Profiles", "Charts")
    if os.path.isdir(charts_path):
        shutil.rmtree(charts_path, ignore_errors=True)
    os.makedirs(charts_path, exist_ok=True)

def _find_child_dir(parent_path: str, dir_name: str):
    if not os.path.isdir(parent_path):
        return None
    target_name = os.path.normcase(dir_name.strip())
    for entry in os.scandir(parent_path):
        if entry.is_dir() and os.path.normcase(entry.name) == target_name:
            return entry.path
    return None


def _clear_market_watch_selection(appdata_path: str, server_name: str):
    bases_path = _find_child_dir(appdata_path, "bases")
    if bases_path is None:
        return

    server_path = _find_child_dir(bases_path, server_name)
    if server_path is None:
        return

    symbols_path = _find_child_dir(server_path, "Symbols")
    if symbols_path is None:
        return

    for entry in os.scandir(symbols_path):
        if entry.is_file() and entry.name.lower().startswith("selected") and entry.name.lower().endswith(".dat"):
            try:
                os.remove(entry.path)
            except OSError:
                pass


def _seed_market_watch_symbols(
    mt5,
    *,
    terminal_exe: str,
    login: int,
    investor_password: str,
    server: str,
):
    """
    Best-effort MarketWatch seeding for symbols used by broker-time probing.

    Prefer a few common 24/7 crypto names first so weekend/off-hours broker-time
    probes have something live to read. If none of those symbols exist on the
    broker, fall back to liquid baseline symbols.
    """
    symbol_select = getattr(mt5, "symbol_select", None)
    if not callable(symbol_select):
        return {
            "selected_symbols": [],
            "attempted_symbols": [],
            "used_fallback": False,
            "symbol_select_available": False,
        }

    attempted_symbols = []
    selected_symbols = []
    used_fallback = False

    if not mt5.initialize(path=terminal_exe, timeout=60000):
        raise RuntimeError(f"mt5.initialize() failed during MarketWatch seed: {_mt5_last_error(mt5)}")

    try:
        if not mt5.login(login, password=investor_password, server=server):
            raise RuntimeError(f"mt5.login() failed during MarketWatch seed: {_mt5_last_error(mt5)}")

        for symbol in MT5_MARKET_WATCH_CRYPTO_SEED_SYMBOLS:
            selected = False
            try:
                selected = bool(symbol_select(symbol, True))
            except Exception:
                selected = False
            attempted_symbols.append({"symbol": symbol, "selected": selected, "group": "crypto"})
            if selected:
                selected_symbols.append(symbol)

        if not selected_symbols:
            used_fallback = True
            for symbol in MT5_MARKET_WATCH_FALLBACK_SYMBOLS:
                selected = False
                try:
                    selected = bool(symbol_select(symbol, True))
                except Exception:
                    selected = False
                attempted_symbols.append({"symbol": symbol, "selected": selected, "group": "fallback"})
                if selected:
                    selected_symbols.append(symbol)
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass

    return {
        "selected_symbols": selected_symbols,
        "attempted_symbols": attempted_symbols,
        "used_fallback": used_fallback,
        "symbol_select_available": True,
    }


class PermanentSetupError(RuntimeError):
    """Raised when setup is running on the wrong host or missing required local config."""


class TradingPasswordDetectedError(Exception):
    """
    Raised when account_info().trade_allowed is True after a successful MT5 login,
    indicating the user submitted a master/trading password instead of the required
    investor (read-only) password. Never retried.
    """


def _classify_setup_error(error_str: str) -> str:
    """Map a raw MT5 error string to a user-facing connection error message."""
    s = str(error_str).lower()
    if any(k in s for k in ("trading/master password", "trading password", "trade_allowed")):
        return (
            "A master/trading password was detected and rejected. "
            "Please resubmit using your MT5 investor (read-only) password."
        )
    if "wrong account" in s or ("expected" in s and "got" in s):
        return (
            "Account mismatch. Check that the account number you entered matches your MT5 login."
        )
    if any(k in s for k in ("auth_failed", "auth failed", "authorization", "invalid password", "invalid account")):
        return "Login failed. Please check your account number and investor password."
    if any(k in s for k in ("no_ipc", "no ipc", "ipc connection", "timeout", "timed out", "connection refused", "network")):
        return "Connection timeout. Check that the server name is exact (e.g. Exness-MT5Real) and try again."
    if any(k in s for k in ("unsupported", "version", "not found", "invalid server", "res_x")):
        return "Invalid server. Enter the exact MT5 server name from your broker (e.g. Exness-MT5Real, ICMarketsSC-Live)."
    return "Connection failed. Check your server name, account number, and investor password, then retry."


def _attempt_setup_failover(
    *,
    mt5_account_id,
    current_target_vm_id,
    setup_attempt_index,
    allow_failover,
    terminal_exe,
    appdata_hash,
    exc,
):
    if not allow_failover:
        return None
    from helpers.mt5_dispatch import (
        current_vm_id,
        dispatch_mt5_cleanup,
        dispatch_mt5_setup,
        is_setup_failover_eligible_error,
        next_setup_failover_vm_id,
        resolve_setup_target_vm_id,
    )

    if not is_setup_failover_eligible_error(exc):
        return None

    resolved_current_vm_id = resolve_setup_target_vm_id(current_target_vm_id)
    next_vm_id = next_setup_failover_vm_id(setup_attempt_index=setup_attempt_index)
    if not next_vm_id:
        return None

    if terminal_exe or appdata_hash:
        try:
            dispatch_mt5_cleanup(
                cleanup_mt5_terminal,
                terminal_exe or "",
                appdata_hash or "",
                account_vm_id=current_vm_id(),
                label="mt5_setup_failover_cleanup",
                extra={"mt5_account_id": mt5_account_id},
            )
        except Exception as cleanup_exc:
            logger.warning(
                "MT5 setup failover cleanup queue failed mt5_account_id=%s: %s",
                mt5_account_id,
                cleanup_exc,
            )

    try:
        from models import MT5Account, db

        account = db.session.get(MT5Account, mt5_account_id)
        if account is not None:
            account.terminal_path = None
            account.appdata_hash = None
            account.vm_id = None
            db.session.commit()
    except Exception as clear_exc:
        db.session.rollback()
        logger.warning(
            "MT5 setup failover partial state clear failed mt5_account_id=%s: %s",
            mt5_account_id,
            clear_exc,
        )

    dispatch_mt5_setup(
        setup_mt5_terminal,
        mt5_account_id,
        target_vm_id=next_vm_id,
        allow_failover=True,
        setup_attempt_index=max(int(setup_attempt_index or 0), 0) + 1,
        label="mt5_setup_failover",
        extra={
            "mt5_account_id": mt5_account_id,
            "from_vm_id": resolved_current_vm_id,
            "next_vm_id": next_vm_id,
            "error": str(exc),
        },
    )
    logger.warning(
        "MT5 setup failovered mt5_account_id=%s from_vm_id=%s next_vm_id=%s error=%s",
        mt5_account_id,
        resolved_current_vm_id,
        next_vm_id,
        exc,
    )
    return {"failover": True, "next_vm_id": next_vm_id}


def _write_mt5_account_status(mt5_account_id: int, status: str, error_message=None):
    """Write connection_status (and optional error message) to the MT5Account row."""
    from models import MT5Account, db

    try:
        account = db.session.get(MT5Account, mt5_account_id)
        if account is None:
            return
        account.connection_status = status
        account.connection_error_message = error_message
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "MT5 status write failed mt5_account_id=%s status=%s error=%s",
            mt5_account_id,
            status,
            exc,
        )


def _mask_account_number_for_log(account_number):
    text_value = str(account_number or "").strip()
    if not text_value:
        return "unknown"
    suffix = text_value[-4:] if len(text_value) >= 4 else text_value
    return f"...{suffix}"


def _send_mt5_ready_email(account):
    user = getattr(account, "user", None)
    if user is None or not getattr(user, "email", None):
        return

    from auth_account import get_public_base_url, render_app_template, send_email_placeholder

    try:
        trade_account = getattr(account, "trade_account", None)
        account_name = getattr(trade_account, "name", None) or f"MT5 account {account.account_number}"
        base_url = get_public_base_url()
        html_body = render_app_template(
            "emails/mt5-ready.html",
            name=user.username,
            account_name=account_name,
            account_number=account.account_number,
            dashboard_url=f"{base_url}/dashboard",
            logo_url=f"{base_url}/static/site-logo.png",
        )
        send_email_placeholder(
            user.email,
            "Your MT5 sync is ready",
            (
                f"Hi {user.username}, your MT5 sync is ready for {account.account_number}. "
                "Open your dashboard any time to review the account and let the journal keep syncing automatically."
            ),
            html_body=html_body,
        )
    except Exception as exc:
        log_ascii_table(
            logger,
            "MT5 Ready Email Failed",
            [
                ("MT5 Account ID", account.id),
                ("User ID", user.id),
                ("Account", _mask_account_number_for_log(getattr(account, "account_number", None))),
                ("Error", exc),
            ],
            level=logging.WARNING,
        )


def _send_mt5_setup_failed_email(account, user_message: str):
    user = getattr(account, "user", None)
    if user is None or not getattr(user, "email", None):
        return

    from auth_account import get_public_base_url, render_app_template, send_email_placeholder

    try:
        base_url = get_public_base_url()
        show_exness_note = is_exness_mt5_account(account)
        html_body = render_app_template(
            "emails/mt5-setup-failed.html",
            name=user.username,
            error_message=user_message,
            dashboard_url=f"{base_url}/dashboard",
            logo_url=f"{base_url}/static/site-logo.png",
            exness_note_paragraphs=(
                EXNESS_MT5_SETUP_EMAIL_NOTE_PARAGRAPHS if show_exness_note else ()
            ),
        )
        if show_exness_note:
            subject = "MT5 connection setup needs attention"
            text_body = (
                f"Hi {user.username}, we weren't able to connect to your MT5 account. "
                f"{user_message}\n\n"
                f"{EXNESS_MT5_SETUP_EMAIL_NOTE_TEXT}\n\n"
                "Open the dashboard to review the status."
            )
        else:
            subject = "MT5 connection setup failed - fix & retry"
            text_body = (
                f"Hi {user.username}, we weren't able to connect to your MT5 account. "
                f"{user_message} "
                "Open the dashboard to fix your details and retry."
            )
        send_email_placeholder(
            user.email,
            subject,
            text_body,
            html_body=html_body,
        )
    except Exception as exc:
        log_ascii_table(
            logger,
            "MT5 Setup Failed Email Failed",
            [
                ("MT5 Account ID", account.id),
                ("User ID", user.id),
                ("Error", exc),
            ],
            level=logging.WARNING,
        )


def _send_trading_password_warning_email(account):
    user = getattr(account, "user", None)
    if user is None or not getattr(user, "email", None):
        return

    from auth_account import get_public_base_url, send_email_placeholder

    try:
        base_url = get_public_base_url()
        dashboard_url = f"{base_url}/dashboard"
        subject = "Action required: MT5 trading password detected — your credentials were cleared"
        text_body = (
            f"Hi {user.username},\n\n"
            "We detected that the password you submitted for MT5 sync was a "
            "MASTER or TRADING password, not an investor (read-only) password.\n\n"
            "To protect your account, we have:\n"
            "  1. Immediately terminated the MT5 connection.\n"
            "  2. Deleted the stored password from our system.\n"
            "  3. Removed the terminal files from our server.\n\n"
            "WHAT YOU NEED TO DO:\n"
            "  - Open your MT5 platform and locate your INVESTOR password "
            "(not your master / trading password).\n"
            "  - In MT5: right-click your account -> Manage Account -> "
            "you will find the investor password there.\n"
            f"  - Return to your dashboard and re-enter your details: {dashboard_url}\n\n"
            "If you are unsure which password to use, contact your broker. "
            "The investor password is read-only and cannot place trades. "
            "Never share your master/trading password with any third-party service.\n\n"
            "MyFXJournal"
        )
        send_email_placeholder(user.email, subject, text_body)
    except Exception as exc:
        log_ascii_table(
            logger,
            "MT5 Trading Password Warning Email Failed",
            [
                ("MT5 Account ID", account.id),
                ("User ID", user.id),
                ("Error", exc),
            ],
            level=logging.WARNING,
        )


def _send_mt5_setup_failed_admin_alert(account, user_message: str, raw_error, *,
                                       task_id=None, kind="setup_failed"):
    """
    Send an MT5 setup failure alert to the ERROR_LOG_TO_EMAIL address.
    Never raises — swallowed + logged so it can't break the task flow.
    """
    from auth_account import send_error_log_email

    try:
        user = getattr(account, "user", None)
        user_email = getattr(user, "email", None) or "unknown"
        user_id = getattr(user, "id", None)
        account_number = _mask_account_number_for_log(getattr(account, "account_number", None))
        server = getattr(account, "server", None) or "unknown"

        subject = f"[MT5 {kind}] user={user_email} account={account_number} server={server}"
        body = (
            f"MT5 setup failure ({kind})\n"
            f"Time (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"Task ID: {task_id or 'unknown'}\n"
            f"MT5 Account ID: {getattr(account, 'id', None)}\n"
            f"User ID: {user_id}\n"
            f"User Email: {user_email}\n"
            f"Trade Account ID: {getattr(account, 'trade_account_id', None)}\n"
            f"Account (masked): {account_number}\n"
            f"Server: {server}\n"
            f"\n"
            f"User-facing message:\n  {user_message}\n"
            f"\n"
            f"Raw error:\n  {raw_error!r}\n"
        )
        send_error_log_email(subject=subject, body=body)
    except Exception as exc:
        logger.warning(
            "MT5 setup admin alert email failed mt5_account_id=%s kind=%s error=%s",
            getattr(account, "id", None),
            kind,
            exc,
        )


def _mt5_last_error(mt5):
    last_error = getattr(mt5, "last_error", None)
    if callable(last_error):
        try:
            return last_error()
        except Exception as exc:
            return f"last_error() failed: {exc}"
    return "last_error unavailable"


def _verify_mt5_terminal_login(
    mt5,
    *,
    terminal_exe: str,
    login: int,
    investor_password: str,
    server: str,
    task_id=None,
    mt5_account_id=None,
    user_id=None,
    trade_account_id=None,
):
    for attempt in range(1, MT5_SETUP_VERIFY_ATTEMPTS + 1):
        retry_in_seconds = None
        try:
            result = mt5.initialize(
                path=terminal_exe,
                login=login,
                password=investor_password,
                server=server,
                timeout=60000,
            )

            if not result:
                error = _mt5_last_error(mt5)
                raise RuntimeError(f"mt5.initialize() failed: {error}")

            account_info = mt5.account_info()
            if account_info is None:
                error = _mt5_last_error(mt5)
                raise RuntimeError(f"mt5.account_info() returned None: {error}")

            actual_login = getattr(account_info, "login", None)
            if actual_login != login:
                raise RuntimeError(
                    f"Wrong account logged in: expected {login}, got {actual_login}"
                )

            # Investor (read-only) password always yields trade_allowed=False.
            # If True, the user submitted a master/trading password — reject immediately.
            if getattr(account_info, "trade_allowed", False):
                raise TradingPasswordDetectedError(
                    f"trading/master password detected for login {_mask_account_number_for_log(login)} "
                    f"on server {server}"
                )

            return
        except TradingPasswordDetectedError:
            raise  # propagate immediately, do not retry inside the loop
        except RuntimeError as exc:
            if attempt >= MT5_SETUP_VERIFY_ATTEMPTS:
                raise

            retry_in_seconds = MT5_SETUP_VERIFY_RETRY_DELAY_SECONDS
            log_ascii_table(
                logger,
                "MT5 Setup Verification Retry",
                [
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("User ID", user_id),
                    ("Trade Account ID", trade_account_id),
                    ("Account", _mask_account_number_for_log(login)),
                    ("Server", server),
                    ("Attempt", f"{attempt}/{MT5_SETUP_VERIFY_ATTEMPTS}"),
                    ("Retry In", f"{retry_in_seconds}s"),
                    ("Error", exc),
                ],
                level=logging.WARNING,
            )
        finally:
            try:
                mt5.shutdown()
            except Exception:
                pass
        if retry_in_seconds:
            time.sleep(retry_in_seconds)


@celery.task(bind=True, max_retries=2, default_retry_delay=30, queue="mt5_setup")
def setup_mt5_terminal(
    self,
    mt5_account_id: int,
    target_vm_id=None,
    allow_failover=True,
    setup_attempt_index=0,
    _wrong_vm_redispatch_count=0,
):
    """
    Set up a new MT5 terminal for a user account.
    Copies base MT5 installation and verifies login via Python API.
    Windows only - fails permanently on other platforms.
    """
    from helpers.utils import decrypt_password
    from models import MT5Account, db

    task_id = getattr(getattr(self, "request", None), "id", None)
    started_at = datetime.now(timezone.utc)
    finished_at = None
    user_id = None
    trade_account_id = None
    login = None
    server = None
    terminal_dir = None
    terminal_exe = None
    was_active = None
    appdata_hash = None
    post_bootstrap = False

    try:
        if os.name != "nt":
            raise PermanentSetupError("setup_mt5_terminal requires Windows")

        if not os.path.exists(MT5_BASE_PATH):
            raise PermanentSetupError(
                f"MT5_BASE_PATH not found: {MT5_BASE_PATH}"
            )

        account = db.session.get(MT5Account, mt5_account_id)
        if account is None:
            finished_at = datetime.now(timezone.utc)
            log_ascii_table(
                logger,
                "MT5 Setup Result",
                [
                    ("Finished", finished_at),
                    ("Duration", duration_label(started_at, finished_at)),
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("Status", "account missing"),
                ],
                level=logging.WARNING,
            )
            return {"error": "MT5Account not found"}
        if account.is_orphaned:
            raise PermanentSetupError("setup_mt5_terminal cannot run for an orphaned MT5 account")
        if getattr(account, "cleanup_marked_at", None) is not None:
            raise PermanentSetupError("setup_mt5_terminal cannot run while MT5 cleanup is pending")
        if not account.has_saved_credentials:
            raise PermanentSetupError("setup_mt5_terminal cannot run without saved MT5 credentials")

        from helpers.mt5_dispatch import (
            guard_wrong_vm_task,
            resolve_setup_target_vm_id,
        )

        resolved_target_vm_id = resolve_setup_target_vm_id(target_vm_id)
        guard_result = guard_wrong_vm_task(
            self,
            target_vm_id=resolved_target_vm_id,
            redispatch=lambda queue, kwargs: setup_mt5_terminal.apply_async(
                args=[mt5_account_id],
                kwargs={
                    "allow_failover": allow_failover,
                    "setup_attempt_index": setup_attempt_index,
                    **kwargs,
                },
                queue=queue,
            ),
        )
        if guard_result is not None:
            return guard_result

        was_active = bool(account.is_active)
        user_id = account.user_id
        trade_account_id = account.trade_account_id
        login = int(account.account_number)
        investor_password = decrypt_password(account.investor_password_encrypted)
        server = account.server

        from helpers.mt5_terminal_paths import (
            build_mt5_terminal_exe_path,
            resolve_mt5_terminal_dir,
        )

        terminal_dir = resolve_mt5_terminal_dir(
            terminals_root=MT5_TERMINALS_ROOT,
            user_id=user_id,
            trade_account_id=trade_account_id,
            terminal_path=account.terminal_path,
        )
        terminal_exe = build_mt5_terminal_exe_path(terminal_dir)
        log_ascii_table(
            logger,
            "MT5 Setup Context",
            [
                ("Started", started_at),
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("User ID", user_id),
                ("Trade Account ID", trade_account_id),
                ("Account", _mask_account_number_for_log(login)),
                ("Server", server),
                ("Base Path", MT5_BASE_PATH),
                ("Terminal Folder", os.path.basename(terminal_dir)),
                ("Terminal Dir", terminal_dir),
                ("Was Active", was_active),
            ],
        )

        if not os.path.exists(terminal_dir):
            shutil.copytree(
                MT5_BASE_PATH,
                terminal_dir,
            )

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

        # Launch the terminal briefly — this causes MT5 to create its AppData folder
        proc = subprocess.Popen([terminal_exe], cwd=terminal_dir)

        new_appdata = None
        try:
            # Wait up to 30s for the AppData folder to appear, detected via origin.txt
            # (works even if the folder already existed from a prior run)
            deadline = time.time() + 30
            while time.time() < deadline:
                new_appdata = _find_base_appdata(terminal_dir)
                if new_appdata:
                    break
                time.sleep(2)

            if new_appdata is None:
                raise PermanentSetupError(
                    "MT5 AppData folder not created after launch — check MT5 installation"
                )

            new_hash = os.path.basename(new_appdata)

            # Copy servers.dat from base AppData so the new terminal knows
            # how to resolve the broker server address
            src_servers = os.path.join(base_appdata, "config", "servers.dat")
            dst_config = os.path.join(new_appdata, "config")
            terminal_config = os.path.join(terminal_dir, "config")
            os.makedirs(dst_config, exist_ok=True)
            os.makedirs(terminal_config, exist_ok=True)
            if os.path.exists(src_servers):
                shutil.copy2(src_servers, os.path.join(dst_config, "servers.dat"))
                shutil.copy2(src_servers, os.path.join(terminal_config, "servers.dat"))

            post_bootstrap = True

            account.appdata_hash = new_hash
            appdata_hash = new_hash
        finally:
            proc.terminate()
            proc.wait(timeout=10)

        # Clear the default chart workspace after the bootstrap launch and
        # before the Python API logs into the account.
        _clear_chart_profiles(new_appdata)

        import MetaTrader5 as mt5

        _verify_mt5_terminal_login(
            mt5,
            terminal_exe=terminal_exe,
            login=login,
            investor_password=investor_password,
            server=server,
            task_id=task_id,
            mt5_account_id=mt5_account_id,
            user_id=user_id,
            trade_account_id=trade_account_id,
        )

        _clear_market_watch_selection(new_appdata, server)
        try:
            market_watch_seed_result = _seed_market_watch_symbols(
                mt5,
                terminal_exe=terminal_exe,
                login=login,
                investor_password=investor_password,
                server=server,
            )
            log_ascii_table(
                logger,
                "MT5 MarketWatch Seed",
                [
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("Account", _mask_account_number_for_log(login)),
                    ("Server", server),
                    ("Crypto Seed Set", ", ".join(MT5_MARKET_WATCH_CRYPTO_SEED_SYMBOLS)),
                    ("Fallback Seed Set", ", ".join(MT5_MARKET_WATCH_FALLBACK_SYMBOLS)),
                    (
                        "Selected Symbols",
                        ", ".join(market_watch_seed_result.get("selected_symbols") or []) or "-",
                    ),
                    ("Used Fallback", market_watch_seed_result.get("used_fallback")),
                    ("Selection API", market_watch_seed_result.get("symbol_select_available")),
                ],
                level=logging.INFO,
            )
        except Exception as exc:
            log_ascii_table(
                logger,
                "MT5 MarketWatch Seed Failed",
                [
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("Account", _mask_account_number_for_log(login)),
                    ("Server", server),
                    ("Error", exc),
                ],
                level=logging.WARNING,
            )

        account.terminal_path = terminal_exe
        account.is_active = True
        account.connection_status = "connected"
        account.connection_error_message = None
        from celery_workers.worker_monitor import get_vm_id

        account.vm_id = get_vm_id()
        db.session.commit()
        try:
            from helpers.mt5_server_seed_shortlist import (
                resolve_mt5_server_seed_shortlist_on_success,
            )

            resolve_mt5_server_seed_shortlist_on_success(server_name=server)
        except Exception as shortlist_exc:
            logger.warning(
                "MT5 server seed shortlist resolve skipped mt5_account_id=%s server=%s error=%s",
                mt5_account_id,
                server,
                shortlist_exc,
            )
        if not was_active:
            _send_mt5_ready_email(account)

        finished_at = datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "MT5 Setup Result",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("User ID", user_id),
                ("Trade Account ID", trade_account_id),
                ("Account", _mask_account_number_for_log(login)),
                ("Server", server),
                ("Terminal Path", terminal_exe),
                ("AppData Hash", appdata_hash or getattr(account, "appdata_hash", None)),
                ("Activated", not was_active),
                ("Status", "setup complete"),
            ],
        )

        return {
            "terminal_path": terminal_exe,
            "status": "setup complete",
            "account": login,
        }
    except TradingPasswordDetectedError as exc:
        db.session.rollback()
        finished_at = datetime.now(timezone.utc)

        user_message = (
            "A master/trading password was detected and rejected. "
            "For your security the connection was terminated and your credentials were cleared. "
            "Please resubmit using your MT5 investor (read-only) password — "
            "not your master or trading password."
        )

        # 1. Clear the stored password immediately and record the failure.
        try:
            account = db.session.get(MT5Account, mt5_account_id)
            if account is not None:
                account.investor_password_encrypted = None
                account.connection_status = "failed"
                account.connection_error_message = user_message
                db.session.commit()
        except Exception as clear_exc:
            db.session.rollback()
            logger.warning(
                "MT5 trading password — failed to clear credentials mt5_account_id=%s: %s",
                mt5_account_id,
                clear_exc,
            )

        # 2. Queue terminal file cleanup (same path as account delete).
        if terminal_exe:
            try:
                from helpers.mt5_dispatch import current_vm_id, dispatch_mt5_cleanup

                dispatch_mt5_cleanup(
                    cleanup_mt5_terminal,
                    terminal_exe,
                    appdata_hash or "",
                    account_vm_id=current_vm_id(),
                    label="mt5_setup_trading_password_cleanup",
                    extra={"mt5_account_id": mt5_account_id},
                )
            except Exception as cleanup_exc:
                logger.warning(
                    "MT5 trading password — terminal cleanup queue failed mt5_account_id=%s: %s",
                    mt5_account_id,
                    cleanup_exc,
                )

        # 3. Email the user + admin.
        try:
            account_for_email = db.session.get(MT5Account, mt5_account_id)
            if account_for_email is not None:
                _send_trading_password_warning_email(account_for_email)
                _send_mt5_setup_failed_admin_alert(
                    account_for_email,
                    user_message,
                    exc,
                    task_id=task_id,
                    kind="trading_password_detected",
                )
        except Exception as email_exc:
            logger.warning(
                "MT5 trading password — warning email failed mt5_account_id=%s: %s",
                mt5_account_id,
                email_exc,
            )

        log_ascii_table(
            logger,
            "MT5 Setup — Trading Password Detected",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("User ID", user_id),
                ("Trade Account ID", trade_account_id),
                ("Account", _mask_account_number_for_log(login)),
                ("Server", server),
                ("Action", "password cleared, terminal cleanup queued, user+admin emailed"),
            ],
            level=logging.ERROR,
        )
        raise  # no retry

    except PermanentSetupError as exc:
        db.session.rollback()
        finished_at = datetime.now(timezone.utc)
        failover_result = _attempt_setup_failover(
            mt5_account_id=mt5_account_id,
            current_target_vm_id=target_vm_id,
            setup_attempt_index=setup_attempt_index,
            allow_failover=allow_failover,
            terminal_exe=terminal_exe,
            appdata_hash=appdata_hash,
            exc=exc,
        )
        if failover_result is not None:
            log_ascii_table(
                logger,
                "MT5 Setup Failover",
                [
                    ("Finished", finished_at),
                    ("Duration", duration_label(started_at, finished_at)),
                    ("Task ID", task_id),
                    ("MT5 Account ID", mt5_account_id),
                    ("Next VM ID", failover_result.get("next_vm_id")),
                    ("Error", exc),
                ],
                level=logging.WARNING,
            )
            return failover_result
        log_ascii_table(
            logger,
            "MT5 Setup Failed",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("User ID", user_id),
                ("Trade Account ID", trade_account_id),
                ("Account", _mask_account_number_for_log(login)),
                ("Server", server),
                ("Terminal Dir", terminal_dir),
                ("Error", exc),
            ],
            level=logging.ERROR,
        )
        logger.error(
            "MT5 setup failed permanently. task_id=%s mt5_account_id=%s user_id=%s trade_account_id=%s",
            task_id,
            mt5_account_id,
            user_id,
            trade_account_id,
        )
        raise
    except Exception as exc:
        db.session.rollback()
        finished_at = datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "MT5 Setup Failed",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("User ID", user_id),
                ("Trade Account ID", trade_account_id),
                ("Account", _mask_account_number_for_log(login)),
                ("Server", server),
                ("Terminal Dir", terminal_dir),
                ("Error", exc),
            ],
            level=logging.ERROR,
        )
        current_retry = getattr(getattr(self, "request", None), "retries", 0)
        at_max_retries = current_retry >= getattr(self, "max_retries", 2)
        if at_max_retries:
            failover_result = _attempt_setup_failover(
                mt5_account_id=mt5_account_id,
                current_target_vm_id=target_vm_id,
                setup_attempt_index=setup_attempt_index,
                allow_failover=allow_failover,
                terminal_exe=terminal_exe,
                appdata_hash=appdata_hash,
                exc=exc,
            )
            if failover_result is not None:
                log_ascii_table(
                    logger,
                    "MT5 Setup Failover",
                    [
                        ("Finished", finished_at),
                        ("Duration", duration_label(started_at, finished_at)),
                        ("Task ID", task_id),
                        ("MT5 Account ID", mt5_account_id),
                        ("Next VM ID", failover_result.get("next_vm_id")),
                        ("Error", exc),
                    ],
                    level=logging.WARNING,
                )
                return failover_result
            logger.error(
                "MT5 setup exhausted retries — marking failed. task_id=%s mt5_account_id=%s user_id=%s",
                task_id,
                mt5_account_id,
                user_id,
            )
            user_message = _classify_setup_error(str(exc))
            _write_mt5_account_status(mt5_account_id, "failed", user_message)
            try:
                failed_account = db.session.get(MT5Account, mt5_account_id)
                if failed_account is not None:
                    _send_mt5_setup_failed_email(failed_account, user_message)
                    _send_mt5_setup_failed_admin_alert(
                        failed_account,
                        user_message,
                        exc,
                        task_id=task_id,
                        kind="setup_failed",
                    )
            except Exception as email_exc:
                logger.warning(
                    "MT5 setup failed email error mt5_account_id=%s: %s",
                    mt5_account_id,
                    email_exc,
                )
            if server and post_bootstrap:
                try:
                    from helpers.mt5_server_seed_shortlist import (
                        is_possible_missing_servers_dat_failure,
                        record_possible_servers_dat_shortlist,
                    )

                    if is_possible_missing_servers_dat_failure(
                        str(exc),
                        post_bootstrap=True,
                    ):
                        record_possible_servers_dat_shortlist(
                            server_name=server,
                            error_message=str(exc),
                            mt5_account_id=mt5_account_id,
                        )
                except Exception as shortlist_exc:
                    logger.warning(
                        "MT5 server seed shortlist record skipped mt5_account_id=%s server=%s error=%s",
                        mt5_account_id,
                        server,
                        shortlist_exc,
                    )
        else:
            logger.exception(
                "MT5 setup failed and will retry. task_id=%s mt5_account_id=%s user_id=%s trade_account_id=%s",
                task_id,
                mt5_account_id,
                user_id,
                trade_account_id,
            )
        _retry_with_backoff(self, exc, base_delay=30, max_delay=300)


@celery.task(bind=True, max_retries=2, default_retry_delay=30, queue="mt5_setup")
def pause_mt5_terminal_process(self, mt5_account_id: int, target_vm_id=None, _wrong_vm_redispatch_count=0):
    """
    Stops the MT5 terminal process for a trial-expired account.

    Preserves ALL of: terminal files, AppData folder, MT5Account row,
    terminal_path, appdata_hash, investor_password_encrypted, is_active.
    This is NOT archive/cleanup — nothing is deleted.

    Fast reactivation: restart the terminal and clear sync_paused_at on upgrade.
    """
    task_id = getattr(getattr(self, "request", None), "id", None)
    started_at = datetime.now(timezone.utc)
    finished_at = None

    from models import MT5Account, db

    account = db.session.get(MT5Account, mt5_account_id)
    if account is None:
        log_ascii_table(
            logger,
            "MT5 Trial Pause Skipped",
            [
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Reason", "account not found"),
            ],
            level=logging.WARNING,
        )
        return {"skipped": "account not found"}

    from helpers.mt5_dispatch import guard_wrong_vm_task

    guard_result = guard_wrong_vm_task(
        self,
        target_vm_id=target_vm_id or account.vm_id,
        redispatch=lambda queue, kwargs: pause_mt5_terminal_process.apply_async(
            args=[mt5_account_id],
            kwargs=kwargs,
            queue=queue,
        ),
    )
    if guard_result is not None:
        return guard_result

    terminal_path = account.terminal_path
    log_ascii_table(
        logger,
        "MT5 Trial Pause Context",
        [
            ("Task ID", task_id),
            ("MT5 Account ID", mt5_account_id),
            ("User ID", account.user_id),
            ("Trade Account ID", account.trade_account_id),
            ("Terminal Path", terminal_path or "none"),
            ("Sync Paused At", account.sync_paused_at or "not set"),
            ("Pause Reason", account.sync_pause_reason or "not set"),
        ],
    )

    if os.name != "nt":
        finished_at = datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "MT5 Trial Pause Result",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("MT5 Account ID", mt5_account_id),
                ("Status", "not Windows, process termination skipped"),
            ],
            level=logging.WARNING,
        )
        return {"status": "not Windows, process termination skipped"}

    process_terminated = False
    if terminal_path and os.path.exists(terminal_path):
        try:
            _terminate_mt5_processes(terminal_path)
            process_terminated = True
        except Exception as term_exc:
            logger.warning(
                "MT5 trial pause: process termination failed mt5_account_id=%s: %s",
                mt5_account_id,
                term_exc,
            )
    elif terminal_path:
        logger.info(
            "MT5 trial pause: terminal exe not found, nothing to terminate mt5_account_id=%s path=%s",
            mt5_account_id,
            terminal_path,
        )

    # Ensure sync_paused_at is stamped on the account row (may already be set
    # by the sync task before dispatching this pause task).
    try:
        refreshed_account = db.session.get(MT5Account, mt5_account_id)
        if refreshed_account is not None and not refreshed_account.sync_paused_at:
            refreshed_account.sync_paused_at = datetime.now(timezone.utc).replace(tzinfo=None)
            refreshed_account.sync_pause_reason = refreshed_account.sync_pause_reason or "trial_expired"
            db.session.commit()
    except Exception as db_exc:
        db.session.rollback()
        logger.warning(
            "MT5 trial pause: DB stamp failed mt5_account_id=%s: %s",
            mt5_account_id,
            db_exc,
        )

    finished_at = datetime.now(timezone.utc)
    log_ascii_table(
        logger,
        "MT5 Trial Pause Result",
        [
            ("Finished", finished_at),
            ("Duration", duration_label(started_at, finished_at)),
            ("Task ID", task_id),
            ("MT5 Account ID", mt5_account_id),
            ("Terminal Path", terminal_path or "none"),
            ("Process Terminated", process_terminated),
            ("Status", "trial pause complete"),
        ],
    )
    return {
        "terminal_path": terminal_path,
        "process_terminated": process_terminated,
        "status": "trial pause complete",
    }


@celery.task(bind=True, max_retries=2, default_retry_delay=10, queue="mt5_setup")
def cleanup_mt5_terminal(
    self,
    terminal_path: str,
    appdata_hash: str,
    mt5_account_id: int | None = None,
    delete_account_row: bool = True,
    clear_cleanup_mark: bool = False,
    cleanup_marked_at=None,
    target_vm_id=None,
    _wrong_vm_redispatch_count=0,
):
    """
    Clean up MT5 terminal files when an MT5Account is deleted.
    Runs on VM only - kills process, deletes terminal folder
    and AppData hash folder.
    """
    task_id = getattr(getattr(self, "request", None), "id", None)
    started_at = datetime.now(timezone.utc)
    finished_at = None
    terminal_dir = os.path.dirname(terminal_path) if terminal_path else ""
    appdata_folder = None
    db_deleted = False
    cleanup_mark_cleared = False

    try:
        if os.name != "nt":
            finished_at = datetime.now(timezone.utc)
            log_ascii_table(
                logger,
                "MT5 Cleanup Result",
                [
                    ("Finished", finished_at),
                    ("Duration", duration_label(started_at, finished_at)),
                    ("Task ID", task_id),
                    ("Terminal Dir", terminal_dir),
                    ("AppData Hash", appdata_hash),
                    ("Status", "not Windows, skipping cleanup"),
                ],
                level=logging.WARNING,
            )
            return {
                "error": "not Windows, skipping cleanup",
                "db_deleted": False,
                "cleanup_mark_cleared": False,
            }

        from helpers.mt5_dispatch import canonical_monitor_vm_id, guard_wrong_vm_task
        from models import MT5Account, db

        cleanup_target_vm_id = canonical_monitor_vm_id(target_vm_id)
        if not cleanup_target_vm_id and mt5_account_id is not None:
            account_for_guard = db.session.get(MT5Account, mt5_account_id)
            if account_for_guard is not None:
                cleanup_target_vm_id = canonical_monitor_vm_id(account_for_guard.vm_id)

        guard_result = guard_wrong_vm_task(
            self,
            target_vm_id=cleanup_target_vm_id,
            redispatch=lambda queue, kwargs: cleanup_mt5_terminal.apply_async(
                args=[terminal_path, appdata_hash],
                kwargs={
                    "mt5_account_id": mt5_account_id,
                    "delete_account_row": delete_account_row,
                    "clear_cleanup_mark": clear_cleanup_mark,
                    "cleanup_marked_at": cleanup_marked_at,
                    **kwargs,
                },
                queue=queue,
            ),
        )
        if guard_result is not None:
            return guard_result

        terminal_exe = terminal_path

        if terminal_exe:
            _terminate_mt5_processes(terminal_exe)

        if terminal_dir and os.path.exists(terminal_dir):
            shutil.rmtree(terminal_dir)
            if os.path.exists(terminal_dir):
                raise OSError(f"terminal dir still exists after cleanup: {terminal_dir}")

        # Prefer the stored MT5 hash when it is valid, but fall back to origin.txt lookup.
        appdata_folder = _resolve_cleanup_appdata_folder(terminal_dir or "", appdata_hash)
        if appdata_folder and os.path.exists(appdata_folder):
            shutil.rmtree(appdata_folder)
            if os.path.exists(appdata_folder):
                raise OSError(f"appdata folder still exists after cleanup: {appdata_folder}")

        if mt5_account_id is not None:
            from models import MT5Account, db

            try:
                account = db.session.get(MT5Account, mt5_account_id)
                if account is not None:
                    if delete_account_row:
                        db.session.delete(account)
                        db.session.commit()
                        db_deleted = True
                    elif clear_cleanup_mark:
                        current_mark = getattr(account, "cleanup_marked_at", None)
                        current_token = current_mark.isoformat() if current_mark is not None else ""
                        if hasattr(cleanup_marked_at, "isoformat"):
                            requested_token = cleanup_marked_at.isoformat()
                        else:
                            requested_token = str(cleanup_marked_at or "").strip()
                        if current_mark is not None and requested_token and current_token == requested_token:
                            account.terminal_path = None
                            account.appdata_hash = None
                            account.is_active = False
                            account.cleanup_marked_at = None
                            db.session.commit()
                            cleanup_mark_cleared = True
            except Exception:
                db.session.rollback()
                raise

        finished_at = datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "MT5 Cleanup Result",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("Terminal Dir", terminal_dir),
                ("AppData Hash", appdata_hash),
                ("AppData Folder", appdata_folder),
                ("DB Deleted", db_deleted),
                ("Cleanup Mark Cleared", cleanup_mark_cleared),
                ("Status", "cleanup complete"),
            ],
        )

        return {
            "terminal_dir": terminal_dir,
            "appdata_hash": appdata_hash,
            "appdata_folder": appdata_folder,
            "db_deleted": db_deleted,
            "cleanup_mark_cleared": cleanup_mark_cleared,
            "status": "cleanup complete",
        }
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "MT5 Cleanup Failed",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("Terminal Dir", terminal_dir),
                ("AppData Hash", appdata_hash),
                ("Error", exc),
            ],
            level=logging.ERROR,
        )
        logger.exception(
            "MT5 cleanup failed. task_id=%s terminal_path=%s appdata_hash=%s",
            task_id,
            terminal_path,
            appdata_hash,
        )
        raise


# Time limits bound how long a frozen MT5 UI can hold the shared mt5_global_lock
# (TTL 600s). soft_time_limit raises SoftTimeLimitExceeded so the task's finally
# blocks release the GUI + global locks and kill the launched terminal; time_limit
# is the hard backstop. NOTE: Celery time limits are only enforced on the prefork
# pool — under --pool=solo (current Windows MT5 workers) they are a no-op, so the
# 600s global-lock TTL plus the in-helper cooperative timeouts remain the real
# bound there.
@celery.task(
    bind=True,
    max_retries=0,
    queue="mt5_setup",
    soft_time_limit=240,
    time_limit=300,
)
def run_mt5_broker_discovery_refresh(
    self,
    *,
    target_vm_id=None,
    broker_search_term="Exness",
    dry_run=False,
    mt5_account_id=None,
    terminal_path=None,
    terminal_data_dir=None,
    admin_user_id=None,
    _wrong_vm_redispatch_count=0,
):
    """
    Admin-only broker/server discovery refresh. GUI automation is PID-scoped to
    the terminal launched for this job only.
    """
    from helpers.mt5_broker_discovery_refresh import (
        DEFAULT_BROKER_SEARCH_TERM,
        RefreshResult,
        execute_broker_discovery_refresh_job,
    )
    from helpers.mt5_dispatch import guard_wrong_vm_task, mt5_setup_queue, routing_vm_id
    from celery_workers.cache import set_mt5_broker_refresh_result

    task_id = getattr(getattr(self, "request", None), "id", None)
    request = getattr(self, "request", None)
    worker_hostname = getattr(request, "hostname", None)
    resolved_vm_id = routing_vm_id(target_vm_id)
    queue_name = mt5_setup_queue(resolved_vm_id) if resolved_vm_id else "mt5_setup"
    started_at = datetime.now(timezone.utc)

    guard_result = guard_wrong_vm_task(
        self,
        target_vm_id=resolved_vm_id,
        queue_kind="setup",
        redispatch=lambda queue, kwargs: run_mt5_broker_discovery_refresh.apply_async(
            kwargs={
                "target_vm_id": resolved_vm_id,
                "broker_search_term": broker_search_term,
                "dry_run": dry_run,
                "mt5_account_id": mt5_account_id,
                "terminal_path": terminal_path,
                "terminal_data_dir": terminal_data_dir,
                "admin_user_id": admin_user_id,
                **kwargs,
            },
            queue=queue,
        ),
    )
    if guard_result is not None:
        result = RefreshResult(
            attempted=True,
            dry_run=bool(dry_run),
            broker_search_term=str(broker_search_term or DEFAULT_BROKER_SEARCH_TERM).strip()
            or DEFAULT_BROKER_SEARCH_TERM,
            terminal_path=terminal_path,
            terminal_data_dir=terminal_data_dir,
            started_at=started_at.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            queue_name=queue_name,
            vm_id=resolved_vm_id,
            vm_name=resolved_vm_id,
            worker_hostname=worker_hostname,
            job_id=task_id,
            mt5_account_id=mt5_account_id,
            admin_user_id=admin_user_id,
            success=bool(guard_result.get("requeued")),
            failed_step=None if guard_result.get("requeued") else "wrong_vm_max_redispatch",
            error_message=None if guard_result.get("requeued") else str(guard_result.get("error")),
        )
        result_dict = result.to_dict()
        result_dict["status"] = "requeued" if guard_result.get("requeued") else "failed"
        result_dict["guard_result"] = guard_result
        if task_id:
            set_mt5_broker_refresh_result(task_id, result_dict)
        return result_dict

    return execute_broker_discovery_refresh_job(
        task_id=task_id,
        worker_hostname=worker_hostname,
        target_vm_id=resolved_vm_id,
        broker_search_term=broker_search_term,
        dry_run=dry_run,
        mt5_account_id=mt5_account_id,
        terminal_path=terminal_path,
        terminal_data_dir=terminal_data_dir,
        admin_user_id=admin_user_id,
    )
