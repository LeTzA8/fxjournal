"""
MT5 broker list helper — keystrokes only (no UIA / no window tree).

Before running: open MetaTrader 5, show the company search field, click inside it
so it has keyboard focus. The script runs a CLI countdown, then types each line from
brokers_to_seed.txt and sends a submit key (default Enter). Whatever window is focused
receives the keys — keep MT5 in front.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & timing
# ---------------------------------------------------------------------------

BROKERS_FILE = Path(__file__).resolve().parent / "brokers_to_seed.txt"
LOG_FILE = Path(__file__).resolve().parent / "mt5_broker_seeding.log"

# CLI countdown (seconds) before first keystroke. 0 = skip.
CLI_COUNTDOWN_SECONDS = 10.0
CLI_COUNTDOWN_TICK_S = 0.15

# Pause after each line (after submit keys), so MT5 can query before the next name.
AFTER_EACH_TERM_S = 6.0

# pywinauto SendKeys sequence after each broker name (e.g. trigger search).
# Default: Enter. If your build needs the Find button, try "{TAB}{ENTER}" and tune.
SUBMIT_AFTER_EACH_TERM = "{ENTER}"

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
    logging.Formatter.converter = time.gmtime


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


def _cli_countdown(seconds: float) -> None:
    if seconds <= 0:
        return
    logger.info("CLI countdown %.1fs — click the MT5 company search field now.", seconds)
    print("", flush=True)
    end = time.monotonic() + seconds
    pad = 72
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        msg = f"  MT5 search field focused? Typing starts in {left:5.1f}s"
        print("\r" + msg + " " * max(0, pad - len(msg)), end="", flush=True)
        time.sleep(CLI_COUNTDOWN_TICK_S)
    print("\r" + " " * pad + "\r", end="")
    print("  Typing broker names now.\n", flush=True)


def _type_one_term(term: str) -> None:
    """Send Ctrl+A, clear, type term, submit — to whatever has keyboard focus."""
    from pywinauto.keyboard import send_keys

    send_keys("^a{BACKSPACE}", pause=0.05)
    send_keys(term.replace("{", "{{").replace("}", "}}"), with_spaces=True, pause=0.02)
    send_keys(SUBMIT_AFTER_EACH_TERM, pause=0.05)


def run_once(countdown_override: float | None = None) -> int:
    started = datetime.now(timezone.utc)
    _configure_logging(LOG_FILE)
    logger.info("=== run start (UTC %s) keystroke-only mode ===", started.isoformat())

    try:
        terms = _read_terms(BROKERS_FILE)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    if not terms:
        logger.error("No broker lines in %s", BROKERS_FILE)
        return 2

    countdown = (
        CLI_COUNTDOWN_SECONDS if countdown_override is None else float(countdown_override)
    )
    logger.info("countdown_sec=%.1f terms=%d", countdown, len(terms))

    if countdown > 0:
        _cli_countdown(countdown)

    for term in terms:
        try:
            _type_one_term(term)
            logger.info("Typed term=%r + submit", term)
            time.sleep(AFTER_EACH_TERM_S)
        except Exception:
            logger.exception("Failed term=%r", term)
            time.sleep(AFTER_EACH_TERM_S)

    logger.info("=== run finish (UTC %s) ===", datetime.now(timezone.utc).isoformat())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Countdown then type broker names + submit to the focused window."
    )
    parser.add_argument(
        "--countdown",
        "--focus-timeout",
        "--manual-focus",
        type=float,
        default=None,
        dest="countdown",
        metavar="SEC",
        help="Countdown seconds before typing (default: CLI_COUNTDOWN_SECONDS in script). 0=skip.",
    )
    args = parser.parse_args(argv)
    return run_once(countdown_override=args.countdown)


if __name__ == "__main__":
    raise SystemExit(main())
