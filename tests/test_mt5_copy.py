from helpers.mt5_copy import is_exness_mt5_server, is_exness_mt5_submission


def test_is_exness_mt5_server_matches_conservatively():
    assert is_exness_mt5_server("Exness-MT5Real")
    assert is_exness_mt5_server("exness-mt5trial")
    assert is_exness_mt5_server("  EXNESS-Real  ")

    assert not is_exness_mt5_server("ICMarketsSC-Live")
    assert not is_exness_mt5_server("")
    assert not is_exness_mt5_server(None)


def test_is_exness_mt5_submission_checks_available_broker_or_server_values():
    assert is_exness_mt5_submission("Broker-Live", "Exness")
    assert is_exness_mt5_submission(None, "exness mt5")

    assert not is_exness_mt5_submission("Broker-Live", "IC Markets")
