"""MT5 per-account terminal folder naming and path resolution."""

from __future__ import annotations

import os
import re

_WINDOWS_SAFE_FOLDER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_FOLDER_NAME_LEN = 120


def build_legacy_mt5_terminal_folder_name(user_id: int, trade_account_id: int) -> str:
    """Pre-label folder name: ``mt5_{user_id}_{trade_account_id}``."""
    return f"mt5_{int(user_id)}_{int(trade_account_id)}"


def build_mt5_terminal_folder_name(
    user_id: int,
    trade_account_id: int,
    mt5_login: str | None = None,
) -> str:
    """
    Labeled per-account terminal folder name.

    ``mt5_login`` is accepted for API compatibility but is intentionally
    omitted from the folder name — MT5 account numbers are sensitive and
    ``trade_account_id`` already uniquely identifies the terminal slot.
    """
    _ = mt5_login
    folder_name = f"mt5_uid{int(user_id)}_taid{int(trade_account_id)}"
    if not is_windows_safe_terminal_folder_name(folder_name):
        raise ValueError(f"unsafe MT5 terminal folder name: {folder_name}")
    return folder_name


def is_windows_safe_terminal_folder_name(folder_name: str) -> bool:
    name = str(folder_name or "").strip()
    if not name or len(name) > _MAX_FOLDER_NAME_LEN:
        return False
    return _WINDOWS_SAFE_FOLDER_RE.fullmatch(name) is not None


def resolve_mt5_terminal_dir(
    *,
    terminals_root: str,
    user_id: int,
    trade_account_id: int,
    terminal_path: str | None = None,
) -> str:
    """
    Resolve the terminal directory for setup/cleanup fallbacks.

    Priority:
    1. Persisted ``terminal_path`` parent when set.
    2. Existing labeled folder on disk.
    3. Existing legacy ``mt5_{uid}_{taid}`` folder on disk.
    4. Labeled folder path for new terminal creation.
    """
    persisted = str(terminal_path or "").strip()
    if persisted:
        return os.path.dirname(os.path.abspath(persisted))

    root = str(terminals_root or "").strip() or r"C:\MT5Terminals"
    labeled_name = build_mt5_terminal_folder_name(user_id, trade_account_id)
    labeled_dir = os.path.join(root, labeled_name)
    if os.path.isdir(labeled_dir):
        return labeled_dir

    legacy_name = build_legacy_mt5_terminal_folder_name(user_id, trade_account_id)
    legacy_dir = os.path.join(root, legacy_name)
    if os.path.isdir(legacy_dir):
        return legacy_dir

    return labeled_dir


def build_mt5_terminal_exe_path(terminal_dir: str) -> str:
    return os.path.join(str(terminal_dir), "terminal64.exe")
