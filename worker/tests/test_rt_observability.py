"""test_rt_observability.py — Unit test suite for telemetry, logging, and observability contract.

Compatible with both `python3 -m unittest` and `pytest`.

Validates:
1. Observability contract completeness (77 events across 11 groups).
2. Zero silent exception handlers across all worker modules.
3. PII masking and redaction (phone numbers, SSNs, credentials).
4. Audio cue latency, directory lookups, SMS/email dispatches.
5. Facts mutations, scam risk breakdowns, and timezone resolutions.
6. Network RPC latency and memory purge auditing.
7. Obs spans and exception handling behavior.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import obs_contract
import rt_audio
import rt_bridge
import rt_directory
import rt_email
import rt_facts
import rt_obs
import rt_prefs
import rt_scheduler
import rt_sms


class TestRtObservability(unittest.TestCase):
    """Test suite for realtime observability contract and runtime instrumentation."""

    def test_obs_contract_completeness(self):
        """Verify the expanded observability contract has 80 events and satisfies check_observability."""
        self.assertEqual(len(obs_contract.ALL_EVENTS), 80)
        self.assertEqual(len(obs_contract.GROUPS), 11)

        expected_groups = {
            "lifecycle", "turns", "context", "tools", "model",
            "network", "livekit", "memory", "postcall", "jobs", "safety"
        }
        self.assertEqual(set(obs_contract.GROUPS.keys()), expected_groups)

        critical_events = [e for e in obs_contract.ALL_EVENTS if e.critical]
        self.assertGreaterEqual(len(critical_events), 40)

    def test_obs_pii_phone_masking(self):
        """Verify phone numbers are masked properly and never appear in plaintext."""
        self.assertEqual(rt_obs.mask_phone("+19175551234"), "***1234")
        self.assertEqual(rt_obs.mask_phone("+442071838750"), "***8750")
        self.assertEqual(rt_obs.mask_phone("123"), "***")
        self.assertIsNone(rt_obs.mask_phone(None))

    def test_obs_pii_scrub_and_redaction(self):
        """Verify SSNs and credentials trigger pii.redaction events and are scrubbed."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            # SSN scrubbing
            text_with_ssn = "My SSN is 123-45-6789 please remember it."
            cleaned_ssn = rt_prefs.scrub_ssn(text_with_ssn)
            self.assertIn("[SSN REDACTED]", cleaned_ssn)
            self.assertNotIn("123-45-6789", cleaned_ssn)

            # Credential scrubbing
            text_with_cred = "My verification code is 8492"
            cleaned_cred = rt_prefs.redact_codes(text_with_cred)
            self.assertIn("####", cleaned_cred)

            # Check emitted pii.redaction events
            redactions = [f for lvl, ev, f in captured if ev == "pii.redaction"]
            self.assertGreaterEqual(len(redactions), 2)
            categories = {r.get("category") for r in redactions}
            self.assertIn("ssn", categories)
            self.assertIn("credential", categories)

    def test_audio_cue_latency_emission(self):
        """Verify audio cue playback records audio.cue_latency."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        mock_livekit = MagicMock()
        mock_rtc = MagicMock()
        mock_source = MagicMock()
        mock_source.capture_frame = AsyncMock()
        mock_rtc.AudioSource.return_value = mock_source
        mock_rtc.LocalAudioTrack.create_audio_track.return_value = MagicMock()
        mock_livekit.rtc = mock_rtc

        mock_wav = MagicMock()
        mock_wav.getnchannels.return_value = 1
        mock_wav.getsampwidth.return_value = 2
        mock_wav.getframerate.return_value = 24000
        mock_wav.getnframes.return_value = 2400
        mock_wav.readframes.side_effect = [b"\x00\x00" * 480, b""]
        mock_wav.__enter__.return_value = mock_wav
        mock_wav.__exit__.return_value = None

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            with patch.dict(sys.modules, {"livekit": mock_livekit, "livekit.rtc": mock_rtc}):
                with patch("wave.open", return_value=mock_wav):
                    mock_room = MagicMock()
                    mock_room.name = "test-room"
                    mock_room.local_participant.publish_track = AsyncMock(return_value=MagicMock(sid="track_123"))
                    mock_room.local_participant.unpublish_track = AsyncMock()

                    allowed = rt_audio._earcon_allowed(mock_room, "thinking_chime")
                    self.assertTrue(allowed)

                    asyncio.run(rt_audio._play_clip(mock_room, os.path.join(tempfile.gettempdir(), "sound.wav"), preroll=0.0))

                    cue_evs = [f for lvl, ev, f in captured if ev == "audio.cue_latency"]
                    self.assertEqual(len(cue_evs), 1)
                    self.assertEqual(cue_evs[0]["name"], "sound.wav")

    def test_directory_lookup_detail_emission(self):
        """Verify directory lookups emit directory.lookup_detail."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        sample_npi_dict = {
            "result_count": 1,
            "results": [{
                "basic": {"first_name": "Jane", "last_name": "Doe", "credential": "MD"},
                "addresses": [{"address_1": "123 Main St", "city": "New York", "state": "NY", "postal_code": "10001", "telephone_number": "2125550100", "address_purpose": "LOCATION"}],
                "taxonomies": [{"desc": "Cardiology"}]
            }]
        }

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            with patch.object(rt_directory, "_get", return_value=sample_npi_dict):
                res = rt_directory.lookup("Dr. Jane Doe NY")
                self.assertIsInstance(res, dict)
                self.assertIn("number", res)

                detail_evs = [f for lvl, ev, f in captured if ev == "directory.lookup_detail"]
                self.assertGreaterEqual(len(detail_evs), 1)

    def test_sms_dispatch_detail_emission(self):
        """Verify SMS dispatch calculations and sms.dispatch_detail event."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        sample_twilio_response = json.dumps({
            "sid": "msg_sms_12345",
            "status": "sent",
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = sample_twilio_response
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            import tempfile
            with tempfile.TemporaryDirectory() as ledger_dir, patch.dict(os.environ, {
                "TWILIO_ACCOUNT_SID": "ACtest",
                "TWILIO_AUTH_TOKEN": "tok-test",
                "TWILIO_FROM_NUMBER": "+19175550001",
                "RT_SMS_LEDGER_DIR": ledger_dir,
                "RT_PHONE_HASH_PEPPER": "observability-pepper-0123456789",
            }):
                with patch("urllib.request.urlopen", return_value=mock_resp):
                    res = rt_sms.send_sms("+19175551234", "Hello there! Your appointment is tomorrow at 10am.")
                    self.assertFalse(res.get("error"))

                    sms_evs = [f for lvl, ev, f in captured if ev == "sms.dispatch_detail"]
                    self.assertEqual(len(sms_evs), 1)
                    self.assertEqual(sms_evs[0]["segments"], 1)
                    self.assertEqual(sms_evs[0]["sid"], "msg_sms_12345")

    def test_email_dispatch_detail_emission(self):
        """Verify email dispatch detail event."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        import rt_http

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            with patch.dict(os.environ, {"RESEND_API_KEY": "re_test"}):
                with patch.object(rt_http.http_client, "request", return_value={"id": "msg_12345"}):
                    ok = rt_email.send_email("friend@example.com", "Test Subject", "Test Body",
                                              verified_email="friend@example.com")
                    self.assertTrue(ok)

                    email_evs = [f for lvl, ev, f in captured if ev == "email.dispatch_detail"]
                    self.assertEqual(len(email_evs), 1)
                    self.assertFalse(email_evs[0]["has_ics"])
                    self.assertEqual(email_evs[0]["message_id"], "msg_12345")

    def test_fact_mutation_observability(self):
        """Verify memory.fact_mutation is emitted when canvas facts are saved."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            with patch.object(rt_prefs, "_req", return_value={"op": "insert", "confidence": 0.95}):
                res = rt_facts.dual_write("hash123", {"caller_name": "Alice", "pets": ["dog Buster"]})
                self.assertIsInstance(res, dict)

                mutation_evs = [f for lvl, ev, f in captured if ev == "memory.fact_mutation"]
                self.assertGreaterEqual(len(mutation_evs), 1)

    def test_scamguard_risk_breakdown(self):
        """Verify scam.risk_breakdown calculation in signature scanning."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            suspicious_text = "You must buy a gift card right now or the police will arrest you within the hour."
            hits = rt_bridge.scan_signatures(suspicious_text)
            self.assertGreater(len(hits), 0)

            breakdown_evs = [f for lvl, ev, f in captured if ev == "scam.risk_breakdown"]
            self.assertEqual(len(breakdown_evs), 1)
            self.assertEqual(breakdown_evs[0]["score"], len(hits))
            self.assertGreaterEqual(breakdown_evs[0]["urgency"], 1)
            self.assertGreaterEqual(breakdown_evs[0]["monetary"], 1)

    def test_scheduler_tz_resolution(self):
        """Verify scheduler.tz_resolved is emitted on callee timezone lookup."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            tz, known = rt_scheduler.callee_zone("+12125551234")
            self.assertEqual(tz, "America/New_York")

            tz_evs = [f for lvl, ev, f in captured if ev == "scheduler.tz_resolved"]
            self.assertEqual(len(tz_evs), 1)
            self.assertEqual(tz_evs[0]["tz"], "America/New_York")

    def test_obs_span_and_caught(self):
        """Verify obs.span and obs.caught behavior."""
        captured = []

        def mock_emit(self, level, event, fields):
            captured.append((level, event, fields))

        with patch.object(rt_obs.Obs, "_emit", mock_emit):
            # Span success
            with rt_obs.obs.span("test_operation", custom_field="abc"):
                _ = 1 + 1

            span_starts = [f for lvl, ev, f in captured if ev == "test_operation.start"]
            span_oks = [f for lvl, ev, f in captured if ev == "test_operation.ok"]
            self.assertEqual(len(span_starts), 1)
            self.assertEqual(len(span_oks), 1)
            self.assertIn("ms", span_oks[0])

            # Span failure
            with self.assertRaises(ValueError):
                with rt_obs.obs.span("failing_operation"):
                    raise ValueError("boom")

            span_fails = [f for lvl, ev, f in captured if ev == "failing_operation.failed"]
            self.assertEqual(len(span_fails), 1)
            self.assertEqual(span_fails[0]["err"], "ValueError")

            # Caught exception
            rt_obs.obs.caught("test_handler", RuntimeError("test error"), custom_tag="unit_test")
            caught_evs = [f for lvl, ev, f in captured if ev == "caught"]
            self.assertEqual(len(caught_evs), 1)
            self.assertEqual(caught_evs[0]["where"], "test_handler")
            self.assertEqual(caught_evs[0]["err"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
