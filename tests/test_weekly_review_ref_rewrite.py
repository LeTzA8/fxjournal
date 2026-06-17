import json

from helpers.weekly_review_ref_rewrite import (
    build_weekly_review_citation_lookup,
    unwrap_bracketed_citation_labels,
)


def test_citation_lookup_uses_trade_date_label():
    payload = {
        "trades": [
            {
                "ref": "T1",
                "symbol": "NAS100",
                "trade_date_label": "15 May 2026 (Fri)",
                "pnl": 100.0,
            }
        ]
    }
    lookup = build_weekly_review_citation_lookup(json.dumps(payload), "UTC")
    assert lookup["T1"]["label"] == "NAS100 | 15 May 2026 (Fri)"


def test_citation_lookup_falls_back_to_opened_at():
    payload = {
        "trades": [
            {
                "ref": "T1",
                "symbol": "EURUSD",
                "opened_at": "2026-03-10T10:00:00Z",
                "pnl": -50.0,
            }
        ]
    }
    lookup = build_weekly_review_citation_lookup(json.dumps(payload), "UTC")
    assert "10 Mar 2026" in lookup["T1"]["label"]


def test_citation_lookup_carries_row_resolution_metadata():
    payload = {
        "trades": [
            {
                "ref": "B1",
                "trade_id": "202",
                "trade_pubkey": "trade-pubkey-202",
                "symbol": "GBPUSD",
                "trade_date_label": "02 Apr 2026 (Thu)",
                "pnl": -42.0,
                "is_bundle": True,
                "bundle_pubkey": "bundle-xyz",
            }
        ]
    }

    lookup = build_weekly_review_citation_lookup(json.dumps(payload), "UTC")

    assert lookup["B1"]["type"] == "bundle"
    assert lookup["B1"]["trade_id"] == 202
    assert lookup["B1"]["trade_pubkey"] == "trade-pubkey-202"
    assert lookup["B1"]["bundle_key"] == "bundle-xyz"


def test_unwrap_bracketed_citation_labels_strips_known_labels():
    lookup = build_weekly_review_citation_lookup(
        json.dumps(
            {
                "trades": [
                    {
                        "ref": "B1",
                        "symbol": "MES",
                        "trade_date_label": "08 Jun 2026 (Mon)",
                        "is_bundle": True,
                        "pnl": -1.0,
                    }
                ]
            }
        ),
        "UTC",
    )
    out = unwrap_bracketed_citation_labels(
        "The loss on [MES bundle | 08 Jun 2026 (Mon)] was costly.",
        lookup,
    )
    assert out == "The loss on MES bundle | 08 Jun 2026 (Mon) was costly."
