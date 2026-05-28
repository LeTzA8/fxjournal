"""Scenario registry for waitlist CTA QA fixtures."""

from __future__ import annotations

FIXTURE_VERSION = "1"

SCENARIO_REGISTRY = {
    "replay_lock": {
        "label": "Replay lock (1m)",
        "email": "dummy-cta-replay@myfxjournal.test",
        "username": "DUMMY CTA Replay",
        "expected_cta": {
            "tier_intent": "trader",
            "feature_interest": "advanced_replay",
            "source": "replay_lock",
            "cta_context": "trade_replay_1m",
        },
        "test_path_template": "/dashboard/trades/{trade_pubkey}",
    },
    "ai_followup": {
        "label": "AI follow-up exhausted",
        "email": "dummy-cta-ai-followup@myfxjournal.test",
        "username": "DUMMY CTA AI Followup",
        "expected_cta": {
            "tier_intent": "trader",
            "feature_interest": "weekly_followup_chat",
            "source": "ai_followup_lock",
            "cta_context": "weekly_followup_trial_limit",
        },
        "test_path": "/dashboard",
    },
    "mt5_expired": {
        "label": "MT5 trial expired",
        "email": "dummy-cta-mt5-expired@myfxjournal.test",
        "username": "DUMMY CTA MT5 Expired",
        "expected_cta": [
            {
                "source": "mt5_trial_expired",
                "feature_interest": "mt5_trial_extension",
                "cta_context": "mt5_trial_expired_extension",
            },
            {
                "source": "mt5_trial_expired",
                "feature_interest": "mt5_sync",
                "cta_context": "mt5_trial_expired_waitlist",
            },
        ],
        "test_path": "/dashboard",
    },
    "mt5_paused": {
        "label": "MT5 sync paused",
        "email": "dummy-cta-mt5-paused@myfxjournal.test",
        "username": "DUMMY CTA MT5 Paused",
        "expected_cta": {
            "source": "mt5_trial_expired",
            "feature_interest": "mt5_sync",
            "cta_context": "mt5_sync_paused_waitlist",
        },
        "test_path": "/dashboard",
    },
    "zero_data": {
        "label": "Zero-data baseline",
        "email": "dummy-cta-zero-data@myfxjournal.test",
        "username": "DUMMY CTA Zero Data",
        "expected_cta": None,
        "test_path": "/dashboard",
    },
    "normal_active": {
        "label": "Normal active control",
        "email": "dummy-cta-normal-active@myfxjournal.test",
        "username": "DUMMY CTA Normal Active",
        "expected_cta": None,
        "test_path": "/dashboard",
    },
}


def all_scenario_keys():
    return list(SCENARIO_REGISTRY.keys())


def fixture_emails_for_keys(keys):
    return [SCENARIO_REGISTRY[key]["email"] for key in keys]
