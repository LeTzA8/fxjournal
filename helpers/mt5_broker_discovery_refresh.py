"""
Admin-only MT5 broker/server discovery refresh via Windows UI automation.

Launches a single terminal process, scopes automation to that PID only, runs the
Open an Account -> Find your company flow, and records optional file diffs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

OPEN_ACCOUNT_DIALOG_TITLE = "Open an Account"
COMPANY_SEARCH_PLACEHOLDER = (
    "add new company like 'CompanyName' or address 'company.com'"
)
FIND_COMPANY_BUTTON_TEXT = "Find your company"
DEFAULT_BROKER_SEARCH_TERM = "Exness"
DEFAULT_DISCOVERY_WAIT_SECONDS = 15

IGNORED_FILE_PATTERNS = (
    re.compile(r"(^|[\\/])logs([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])history([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])ticks([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])temp([\\/]|$)", re.IGNORECASE),
    re.compile(r"\.tmp$", re.IGNORECASE),
    re.compile(r"(^|[\\/])screenshots([\\/]|$)", re.IGNORECASE),
    re.compile(r"\.log$", re.IGNORECASE),
)

USEFUL_FILE_HINTS = (
    "servers.dat",
    "config",
    "cache",
    "bases",
    "origin.txt",
)


@dataclass
class FileChange:
    path: str
    change_type: str
    before_hash: str | None = None
    after_hash: str | None = None
    size_delta: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RefreshResult:
    success: bool = False
    attempted: bool = False
    dry_run: bool = False
    broker_search_term: str = DEFAULT_BROKER_SEARCH_TERM
    terminal_path: str | None = None
    terminal_data_dir: str | None = None
    pid: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    failed_step: str | None = None
    error_message: str | None = None
    window_titles_seen: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    files_changed: list[FileChange] = field(default_factory=list)
    queue_name: str | None = None
    vm_id: str | None = None
    vm_name: str | None = None
    worker_hostname: str | None = None
    job_id: str | None = None
    mt5_account_id: int | None = None
    admin_user_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files_changed"] = [
            change.to_dict() if isinstance(change, FileChange) else change
            for change in self.files_changed
        ]
        return payload


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def resolve_terminal_data_dir(
    terminal_path: str,
    *,
    appdata_hash: str | None = None,
) -> str | None:
    from celery_workers.mt5_setup_tasks import (
        APPDATA_TERMINAL_PATH,
        _find_base_appdata,
        _resolve_cleanup_appdata_folder,
    )

    normalized_terminal = str(terminal_path or "").strip()
    if not normalized_terminal:
        return None

    terminal_dir = os.path.dirname(normalized_terminal)
    folder = _resolve_cleanup_appdata_folder(terminal_dir, appdata_hash or "")
    if folder and os.path.isdir(folder):
        return folder

    folder = _find_base_appdata(terminal_dir)
    if folder and os.path.isdir(folder):
        return folder

    if appdata_hash:
        candidate = os.path.join(APPDATA_TERMINAL_PATH, str(appdata_hash).strip().upper())
        if os.path.isdir(candidate):
            return candidate
    return None


def _should_ignore_relative_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    return any(pattern.search(normalized) for pattern in IGNORED_FILE_PATTERNS)


def _file_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "size": int(stat.st_size),
        "mtime": float(stat.st_mtime),
        "hash": digest.hexdigest(),
    }


def snapshot_terminal_data_dir(data_dir: str | None) -> dict[str, dict[str, Any]]:
    if not data_dir or not os.path.isdir(data_dir):
        return {}

    root = Path(data_dir)
    snapshot: dict[str, dict[str, Any]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _should_ignore_relative_path(relative):
            continue
        try:
            snapshot[relative] = _file_fingerprint(path)
        except OSError:
            continue
    return snapshot


def diff_terminal_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[FileChange]:
    changes: list[FileChange] = []
    all_paths = sorted(set(before) | set(after))
    for relative in all_paths:
        before_row = before.get(relative)
        after_row = after.get(relative)
        if before_row is None and after_row is not None:
            changes.append(
                FileChange(
                    path=relative,
                    change_type="added",
                    before_hash=None,
                    after_hash=after_row.get("hash"),
                    size_delta=int(after_row.get("size") or 0),
                )
            )
            continue
        if before_row is not None and after_row is None:
            changes.append(
                FileChange(
                    path=relative,
                    change_type="removed",
                    before_hash=before_row.get("hash"),
                    after_hash=None,
                    size_delta=-int(before_row.get("size") or 0),
                )
            )
            continue
        if before_row and after_row and before_row.get("hash") != after_row.get("hash"):
            changes.append(
                FileChange(
                    path=relative,
                    change_type="modified",
                    before_hash=before_row.get("hash"),
                    after_hash=after_row.get("hash"),
                    size_delta=int(after_row.get("size") or 0) - int(before_row.get("size") or 0),
                )
            )
    useful = [
        change
        for change in changes
        if any(hint in change.path.lower() for hint in USEFUL_FILE_HINTS)
        or change.path.lower().endswith(".dat")
    ]
    return useful or changes


def _collect_window_titles_for_pid(pid: int) -> list[str]:
    titles: list[str] = []
    try:
        import psutil
    except ImportError:
        return titles

    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return titles

    def _callback(hwnd, _):
        try:
            import win32gui
            import win32process
        except ImportError:
            return False
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid != pid:
            return True
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        if title:
            titles.append(title)
        return True

    try:
        import win32gui

        win32gui.EnumWindows(_callback, None)
    except Exception:
        return titles
    return sorted(set(titles))


def _default_screenshot_dir() -> Path:
    base = os.getenv("FXJ_WORKER_LOG_DIR", "").strip()
    if not base:
        base = os.path.join(os.getcwd(), "worker_debug")
    return Path(base) / "broker_refresh_screenshots"


def _capture_pid_screenshot(pid: int, *, failed_step: str) -> str | None:
    titles = _collect_window_titles_for_pid(pid)
    if not titles:
        return None
    screenshot_dir = _default_screenshot_dir()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = screenshot_dir / f"broker_refresh_{pid}_{failed_step}_{stamp}.png"
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    try:
        image = ImageGrab.grab(all_screens=True)
        image.save(output_path)
        return str(output_path)
    except Exception as exc:
        logger.warning("broker refresh screenshot failed pid=%s error=%s", pid, exc)
        return None


def _terminate_pid(pid: int | None, *, terminal_path: str | None = None) -> None:
    if pid is None:
        return
    try:
        import psutil

        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        if terminal_path:
            from celery_workers.mt5_setup_tasks import _terminate_mt5_processes

            _terminate_mt5_processes(terminal_path)


def _launch_terminal_process(terminal_path: str) -> int:
    normalized = os.path.abspath(terminal_path)
    if not os.path.isfile(normalized):
        raise FileNotFoundError(f"terminal executable not found: {normalized}")
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [normalized],
        cwd=os.path.dirname(normalized) or None,
        creationflags=creationflags,
    )
    return int(process.pid)


def _wait_for_pid_window(pid: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        titles = _collect_window_titles_for_pid(pid)
        if titles:
            return
        time.sleep(0.5)
    raise TimeoutError("main MT5 window did not appear for launched PID")


def _connect_application_for_pid(pid: int):
    from pywinauto import Application

    return Application(backend="uia").connect(process=pid, timeout=20)


def _find_open_account_dialog(app, pid: int):
    titles = _collect_window_titles_for_pid(pid)
    for title in titles:
        if OPEN_ACCOUNT_DIALOG_TITLE.casefold() in title.casefold():
            return app.window(title_re=re.escape(title))
    return app.window(title_re=f".*{re.escape(OPEN_ACCOUNT_DIALOG_TITLE)}.*")


def _open_account_dialog_from_main(app, pid: int, *, timeout_seconds: float):
    existing = None
    deadline = time.monotonic() + min(timeout_seconds, 10)
    while time.monotonic() < deadline:
        try:
            existing = _find_open_account_dialog(app, pid)
            if existing.exists(timeout=0.2):
                return existing
        except Exception:
            pass
        time.sleep(0.4)

    titles = _collect_window_titles_for_pid(pid)
    for title in titles:
        if not title:
            continue
        try:
            window = app.window(title_re=f".*{re.escape(title)}.*")
            if not window.exists(timeout=0.5):
                continue
            for label in ("Open an Account", "Open An Account", "open an account"):
                try:
                    window.child_window(title=label, control_type="Hyperlink").click_input()
                    dialog = _find_open_account_dialog(app, pid)
                    if dialog.exists(timeout=5):
                        return dialog
                except Exception:
                    pass
                try:
                    window.child_window(title=label, control_type="Button").click_input()
                    dialog = _find_open_account_dialog(app, pid)
                    if dialog.exists(timeout=5):
                        return dialog
                except Exception:
                    pass
            try:
                window.set_focus()
                from pywinauto.keyboard import send_keys

                send_keys("%f")
                time.sleep(0.4)
                send_keys("o")
                dialog = _find_open_account_dialog(app, pid)
                if dialog.exists(timeout=5):
                    return dialog
            except Exception:
                pass
        except Exception:
            continue
    raise RuntimeError("could not open Open an Account dialog for launched PID")


def _control_label(control) -> str:
    try:
        text = (control.window_text() or "").strip()
        if text:
            return text
    except Exception:
        pass
    try:
        name = (control.element_info.name or "").strip()
        if name:
            return name
    except Exception:
        pass
    return ""


def _company_search_label_score(label: str) -> int:
    normalized = str(label or "").strip().casefold()
    if not normalized:
        # Unnamed edit controls are common once the placeholder collapses on focus.
        return 5
    if normalized == COMPANY_SEARCH_PLACEHOLDER.casefold():
        return 100
    if "add new company" in normalized:
        return 85
    if "companyname" in normalized or "company.com" in normalized:
        return 75
    if "company" in normalized and ("address" in normalized or " like " in f" {normalized} "):
        return 70
    if "company" in normalized:
        return 35
    return 0


def _iter_search_field_candidates(dialog):
    for control_type in ("Edit", "ComboBox"):
        try:
            for control in dialog.descendants(control_type=control_type):
                try:
                    if control.exists(timeout=0):
                        yield control
                except Exception:
                    continue
        except Exception:
            continue


def _find_company_search_input(dialog):
    for kwargs in (
        {"title": COMPANY_SEARCH_PLACEHOLDER},
        {"title_re": r".*add new company.*", "control_type": "Edit"},
        {"title_re": r".*companyname.*", "control_type": "Edit"},
        {"title_re": r".*company\.com.*", "control_type": "Edit"},
        {"title_re": r".*company.*", "control_type": "Edit"},
    ):
        try:
            control = dialog.child_window(**kwargs)
            if control.exists(timeout=1):
                return control
        except Exception:
            continue

    best_control = None
    best_score = 0
    for control in _iter_search_field_candidates(dialog):
        score = _company_search_label_score(_control_label(control))
        if score > best_score:
            best_score = score
            best_control = control

    if best_control is not None and best_score >= 5:
        return best_control
    return None


def _fill_company_search_input(search_input, term: str) -> None:
    term = str(term or "").strip()
    if not term:
        raise ValueError("broker search term is required")

    try:
        search_input.wait("visible", timeout=5)
    except Exception:
        pass

    # Physical click is required for many MT5 builds; set_focus alone leaves the
    # dialog window as the keyboard target so type_keys hits the shell.
    try:
        search_input.click_input()
    except Exception:
        search_input.set_focus()

    time.sleep(0.25)

    try:
        search_input.set_edit_text(term)
        return
    except Exception:
        logger.debug("broker refresh set_edit_text failed; falling back to type_keys", exc_info=True)

    try:
        search_input.click_input()
    except Exception:
        search_input.set_focus()
    try:
        search_input.type_keys("^a{BACKSPACE}", set_foreground=True)
    except Exception:
        pass
    search_input.type_keys(term, with_spaces=True, set_foreground=True)


def _click_find_company(dialog):
    for label in (FIND_COMPANY_BUTTON_TEXT, "Find Your Company"):
        try:
            button = dialog.child_window(title=label, control_type="Button")
            if button.exists(timeout=1):
                button.click_input()
                return
        except Exception:
            continue
    try:
        from pywinauto.keyboard import send_keys

        dialog.set_focus()
        send_keys("{TAB}{ENTER}")
    except Exception as exc:
        raise RuntimeError("Find your company button not found") from exc


def _close_open_account_dialog(dialog):
    for label in ("Cancel", "Close"):
        try:
            button = dialog.child_window(title=label, control_type="Button")
            if button.exists(timeout=1):
                button.click_input()
                return
        except Exception:
            continue
    try:
        from pywinauto.keyboard import send_keys

        dialog.set_focus()
        send_keys("{ESC}")
    except Exception:
        pass


def refresh_broker_server_cache(
    terminal_path: str,
    terminal_data_dir: str | None,
    broker_search_term: str,
    account_id: int | None = None,
    timeout_seconds: int = 60,
    dry_run: bool = False,
    *,
    launch_process: Callable[[str], int] | None = None,
    connect_application: Callable[[int], Any] | None = None,
    collect_window_titles: Callable[[int], list[str]] | None = None,
    terminate_process: Callable[[int | None, str | None], None] | None = None,
) -> RefreshResult:
    started = _utc_now()
    term = str(broker_search_term or DEFAULT_BROKER_SEARCH_TERM).strip() or DEFAULT_BROKER_SEARCH_TERM
    result = RefreshResult(
        attempted=not dry_run,
        dry_run=bool(dry_run),
        broker_search_term=term,
        terminal_path=str(terminal_path or "").strip() or None,
        terminal_data_dir=terminal_data_dir,
        started_at=_iso(started),
        mt5_account_id=account_id,
    )

    if dry_run:
        result.success = True
        result.finished_at = _iso(_utc_now())
        return result

    if os.name != "nt":
        result.failed_step = "platform"
        result.error_message = "broker discovery refresh requires Windows"
        result.finished_at = _iso(_utc_now())
        return result

    launch_fn = launch_process or _launch_terminal_process
    connect_fn = connect_application or _connect_application_for_pid
    titles_fn = collect_window_titles or _collect_window_titles_for_pid
    terminate_fn = terminate_process or _terminate_pid

    pid: int | None = None
    before_snapshot: dict[str, dict[str, Any]] = {}
    try:
        if not result.terminal_path:
            raise ValueError("terminal_path is required for broker discovery refresh")
        before_snapshot = snapshot_terminal_data_dir(result.terminal_data_dir)
        pid = launch_fn(result.terminal_path)
        result.pid = pid
        result.window_titles_seen = titles_fn(pid)
        _wait_for_pid_window(pid, timeout_seconds=min(timeout_seconds, 30))
        result.window_titles_seen = titles_fn(pid)

        app = connect_fn(pid)
        dialog = _open_account_dialog_from_main(app, pid, timeout_seconds=timeout_seconds)
        result.window_titles_seen = titles_fn(pid)

        search_input = _find_company_search_input(dialog)
        if search_input is None:
            raise RuntimeError("company search input not found in Open an Account dialog")
        _fill_company_search_input(search_input, term)

        _click_find_company(dialog)
        wait_seconds = min(max(timeout_seconds / 4, 10), 20)
        time.sleep(wait_seconds)
        result.window_titles_seen = titles_fn(pid)

        _close_open_account_dialog(dialog)
        after_snapshot = snapshot_terminal_data_dir(result.terminal_data_dir)
        result.files_changed = diff_terminal_snapshots(before_snapshot, after_snapshot)
        result.success = True
    except Exception as exc:
        result.success = False
        if result.failed_step is None:
            step_name = getattr(exc, "failed_step", None)
            result.failed_step = step_name or _infer_failed_step(str(exc))
        result.error_message = str(exc)
        if pid is not None:
            result.window_titles_seen = titles_fn(pid)
            result.screenshot_path = _capture_pid_screenshot(pid, failed_step=result.failed_step or "error")
    finally:
        terminate_fn(pid, terminal_path=result.terminal_path)
        result.finished_at = _iso(_utc_now())

    return result


def execute_broker_discovery_refresh_job(
    *,
    task_id=None,
    worker_hostname=None,
    target_vm_id=None,
    broker_search_term=DEFAULT_BROKER_SEARCH_TERM,
    dry_run=False,
    mt5_account_id=None,
    terminal_path=None,
    terminal_data_dir=None,
    admin_user_id=None,
):
    """Worker-side broker refresh execution after wrong-VM guard has passed."""
    from helpers.app_settings import (
        MT5_BROKER_DISCOVERY_REFRESH_ENABLED_KEY,
        get_bool_app_setting,
    )
    from helpers.mt5_dispatch import mt5_setup_queue, routing_vm_id
    from celery_workers.cache import (
        MT5_GLOBAL_LOCK_SETUP_TTL,
        MT5_GUI_BROKER_REFRESH_LOCK_TTL,
        acquire_mt5_global_lock,
        claim_lock,
        mt5_gui_broker_refresh_lock_key,
        release_lock,
        release_mt5_global_lock,
        set_mt5_broker_refresh_result,
    )
    from celery_workers.worker_monitor import get_vm_id

    mt5_base_path = (
        os.environ.get("MT5_BASE_PATH", r"C:\Program Files\MetaTrader 5").strip()
        or r"C:\Program Files\MetaTrader 5"
    )

    resolved_vm_id = routing_vm_id(target_vm_id)
    queue_name = mt5_setup_queue(resolved_vm_id) if resolved_vm_id else "mt5_setup"
    started_at = _utc_now()

    def _store_result(result: RefreshResult):
        payload = result.to_dict()
        payload["status"] = "completed"
        if task_id:
            set_mt5_broker_refresh_result(task_id, payload)

    feature_enabled = get_bool_app_setting(MT5_BROKER_DISCOVERY_REFRESH_ENABLED_KEY, default=False)
    if not dry_run and not feature_enabled:
        result = RefreshResult(
            attempted=False,
            dry_run=False,
            success=False,
            failed_step="feature_disabled",
            error_message="mt5_broker_discovery_refresh_enabled is disabled",
            broker_search_term=str(broker_search_term or DEFAULT_BROKER_SEARCH_TERM).strip()
            or DEFAULT_BROKER_SEARCH_TERM,
            terminal_path=terminal_path,
            terminal_data_dir=terminal_data_dir,
            started_at=_iso(started_at),
            finished_at=_iso(_utc_now()),
            queue_name=queue_name,
            vm_id=resolved_vm_id,
            vm_name=resolved_vm_id,
            worker_hostname=worker_hostname,
            job_id=task_id,
            mt5_account_id=mt5_account_id,
            admin_user_id=admin_user_id,
        )
        _store_result(result)
        return result.to_dict()

    lock_key = mt5_gui_broker_refresh_lock_key(resolved_vm_id)
    lock_token = f"broker_refresh:{task_id or 'unknown'}:{resolved_vm_id or 'unknown'}"
    lock_acquired = claim_lock(lock_key, lock_token, MT5_GUI_BROKER_REFRESH_LOCK_TTL)
    if not lock_acquired:
        result = RefreshResult(
            attempted=False,
            dry_run=bool(dry_run),
            success=False,
            failed_step="lock_unavailable",
            error_message="Another broker discovery refresh is already running on this VM",
            broker_search_term=str(broker_search_term or DEFAULT_BROKER_SEARCH_TERM).strip()
            or DEFAULT_BROKER_SEARCH_TERM,
            terminal_path=terminal_path,
            terminal_data_dir=terminal_data_dir,
            started_at=_iso(started_at),
            finished_at=_iso(_utc_now()),
            queue_name=queue_name,
            vm_id=resolved_vm_id,
            vm_name=resolved_vm_id,
            worker_hostname=worker_hostname,
            job_id=task_id,
            mt5_account_id=mt5_account_id,
            admin_user_id=admin_user_id,
        )
        _store_result(result)
        return result.to_dict()

    global_lock_token = f"broker_refresh_global:{task_id or 'unknown'}"
    global_lock_acquired = False
    try:
        if dry_run:
            result = RefreshResult(
                attempted=True,
                dry_run=True,
                success=True,
                broker_search_term=str(broker_search_term or DEFAULT_BROKER_SEARCH_TERM).strip()
                or DEFAULT_BROKER_SEARCH_TERM,
                terminal_path=terminal_path,
                terminal_data_dir=terminal_data_dir,
                started_at=_iso(started_at),
                finished_at=_iso(_utc_now()),
                queue_name=queue_name,
                vm_id=resolved_vm_id,
                vm_name=resolved_vm_id,
                worker_hostname=worker_hostname,
                job_id=task_id,
                mt5_account_id=mt5_account_id,
                admin_user_id=admin_user_id,
            )
            _store_result(result)
            logger.info(
                "MT5 broker discovery refresh dry-run task_id=%s vm_id=%s queue=%s admin_user_id=%s term=%s",
                task_id,
                resolved_vm_id,
                queue_name,
                admin_user_id,
                result.broker_search_term,
            )
            return result.to_dict()

        global_lock_acquired = acquire_mt5_global_lock(
            global_lock_token,
            MT5_GLOBAL_LOCK_SETUP_TTL,
            wait_seconds=0,
        )
        if not global_lock_acquired:
            result = RefreshResult(
                attempted=False,
                dry_run=False,
                success=False,
                failed_step="mt5_global_lock_busy",
                error_message="MT5 global runtime lock is busy on this VM",
                broker_search_term=str(broker_search_term or DEFAULT_BROKER_SEARCH_TERM).strip()
                or DEFAULT_BROKER_SEARCH_TERM,
                terminal_path=terminal_path,
                terminal_data_dir=terminal_data_dir,
                started_at=_iso(started_at),
                finished_at=_iso(_utc_now()),
                queue_name=queue_name,
                vm_id=resolved_vm_id,
                vm_name=resolved_vm_id,
                worker_hostname=worker_hostname,
                job_id=task_id,
                mt5_account_id=mt5_account_id,
                admin_user_id=admin_user_id,
            )
            _store_result(result)
            return result.to_dict()

        resolved_terminal_path = str(terminal_path or "").strip()
        if not resolved_terminal_path:
            resolved_terminal_path = os.path.join(mt5_base_path, "terminal64.exe")

        resolved_data_dir = terminal_data_dir
        if not resolved_data_dir:
            appdata_hash = None
            if mt5_account_id is not None:
                from models import MT5Account, db

                account = db.session.get(MT5Account, mt5_account_id)
                if account is not None:
                    appdata_hash = getattr(account, "appdata_hash", None)
            resolved_data_dir = resolve_terminal_data_dir(
                resolved_terminal_path,
                appdata_hash=appdata_hash,
            )

        automation_result = refresh_broker_server_cache(
            terminal_path=resolved_terminal_path,
            terminal_data_dir=resolved_data_dir,
            broker_search_term=str(broker_search_term or DEFAULT_BROKER_SEARCH_TERM).strip()
            or DEFAULT_BROKER_SEARCH_TERM,
            account_id=mt5_account_id,
            timeout_seconds=60,
            dry_run=False,
        )
        automation_result.queue_name = queue_name
        automation_result.vm_id = resolved_vm_id
        automation_result.vm_name = resolved_vm_id
        automation_result.worker_hostname = worker_hostname or get_vm_id()
        automation_result.job_id = task_id
        automation_result.admin_user_id = admin_user_id
        _store_result(automation_result)
        logger.info(
            "MT5 broker discovery refresh finished task_id=%s vm_id=%s success=%s failed_step=%s admin_user_id=%s",
            task_id,
            resolved_vm_id,
            automation_result.success,
            automation_result.failed_step,
            admin_user_id,
        )
        return automation_result.to_dict()
    finally:
        if global_lock_acquired:
            release_mt5_global_lock(global_lock_token)
        release_lock(lock_key, lock_token)


def _infer_failed_step(message: str) -> str:
    lowered = message.lower()
    if "search input" in lowered or "company search" in lowered:
        return "company_search_input"
    if "find your company" in lowered:
        return "find_company_button"
    if "dialog" in lowered:
        return "open_account_dialog"
    if "company" in lowered:
        return "company_search_input"
    if "find your company" in lowered:
        return "find_company_button"
    if "window" in lowered:
        return "main_window"
    if "terminal" in lowered and "not found" in lowered:
        return "launch_terminal"
    return "automation"
