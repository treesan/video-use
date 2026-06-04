"""Transcribe a video with Volcengine BigASR Turbo (volc.bigasr.auc_turbo).

Extracts mono 16kHz WAV audio via ffmpeg, sends as Base64 to the Volcengine
flash endpoint, and converts the response to ElevenLabs Scribe-compatible JSON
so that pack_transcripts.py and render.py work unchanged.

Supported features:
  - Word-level timestamps (ms → s conversion)
  - Speaker diarization (enable_speaker_info)
  - Emotion detection (enable_emotion_detection)
  - Punctuation (enable_punc) and inverse text normalization (enable_itn)

Cached: if the output file already exists, the upload is skipped.

Usage:
    python helpers/transcribe_volc.py <video_path>
    python helpers/transcribe_volc.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe_volc.py <video_path> --language zh-CN
    python helpers/transcribe_volc.py <video_path> --num-speakers 2
    python helpers/transcribe_volc.py <video_path> --audio-format mp3

Environment variables:
    VOLC_ASR_APP_KEY  — App Key from Volcengine console (new version)
    VOLC_ASR_ACCESS_KEY — Access Key from Volcengine console (old version, optional)
    or set both in .env at the video-use repo root.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests

# --------------- Constants ---------------

VOLC_FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"

# Language mapping: user-friendly codes → Volcengine codes
LANGUAGE_MAP = {
    "zh": "zh-CN",
    "zh-CN": "zh-CN",
    "en": "en-US",
    "en-US": "en-US",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "yue": "yue-CN",
    "auto": None,  # omit language field for auto-detect
}


# --------------- API key loading ---------------


def load_api_keys() -> tuple[str, str]:
    """Load Volcengine API keys from .env or environment.

    Returns (app_key, access_key).  access_key may be empty for new-version
    console users who only need app_key.
    """
    # Try .env at repo root
    repo_root = Path(__file__).resolve().parent.parent
    env_candidates = [repo_root / ".env", Path(".env")]

    app_key = ""
    access_key = ""

    for candidate in env_candidates:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "VOLC_ASR_APP_KEY":
                    app_key = app_key or v
                elif k == "VOLC_ASR_ACCESS_KEY":
                    access_key = access_key or v

    app_key = app_key or os.environ.get("VOLC_ASR_APP_KEY", "")
    access_key = access_key or os.environ.get("VOLC_ASR_ACCESS_KEY", "")

    if not app_key:
        sys.exit(
            "VOLC_ASR_APP_KEY not found.\n"
            "Set it in .env (VOLC_ASR_APP_KEY=xxx) or as environment variable.\n"
            "Get your key from: https://console.volcengine.com/speech/new/setting/apikeys"
        )

    return app_key, access_key


# --------------- Audio extraction ---------------


def extract_audio(video_path: Path, dest: Path, fmt: str = "wav") -> None:
    """Extract mono 16kHz audio from video.

    For 'wav' format: PCM s16le (lossless, larger).
    For 'mp3' format: libmp3lame 128k (smaller, good for long recordings).
    """
    if fmt == "mp3":
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "128k",
            str(dest),
        ]
    else:  # wav
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(dest),
        ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------- Volcengine API call ---------------


def call_volc_asr(
    audio_path: Path,
    app_key: str,
    access_key: str,
    language: str | None = None,
    enable_speaker_info: bool = True,
    enable_emotion_detection: bool = False,
    enable_itn: bool = True,
    enable_punc: bool = True,
    enable_ddc: bool = False,
) -> dict:
    """Call Volcengine BigASR Turbo flash endpoint.

    Returns the raw Volcengine response JSON.
    """
    request_id = str(uuid.uuid4())

    # Build headers — support both new-version (X-Api-Key) and old-version
    # (X-Api-App-Key + X-Api-Access-Key) console credentials.
    headers: dict[str, str] = {
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    if access_key:
        headers["X-Api-App-Key"] = app_key
        headers["X-Api-Access-Key"] = access_key
    else:
        headers["X-Api-Key"] = app_key

    # Read audio as Base64
    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")

    # Build request body
    body: dict = {
        "user": {"uid": app_key},
        "audio": {"data": audio_b64},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": enable_itn,
            "enable_punc": enable_punc,
            "enable_ddc": enable_ddc,
            "enable_speaker_info": enable_speaker_info,
            "show_utterances": True,
        },
    }

    # Language: only set if explicitly specified (not auto-detect)
    if language:
        volc_lang = LANGUAGE_MAP.get(language, language)
        if volc_lang is not None:
            body["audio"]["language"] = volc_lang

    # Emotion detection (only works when language is zh-CN or omitted)
    if enable_emotion_detection:
        body["request"]["enable_emotion_detection"] = True

    resp = requests.post(VOLC_FLASH_URL, json=body, headers=headers, timeout=1800)

    # Check response status from header
    status_code = resp.headers.get("X-Api-Status-Code", "")
    if status_code != "20000000":
        msg = resp.headers.get("X-Api-Message", resp.text[:500])
        raise RuntimeError(
            f"Volcengine ASR failed: status={status_code}, message={msg}"
        )

    return resp.json()


# --------------- Format conversion: Volcengine → Scribe-compatible ---------------


def volc_to_scribe(volc_response: dict) -> dict:
    """Convert Volcengine BigASR response to ElevenLabs Scribe-compatible JSON.

    Scribe format (what pack_transcripts.py and render.py expect):
    {
        "text": "full transcript text",
        "words": [
            {"type": "word", "text": "你好", "start": 0.45, "end": 0.77, "speaker_id": "0"},
            {"type": "spacing", "start": 0.77, "end": 1.10},
            {"type": "word", "text": "世界", "start": 1.10, "end": 1.53, "speaker_id": "0"},
            ...
        ]
    }

    Volcengine format:
    {
        "result": {
            "text": "full text",
            "utterances": [
                {
                    "text": "你好世界。",
                    "start_time": 450, "end_time": 1530,   // milliseconds
                    "words": [
                        {"text": "你", "start_time": 450, "end_time": 570},
                        {"text": "好", "start_time": 570, "end_time": 770},
                        ...
                    ],
                    "speaker": 0   // optional, when enable_speaker_info
                }
            ]
        },
        "audio_info": {"duration": 2499}
    }
    """
    result = volc_response.get("result", {})
    utterances = result.get("utterances", [])
    full_text = result.get("text", "")
    audio_info = volc_response.get("audio_info", {})

    scribe_words: list[dict] = []
    prev_end_s: float | None = None

    for utt_idx, utt in enumerate(utterances):
        utt_words = utt.get("words", [])
        speaker = utt.get("speaker")
        # Scribe uses string speaker IDs like "speaker_0"
        speaker_id = str(speaker) if speaker is not None else None

        for w in utt_words:
            start_ms = w.get("start_time", 0)
            end_ms = w.get("end_time", 0)
            start_s = start_ms / 1000.0
            end_s = end_ms / 1000.0
            text = w.get("text", "").strip()

            if not text:
                continue

            # Insert spacing between words if there's a gap >= 0.05s
            if prev_end_s is not None and start_s - prev_end_s >= 0.05:
                scribe_words.append({
                    "type": "spacing",
                    "start": prev_end_s,
                    "end": start_s,
                })

            scribe_words.append({
                "type": "word",
                "text": text,
                "start": start_s,
                "end": end_s,
                "speaker_id": speaker_id,
            })
            prev_end_s = end_s

        # Insert spacing between utterances if there's a gap
        utt_end_ms = utt.get("end_time", 0)
        utt_end_s = utt_end_ms / 1000.0
        if utt_end_s > (prev_end_s or 0):
            if prev_end_s is not None and utt_end_s - prev_end_s >= 0.05:
                # Already handled by word-level spacing above; skip
                pass
            prev_end_s = utt_end_s

    return {
        "text": full_text,
        "words": scribe_words,
        # Preserve Volcengine raw data for reference
        "_volc_raw": volc_response,
        "_backend": "volc.bigasr.auc_turbo",
        "_audio_duration_ms": audio_info.get("duration", 0),
    }


# --------------- Main transcription function ---------------


def transcribe_one(
    video: Path,
    edit_dir: Path,
    app_key: str,
    access_key: str,
    language: str | None = None,
    enable_speaker_info: bool = True,
    enable_emotion_detection: bool = False,
    audio_format: str = "auto",
    verbose: bool = True,
) -> Path:
    """Transcribe a single video via Volcengine BigASR Turbo.

    Returns path to transcript JSON (Scribe-compatible format).

    Cached: returns existing path immediately if the transcript already exists.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    # Determine audio format: auto-detect based on video duration
    if audio_format == "auto":
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
                capture_output=True, text=True, check=True,
            )
            duration_s = float(probe.stdout.strip())
            # WAV at 16kHz mono ≈ 32KB/s. 100MB limit ≈ ~50 min.
            # For long videos (>30 min), use MP3 to stay under the size limit.
            audio_format = "mp3" if duration_s > 1800 else "wav"
        except Exception:
            audio_format = "wav"

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        ext = "mp3" if audio_format == "mp3" else "wav"
        audio = Path(tmp) / f"{video.stem}.{ext}"
        extract_audio(video, audio, fmt=audio_format)
        size_mb = audio.stat().st_size / (1024 * 1024)

        # Safety check: volc.bigasr.auc_turbo limit is 100MB for the request
        # but "尽量20M以内" for Base64 upload per docs
        if size_mb > 80:
            if verbose:
                print(f"  WARNING: audio is {size_mb:.1f}MB, may exceed upload limit")

        if verbose:
            print(f"  uploading {video.stem}.{ext} ({size_mb:.1f} MB) → Volcengine BigASR Turbo", flush=True)

        volc_response = call_volc_asr(
            audio_path=audio,
            app_key=app_key,
            access_key=access_key,
            language=language,
            enable_speaker_info=enable_speaker_info,
            enable_emotion_detection=enable_emotion_detection,
        )

    # Convert to Scribe-compatible format
    scribe_compat = volc_to_scribe(volc_response)

    out_path.write_text(json.dumps(scribe_compat, indent=2, ensure_ascii=False))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        n_words = sum(1 for w in scribe_compat.get("words", []) if w.get("type") == "word")
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        print(f"    words: {n_words}")
        if scribe_compat.get("_audio_duration_ms"):
            dur_s = scribe_compat["_audio_duration_ms"] / 1000
            print(f"    audio duration: {dur_s:.1f}s")

    return out_path


# --------------- CLI ---------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Transcribe a video with Volcengine BigASR Turbo (volc.bigasr.auc_turbo)"
    )
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Language code: zh, en, ja, ko, yue, auto (default: auto-detect)",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Hint for speaker count (informational; Volcengine auto-detects up to 10)",
    )
    ap.add_argument(
        "--no-speaker-info",
        action="store_true",
        help="Disable speaker diarization",
    )
    ap.add_argument(
        "--emotion-detection",
        action="store_true",
        help="Enable emotion detection (happy/sad/angry/neutral/surprise per utterance)",
    )
    ap.add_argument(
        "--audio-format",
        type=str,
        default="auto",
        choices=["auto", "wav", "mp3"],
        help="Audio format for upload: auto (default), wav, mp3",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    app_key, access_key = load_api_keys()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        app_key=app_key,
        access_key=access_key,
        language=args.language,
        enable_speaker_info=not args.no_speaker_info,
        enable_emotion_detection=args.emotion_detection,
        audio_format=args.audio_format,
    )


if __name__ == "__main__":
    main()
