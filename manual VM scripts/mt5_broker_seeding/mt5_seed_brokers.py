"""
Seed MT5's broker/server discovery cache by driving the built-in UI search flow.

- Does not log into any account and does not touch servers.dat on disk.
- **Default:** “manual focus” — you click the **company search** field in MT5 during
  a short countdown; the script then sends keystrokes there (no UIA dialog lookup).
- **Optional:** set `MANUAL_FOCUS_SECONDS = 0` or run with `--manual-focus 0` to use
  UI automation to find the Open an Account window instead. The File menu is not
  used unless `USE_FILE_MENU = True`.
"""

from __future__ import annotations

import argparse
import logging
import re
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

# Wait for the Open an Account window (must already be visible unless USE_FILE_MENU).
EXISTING_DIALOG_WAIT_S = 45.0

# When False (default): never touch the File menu; wizard must stay open for all terms.
# When True: if the wizard is missing at start, try File → Open an Account (with retries).
USE_FILE_MENU = False

# Retries when opening via menu only (USE_FILE_MENU True).
MENU_OPEN_RETRIES = 6
MENU_OPEN_RETRY_DELAY_S = 1.2

# If True, press Esc once after the last broker (optional).
CLOSE_DIALOG_AT_END = False

# Default > 0: “manual focus” — sleep this many seconds while you click the MT5 company
# search field; then keystrokes go to whatever has focus. Set to 0 (or run
# `--manual-focus 0`) to find the dialog via UI automation instead.
MANUAL_FOCUS_SECONDS = 10.0

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


def _app_process_id(app) -> int | None:
    try:
        return int(app.process)
    except Exception:
        return None


def _element_title(el) -> str:
    try:
        return (el.name or "").strip()
    except Exception:
        return ""


def _title_matches_visible_or_uia(name: str | None) -> bool:
    if not name:
        return False
    return bool(re.search(OPEN_ACCOUNT_DIALOG_TITLE_RE, name, re.I | re.DOTALL))


def _wrap_dialog_element(el):
    """Turn a UIA element from find_elements into something with set_focus / descendants."""
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper

        return UIAWrapper(el)
    except Exception:
        return None


def _find_dialog_via_process_scan(app) -> object | None:
    """Scan all top-level UIA windows of the MT5 process (app.window often misses the wizard)."""
    from pywinauto.findwindows import find_elements

    pid = _app_process_id(app)
    if pid is None:
        return None
    try:
        elems = find_elements(
            process=pid,
            backend=PYWINAUTO_BACKEND,
            top_level_only=True,
        )
    except Exception:
        return None
    for el in elems:
        if not _title_matches_visible_or_uia(_element_title(el)):
            continue
        try:
            hw = int(el.handle)
        except Exception:
            hw = None
        if hw:
            try:
                w = app.window(handle=hw)
                if w.exists(timeout=0.4):
                    logger.info("Found wizard via process scan + handle (title=%r).", _element_title(el))
                    return w
            except Exception:
                pass
        wrap = _wrap_dialog_element(el)
        if wrap is not None:
            logger.info("Found wizard via process scan + UIAWrapper (title=%r).", _element_title(el))
            return wrap
    return None


def _find_open_account_dialog(app, total_wait_s: float):
    """Return dialog wrapper/spec if it appears within total_wait_s seconds."""
    if total_wait_s <= 0:
        return None
    dlg_spec = _open_account_dialog_spec(app)
    deadline = time.monotonic() + total_wait_s
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            if dlg_spec.exists(timeout=max(0.05, min(1.0, remaining))):
                logger.info("Found wizard via app.window(title_re=...).")
                return dlg_spec
        except Exception:
            pass
        try:
            for w in app.windows(title_re=OPEN_ACCOUNT_DIALOG_TITLE_RE):
                if w.exists(timeout=0.35):
                    logger.info("Found wizard via app.windows(title_re=...).")
                    return w
        except Exception:
            pass
        found = _find_dialog_via_process_scan(app)
        if found is not None:
            return found
        time.sleep(0.25)
    return None


def _ensure_open_account_dialog(app):
    """Return the wizard window; optional File-menu fallback only when USE_FILE_MENU."""
    dlg = _find_open_account_dialog(app, EXISTING_DIALOG_WAIT_S)
    if dlg is not None:
        logger.info(
            "Found Open an Account dialog (waited up to %.1fs). File menu not used.",
            EXISTING_DIALOG_WAIT_S,
        )
        try:
            dlg.set_focus()
        except Exception:
            pass
        time.sleep(AFTER_MENU_OPEN_S)
        return dlg
    if USE_FILE_MENU:
        logger.warning("Wizard not found in time; trying File menu (USE_FILE_MENU=True).")
        return _open_account_dialog_via_menu(app)
    raise RuntimeError(
        "Open an Account dialog not found by UI automation. Leave the wizard open, "
        "increase EXISTING_DIALOG_WAIT_S, widen OPEN_ACCOUNT_DIALOG_TITLE_RE, or use "
        "the default manual-focus mode (MANUAL_FOCUS_SECONDS > 0). "
        "Optional: USE_FILE_MENU = True to open the wizard via the File menu."
    )


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


def _type_search_term(dialog, term: str) -> None:
    from pywinauto.keyboard import send_keys

    edits = [w for w in dialog.descendants(control_type="Edit")]
    target = None
    for ed in edits:
        try:
            ei = getattr(ed, "element_info", None)
            en = getattr(ei, "name", None) if ei is not None else None
            label = (ed.window_text() or "") + " " + (en or "")
        except Exception:
            label = ""
        if "company" in label.lower() or "company.com" in label.lower():
            target = ed
            break
    if target is None and edits:
        target = edits[0]
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


def _type_terms_foreground_focus(terms: list[str], lead_seconds: float) -> None:
    """Send keys to whatever control is focused (user must click MT5 search box first)."""
    from pywinauto.keyboard import send_keys

    logger.warning(
        "MANUAL_FOCUS mode: in %.0fs click the MT5 **company search** field so it has "
        "keyboard focus — then the script types each broker name.",
        lead_seconds,
    )
    print(
        f"\n>>> Click the company search box in MT5 within {lead_seconds:.0f} seconds… <<<\n",
        flush=True,
    )
    time.sleep(lead_seconds)
    for term in terms:
        send_keys("^a{BACKSPACE}", pause=0.05)
        send_keys(term.replace("{", "{{").replace("}", "}}"), with_spaces=True, pause=0.03)
        time.sleep(AFTER_TYPING_S)
        logger.info(
            'SEARCH ok (foreground keys) term=%r — ensure focus stayed in the search field',
            term,
        )
        time.sleep(BETWEEN_BROKERS_S)


def _close_open_account_dialog(app) -> None:
    from pywinauto.keyboard import send_keys

    try:
        dlg = _open_account_dialog_spec(app)
        dlg.set_focus()
    except Exception:
        pass
    send_keys("{ESC}", pause=0.05)
    time.sleep(0.5)


def run_once(manual_focus_override: float | None = None) -> int:
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

    manual_secs = MANUAL_FOCUS_SECONDS if manual_focus_override is None else float(manual_focus_override)
    if manual_secs > 0:
        _type_terms_foreground_focus(terms, manual_secs)
    else:
        try:
            dlg = _ensure_open_account_dialog(app)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1

        for term in terms:
            try:
                try:
                    dlg.set_focus()
                except Exception:
                    pass
                _type_search_term(dlg, term)
                logger.info(
                    'SEARCH ok term=%r (MT5 should have triggered a lookup; no login performed)',
                    term,
                )
                time.sleep(BETWEEN_BROKERS_S)
            except Exception as exc:
                logger.exception('SEARCH fail term=%r err=%s', term, exc)
                time.sleep(BETWEEN_BROKERS_S)

    if CLOSE_DIALOG_AT_END:
        _close_open_account_dialog(app)

    finished = datetime.now(timezone.utc)
    logger.info("=== run finish (UTC %s) ===", finished.isoformat())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed MT5 broker/server discovery via UI automation.")
    parser.add_argument(
        "--dump-dialog",
        action="store_true",
        help="Print UIA control tree for the Open an Account dialog (wizard must be visible, or USE_FILE_MENU).",
    )
    parser.add_argument(
        "--manual-focus",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Wait SEC seconds (click MT5 company search); then type to foreground. "
            "Default uses MANUAL_FOCUS_SECONDS in this file (10). Pass 0 to find the "
            "dialog with UI automation instead."
        ),
    )
    args = parser.parse_args(argv)

    if not args.dump_dialog:
        return run_once(manual_focus_override=args.manual_focus)

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

    try:
        dlg = _ensure_open_account_dialog(app)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    out_path = Path(__file__).resolve().parent / "mt5_open_account_uia_tree.txt"
    dlg.print_control_identifiers(depth=None, filename=str(out_path))
    print(f"Wrote {out_path}")
    _close_open_account_dialog(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
