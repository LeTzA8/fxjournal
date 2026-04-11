from celery_workers import logging_utils


def test_ascii_table_truncates_long_values_by_default(monkeypatch):
    monkeypatch.delenv("FXJ_ASCII_LOG_MAX_WIDTH", raising=False)
    long_val = "x" * 120
    table = logging_utils.ascii_table("T", [("Key", long_val)])
    assert "..." in table
    assert "x" * 72 not in table


def test_ascii_table_full_width_when_env_zero(monkeypatch):
    monkeypatch.setenv("FXJ_ASCII_LOG_MAX_WIDTH", "0")
    long_val = "y" * 500
    table = logging_utils.ascii_table("T", [("Key", long_val)])
    assert "y" * 500 in table


def test_ascii_table_custom_numeric_width(monkeypatch):
    monkeypatch.setenv("FXJ_ASCII_LOG_MAX_WIDTH", "200")
    long_val = "z" * 180
    table = logging_utils.ascii_table("T", [("Key", long_val)])
    assert "z" * 180 in table
    assert "..." not in table
