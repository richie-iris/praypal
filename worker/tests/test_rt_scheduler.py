"""test_rt_scheduler.py — Unit tests for scheduler job execution, timestamp normalization, and retries."""
from __future__ import annotations

from unittest.mock import patch
from rt_scheduler import (
    _normalize_iso,
    calling_window,
    execute_job,
    mark_job_done,
    MAX_ATTEMPTS,
)


def test_normalize_iso():
    assert _normalize_iso("2026-08-22 14:30:00") == "2026-08-22T14:30:00"
    assert _normalize_iso("2026-08-22T14:30:00Z") == "2026-08-22T14:30:00+00:00"
    assert _normalize_iso("2026-08-22T14:30:00Z+00:00") == "2026-08-22T14:30:00+00:00"
    assert _normalize_iso("2026-08-22T14:30:00+0000") == "2026-08-22T14:30:00+00:00"


def test_calling_window():
    lo, hi = calling_window(requested_live=False)
    assert lo == 8
    assert hi == 21

    lo_live, hi_live = calling_window(requested_live=True)
    assert lo_live == 7
    assert hi_live == 23


def test_execute_job_unknown_type():
    res = execute_job({"job_type": "teleportation", "payload": {}})
    assert res.get("error") is True
    assert "unknown job type" in res.get("message", "")


def test_mark_job_done_retry_transition():
    with patch("rt_prefs._req") as mock_req:
        mock_req.return_value = True

        # Transient error, attempts = 1 (< MAX_ATTEMPTS) -> status should be 'pending'
        mark_job_done(101, {"error": True, "message": "SIP timeout"}, attempts=1)
        mock_req.assert_called_with("POST", "rpc/rt_update_job_status", {
            "p_id": 101,
            "p_status": "pending",
            "p_result": '{"error": true, "message": "SIP timeout"}',
        })

        # Terminal error, attempts = MAX_ATTEMPTS -> status should be 'failed'
        mark_job_done(102, {"error": True, "message": "Line busy"}, attempts=MAX_ATTEMPTS)
        mock_req.assert_called_with("POST", "rpc/rt_update_job_status", {
            "p_id": 102,
            "p_status": "failed",
            "p_result": '{"error": true, "message": "Line busy"}',
        })

        # Success -> status should be 'done'
        mark_job_done(103, {"error": False, "message": "Delivered"}, attempts=1)
        mock_req.assert_called_with("POST", "rpc/rt_update_job_status", {
            "p_id": 103,
            "p_status": "done",
            "p_result": '{"error": false, "message": "Delivered"}',
        })


def test_parse_when_relative_and_iso():
    from rt_scheduler import _parse_when

    dt_iso = _parse_when("2026-09-01T10:00:00-04:00")
    assert dt_iso.hour == 10
    assert dt_iso.minute == 0

    dt_rel = _parse_when("tomorrow 10am", caller_e164="+19175551234")
    assert dt_rel.hour == 10
    assert dt_rel.minute == 0

    dt_pm = _parse_when("tomorrow at 3pm", caller_e164="+19175551234")
    assert dt_pm.hour == 15

    dt_in = _parse_when("in 2 hours", caller_e164="+19175551234")
    assert dt_in is not None
