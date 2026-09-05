"""heygen_generate.py — Automated HeyGen Avatar Video Generator for PrayPal.

Automates the production of the Founding Video Manifesto featuring Richie Etwaru.
Takes the founder portrait frame (docs/assets/richie_etwaru_founder.jpg) and the speech
script (docs/FOUNDING_MANIFESTO_SEPT_5_2026.md), invokes HeyGen's Talking Photo API,
and downloads the finished video.

Usage:
  # Dry-run / inspect extracted script:
  python heygen_generate.py --dry-run

  # List available HeyGen voices:
  python heygen_generate.py --list-voices

  # Generate video using default portrait and extracted manifesto:
  python heygen_generate.py

  # Generate with specific voice ID or custom audio voiceover:
  python heygen_generate.py --voice-id <VOICE_ID>
  python heygen_generate.py --audio-file path/to/voiceover.mp3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import requests

load_dotenv(".env.local")
load_dotenv(".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = "https://api.heygen.com"

UPLOAD_URL = "https://upload.heygen.com"

DEFAULT_IMAGE = Path(__file__).resolve().parent.parent.parent / "docs" / "assets" / "richie_etwaru_founder.jpg"
DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "docs" / "FOUNDING_MANIFESTO_SEPT_5_2026.md"
DEFAULT_OUT = Path(__file__).resolve().parent.parent.parent / "docs" / "assets" / "founding_manifesto.mp4"


def get_api_key(cli_key: str | None) -> str:
    key = cli_key or os.getenv("HEYGEN_API_KEY", "").strip()
    if not key:
        print("❌ Error: HEYGEN_API_KEY environment variable or --api-key argument is required.", file=sys.stderr)
        print("Get your API key at https://app.heygen.com/settings?nav=API", file=sys.stderr)
        sys.exit(1)
    return key


def extract_parts(markdown_path: Path) -> dict[int, tuple[str, str]]:
    """Extract individual parts with their titles and spoken text."""
    if not markdown_path.exists():
        print(f"❌ Script file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)

    content = markdown_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    parts: dict[int, tuple[str, str]] = {}
    current_part: int | None = None
    current_title = ""
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        match = re.search(r"### PART (\d+):\s*(.*)", stripped, re.IGNORECASE)
        if match:
            if current_part is not None:
                parts[current_part] = (current_title, " ".join(current_lines).strip())
            current_part = int(match.group(1))
            current_title = match.group(2).strip()
            current_lines = []
            continue

        if current_part is not None:
            if stripped.startswith("---") and len(stripped) <= 5:
                continue
            if stripped.startswith("*(") or stripped.startswith("(") or stripped.startswith("#"):
                continue
            if stripped.startswith("> \"") or stripped.startswith(">"):
                clean = stripped.lstrip("> ").strip().strip('"')
                if clean and not clean.startswith("*(") and not clean.startswith("("):
                    current_lines.append(clean)
            else:
                clean = stripped.strip('"').strip()
                if clean and not clean.startswith("*(") and not clean.startswith("("):
                    current_lines.append(clean)

    if current_part is not None:
        parts[current_part] = (current_title, " ".join(current_lines).strip())

    return parts


def extract_script_text(markdown_path: Path, part_num: int | None = None) -> str:
    """Extract clean spoken monologue for a specific part or all parts."""
    parts = extract_parts(markdown_path)
    if part_num is not None:
        if part_num not in parts:
            print(f"❌ Part {part_num} not found. Available parts: {list(parts.keys())}", file=sys.stderr)
            sys.exit(1)
        return parts[part_num][1]

    # Return concatenated parts
    return " ".join(p[1] for p in parts.values())



def list_voices(api_key: str) -> None:
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    resp = requests.get(f"{BASE_URL}/v2/voices", headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"❌ Failed to fetch voices ({resp.status_code}): {resp.text}", file=sys.stderr)
        return

    data = resp.json().get("data", {}).get("voices", [])
    print(f"\nFound {len(data)} available voices:")
    for v in data[:25]:
        print(f"  • {v.get('voice_id')}: {v.get('name')} ({v.get('language')}, {v.get('gender')})")
    if len(data) > 25:
        print(f"  ... and {len(data) - 25} more. Use --voice-id <id> to select.")


def upload_image(api_key: str, image_path: Path) -> str:
    """Upload portrait image as an asset via HeyGen v3."""
    print(f"📸 Uploading portrait to HeyGen: {image_path.name}...")
    headers = {"x-api-key": api_key}

    with open(image_path, "rb") as f:
        files = {"file": (image_path.name, f, "image/jpeg")}
        resp = requests.post(f"{BASE_URL}/v3/assets", headers=headers, files=files, timeout=60)

    if resp.status_code != 200:
        print(f"❌ Image upload failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    res_data = resp.json().get("data", {})
    asset_id = res_data.get("asset_id") or res_data.get("id")
    print(f"✅ Image uploaded successfully. Asset ID: {asset_id}")
    return asset_id


def upload_audio(api_key: str, audio_path: Path) -> str:
    """Upload custom recorded voiceover audio via HeyGen v3."""
    print(f"🎙️ Uploading custom audio voiceover: {audio_path.name}...")
    headers = {"x-api-key": api_key}
    with open(audio_path, "rb") as f:
        files = {"file": (audio_path.name, f, "audio/mpeg")}
        resp = requests.post(f"{BASE_URL}/v3/assets", headers=headers, files=files, timeout=90)

    if resp.status_code != 200:
        print(f"❌ Audio upload failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    audio_id = resp.json().get("data", {}).get("asset_id") or resp.json().get("data", {}).get("id")
    print(f"✅ Audio uploaded successfully. Asset ID: {audio_id}")
    return audio_id


def generate_video(
    api_key: str,
    image_id: str,
    script_text: str,
    title: str = "PrayPal Founding Manifesto",
    voice_id: str | None = None,
    audio_asset_id: str | None = None,
) -> str:
    """Submit HeyGen v3 Image-to-Video generation job."""
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    selected_voice = voice_id or "4462feff75f54d31a3242c47d376ded1"  # Richie Etwaru First Voice

    payload: dict = {
        "type": "image",
        "image": {
            "type": "asset_id",
            "asset_id": image_id,
        },
        "title": title,
        "resolution": "1080p",
        "aspect_ratio": "auto",
    }

    if audio_asset_id:
        payload["audio"] = {
            "type": "asset_id",
            "asset_id": audio_asset_id,
        }
    else:
        payload["script"] = script_text
        payload["voice_id"] = selected_voice

    print(f"🚀 Submitting v3 video generation job to HeyGen (Voice: {selected_voice})...")
    resp = requests.post(f"{BASE_URL}/v3/videos", headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        print(f"❌ Video generation request failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    video_id = resp.json().get("data", {}).get("video_id") or resp.json().get("data", {}).get("id")
    print(f"✅ Job queued. Video ID: {video_id}")
    return video_id


def poll_and_download(api_key: str, video_id: str, output_path: Path) -> None:
    """Poll HeyGen v3 API until video finishes rendering, then download."""
    headers = {"x-api-key": api_key}
    print(f"⏳ Rendering video in HeyGen... (Job: {video_id})")

    start_time = time.time()
    while True:
        resp = requests.get(f"{BASE_URL}/v3/videos/{video_id}", headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"⚠️ Status check failed: {resp.text}. Retrying in 10s...")
            time.sleep(10)
            continue

        data = resp.json().get("data", {})
        status = data.get("status")
        elapsed = int(time.time() - start_time)

        if status == "completed":
            video_url = data.get("video_url") or data.get("url")
            print(f"\n🎉 Rendering completed in {elapsed}s!")
            print(f"🌐 Video URL: {video_url}")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"⬇️ Downloading to {output_path}...")
            video_data = requests.get(video_url, timeout=120).content
            output_path.write_bytes(video_data)
            print(f"✅ Finished! Video saved to: {output_path}")
            return
        elif status == "failed":
            error_msg = data.get("error", "Unknown error")
            print(f"\n❌ Video generation failed: {error_msg}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"   [{elapsed}s] Status: {status}...", end="\r", flush=True)
            time.sleep(10)



def main() -> None:
    parser = argparse.ArgumentParser(description="Automate HeyGen video manifesto generation for PrayPal")
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="Path to founder portrait image")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT, help="Path to manifesto markdown script")
    parser.add_argument("--output", type=Path, default=None, help="Output destination for finished MP4")
    parser.add_argument("--part", type=int, default=None, help="Generate a specific part (1-6)")
    parser.add_argument("--all-parts", action="store_true", help="Generate all parts as individual video files")
    parser.add_argument("--voice-id", type=str, default=None, help="Specific HeyGen Voice ID")
    parser.add_argument("--audio-file", type=Path, default=None, help="Optional pre-recorded audio file to lip-sync")
    parser.add_argument("--api-key", type=str, default=None, help="HeyGen API Key")
    parser.add_argument("--list-voices", action="store_true", help="List available HeyGen voices and exit")
    parser.add_argument("--dry-run", action="store_true", help="Extract script and inspect without invoking API")

    args = parser.parse_args()

    if args.list_voices:
        key = get_api_key(args.api_key)
        list_voices(key)
        return

    parts_dict = extract_parts(args.script)
    print("==================================================================")
    print(f"📜 PRAYPAL FOUNDING VIDEO MANIFESTO — {len(parts_dict)} PARTS FOUND")
    print("==================================================================")
    for num, (title, text) in sorted(parts_dict.items()):
        words = len(text.split())
        print(f"  Part {num}: {title} ({words} words, ~{max(1, words // 130)} min)")

    if args.dry_run:
        print("==================================================================")
        print("🔍 Dry-run complete. Image:", args.image)
        return

    api_key = get_api_key(args.api_key)

    # 1. Upload portrait asset once
    image_id = upload_image(api_key, args.image)

    # 2. Upload optional audio file if provided
    audio_id = None
    if args.audio_file:
        audio_id = upload_audio(api_key, args.audio_file)

    parts_to_generate: list[int]
    if args.all_parts:
        parts_to_generate = sorted(parts_dict.keys())
    elif args.part is not None:
        parts_to_generate = [args.part]
    else:
        # If no part specified, generate Part 1 by default (or user can pass --all-parts)
        parts_to_generate = [1]

    for p_num in parts_to_generate:
        p_title, p_text = parts_dict[p_num]
        out_file = args.output if (len(parts_to_generate) == 1 and args.output) else (
            args.script.parent / "assets" / f"founding_manifesto_part_{p_num}.mp4"
        )
        print("\n------------------------------------------------------------------")
        print(f"🎬 Generating Part {p_num}: {p_title}")
        print(f"Text snippet: {p_text[:120]}...")
        print("------------------------------------------------------------------")

        video_id = generate_video(
            api_key=api_key,
            image_id=image_id,
            script_text=p_text,
            voice_id=args.voice_id,
            audio_asset_id=audio_id,
        )

        poll_and_download(api_key, video_id, out_file)


if __name__ == "__main__":
    main()

