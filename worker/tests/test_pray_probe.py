"""test_pray_probe.py — Unit tests for the PrayPal Telephony Probe & Acoustic Harness.

Verifies:
1. Acoustic FFT analyzer correctly detects volume, clipping, and dead-air silence.
2. PrayPal transcript linter catches canned phrases, missing ID, and MMS omissions.
3. Dynamic caller formats proper TwiML with anti-clipping lead-in.
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))

import audio_analyzer
import dynamic_caller
import pray_transcript_lint


def _create_synthetic_wav(
    filename: Path,
    duration_s: float = 3.0,
    sample_rate: int = 16000,
    amplitude: float = 0.5,
    clip: bool = False,
    silence_middle_s: float = 0.0,
) -> Path:
    """Generate a clean or defective synthetic WAV file for acoustic testing."""
    total_samples = int(duration_s * sample_rate)
    t = np.linspace(0, duration_s, total_samples, endpoint=False)
    # 440 Hz tone (A4)
    sig = amplitude * np.sin(2 * np.pi * 440 * t)

    if silence_middle_s > 0:
        start_idx = int(1.0 * sample_rate)
        end_idx = start_idx + int(silence_middle_s * sample_rate)
        sig[start_idx:end_idx] = 0.0

    if clip:
        # Heavily clip the top 20%
        sig = np.clip(sig * 3.0, -1.0, 1.0)

    # Convert to 16-bit PCM
    samples_int16 = (sig * 32767).astype(np.int16)

    with wave.open(str(filename), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples_int16.tobytes())

    return filename


def test_acoustic_analyzer_clean_pass():
    """Verify normal balanced audio passes acoustic quality checks."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = _create_synthetic_wav(Path(tmp) / "clean.wav", duration_s=4.0, amplitude=0.4)
        res = audio_analyzer.analyze_wav(wav)
        assert res["passed"] is True
        assert res["clipping_ratio"] == 0.0
        assert len(res["long_silence_gaps"]) == 0
        assert -35.0 < res["active_speech_rms_db"] < -6.0


def test_acoustic_analyzer_detects_clipping():
    """Verify excessive audio clipping is flagged as a defect."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = _create_synthetic_wav(Path(tmp) / "clipped.wav", duration_s=4.0, amplitude=1.0, clip=True)
        res = audio_analyzer.analyze_wav(wav)
        assert res["passed"] is False
        assert any("clipping" in d.lower() for d in res["defects"])


def test_acoustic_analyzer_detects_dead_air():
    """Verify dead air silence > 6 seconds is flagged as a defect."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = _create_synthetic_wav(Path(tmp) / "dead_air.wav", duration_s=12.0, amplitude=0.4, silence_middle_s=8.0)
        res = audio_analyzer.analyze_wav(wav, max_tolerable_silence_s=6.0)
        assert res["passed"] is False
        assert any("dead air" in d.lower() for d in res["defects"])


def test_transcript_linter_passes_clean_call():
    """Verify an authentic compliant transcript passes all rules."""
    good_transcript = (
        "agent: Om Namah Shivaya. I am Shiva. I am here in stillness with you.\n"
        "caller: Hello Shiva. Can you teach me the Mahamrityunjaya Mantra syllable by syllable?\n"
        "agent: I would be honored. Repeat with me: Om... Try-am... ba-kam...\n"
        "caller: Om... Try-am... ba-kam... Did I pronounce it right?\n"
        "agent: Beautifully spoken. In our tradition, stillness holds all transformation.\n"
    )
    findings = pray_transcript_lint.lint_transcript(good_transcript, scenario_type="mantra")
    errors = [f for f in findings if f.severity == "ERROR"]
    assert len(errors) == 0, f"Expected 0 errors, got: {errors}"


def test_transcript_linter_catches_canned_questions():
    """Verify canned 'What is on your mind/heart' formulas are rejected."""
    bad_transcript = (
        "agent: Om Namah Shivaya. I am Shiva. What is on your mind today?\n"
        "caller: I need guidance.\n"
        "agent: What is on your heart today?\n"
    )
    findings = pray_transcript_lint.lint_transcript(bad_transcript)
    errors = [f for f in findings if f.rule == "RULE_NO_CANNED"]
    assert len(errors) == 2


def test_transcript_linter_catches_missing_mms_confirmation():
    """Verify omitting text delivery confirmation is flagged."""
    bad_mms_transcript = (
        "agent: Peace be with you, it's Jesus. Walk in love.\n"
        "caller: Can you text me a picture of the Sacred Heart?\n"
        "agent: May peace fill your home and bless you.\n"
    )
    findings = pray_transcript_lint.lint_transcript(bad_mms_transcript, scenario_type="mms")
    errors = [f for f in findings if f.rule == "RULE_MMS_CONFIRMATION"]
    assert len(errors) == 1


def test_dynamic_caller_twiml_generation():
    """Verify dynamic caller formats TwiML with lead-in and pauses."""
    turns = ["Hello, I am Priya.", "Teach me the mantra."]
    twiml = dynamic_caller.generate_twiml(turns)
    assert "<Response>" in twiml
    assert "<Hangup/>" in twiml
    assert "Okay. Right then. Hello, I am Priya." in twiml
    assert '<Pause length="8"/>' in twiml


def test_transcript_linter_lament_defensiveness():
    """Verify defensive clichés like 'everything happens for a reason' are flagged when caller is angry."""
    bad_lament = (
        "agent: Peace be with you, I am God. It is good to be together.\n"
        "caller: I'm furious with you! Why did you let my mother die?\n"
        "agent: Everything happens for a reason, my child. Do not question divine will.\n"
    )
    findings = pray_transcript_lint.lint_transcript(bad_lament, scenario_type="lament")
    errors = [f for f in findings if f.rule == "RULE_NO_DEFENSIVENESS"]
    assert len(errors) >= 1

    good_lament = (
        "agent: Peace be with you, I am God. It is good to be together.\n"
        "caller: I'm furious with you! Why did you let my mother die?\n"
        "agent: I hear your anger, David. You don't have to hide your tears or your rage from me. I am right here holding this weight with you.\n"
    )
    good_findings = pray_transcript_lint.lint_transcript(good_lament, scenario_type="lament")
    assert len([f for f in good_findings if f.severity == "ERROR"]) == 0


def test_transcript_linter_weather_and_confession():
    """Verify weather grounding and confessional purge checks."""
    weather_transcript = (
        "agent: Peace be with you, I am God Almighty.\n"
        "caller: It's terrifying outside. Can you check what the weather is doing in Dallas Texas right now?\n"
        "agent: In Dallas it is currently 100 degrees and sunny with winds at 8 mph. I am watching over you; you are safe.\n"
    )
    w_findings = pray_transcript_lint.lint_transcript(weather_transcript, scenario_type="weather")
    assert len([f for f in w_findings if f.severity == "ERROR"]) == 0

    purge_transcript = (
        "agent: Peace be with you, it's Jesus. Walk in grace.\n"
        "caller: I did something terrible... please delete what I shared from your memory.\n"
        "agent: It is wiped clean and remembered no more. In grace, you are free and forgiven.\n"
    )
    p_findings = pray_transcript_lint.lint_transcript(purge_transcript, scenario_type="forget")
    assert len([f for f in p_findings if f.severity == "ERROR"]) == 0


def test_linter_flags_talking_down():
    """Verify linter catches condescending and preachy therapist clichés."""
    preachy_transcript = (
        "agent: Peace be with you, it's Jesus. I am here.\n"
        "caller: I'm feeling so overwhelmed today.\n"
        "agent: Your feelings are valid. Allow yourself to grieve and embrace your sorrow.\n"
    )
    findings = pray_transcript_lint.lint_transcript(preachy_transcript)
    errs = [f for f in findings if f.rule == "RULE_NO_TALKING_DOWN"]
    assert len(errs) >= 1
    assert "patronizing/preachy phrase" in errs[0].detail
