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


def test_ascii_table_narrow_one_metric_per_line(monkeypatch):
    monkeypatch.setenv("FXJ_ASCII_LOG_LINE_MAX", "80")
    block = logging_utils.ascii_table_narrow(
        "Title",
        [("Short", "v"), ("Wide metric name here", "x" * 100)],
    )
    assert block.startswith("Title\n")
    assert "  Short: v" in block
    assert "+-" not in block
    assert "..." in block


def test_log_ascii_table_uses_narrow_when_layout_env_set(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("FXJ_ASCII_LOG_LAYOUT", "narrow")
    monkeypatch.setenv("FXJ_ASCII_LOG_LINE_MAX", "90")
    caplog.set_level(logging.INFO, logger="testlog")

    logging_utils.log_ascii_table(
        logging.getLogger("testlog"),
        "Box Check",
        [("K", "1")],
    )
    assert "  K: 1" in caplog.text
    assert "+-" not in caplog.text


def test_log_ascii_table_uses_box_when_layout_table(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("FXJ_ASCII_LOG_LAYOUT", "table")
    caplog.set_level(logging.INFO, logger="testlog2")

    logging_utils.log_ascii_table(
        logging.getLogger("testlog2"),
        "Boxed",
        [("K", "1")],
    )
    assert "+-" in caplog.text
    assert "| Metric" in caplog.text
