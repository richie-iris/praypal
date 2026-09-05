"""test_rt_logger.py — Unit tests for JSON structured logger and context binding."""
from __future__ import annotations

import json
from rt_logger import get_logger


def test_logger_json_output(capsys):
    logger = get_logger("unit_test")
    logger.info("Test message", test_key="test_value")

    captured = capsys.readouterr()
    assert captured.out.strip() != ""
    data = json.loads(captured.out.strip())

    assert data["module"] == "unit_test"
    assert data["level"] == "INFO"
    assert data["msg"] == "Test message"
    assert data["test_key"] == "test_value"
    assert "ts" in data


def test_logger_error_stderr(capsys):
    logger = get_logger("unit_test")
    logger.error("Something went wrong", error_code=500)

    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    data = json.loads(captured.err.strip())

    assert data["level"] == "ERROR"
    assert data["error_code"] == 500


def test_logger_bind(capsys):
    logger = get_logger("base_module")
    bound = logger.bind(call_id="call-999", caller_phone="+15551234567")

    bound.info("Processing call step", step="greeting")
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())

    assert data["call_id"] == "call-999"
    # Bound phone numbers are scrubbed on the way out (PII policy); the raw
    # digits must not reach the log line in any field.
    assert data["caller_phone"] == "[phone]"
    assert "15551234567" not in captured.out
    assert data["step"] == "greeting"


def test_logger_exception(capsys):
    logger = get_logger("error_module")
    try:
        raise ValueError("Invalid configuration value")
    except ValueError as exc:
        logger.exception("Operation failed", exc_info=exc)

    captured = capsys.readouterr()
    data = json.loads(captured.err.strip())
    assert data["level"] == "ERROR"
    assert data["exception_type"] == "ValueError"
    assert data["exception_msg"] == "Invalid configuration value"
