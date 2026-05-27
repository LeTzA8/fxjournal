"""Backward-compatible aliases — use helpers.universal_weekly_payload instead."""

from helpers.universal_weekly_payload import (
    build_universal_weekly_payload as build_weekly_prompt_payload,
    format_universal_weekly_payload as format_weekly_prompt_payload,
)

__all__ = ["build_weekly_prompt_payload", "format_weekly_prompt_payload"]
