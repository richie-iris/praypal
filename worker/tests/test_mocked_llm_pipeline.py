"""test_mocked_llm_pipeline.py — Unit tests simulating full LLM extraction & synthesis without live Gemini API keys."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
import urllib.error
import io

from rt_postcall_worker import (
    process_post_call_transcript,
    _gemini_json,
    _compile_next_call_context,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_postcall_worker_with_mocked_gemini():
    """Verify end-to-end post-call pipeline execution with mocked Gemini response."""
    transcript_path = FIXTURES_DIR / "transcript_standard_call.txt"
    fixture_path = FIXTURES_DIR / "gemini_extraction_response.json"

    transcript = transcript_path.read_text(encoding="utf-8")
    mock_extracted = json.loads(fixture_path.read_text(encoding="utf-8"))

    rpc_calls = []

    def fake_rpc(method, endpoint, body=None, **kwargs):
        rpc_name = endpoint.replace("rpc/", "")
        rpc_calls.append((rpc_name, body or {}))
        if rpc_name == "rt_get_caller_full_bundle":
            return {
                "caller": {"display_name": "Friend", "loved_ones": ""},
                "schemas": [],
                "reminders": [],
                "facts": [],
            }
        return True

    with patch("rt_postcall_worker._gemini_json", return_value=mock_extracted), \
         patch("rt_prefs._req", side_effect=fake_rpc), \
         patch.dict("os.environ", {"GOOGLE_API_KEY": "fake-test-key"}):

        result = process_post_call_transcript("+15551234567", transcript, call_id="call-test-123")

        assert result.get("status") != "skipped"

        # Verify RPC calls saved extracted data
        called_names = [call[0] for call in rpc_calls]
        assert "rt_set_display_name" in called_names
        assert "rt_set_last_name" in called_names
        assert "rt_set_loved_ones" in called_names
        assert "rt_add_schema_entry" in called_names

        # Check display name set to Arthur
        name_call = next(c for c in rpc_calls if c[0] == "rt_set_display_name")
        assert name_call[1]["p_name"] == "Arthur"

        # Check last name set to Pendelton
        last_name_call = next(c for c in rpc_calls if c[0] == "rt_set_last_name")
        assert last_name_call[1]["p_last"] == "Pendelton"


def test_gemini_json_parsing_and_markdown_fence_stripping():
    """Verify _gemini_json strips ```json markdown fences and parses clean dict."""
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": "```json\n{\"caller_name\": \"Arthur\", \"status\": \"ok\"}\n```"
                        }
                    ]
                }
            }
        ]
    }
    raw_bytes = json.dumps(payload).encode("utf-8")

    mock_resp = io.BytesIO(raw_bytes)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = _gemini_json("fake-key", "test prompt", retries=1)
        assert res == {"caller_name": "Arthur", "status": "ok"}


def test_gemini_json_retry_on_429_rate_limit():
    """Verify _gemini_json retries on HTTP 429 rate limit before succeeding."""
    success_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "{\"result\": \"success_after_retry\"}"}
                    ]
                }
            }
        ]
    }
    success_bytes = json.dumps(success_payload).encode("utf-8")

    error_429 = urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )

    side_effects = [error_429, io.BytesIO(success_bytes)]

    with patch("urllib.request.urlopen", side_effect=side_effects), \
         patch("time.sleep", return_value=None):
        res = _gemini_json("fake-key", "test prompt", retries=3)
        assert res == {"result": "success_after_retry"}


def test_compile_next_call_context_with_mocked_gemini():
    """Verify _compile_next_call_context compiles briefing note from mock bundle."""
    bundle_data = {
        "caller": {
            "display_name": "Arthur",
            "loved_ones": "Daughter Sarah (Denver)",
            "agent_alias": "Clara",
        },
        "schemas": [
            {
                "category": "family",
                "data_summary": "Daughter Sarah lives in Denver, visiting next Friday",
            }
        ],
        "reminders": [
            {"reminder_text": "Pick up birthday cake"}
        ],
        "facts": [
            {"kind": "thread", "value_text": "Sarah's visit next Friday"}
        ],
    }

    mock_briefing = {
        "next_call_context": "Arthur is expecting his daughter Sarah from Denver next Friday. Check in on how the visit went and if he picked up the birthday cake."
    }

    rpc_saves = []

    def fake_rpc(method, endpoint, body=None, **kwargs):
        rpc_name = endpoint.replace("rpc/", "")
        if rpc_name == "rt_get_caller_full_bundle":
            return bundle_data
        if rpc_name == "rt_set_caller_context":
            rpc_saves.append(body)
            return True
        return True

    with patch("rt_prefs._req", side_effect=fake_rpc), \
         patch("rt_postcall_worker._gemini_json", return_value=mock_briefing):
        ok = _compile_next_call_context("+15551234567", "hash123", "fake-api-key", "Arthur")
        assert ok is True
        assert len(rpc_saves) == 1
        assert "Sarah from Denver" in rpc_saves[0]["p_context"]
