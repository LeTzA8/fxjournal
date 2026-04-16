"""
Seed MT5's broker/server discovery cache by driving the built-in UI search flow.

- Does not log into any account and does not touch servers.dat on disk.
- Types each search term into the broker search field. If the “Open an Account”
  wizard is already visible (common on first launch), that dialog is used and
  the File menu is skipped — the main window is often disabled while the wizard
  is open, which breaks menu automation. Otherwise uses File -> Open an Account
  (configurable). Adjust menu/title regex if your UI language differs.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & timing (edit these for your machine)
# ---------------------------------------------------------------------------

# Default install path; change if your "base" terminal lives elsewhere.
MT5_EXE_PATH = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")

# Plain-text list: one search term per line; # comments and blanks ignored.
BROKERS_FILE = Path(__file__).resolve().parent / "brokers_to_seed.txt"

# Simple append log next to this script.
LOG_FILE = Path(__file__).resolve().parent / "mt5_broker_seeding.log"

# Optional: data folder is not read/written by this script; reserved for your notes.
MT5_DATA_FOLDER_HINT: Path | None = None  # e.g. Path(r"C:\Users\You\AppData\Roaming\MetaQuotes\Terminal\...")

# Seconds
STARTUP_WAIT_S = 18.0
AFTER_MENU_OPEN_S = 2.5
AFTER_TYPING_S = 6.0
BETWEEN_BROKERS_S = 2.0
CONNECT_RETRY_S = 2.0
CONNECT_RETRIES = 15

# When True, look for an already-open wizard before using the File menu.
PREFER_EXISTING_OPEN_ACCOUNT_DIALOG = True
# How long to poll for the wizard after connect/start (first broker only).
EXISTING_DIALOG_FIRST_WAIT_S = 35.0
# After ESC, the wizard is usually gone — only brief poll before File menu.
EXISTING_DIALOG_QUICK_WAIT_S = 1.5

# Retries when opening via menu (main window may need a moment after wizard closes).
MENU_OPEN_RETRIES = 6
MENU_OPEN_RETRY_DELAY_S = 1.2

# English UI default. Must match the exact menu path MT5 exposes to accessibility.
# If this fails, run once with: python mt5_seed_brokers.py --dump-dialog
# Other builds sometimes use:
#   "File->Login to Trade Account"
# Examples (non-English): set to your exact captions, e.g. "Datei->Konto eröffnen…"
FILE_MENU_OPEN_ACCOUNT = "File->Open an Account"

# Window title fragments (case-insensitive regex). Widen if needed.
MT5_MAIN_TITLE_RE = r".*MetaTrader 5.*"
OPEN_ACCOUNT_DIALOG_TITLE_RE = r".*(Open an Account|Open Account).*"

# pywinauto backend: "uia" is usually best on Win10/11 for MT5.
PYWINAUTO_BACKEND = "uia"

# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

logger = logging.getLogger("mt5_seed_brokers")


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.Formatter.converter = time.gmtime  # suffix Z in format => UTC


def _read_terms(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Brokers list not found: {path}")
    terms: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return terms


def _attach_application(exe: Path):
    from pywinauto import Application
    from pywinauto.application import ProcessNotFoundError

    exe_s = str(exe)
    try:
        app = Application(backend=PYWINAUTO_BACKEND).connect(path=exe_s)
        logger.info("Connected to already-running MT5 (%s)", exe_s)
        return app
    except ProcessNotFoundError:
        logger.info("MT5 not running; starting %s", exe_s)
        if not exe.is_file():
            raise FileNotFoundError(f"MT5 executable not found: {exe}")
        Application(backend=PYWINAUTO_BACKEND).start(exe_s)
        time.sleep(STARTUP_WAIT_S)
        last_exc: Exception | None = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            try:
                app = Application(backend=PYWINAUTO_BACKEND).connect(path=exe_s)
                logger.info("Connected to MT5 after start (attempt %s)", attempt)
                return app
            except ProcessNotFoundError as exc:
                last_exc = exc
                logger.warning("Waiting for MT5 process… (%s/%s)", attempt, CONNECT_RETRIES)
                time.sleep(CONNECT_RETRY_S)
        raise RuntimeError(f"Could not connect to MT5 after start: {last_exc}") from last_exc


def _main_window(app):
    return app.window(title_re=MT5_MAIN_TITLE_RE, found_index=0)


def _open_account_dialog_spec(app):
    return app.window(title_re=OPEN_ACCOUNT_DIALOG_TITLE_RE, found_index=0)


def _find_open_account_dialog(app, total_wait_s: float):
    """Return a dialog window spec if it appears within total_wait_s seconds."""
    if total_wait_s <= 0:
        return None
    dlg = _open_account_dialog_spec(app)
    deadline = time.monotonic() + total_wait_s
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            if dlg.exists(timeout=max(0.05, min(1.0, remaining))):
                return dlg
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _open_account_dialog_via_menu(app):
    from pywinauto.base_wrapper import ElementNotEnabled

    last_exc: Exception | None = None
    for attempt in range(1, MENU_OPEN_RETRIES + 1):
        main = _main_window(app)
        try:
            try:
                main.restore()
            except Exception:
                pass
            main.set_focus()
        except Exception:
            pass
        time.sleep(0.5)
        try:
            main.menu_select(FILE_MENU_OPEN_ACCOUNT)
            time.sleep(AFTER_MENU_OPEN_S)
            dlg = _open_account_dialog_spec(app)
            if dlg.exists(timeout=8):
                return dlg
            last_exc = RuntimeError("Menu opened but Open Account dialog not found")
        except (ElementNotEnabled, IndexError, AttributeError, Exception) as exc:
            last_exc = exc
            logger.warning(
                "File menu open attempt %s/%s failed: %s",
                attempt,
                MENU_OPEN_RETRIES,
                exc,
            )
        time.sleep(MENU_OPEN_RETRY_DELAY_S)
    raise RuntimeError(
        f"Could not open account dialog via menu after {MENU_OPEN_RETRIES} tries"
    ) from last_exc


def _open_account_dialog(app, *, wait_for_existing_s: float):
    if PREFER_EXISTING_OPEN_ACCOUNT_DIALOG and wait_for_existing_s > 0:
        dlg = _find_open_account_dialog(app, wait_for_existing_s)
        if dlg is not None:
            logger.info(
                "Using already-open Open Account dialog (skipped File menu; "
                "waited up to %.1fs).",
                wait_for_existing_s,
            )
            try:
                dlg.set_focus()
            except Exception:
                pass
            time.sleep(AFTER_MENU_OPEN_S)
            return dlg
    return _open_account_dialog_via_menu(app)


def _type_search_term(dialog, term: str) -> None:
    from pywinauto.keyboard import send_keys

    edits = [w for w in dialog.descendants(control_type="Edit")]
    target = edits[0] if edits else None
    if target is not None:
        try:
            target.set_focus()
        except Exception:
            pass
        # Clear field then type the term (Unicode-safe for most broker names).
        send_keys("^a{BACKSPACE}", pause=0.05)
        target.type_keys(term, with_spaces=True, pause=0.03)
    else:
        # Fallback: focus dialog and type (less reliable if focus order changes).
        try:
            dialog.set_focus()
        except Exception:
            pass
        send_keys("^a{BACKSPACE}", pause=0.05)
        send_keys(term.replace("{", "{{").replace("}", "}}"), with_spaces=True, pause=0.03)

    time.sleep(AFTER_TYPING_S)


def _close_open_account_dialog(app) -> None:
    from pywinauto.keyboard import send_keys

    try:
        dlg = _open_account_dialog_spec(app)
        dlg.set_focus()
    except Exception:
        pass
    send_keys("{ESC}", pause=0.05)
    time.sleep(0.5)


def run_once() -> int:
    started = datetime.now(timezone.utc)
    _configure_logging(LOG_FILE)
    logger.info("=== run start (UTC %s) ===", started.isoformat())
    if MT5_DATA_FOLDER_HINT:
        logger.info("MT5_DATA_FOLDER_HINT is set (informational only): %s", MT5_DATA_FOLDER_HINT)

    try:
        terms = _read_terms(BROKERS_FILE)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2

    if not terms:
        logger.error("No broker search terms after skipping blanks/comments: %s", BROKERS_FILE)
        return 2

    try:
        app = _attach_application(MT5_EXE_PATH)
    except Exception as exc:
        logger.exception("Failed to start/connect MT5: %s", exc)
        return 1

    for idx, term in enumerate(terms):
        try:
            wait_existing = EXISTING_DIALOG_FIRST_WAIT_S if idx == 0 else EXISTING_DIALOG_QUICK_WAIT_S
            dlg = _open_account_dialog(app, wait_for_existing_s=wait_existing)
            _type_search_term(dlg, term)
            logger.info('SEARCH ok term=%r (MT5 should have triggered a lookup; no login performed)', term)
            _close_open_account_dialog(app)
            time.sleep(BETWEEN_BROKERS_S)
        except Exception as exc:
            logger.exception('SEARCH fail term=%r err=%s', term, exc)
            try:
                _close_open_account_dialog(app)
            except Exception:
                pass
            time.sleep(BETWEEN_BROKERS_S)

    finished = datetime.now(timezone.utc)
    logger.info("=== run finish (UTC %s) ===", finished.isoformat())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed MT5 broker/server discovery via UI automation.")
    parser.add_argument(
        "--dump-dialog",
        action="store_true",
        help="Open File->Open an Account once and print UIA control tree, then exit (for tuning selectors).",
    )
    args = parser.parse_args(argv)

    if not args.dump_dialog:
        return run_once()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    exe = MT5_EXE_PATH
    if not exe.is_file():
        print(f"MT5 executable not found: {exe}", file=sys.stderr)
        return 2
    try:
        app = _attach_application(exe)
    except Exception as exc:
        print(f"Failed to start/connect MT5: {exc}", file=sys.stderr)
        return 1

    dlg = _open_account_dialog(app, wait_for_existing_s=EXISTING_DIALOG_FIRST_WAIT_S)
    out_path = Path(__file__).resolve().parent / "mt5_open_account_uia_tree.txt"
    dlg.print_control_identifiers(depth=None, filename=str(out_path))
    print(f"Wrote {out_path}")
    _close_open_account_dialog(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
