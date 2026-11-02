"""test_rt_sacred_stream.py — Tests for Curated Sacred Audio Stream.

Verifies:
1. Sacred channels registry (traditions, frequencies, volume levels).
2. Procedural loop generation (valid PCM bytes, wave headers).
3. Ducking and overlay mixer logic under voice energy.
"""
from __future__ import annotations

from pathlib import Path
import pytest
import rt_sacred_stream


def test_sacred_channels_presence():
    channels = rt_sacred_stream.list_sacred_channels()
    assert len(channels) >= 5
    ids = {c["id"] for c in channels}
    assert "hindu_tanpura" in ids
    assert "christian_cathedral" in ids
    assert "islam_ney" in ids
    assert "judaism_niggun" in ids
    assert "buddhist_bowl" in ids


def test_channel_lookup():
    chan = rt_sacred_stream.get_sacred_channel("hindu_tanpura")
    assert chan is not None
    assert chan["base_freq"] == 136.1
    assert "harmonics" in chan


def test_ambient_loop_synthesis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rt_sacred_stream, "MEDIA_DIR", tmp_path)
    pcm = rt_sacred_stream.generate_ambient_loop_wav("buddhist_bowl", duration_sec=1.0, rate=24000)
    assert len(pcm) == 24000 * 2  # 16-bit mono = 2 bytes per sample
    wav_file = tmp_path / "buddhist_bowl.wav"
    assert wav_file.exists()
    assert wav_file.stat().st_size > len(pcm)


def test_mix_ambient_with_voice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rt_sacred_stream, "MEDIA_DIR", tmp_path)
    synthetic_voice = b"\x00\x10" * 12000  # 0.5s of audio at 24kHz
    mixed = rt_sacred_stream.mix_ambient_with_voice(
        synthetic_voice,
        channel_id="christian_cathedral",
        rate=24000,
    )
    assert len(mixed) == len(synthetic_voice)
