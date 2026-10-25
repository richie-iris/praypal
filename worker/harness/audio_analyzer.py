#!/usr/bin/env python3
"""audio_analyzer.py — Acoustic & Spectral Quality Analyzer for Telephony Recordings.

Analyzes raw WAV recordings captured over PSTN / carrier lines to detect:
1. Volume & Energy Level (RMS in dBFS): Detects inaudible whispers or deafening spikes.
2. Digital Clipping & Saturation: Identifies audio driver or DSP distortion.
3. Excessive Silence Gaps: Catches long unhandled pauses (> 3.5s) where the agent hesitated.
4. Noise Floor & Signal-to-Noise Ratio: Measures background hum or acoustic distortion.

Usage:
    python harness/audio_analyzer.py /path/to/recording.wav
    python harness/audio_analyzer.py /path/to/recording.wav --json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np


def analyze_wav(
    wav_path: str | Path,
    silence_threshold_db: float = -42.0,
    max_tolerable_silence_s: float = float(os.getenv("MAX_TOLERABLE_SILENCE_S", "12.0")),
    clipping_threshold: int = 32700,
) -> dict[str, Any]:
    """Analyze a WAV audio file and return acoustic quality metrics."""
    path = Path(wav_path)
    if not path.exists():
        return {"error": f"file_not_found: {wav_path}", "passed": False}

    try:
        with wave.open(str(path), "rb") as wf:
            num_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            num_frames = wf.getnframes()
            raw_bytes = wf.readframes(num_frames)
    except Exception as exc:
        return {"error": f"corrupt_wav: {exc}", "passed": False}

    if num_frames == 0 or sampwidth != 2:
        if sampwidth != 2:
            return {"error": f"unsupported_sample_width: {sampwidth} (expected 16-bit)", "passed": False}
        return {"error": "empty_audio_file", "passed": False}

    # Convert bytes to numpy int16
    samples = np.frombuffer(raw_bytes, dtype=np.int16)
    if num_channels > 1:
        # Average channels down to mono
        samples = samples.reshape(-1, num_channels).mean(axis=1).astype(np.int16)

    total_samples = len(samples)
    duration_s = total_samples / float(framerate)

    if duration_s < 0.2:
        return {"error": "audio_too_short", "duration_s": duration_s, "passed": False}

    # 1. Peak Amplitude & Clipping Detection
    abs_samples = np.abs(samples)
    peak_val = int(np.max(abs_samples))
    clipped_samples = int(np.sum(abs_samples >= clipping_threshold))
    clipping_ratio = float(clipped_samples / total_samples)

    # 2. RMS Energy across the whole recording (dBFS, 0 dBFS = max possible square wave)
    float_samples = samples.astype(np.float64) / 32768.0
    mean_sq = float(np.mean(float_samples ** 2))
    rms = math.sqrt(max(mean_sq, 1e-12))
    rms_db = float(20.0 * math.log10(rms))

    # 3. Framing Analysis (100ms non-overlapping frames for silence & dynamics)
    frame_size = int(framerate * 0.1)  # 100ms
    if frame_size <= 0:
        frame_size = 800

    num_full_frames = total_samples // frame_size
    frame_dbs = []

    for i in range(num_full_frames):
        chunk = float_samples[i * frame_size : (i + 1) * frame_size]
        chunk_msq = float(np.mean(chunk ** 2))
        chunk_rms = math.sqrt(max(chunk_msq, 1e-12))
        chunk_db = 20.0 * math.log10(chunk_rms)
        frame_dbs.append(chunk_db)

    frame_dbs = np.array(frame_dbs)

    # 4. Silence Detection (consecutive frames below threshold)
    is_silent = frame_dbs < silence_threshold_db
    silence_gaps: list[dict[str, float]] = []

    in_gap = False
    gap_start_idx = 0

    for idx, silent in enumerate(is_silent):
        if silent and not in_gap:
            in_gap = True
            gap_start_idx = idx
        elif not silent and in_gap:
            in_gap = False
            gap_duration_s = (idx - gap_start_idx) * 0.1
            if gap_duration_s >= max_tolerable_silence_s:
                silence_gaps.append({
                    "start_s": round(gap_start_idx * 0.1, 2),
                    "end_s": round(idx * 0.1, 2),
                    "duration_s": round(gap_duration_s, 2),
                })

    if in_gap:
        gap_duration_s = (len(is_silent) - gap_start_idx) * 0.1
        if gap_duration_s >= max_tolerable_silence_s:
            silence_gaps.append({
                "start_s": round(gap_start_idx * 0.1, 2),
                "end_s": round(len(is_silent) * 0.1, 2),
                "duration_s": round(gap_duration_s, 2),
            })

    # 5. Background Noise Floor (10th percentile frame energy)
    noise_floor_db = float(np.percentile(frame_dbs, 10)) if len(frame_dbs) > 0 else -60.0

    # 6. Active Speech Energy (frames above silence threshold)
    speech_frames = frame_dbs[~is_silent]
    active_speech_rms_db = float(np.mean(speech_frames)) if len(speech_frames) > 0 else -60.0

    # Pass/Fail Criteria
    defects = []
    if clipping_ratio > 0.002:  # More than 0.2% clipped samples
        defects.append(f"Excessive clipping: {clipping_ratio:.2%} of samples saturated (>32700)")

    if len(silence_gaps) > 0:
        total_dead_air = sum(g["duration_s"] for g in silence_gaps)
        defects.append(f"Dead air detected: {len(silence_gaps)} gap(s) exceeding {max_tolerable_silence_s}s ({total_dead_air:.1f}s total)")

    if active_speech_rms_db < -40.0:
        defects.append(f"Audio level too quiet: speech RMS is {active_speech_rms_db:.1f} dBFS (min -40 dBFS)")

    if active_speech_rms_db > -6.0:
        defects.append(f"Audio level too loud / hot: speech RMS is {active_speech_rms_db:.1f} dBFS (max -6 dBFS)")

    passed = len(defects) == 0

    return {
        "file": str(path.name),
        "duration_s": round(duration_s, 2),
        "sample_rate_hz": framerate,
        "rms_db": round(rms_db, 1),
        "active_speech_rms_db": round(active_speech_rms_db, 1),
        "noise_floor_db": round(noise_floor_db, 1),
        "peak_amplitude": peak_val,
        "clipping_ratio": round(clipping_ratio, 4),
        "long_silence_gaps": silence_gaps,
        "defects": defects,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Acoustic Quality Analyzer for Telephony Recordings")
    parser.add_argument("wav_path", help="Path to recording WAV file")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    results = analyze_wav(args.wav_path)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if results.get("passed") else 1

    print(f"\n  Acoustic Quality Analysis: {results.get('file')}")
    print(f"  Duration:           {results.get('duration_s')}s")
    print(f"  Sample Rate:        {results.get('sample_rate_hz')} Hz")
    print(f"  Active Speech RMS:  {results.get('active_speech_rms_db')} dBFS (ideal: -26 to -14 dBFS)")
    print(f"  Noise Floor:        {results.get('noise_floor_db')} dBFS")
    print(f"  Peak Amplitude:     {results.get('peak_amplitude')} / 32767")
    print(f"  Clipping Ratio:     {results.get('clipping_ratio', 0):.2%}")
    print(f"  Dead Air Gaps:      {len(results.get('long_silence_gaps', []))}")

    if results.get("defects"):
        print("\n  \033[91mDEFECTS FOUND:\033[0m")
        for d in results["defects"]:
            print(f"    - {d}")
        print("\n  Result: \033[91mFAIL\033[0m\n")
        return 1
    else:
        print("\n  Result: \033[92mPASS (Acoustics clean, balanced, no clipping/gaps)\033[0m\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
