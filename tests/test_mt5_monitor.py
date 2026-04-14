import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_mt5_monitor_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "mt5_monitor.py"
    spec = importlib.util.spec_from_file_location("fxj_mt5_monitor_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sync_diag_snapshot_filters_to_active_accounts_and_picks_worst_lag(monkeypatch):
    mt5_monitor = _load_mt5_monitor_module()

    accounts = [
        SimpleNamespace(id=18, account_number="26215719", server="FivePercentOnline-Real", is_active=True, archived_at=None),
        SimpleNamespace(id=19, account_number="401104229", server="XMGlobal-MT5 15", is_active=False, archived_at=None),
        SimpleNamespace(id=22, account_number="99900011", server="Broker-Server", is_active=True, archived_at=None),
    ]

    monkeypatch.setattr(
        mt5_monitor,
        "list_worker_states",
        lambda worker_kind: [
            {
                "worker_id": "18",
                "history_stale": "1",
                "latest_deal_lag_minutes": "461",
                "latest_deal_utc": "2026-04-14T07:02:53+00:00",
                "latest_deal_position": "536321849",
                "open_positions": "0",
                "db_open_mt5_count": "1",
            },
            {
                "worker_id": "19",
                "history_stale": "1",
                "latest_deal_lag_minutes": "900",
            },
            {
                "worker_id": "22",
                "history_stale": "0",
                "latest_deal_lag_minutes": "15",
            },
        ],
    )

    snapshot = mt5_monitor._sync_diag_snapshot(accounts)

    assert snapshot["ok"] is True
    assert len(snapshot["rows"]) == 2
    assert len(snapshot["stale_rows"]) == 1
    assert snapshot["worst"]["mt5_account_id"] == "18"
    assert snapshot["worst"]["latest_deal_lag_minutes"] == 461


def test_sync_diag_block_renders_stale_account_details():
    mt5_monitor = _load_mt5_monitor_module()

    snapshot = {
        "ok": True,
        "stale_rows": [
            {
                "mt5_account_id": "18",
                "account_number": "26215719",
                "latest_deal_lag_minutes": 461,
                "latest_deal_utc": "2026-04-14T07:02:53+00:00",
                "latest_deal_position": "536321849",
                "open_positions": 0,
                "db_open_mt5_count": 1,
            }
        ],
        "worst": {
            "mt5_account_id": "18",
            "account_number": "26215719",
            "latest_deal_lag_minutes": 461,
            "latest_deal_utc": "2026-04-14T07:02:53+00:00",
            "latest_deal_position": "536321849",
            "open_positions": 0,
            "db_open_mt5_count": 1,
        },
    }

    rendered = "\n".join(mt5_monitor._ansi_strip(line) for line in mt5_monitor._sync_diag_block(snapshot))

    assert "SYNC DIAGNOSTICS" in rendered
    assert "26215719" in rendered
    assert "7h41m" in rendered
    assert "04-14 07:02:53" in rendered
    assert "536321849" in rendered
