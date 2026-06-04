"""Transcribe a video — unified entry point with auto backend selection.

Dispatches to the best ASR backend based on language and available API keys:
  - Volcengine BigASR Turbo (volc.bigasr.auc_turbo): Chinese & CJK languages,
    word-level timestamps, speaker diarization, Base64 upload, fast one-shot.
  - ElevenLabs Scribe: English & other languages, word-level timestamps,
    speaker diarization, audio event tagging.

Auto-selection rules (--backend auto):
  - If language is zh/zh-CN/yue/ja/ko → Volcengine
  - If language is en or unspecified → check key availability:
      both keys present → Volcengine (Chinese-first assumption for our use case)
      only Scribe key   → Scribe
      only Volc key     → Volcengine
  - Override with --backend volc or --backend scribe

All backends output Scribe-compatible JSON so that pack_transcripts.py and
render.py work unchanged.

Cached: if the output file already exists, the upload is skipped.

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --language zh-CN
    python helpers/transcribe.py <video_path> --language en
    python helpers/transcribe.py <video_path> --backend volc
    python helpers/transcribe.py <video_path> --backend scribe
    python helpers/transcribe.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe.py <video_path> --num-speakers 2
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

# --------------- Backend detection helpers ---------------


def _load_env_value(key: str) -> str:
    """Load a single value from .env files or environment."""
    repo_root = Path(__file__).resolve().parent.parent
    for candidate in [repo_root / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return os.environ.get(key, "")


def _has_volc_key() -> bool:
    return bool(_load_env_value("VOLC_ASR_APP_KEY"))


def _has_scribe_key() -> bool:
    return bool(_load_env_value("ELEVENLABS_API_KEY"))


# Languages where Volcengine is strongly preferred (better Chinese/CJK support)
_VOLC_PREFERRED_LANGUAGES = {"zh", "zh-CN", "yue", "yue-CN", "ja", "ja-JP", "ko", "ko-KR"}


def select_backend(language: str | None, force: str | None = None) -> str:
    """Decide which backend to use.

    Returns "volc" or "scribe".

    Args:
        language: User-specified language code (or None for auto-detect).
        force: User-forced backend via --backend (or None for auto).
    """
    if force:
        if force not in ("volc", "scribe"):
            sys.exit(f"Unknown backend: {force}. Use 'volc', 'scribe', or 'auto'.")
        return force

    # Auto-selection based on language and key availability
    has_volc = _has_volc_key()
    has_scribe = _has_scribe_key()

    if not has_volc and not has_scribe:
        sys.exit(
            "No ASR API key found.\n"
            "Set VOLC_ASR_APP_KEY (Volcengine) or ELEVENLABS_API_KEY (Scribe) in .env or environment."
        )

    # Chinese/CJK → always prefer Volcengine if available
    if language and language in _VOLC_PREFERRED_LANGUAGES:
        if has_volc:
            return "volc"
        if has_scribe:
            print(f"  ⚠️  Language {language} is better served by Volcengine, but only Scribe key found. Using Scribe.", file=sys.stderr)
            return "scribe"

    # English → Scribe if available (Scribe's English diarization + audio events are stronger)
    if language and language.startswith("en"):
        if has_scribe:
            return "scribe"
        if has_volc:
            print("  ℹ️  English with Volcengine backend (no Scribe key found).", file=sys.stderr)
            return "volc"

    # Unspecified language → prefer Volcengine (our primary use case is Chinese travel content)
    if has_volc:
        return "volc"
    return "scribe"


# --------------- Scribe backend (original logic) ---------------

SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def _load_scribe_key() -> str:
    key = _load_env_value("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY not found in .env or environment")
    return key


def _extract_audio(video_path: Path, dest: Path, fmt: str = "wav") -> None:
    """Extract mono 16kHz audio from video."""
    if fmt == "mp3":
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "128k",
            str(dest),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(dest),
        ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _call_scribe(
    audio_path: Path,
    api_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
) -> dict:
    data: dict[str, str] = {
        "model_id": "scribe_v1",
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if language:
        data["language_code"] = language
    if num_speakers:
        data["num_speakers"] = str(num_speakers)

    with open(audio_path, "rb") as f:
        resp = requests.post(
            SCRIBE_URL,
            headers={"xi-api-key": api_key},
            files={"file": (audio_path.name, f, "audio/wav")},
            data=data,
            timeout=1800,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Scribe returned {resp.status_code}: {resp.text[:500]}")

    return resp.json()


def _transcribe_scribe(
    video: Path,
    edit_dir: Path,
    language: str | None = None,
    num_speakers: int | None = None,
    verbose: bool = True,
) -> Path:
    """Transcribe using ElevenLabs Scribe. Returns path to transcript JSON."""
    api_key = _load_scribe_key()

    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        _extract_audio(video, audio)
        size_mb = audio.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  uploading {video.stem}.wav ({size_mb:.1f} MB) → Scribe", flush=True)
        payload = _call_scribe(audio, api_key, language, num_speakers)

    # Tag backend
    if isinstance(payload, dict):
        payload["_backend"] = "elevenlabs_scribe"

    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        if isinstance(payload, dict) and "words" in payload:
            print(f"    words: {len(payload['words'])}")

    return out_path


# --------------- Volcengine backend ---------------


def _transcribe_volc(
    video: Path,
    edit_dir: Path,
    language: str | None = None,
    num_speakers: int | None = None,
    enable_speaker_info: bool = True,
    enable_emotion_detection: bool = False,
    audio_format: str = "auto",
    verbose: bool = True,
) -> Path:
    """Transcribe using Volcengine BigASR Turbo. Returns path to transcript JSON.

    Delegates to transcribe_volc.transcribe_one for the actual API call,
    keeping all the conversion logic in one place.
    """
    try:
        from transcribe_volc import (
            load_api_keys,
            transcribe_one as volc_transcribe_one,
        )
    except ImportError:
        # Fall back to absolute import
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from transcribe_volc import (
            load_api_keys,
            transcribe_one as volc_transcribe_one,
        )

    app_key, access_key = load_api_keys()

    return volc_transcribe_one(
        video=video,
        edit_dir=edit_dir,
        app_key=app_key,
        access_key=access_key,
        language=language,
        enable_speaker_info=enable_speaker_info,
        enable_emotion_detection=enable_emotion_detection,
        audio_format=audio_format,
        verbose=verbose,
    )


# --------------- Unified entry point ---------------


def transcribe_one(
    video: Path,
    edit_dir: Path,
    language: str | None = None,
    num_speakers: int | None = None,
    backend: str = "auto",
    enable_speaker_info: bool = True,
    enable_emotion_detection: bool = False,
    audio_format: str = "auto",
    verbose: bool = True,
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON.

    Auto-selects backend based on language and API key availability,
    or uses the explicitly specified backend.

    Cached: returns existing path immediately if the transcript already exists.
    """
    # Check cache before selecting backend (both backends produce same output path)
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    # Select backend
    chosen = select_backend(language, force=None if backend == "auto" else backend)

    if verbose:
        labels = {"volc": "Volcengine BigASR Turbo", "scribe": "ElevenLabs Scribe"}
        print(f"  backend: {labels[chosen]}" + (f" (auto-selected for {language or 'auto-detect'})" if backend == "auto" else ""))

    if chosen == "volc":
        return _transcribe_volc(
            video=video,
            edit_dir=edit_dir,
            language=language,
            num_speakers=num_speakers,
            enable_speaker_info=enable_speaker_info,
            enable_emotion_detection=enable_emotion_detection,
            audio_format=audio_format,
            verbose=verbose,
        )
    else:
        return _transcribe_scribe(
            video=video,
            edit_dir=edit_dir,
            language=language,
            num_speakers=num_speakers,
            verbose=verbose,
        )


# --------------- CLI ---------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Transcribe a video — auto-selects ASR backend (Volcengine / ElevenLabs Scribe)"
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
        help="Language code: zh, zh-CN, en, ja, ko, yue, auto (default: auto-detect)",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Optional number of speakers when known. Improves diarization accuracy.",
    )
    ap.add_argument(
        "--backend",
        type=str,
        default="auto",
        choices=["auto", "volc", "scribe"],
        help="ASR backend: auto (default), volc (Volcengine BigASR Turbo), scribe (ElevenLabs Scribe)",
    )
    ap.add_argument(
        "--no-speaker-info",
        action="store_true",
        help="Disable speaker diarization (Volcengine backend only)",
    )
    ap.add_argument(
        "--emotion-detection",
        action="store_true",
        help="Enable emotion detection (Volcengine backend only)",
    )
    ap.add_argument(
        "--audio-format",
        type=str,
        default="auto",
        choices=["auto", "wav", "mp3"],
        help="Audio format for Volcengine upload: auto (default), wav, mp3",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        language=args.language,
        num_speakers=args.num_speakers,
        backend=args.backend,
        enable_speaker_info=not args.no_speaker_info,
        enable_emotion_detection=args.emotion_detection,
        audio_format=args.audio_format,
    )


if __name__ == "__main__":
    main()
