"""
Seed MT5's broker/server discovery cache by driving the built-in UI search flow.

- Does not log into any account and does not touch servers.dat on disk.
- After the Open an Account dialog is found, the script **waits until the company
  search field has keyboard focus** (you click into it). No fixed sleep — it polls UIA.
- Then for each broker: **re-click the search field → type → Find your company → wait**.
- Set `COMPANY_FIELD_FOCUS_TIMEOUT_S = 0` or `--focus-timeout 0` to skip waiting (the
  script will focus the edit itself). The File menu is not used unless `USE_FILE_MENU`.
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
# After clicking “Find your company”, give MT5 time to query before the next broker.
AFTER_FIND_CLICK_S = 6.0
# Brief pause after typing, before pressing Find (lets the field settle).
PAUSE_BEFORE_FIND_CLICK_S = 0.25
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

# Max seconds to wait for you to click the company search field (UIA keyboard focus).
# 0 = skip (first cycle uses script click to focus the edit).
COMPANY_FIELD_FOCUS_TIMEOUT_S = 120.0
FOCUS_POLL_INTERVAL_S = 0.2

# “Find your company” button (UIA title/name). Regex, case-insensitive.
FIND_COMPANY_BUTTON_TITLE_RE = r"Find.*your.*company"

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


def _uia_raw_element(ctrl) -> object | None:
    """Return the underlying IUIAutomationElement for a pywinauto control, if available."""
    try:
        w = ctrl.wrapper_object() if hasattr(ctrl, "wrapper_object") else ctrl
        ei = getattr(w, "element_info", None)
        if ei is None:
            return None
        for attr in ("_element", "element", "iface", "interface"):
            el = getattr(ei, attr, None)
            if el is None:
                continue
            try:
                _ = el.CurrentHasKeyboardFocus
                return el
            except Exception:
                continue
    except Exception:
        return None
    return None


def _control_has_keyboard_focus(ctrl) -> bool:
    el = _uia_raw_element(ctrl)
    if el is None:
        return False
    try:
        return bool(el.CurrentHasKeyboardFocus)
    except Exception:
        return False


def _uia_get_focused_element():
    try:
        from pywinauto.uia_defines import IUIA

        core = IUIA()
        if hasattr(core, "iuia") and hasattr(core.iuia, "GetFocusedElement"):
            return core.iuia.GetFocusedElement()
        if hasattr(core, "GetFocusedElement"):
            return core.GetFocusedElement()
        iuia = getattr(core, "iuia", None)
        if iuia is not None:
            return iuia.GetFocusedElement()
    except Exception:
        return None
    return None


def _uia_runtime_ids_equal(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        ra = list(a.GetRuntimeId())
        rb = list(b.GetRuntimeId())
        return bool(ra) and ra == rb
    except Exception:
        return False


def _focused_element_is_company_edit(edit) -> bool:
    mine = _uia_raw_element(edit)
    focused = _uia_get_focused_element()
    if mine is None or focused is None:
        return False
    return _uia_runtime_ids_equal(focused, mine)


def _wait_until_company_edit_focused(dlg, timeout_s: float) -> bool:
    if timeout_s <= 0:
        return True
    logger.info(
        "Waiting up to %.0fs for keyboard focus in the company search field (click it when ready)…",
        timeout_s,
    )
    print(
        "\n>>> Click the company search box in Open an Account — script continues when it has focus… <<<\n",
        flush=True,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            edit = _pick_company_search_edit(dlg)
            if edit is not None and (
                _control_has_keyboard_focus(edit) or _focused_element_is_company_edit(edit)
            ):
                logger.info("Company search field has keyboard focus — starting broker list.")
                return True
        except Exception:
            pass
        time.sleep(FOCUS_POLL_INTERVAL_S)
    logger.error(
        "Timed out after %.0fs — the company search field never received keyboard focus.",
        timeout_s,
    )
    return False


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
        "click the company search field (or increase COMPANY_FIELD_FOCUS_TIMEOUT_S), "
        "increase EXISTING_DIALOG_WAIT_S, widen OPEN_ACCOUNT_DIALOG_TITLE_RE, or set "
        "USE_FILE_MENU = True to open the wizard via the File menu."
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


def _pick_company_search_edit(dlg) -> object | None:
    edits = [w for w in dlg.descendants(control_type="Edit")]
    if not edits:
        return None
    for ed in edits:
        try:
            ei = getattr(ed, "element_info", None)
            en = getattr(ei, "name", None) if ei is not None else None
            label = (ed.window_text() or "") + " " + (en or "")
        except Exception:
            label = ""
        if "company" in label.lower() or "company.com" in label.lower():
            return ed
    return edits[0]


def _focus_company_search_edit(dlg) -> None:
    target = _pick_company_search_edit(dlg)
    if target is None:
        try:
            dlg.set_focus()
        except Exception:
            pass
        return
    try:
        target.click_input()
    except Exception:
        try:
            target.set_focus()
        except Exception:
            pass


def _clear_and_type_company_term(dlg, term: str) -> None:
    from pywinauto.keyboard import send_keys

    target = _pick_company_search_edit(dlg)
    if target is not None:
        try:
            target.set_focus()
        except Exception:
            pass
        send_keys("^a{BACKSPACE}", pause=0.05)
        target.type_keys(term, with_spaces=True, pause=0.03)
    else:
        try:
            dlg.set_focus()
        except Exception:
            pass
        send_keys("^a{BACKSPACE}", pause=0.05)
        send_keys(term.replace("{", "{{").replace("}", "}}"), with_spaces=True, pause=0.03)


def _button_label(btn) -> str:
    try:
        ei = getattr(btn, "element_info", None)
        en = getattr(ei, "name", None) if ei is not None else None
        return ((btn.window_text() or "") + " " + (en or "")).strip()
    except Exception:
        return (btn.window_text() or "").strip()


def _click_find_company_button(dlg) -> None:
    try:
        btn = dlg.child_window(title_re=FIND_COMPANY_BUTTON_TITLE_RE, control_type="Button")
        if btn.exists(timeout=2):
            btn.click_input()
            logger.info("Clicked Find-company button (child_window title_re).")
            return
    except Exception:
        pass
    for w in dlg.descendants(control_type="Button"):
        label = _button_label(w)
        if not label:
            continue
        if re.search(FIND_COMPANY_BUTTON_TITLE_RE, label, re.I):
            w.click_input()
            logger.info("Clicked Find-company button (matched %r).", label)
            return
    raise RuntimeError(
        "Could not find the 'Find your company' button — run --dump-dialog and adjust "
        "FIND_COMPANY_BUTTON_TITLE_RE if your UI language differs."
    )


def _broker_seed_cycle(dlg, term: str) -> None:
    _focus_company_search_edit(dlg)
    _clear_and_type_company_term(dlg, term)
    time.sleep(PAUSE_BEFORE_FIND_CLICK_S)
    _click_find_company_button(dlg)
    time.sleep(AFTER_FIND_CLICK_S)


def _close_open_account_dialog(app) -> None:
    from pywinauto.keyboard import send_keys

    try:
        dlg = _open_account_dialog_spec(app)
        dlg.set_focus()
    except Exception:
        pass
    send_keys("{ESC}", pause=0.05)
    time.sleep(0.5)


def run_once(focus_timeout_override: float | None = None) -> int:
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

    focus_timeout = (
        COMPANY_FIELD_FOCUS_TIMEOUT_S
        if focus_timeout_override is None
        else float(focus_timeout_override)
    )

    try:
        dlg = _ensure_open_account_dialog(app)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    try:
        dlg.set_focus()
    except Exception:
        pass
    time.sleep(AFTER_MENU_OPEN_S)

    if focus_timeout > 0 and not _wait_until_company_edit_focused(dlg, focus_timeout):
        return 1

    for term in terms:
        try:
            _broker_seed_cycle(dlg, term)
            logger.info(
                'SEARCH ok term=%r (typed + Find clicked; no login performed)',
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
        "--focus-timeout",
        "--manual-focus",
        type=float,
        default=None,
        dest="focus_timeout",
        metavar="SEC",
        help=(
            "Max seconds to wait for you to click the company search field (UIA focus). "
            "Default uses COMPANY_FIELD_FOCUS_TIMEOUT_S in this file. Pass 0 to skip."
        ),
    )
    args = parser.parse_args(argv)

    if not args.dump_dialog:
        return run_once(focus_timeout_override=args.focus_timeout)

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
