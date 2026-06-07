"""Shared MT5 copy helpers."""

from __future__ import annotations


EXNESS_MT5_SETUP_UI_NOTE = (
    "Exness note: if you already submitted your MT5 details, you do not need to "
    "resubmit them right now. Your details are saved. We are aware of a "
    "broker-server discovery issue affecting some Exness accounts and will try "
    "to resolve it from our side first. We will contact you if we need updated "
    "details."
)

EXNESS_MT5_SETUP_EMAIL_NOTE_PARAGRAPHS = (
    "Note for Exness users: we are currently seeing an MT5 broker-server "
    "discovery issue with some Exness accounts.",
    "If you have already submitted your MT5 login, investor password, and "
    "server name, you do not need to resubmit them right now. Your details are "
    "saved. The issue may be on the connection/setup side, not necessarily "
    "with the details you entered.",
    "We will try to resolve it from our side first, and we will contact you if "
    "we need updated details.",
)

EXNESS_MT5_SETUP_EMAIL_NOTE_TEXT = "\n\n".join(EXNESS_MT5_SETUP_EMAIL_NOTE_PARAGRAPHS)


def is_exness_mt5_server(server: str | None) -> bool:
    normalized_server = str(server or "").strip().casefold()
    return "exness" in normalized_server


def is_exness_mt5_submission(*broker_or_server_values: str | None) -> bool:
    return any(is_exness_mt5_server(value) for value in broker_or_server_values)


def is_exness_mt5_account(mt5_account) -> bool:
    return is_exness_mt5_server(getattr(mt5_account, "server", None))
