"""Tests for the futures proxy guardrail prompt file.

Verifies:
- prompts/futures_proxy_replay_guardrails.txt exists and contains required rules
- Existing prompt files are NOT modified by the Phase 1 scaffold
"""

from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load(filename):
    path = PROMPTS_DIR / filename
    assert path.exists(), f"Expected prompt file not found: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# New guardrail file
# ---------------------------------------------------------------------------

def test_guardrail_file_exists():
    path = PROMPTS_DIR / "futures_proxy_replay_guardrails.txt"
    assert path.exists(), "prompts/futures_proxy_replay_guardrails.txt must exist"
    assert path.stat().st_size > 0, "guardrail file must not be empty"


def test_guardrail_file_contains_replay_accuracy_rule():
    content = _load("futures_proxy_replay_guardrails.txt")
    assert "replay_accuracy" in content
    assert "approximate" in content


def test_guardrail_file_blocks_exact_price_claims():
    content = _load("futures_proxy_replay_guardrails.txt")
    assert "Do NOT" in content or "NOT ALLOWED" in content
    # At least three of the banned exact-price phrases must appear
    banned_phrases = [
        "entered exactly",
        "points below",
        "MFE was exactly",
        "missed your TP",
        "liquidity sweep",
    ]
    found = [p for p in banned_phrases if p in content]
    assert len(found) >= 3, f"Expected ≥3 banned phrases, found: {found}"


def test_guardrail_file_allows_timing_language():
    content = _load("futures_proxy_replay_guardrails.txt")
    for phrase in ("during", "around", "session", "timing"):
        assert phrase in content, f"Expected timing word '{phrase}' in guardrail file"


def test_guardrail_file_names_suppressed_fields():
    content = _load("futures_proxy_replay_guardrails.txt")
    for field in ("mfe_r", "mae_r", "tp_capture_pct", "post_exit_direction"):
        assert field in content, f"Expected suppressed field '{field}' in guardrail file"


def test_guardrail_file_names_source_of_truth_fields():
    content = _load("futures_proxy_replay_guardrails.txt")
    for field in ("entry_price", "exit_price", "entry_time", "pnl"):
        assert field in content, f"Expected source-of-truth field '{field}' in guardrail file"


def test_guardrail_file_has_disclaimer_section():
    content = _load("futures_proxy_replay_guardrails.txt")
    assert "DISCLAIMER" in content


def test_guardrail_file_states_it_is_not_wired_yet():
    """Phase 1 contract: the file must note it is not yet wired into existing prompts."""
    content = _load("futures_proxy_replay_guardrails.txt")
    assert "Phase 1" in content or "NOT wired" in content or "not wired" in content


# ---------------------------------------------------------------------------
# Regression: existing prompt files are unchanged
# ---------------------------------------------------------------------------

def test_dashboard_advice_prompt_has_no_proxy_replay_section():
    content = _load("dashboard_advice.txt")
    assert "APPROXIMATE REPLAY GUARDRAILS" not in content
    assert "replay_accuracy" not in content


def test_dashboard_advice_rewrite_prompt_has_no_proxy_replay_section():
    content = _load("dashboard_advice_rewrite.txt")
    assert "APPROXIMATE REPLAY GUARDRAILS" not in content
    assert "replay_accuracy" not in content


def test_followup_prompt_has_no_proxy_replay_section():
    content = _load("weekly_review_followup.txt")
    assert "APPROXIMATE REPLAY GUARDRAILS" not in content
    assert "replay_accuracy" not in content
