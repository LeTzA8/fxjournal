import importlib.util
from pathlib import Path


def _load_migration_module(file_name: str):
    migrations_dir = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    module_path = migrations_dir / file_name
    spec = importlib.util.spec_from_file_location(file_name.replace(".py", ""), module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_waitlist_and_entitlement_migrations_are_ordered():
    waitlist = _load_migration_module("20260510_0055_upgrade_waitlist.py")
    entitlements = _load_migration_module("20260510_0056_entitlement_fields.py")
    waitlist_intent = _load_migration_module("20260510_0057_waitlist_intent_fields.py")
    journal_mvp = _load_migration_module("20260512_0057_journal_mvp.py")
    premium_trial = _load_migration_module("20260517_0058_premium_trial_started_at.py")
    last_active = _load_migration_module("20260524_0059_user_last_active_at.py")

    assert waitlist.revision == "20260510_0055"
    assert waitlist.down_revision == "20260426_0054"
    assert entitlements.revision == "20260510_0056"
    assert entitlements.down_revision == "20260510_0055"
    assert waitlist_intent.revision == "20260510_0057"
    assert waitlist_intent.down_revision == "20260510_0056"
    assert journal_mvp.revision == "20260512_0057"
    assert journal_mvp.down_revision == "20260510_0057"
    assert premium_trial.revision == "20260517_0058"
    assert premium_trial.down_revision == "20260512_0057"
    assert last_active.revision == "20260524_0059"
    assert last_active.down_revision == "20260517_0058"
