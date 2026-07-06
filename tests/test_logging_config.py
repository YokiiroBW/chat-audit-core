import json
import logging

from app.logging_config import FallbackJsonFormatter


def test_fallback_json_formatter_emits_structured_extra_fields():
    record = logging.LogRecord(
        name="chat-audit-test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event happened",
        args=(),
        exc_info=None,
    )
    record.robot_id = "10001"
    record.connection_id = "conn-1"

    payload = json.loads(FallbackJsonFormatter().format(record))

    assert payload["name"] == "chat-audit-test"
    assert payload["levelname"] == "INFO"
    assert payload["message"] == "event happened"
    assert payload["robot_id"] == "10001"
    assert payload["connection_id"] == "conn-1"
