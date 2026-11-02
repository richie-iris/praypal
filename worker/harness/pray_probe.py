#!/usr/bin/env python3
"""pray_probe.py — Master Automated Telephony Probe & Acoustic Harness for PrayPal.

Dials the live PrayPal hotline (+1 862-358-8238) from provisioned Twilio numbers,
records call audio with carrier fidelity, analyzes acoustic quality via FFT,
validates transcripts against core conversational rules, and reports results.

Usage:
    python harness/pray_probe.py --list
    python harness/pray_probe.py --scenario mantra --dry-run
    python harness/pray_probe.py --scenario mantra
    python harness/pray_probe.py --scenario mms
    python harness/pray_probe.py --scenario transfer
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv

load_dotenv(HERE / ".env.harness", override=True)
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

import audio_analyzer
import dynamic_caller
import pray_transcript_lint

TARGET_HOTLINE = os.getenv("RT_PUBLIC_NUMBER", "+18623588238").strip()

SCENARIO_CONFIGS: dict[str, dict] = {
    "mantra": {
        "persona_id": "priya",
        "why": "Lord Shiva mantra pronunciation — syllable-by-syllable teaching, chamber reverb, and peer dialogue.",
        "expected_guide": "shiva",
    },
    "mms": {
        "persona_id": "elena",
        "why": "MMS sacred image delivery — image dispatch tool execution and text confirmation line.",
        "expected_guide": "jesus",
    },
    "transfer": {
        "persona_id": "marcus",
        "why": "Random guide transfer in grief — ensures immediate handover without stalling or asking caller to choose.",
        "expected_guide": "god",
    },
    "lament": {
        "persona_id": "david",
        "why": "Raw human anger & theodicy — tests zero defensiveness, humble presence, and un-preachy spiritual accompaniment.",
        "expected_guide": "god",
    },
    "weather": {
        "persona_id": "hannah",
        "why": "Live Google search grounding — tests real-time weather in Dallas Texas without hallucinating or preaching.",
        "expected_guide": "god",
    },
    "forget": {
        "persona_id": "miguel",
        "why": "Confessional deletion — executes db_tool(action='delete') and reassures seeker that private confession is wiped clean.",
        "expected_guide": "jesus",
    },
}


def _get_env(name: str, fallback: str = "") -> str:
    val = (os.getenv(name) or fallback).strip()
    return val


def get_auth_header(sid: str, token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{sid}:{token}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def place_carrier_call(
    to_number: str,
    from_number: str,
    twiml_body: str,
    record: bool = True,
) -> str:
    """Initiate live outbound call via Twilio REST API with full audio recording."""
    sid = _get_env("TWILIO_ACCOUNT_SID")
    token = _get_env("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required to place calls.")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    post_data = {
        "To": to_number,
        "From": from_number,
        "Twiml": twiml_body,
    }
    if record:
        post_data["Record"] = "true"
        post_data["RecordingChannels"] = "dual"

    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(post_data).encode("utf-8"),
        headers=get_auth_header(sid, token),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        return res["sid"]


def wait_for_call_completion(call_sid: str, timeout_s: int = 180) -> dict:
    """Poll Twilio until call reaches terminal status (completed, busy, failed, no-answer)."""
    sid = _get_env("TWILIO_ACCOUNT_SID")
    token = _get_env("TWILIO_AUTH_TOKEN")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls/{call_sid}.json"
    headers = get_auth_header(sid, token)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            status = data.get("status")
            if status in ("completed", "busy", "failed", "no-answer", "canceled"):
                return data
        time.sleep(4)

    return {"status": "timeout", "sid": call_sid}


def fetch_call_recording_wav(call_sid: str, dest_dir: Path) -> Path | None:
    """Download the carrier WAV recording for a completed Twilio call."""
    sid = _get_env("TWILIO_ACCOUNT_SID")
    token = _get_env("TWILIO_AUTH_TOKEN")
    list_url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls/{call_sid}/Recordings.json"
    headers = get_auth_header(sid, token)

    # Allow a few seconds for Twilio to finalize the media file
    for _ in range(4):
        time.sleep(3)
        req = urllib.request.Request(list_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            recordings = data.get("recordings") or []
            if recordings:
                rec_sid = recordings[0]["sid"]
                wav_url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Recordings/{rec_sid}.wav"
                dest_file = dest_dir / f"{call_sid}.wav"
                wav_req = urllib.request.Request(wav_url, headers=headers)
                with urllib.request.urlopen(wav_req, timeout=30) as wav_resp:
                    dest_file.write_bytes(wav_resp.read())
                return dest_file

    return None


def fetch_call_transcript_from_db(before_call_id: str | None = None, timeout_s: int = 45) -> tuple[str, str] | None:
    """Read back the newly generated transcript from Supabase rt.calls via rt_console_calls."""
    import rt_prefs

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            calls = rt_prefs._req("POST", "rpc/rt_console_calls", {"p_limit": 5}) or []
            if calls:
                top = calls[0]
                cid = top.get("call_id")
                transcript = top.get("transcript") or ""
                if cid != before_call_id and transcript:
                    return cid, transcript
        except Exception:
            pass
        time.sleep(3)

    return None


def get_latest_db_call_id() -> str | None:
    """Get the most recent call_id before placing the probe call."""
    import rt_prefs

    try:
        calls = rt_prefs._req("POST", "rpc/rt_console_calls", {"p_limit": 5}) or []
        if calls:
            return calls[0].get("call_id")
    except Exception:
        pass
    return None



def run_scenario(
    scenario_name: str,
    dry_run: bool = False,
    record_audio: bool = True,
    use_llm: bool = True,
) -> int:
    """Execute a single end-to-end telephony scenario."""
    if scenario_name not in SCENARIO_CONFIGS:
        print(f"Error: Unknown scenario '{scenario_name}'. Available: {list(SCENARIO_CONFIGS.keys())}")
        return 2

    config = SCENARIO_CONFIGS[scenario_name]
    persona_id = config["persona_id"]
    persona = dynamic_caller.SEEKERS[persona_id]

    print(f"\n========================================================")
    print(f"  PRAYPAL TELEPHONY PROBE — SCENARIO: {scenario_name.upper()}")
    print(f"========================================================")
    print(f"  Seeker:    {persona.name} ({persona.e164})")
    print(f"  Hotline:   {TARGET_HOTLINE}")
    print(f"  Purpose:   {config['why']}")

    # 1. Generate Conversational Turns
    print(f"\n  [1/5] Generating Seeker Dialogue (LLM={use_llm})...")
    turns = dynamic_caller.generate_seeker_turns(persona_id, use_llm=use_llm)
    for i, t in enumerate(turns, 1):
        print(f"    Turn {i}: \"{t}\"")

    reply_wait = 14 if scenario_name in ("weather", "forget") else dynamic_caller.REPLY_WAIT
    twiml_xml = dynamic_caller.generate_twiml(turns, reply_wait_s=reply_wait)
    mins = (dynamic_caller.OPENING_WAIT + len(turns) * reply_wait) / 60.0
    est_cost = mins * 0.22

    print(f"  Estimated Call Duration: ~{mins:.1f} mins (Est. COGS: ~${est_cost:.2f})")

    if dry_run:
        print(f"\n  [DRY RUN] Generated TwiML:\n  {twiml_xml}")
        print("\n  \033[92m[DRY RUN PASSED]\033[0m Zero telephony dollars spent.\n")
        return 0

    # 2. Place Live Carrier Call
    print(f"\n  [2/5] Dialing {TARGET_HOTLINE} from {persona.e164} via Twilio...")
    before_call_id = get_latest_db_call_id()

    call_sid = place_carrier_call(
        to_number=TARGET_HOTLINE,
        from_number=persona.e164,
        twiml_body=twiml_xml,
        record=record_audio,
    )
    print(f"  Call Dispatched: {call_sid}")
    print(f"  Waiting for call completion...")

    # 3. Wait for Call Completion
    call_status = wait_for_call_completion(call_sid)
    dur = call_status.get("duration", "0")
    print(f"  Call Terminal Status: {call_status.get('status')} (Duration: {dur}s)")

    if call_status.get("status") != "completed":
        print(f"\n  \033[91m[FAIL]\033[0m Call failed to complete cleanly: {call_status.get('status')}")
        return 1

    # 4. Readback & Lint Transcript
    print(f"\n  [3/5] Polling Database for Transcript...")
    db_result = fetch_call_transcript_from_db(before_call_id)
    transcript_text = ""
    transcript_passed = True

    if db_result:
        call_id, transcript_text = db_result
        print(f"  Captured Call ID: {call_id}")
        print(f"\n  Verbatim Call Transcript:")
        for line in transcript_text.splitlines():
            print(f"    {line}")

        print(f"\n  [4/5] Running PrayPal Behavioral Linter...")
        findings = pray_transcript_lint.lint_transcript(transcript_text, scenario_type=scenario_name)
        errors = [f for f in findings if f.severity == "ERROR"]
        warns = [f for f in findings if f.severity == "WARN"]
        print(f"  Findings: {len(errors)} error(s), {len(warns)} warning(s)")
        for f in findings:
            print(f"  {f}")
        if errors:
            transcript_passed = False
    else:
        print("  Warning: Transcript could not be retrieved from database within timeout.")
        transcript_passed = False

    # 5. Fetch Recording & Run Acoustic Analysis
    acoustic_passed = True
    if record_audio:
        print(f"\n  [5/5] Downloading Carrier Recording & Running Acoustic FFT...")
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_file = fetch_call_recording_wav(call_sid, Path(tmp_dir))
            if wav_file and wav_file.exists():
                acoustic_res = audio_analyzer.analyze_wav(wav_file)
                print(f"  Recording File:     {acoustic_res.get('file')}")
                print(f"  Duration Analyzed:  {acoustic_res.get('duration_s')}s")
                print(f"  Active Speech RMS:  {acoustic_res.get('active_speech_rms_db')} dBFS")
                print(f"  Dead Air Gaps:      {len(acoustic_res.get('long_silence_gaps', []))}")
                print(f"  Clipping Ratio:     {acoustic_res.get('clipping_ratio', 0):.2%}")
                if acoustic_res.get("defects"):
                    print("  Acoustic Defects:")
                    for d in acoustic_res["defects"]:
                        print(f"    - \033[91m{d}\033[0m")
                    acoustic_passed = False
                else:
                    print("  \033[92m✓ Acoustics clean: no clipping, balanced speech RMS, no dead air.\033[0m")
            else:
                print("  Warning: Twilio recording could not be downloaded.")
                acoustic_passed = False

    # Final Verdict
    print("\n--------------------------------------------------------")
    if transcript_passed and acoustic_passed:
        print(f"  SCENARIO {scenario_name.upper()}: \033[92mPASS (All Conversational & Acoustic Invariants Verified)\033[0m")
        print("--------------------------------------------------------\n")
        return 0
    else:
        print(f"  SCENARIO {scenario_name.upper()}: \033[91mFAIL (Defects detected)\033[0m")
        print("--------------------------------------------------------\n")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="PrayPal Automated Telephony Probe")
    parser.add_argument("--scenario", choices=list(SCENARIO_CONFIGS.keys()), help="Scenario to run")
    parser.add_argument("--list", action="store_true", help="List all available scenarios and exit")
    parser.add_argument("--dry-run", action="store_true", help="Generate dialogue and TwiML without dialing")
    parser.add_argument("--no-record", action="store_true", help="Disable carrier call recording")
    parser.add_argument("--no-llm", action="store_true", help="Use static templates instead of LLM generation")
    args = parser.parse_args()

    if args.list or not args.scenario:
        print("\n  Available PrayPal Telephony Scenarios:\n")
        for key, conf in SCENARIO_CONFIGS.items():
            seeker = dynamic_caller.SEEKERS[conf["persona_id"]]
            print(f"    {key:<10} Caller: {seeker.name:<16} ({seeker.e164})")
            print(f"    {'':<10} Purpose: {conf['why']}\n")
        return 0

    return run_scenario(
        args.scenario,
        dry_run=args.dry_run,
        record_audio=not args.no_record,
        use_llm=not args.no_llm,
    )


if __name__ == "__main__":
    raise SystemExit(main())
