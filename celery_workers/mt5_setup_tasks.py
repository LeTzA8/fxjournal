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

# Crypto symbols added to market watch at terminal setup so the UTC-offset
# probe has 24/7 tick data available even when forex is closed.
_MARKET_WATCH_SEED_SYMBOLS = [
    "BTCUSD",
    "BTCUSDT",
    "XBTUSD",
    "BTCUSD.r",
    "BTC/USD",
]


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


def _seed_market_watch_symbols(mt5, terminal_exe: str, login: int, password: str, server: str):
    """
    Open a brief MT5 session to add probe symbols to market watch so they
    persist in selected*.dat for every future sync.  Failures are non-fatal —
    the probe falls back to Redis cache and EURUSD during market hours.
    """
    try:
        result = mt5.initialize(
            path=terminal_exe,
            login=login,
            password=password,
            server=server,
            timeout=60000,
        )
        if not result:
            return
        for symbol in _MARKET_WATCH_SEED_SYMBOLS:
            try:
                mt5.symbol_select(symbol, True)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


class PermanentSetupError(RuntimeError):
    """Raised when setup is running on the wrong host or missing required local config."""


class TradingPasswordDetectedError(Exception):
    """
    Raised when account_info().trade_allowed is True after a successful MT5 login,
    indicating the user submitted a master/trading password instead of the required
    investor (read-only) password.  This error is never retried.
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
        return (
            "Login failed. Please check your account number and investor password."
        )
    if any(k in s for k in ("no_ipc", "no ipc", "ipc connection", "timeout", "timed out", "connection refused", "network")):
        return (
            "Connection timeout. Check that the server name is exact (e.g. Exness-MT5Real) and try again."
        )
    if any(k in s for k in ("unsupported", "version", "not found", "invalid server", "res_x")):
        return (
            "Invalid server. Enter the exact MT5 server name from your broker (e.g. Exness-MT5Real, ICMarketsSC-Live)."
        )
    return (
        "Connection failed. Check your server name, account number, and investor password, then retry."
    )


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
        html_body = render_app_template(
            "emails/mt5-setup-failed.html",
            name=user.username,
            error_message=user_message,
            dashboard_url=f"{base_url}/dashboard",
            logo_url=f"{base_url}/static/site-logo.png",
        )
        send_email_placeholder(
            user.email,
            "MT5 connection setup failed — fix & retry",
            (
                f"Hi {user.username}, we weren't able to connect to your MT5 account. "
                f"{user_message} "
                "Open the dashboard to fix your details and retry."
            ),
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
            "  • Open your MT5 platform and locate your INVESTOR password "
            "(not your master / trading password).\n"
            "  • In MT5: right-click your account → Manage Account → "
            "you will find the investor password there.\n"
            f"  • Return to your dashboard and re-enter your details: {dashboard_url}\n\n"
            "If you are unsure which password to use, contact your broker. "
            "The investor password is read-only and cannot place trades. "
            "Never share your master/trading password with any third-party service.\n\n"
            "MyFXJournal"
        )
        send_email_placeholder(
            user.email,
            subject,
            text_body,
        )
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
            # Note: some brokers disable trading at account level even with master password,
            # so this is a safe check (no false positives, rare false negatives only).
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
def setup_mt5_terminal(self, mt5_account_id: int):
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

        was_active = bool(account.is_active)
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
        logger.info(
            "MT5 setup base_appdata mt5_account_id=%s basename=%s",
            mt5_account_id,
            os.path.basename(base_appdata),
        )

        # Launch the terminal briefly — this causes MT5 to create its AppData folder
        proc = subprocess.Popen([terminal_exe], cwd=terminal_dir)
        logger.info(
            "MT5 setup bootstrap_launch mt5_account_id=%s terminal_dir=%s",
            mt5_account_id,
            terminal_dir,
        )

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
            logger.info(
                "MT5 setup new_terminal_appdata mt5_account_id=%s appdata_hash=%s",
                mt5_account_id,
                new_hash,
            )

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
                logger.info(
                    "MT5 setup servers_dat_copied mt5_account_id=%s",
                    mt5_account_id,
                )

            account.appdata_hash = new_hash
            appdata_hash = new_hash
        finally:
            proc.terminate()
            proc.wait(timeout=10)

        # Clear the default chart workspace after the bootstrap launch and
        # before the Python API logs into the account.
        _clear_chart_profiles(new_appdata)
        logger.info("MT5 setup pre_verify_login mt5_account_id=%s", mt5_account_id)

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

        _seed_market_watch_symbols(
            mt5,
            terminal_exe=terminal_exe,
            login=login,
            password=investor_password,
            server=server,
        )

        account.terminal_path = terminal_exe
        account.is_active = True
        account.archived_at = None
        account.archive_reason = None
        account.connection_status = "connected"
        account.connection_error_message = None
        db.session.commit()
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

        # 1. Clear the stored password immediately and record the failure.
        try:
            account = db.session.get(MT5Account, mt5_account_id)
            if account is not None:
                account.investor_password_encrypted = None
                account.connection_status = "failed"
                account.connection_error_message = (
                    "A master/trading password was detected and rejected. "
                    "For your security the connection was terminated and your credentials were cleared. "
                    "Please resubmit using your MT5 investor (read-only) password — "
                    "not your master or trading password."
                )
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
                cleanup_mt5_terminal.apply_async(
                    args=[terminal_exe, appdata_hash or ""],
                    queue="mt5_setup",
                )
            except Exception as cleanup_exc:
                logger.warning(
                    "MT5 trading password — terminal cleanup queue failed mt5_account_id=%s: %s",
                    mt5_account_id,
                    cleanup_exc,
                )

        # 3. Email the user.
        try:
            account_for_email = db.session.get(MT5Account, mt5_account_id)
            if account_for_email is not None:
                _send_trading_password_warning_email(account_for_email)
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
                ("Action", "password cleared, terminal cleanup queued, user emailed"),
            ],
            level=logging.ERROR,
        )
        raise  # no retry

    except PermanentSetupError as exc:
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
            except Exception as email_exc:
                logger.warning(
                    "MT5 setup failed email error mt5_account_id=%s: %s",
                    mt5_account_id,
                    email_exc,
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


@celery.task(bind=True, max_retries=2, default_retry_delay=10, queue="mt5_setup")
def cleanup_mt5_terminal(self, terminal_path: str, appdata_hash: str):
    """
    Clean up MT5 terminal files when an MT5Account is deleted.
    Runs on VM only - kills process, deletes terminal folder
    and AppData hash folder.
    """
    task_id = getattr(getattr(self, "request", None), "id", None)
    started_at = datetime.now(timezone.utc)
    finished_at = None
    terminal_dir = os.path.dirname(terminal_path)

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
            return {"error": "not Windows, skipping cleanup"}

        terminal_exe = terminal_path

        _terminate_mt5_processes(terminal_exe)

        if terminal_dir and os.path.exists(terminal_dir):
            shutil.rmtree(terminal_dir, ignore_errors=True)

        # Prefer the stored MT5 hash when it is valid, but fall back to origin.txt lookup.
        appdata_folder = _resolve_cleanup_appdata_folder(terminal_dir, appdata_hash)
        if appdata_folder and os.path.exists(appdata_folder):
            shutil.rmtree(appdata_folder, ignore_errors=True)

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
                ("Status", "cleanup complete"),
            ],
        )

        return {
            "terminal_dir": terminal_dir,
            "appdata_hash": appdata_hash,
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
