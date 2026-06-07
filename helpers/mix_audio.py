"""Mix video audio with background music.

Uses ffmpeg directly — no pydub or other audio libraries.
Supports simple mixing, voiceover ducking, BGM looping, and
two-pass loudness normalization (-14 LUFS / -1 dBTP / LRA 11).

Usage:
    python helpers/mix_audio.py video.mp4 bgm.mp3
    python helpers/mix_audio.py video.mp4 bgm.mp3 --duck-voiceover --loop-bgm
    python helpers/mix_audio.py video.mp4 bgm.mp3 --bgm-volume 0.2 --fade-in 3.0 --fade-out 4.0
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

# -------- Loudness targets (matches render.py) --------------------------------

LOUDNORM_I = -14
LOUDNORM_TP = -1
LOUDNORM_LRA = 11

# -------- Helpers --------------------------------------------------------------


def get_duration(path: Path) -> float:
    """Get duration of a media file via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    return float(data["format"]["duration"])


def has_audio_stream(path: Path) -> bool:
    """Check whether a video file contains an audio stream."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    return "audio" in out.stdout.strip()


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with input_i, input_tp, input_lra, input_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stderr = proc.stderr

    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(input_path: Path, output_path: Path) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed.
    """
    logging.info("loudnorm pass 1: measuring %s", input_path.name)
    measurement = measure_loudness(input_path)
    if measurement is None:
        logging.warning("loudnorm measurement failed — falling back to 1-pass")
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    logging.info(
        "  measured: I=%s LUFS  TP=%s  LRA=%s",
        measurement["input_i"], measurement["input_tp"], measurement["input_lra"],
    )

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    logging.info("loudnorm pass 2: normalizing -> %s", output_path.name)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Main pipeline --------------------------------------------------------


def mix_audio(
    video_path: Path,
    bgm_path: Path,
    output_path: Path,
    bgm_volume: float = 0.3,
    fade_in: float = 2.0,
    fade_out: float = 3.0,
    duck_voiceover: bool = False,
    bgm_start_offset: float = 0.0,
    loop_bgm: bool = False,
) -> Path:
    """Mix BGM into video and apply two-pass loudness normalization.

    Returns the final output path.
    """
    video_dur = get_duration(video_path)
    bgm_dur = get_duration(bgm_path)
    has_audio = has_audio_stream(video_path)

    logging.info("Video: %s (%.2fs, audio=%s)", video_path.name, video_dur, has_audio)
    logging.info("BGM:   %s (%.2fs)", bgm_path.name, bgm_dur)

    # Effective BGM duration after start offset
    effective_bgm_dur = max(bgm_dur - bgm_start_offset, 0.0)

    # Determine if we need -stream_loop for BGM
    need_loop = loop_bgm and effective_bgm_dur < video_dur

    # Build the intermediate (pre-loudnorm) path next to the final output
    pre_loudnorm = output_path.with_suffix(".pre_loudnorm.mp4")

    # ---- Build ffmpeg command -------------------------------------------------

    # Common ffmpeg prefix
    cmd: list[str] = ["ffmpeg", "-y", "-hide_banner", "-nostats"]

    # Input(s)
    cmd += ["-i", str(video_path)]

    if need_loop:
        cmd += ["-stream_loop", "-1"]

    if bgm_start_offset > 0:
        cmd += ["-itsoffset", str(bgm_start_offset)]

    cmd += ["-i", str(bgm_path)]

    # ---- Filter chain ---------------------------------------------------------

    video_dur_str = f"{video_dur:.6f}"
    # Clamp fades so they never overlap
    actual_fade_in = min(fade_in, video_dur / 2)
    actual_fade_out = min(fade_out, video_dur / 2)
    fade_in_str = f"{actual_fade_in:.6f}"
    fade_out_start = max(video_dur - actual_fade_out, 0.0)
    fade_out_start_str = f"{fade_out_start:.6f}"
    fade_out_dur_str = f"{actual_fade_out:.6f}"
    bgm_vol_str = f"{bgm_volume:.4f}"

    if not has_audio:
        # Silent video — BGM is the only audio track
        filter_complex = (
            f"[1:a]atrim=0:{video_dur_str}"
            f",afade=t=in:st=0:d={fade_in_str}"
            f",afade=t=out:st={fade_out_start_str}:d={fade_out_dur_str}"
            f",volume={bgm_vol_str}[loud]"
        )
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[loud]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        ]
        if need_loop:
            cmd += ["-shortest"]

    elif duck_voiceover:
        # Voiceover ducking — sidechaincompress lowers BGM when voice is present
        # sidechaincompress takes two inputs: [main][sidechain]
        filter_complex = (
            f"[0:a]volume=1.0[orig];"
            f"[1:a]afade=t=in:st=0:d={fade_in_str}"
            f",afade=t=out:st={fade_out_start_str}:d={fade_out_dur_str}"
            f",volume={bgm_vol_str}[bgm];"
            f"[bgm][orig]sidechaincompress=threshold=0.1:ratio=10:attack=0.01:release=0.5:makeup=1[ducked];"
            f"[orig][ducked]amix=inputs=2:duration=first:dropout_transition=3[loud]"
        )
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[loud]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        ]
        if need_loop:
            cmd += ["-shortest"]

    else:
        # Simple mix — original audio + BGM
        filter_complex = (
            f"[0:a]volume=1.0[orig];"
            f"[1:a]afade=t=in:st=0:d={fade_in_str}"
            f",afade=t=out:st={fade_out_start_str}:d={fade_out_dur_str}"
            f",volume={bgm_vol_str}[bgm];"
            f"[orig][bgm]amix=inputs=2:duration=first:dropout_transition=3[loud]"
        )
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[loud]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        ]
        if need_loop:
            cmd += ["-shortest"]

    cmd += ["-movflags", "+faststart", str(pre_loudnorm)]

    logging.info("Mixing BGM into video...")
    logging.debug("  $ %s", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # ---- Two-pass loudness normalization --------------------------------------

    logging.info("Applying two-pass loudness normalization...")
    apply_loudnorm_two_pass(pre_loudnorm, output_path)

    # Clean up intermediate
    pre_loudnorm.unlink(missing_ok=True)

    logging.info("Done: %s", output_path)
    return output_path


# -------- CLI ------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    ap = argparse.ArgumentParser(
        description="Mix video audio with background music (ffmpeg-based).",
    )
    ap.add_argument("video_path", type=Path, help="Input video file (MP4)")
    ap.add_argument("bgm_path", type=Path, help="BGM audio file (MP3/WAV)")
    ap.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output video path (default: <video>_mixed.mp4)",
    )
    ap.add_argument(
        "--bgm-volume", type=float, default=0.3,
        help="BGM volume 0.0-1.0 (default: 0.3)",
    )
    ap.add_argument(
        "--fade-in", type=float, default=2.0,
        help="BGM fade-in duration in seconds (default: 2.0)",
    )
    ap.add_argument(
        "--fade-out", type=float, default=3.0,
        help="BGM fade-out duration in seconds (default: 3.0)",
    )
    ap.add_argument(
        "--duck-voiceover", action="store_true",
        help="Enable voiceover ducking (auto-lower BGM when voice detected)",
    )
    ap.add_argument(
        "--bgm-start-offset", type=float, default=0.0,
        help="Start BGM at this offset in seconds (default: 0.0)",
    )
    ap.add_argument(
        "--loop-bgm", action="store_true",
        help="Loop BGM if shorter than video",
    )

    args = ap.parse_args()

    if not args.video_path.exists():
        logging.error("Video file not found: %s", args.video_path)
        sys.exit(1)
    if not args.bgm_path.exists():
        logging.error("BGM file not found: %s", args.bgm_path)
        sys.exit(1)

    output_path = args.output
    if output_path is None:
        stem = args.video_path.stem + "_mixed"
        output_path = args.video_path.with_name(stem + args.video_path.suffix)

    mix_audio(
        video_path=args.video_path,
        bgm_path=args.bgm_path,
        output_path=output_path,
        bgm_volume=args.bgm_volume,
        fade_in=args.fade_in,
        fade_out=args.fade_out,
        duck_voiceover=args.duck_voiceover,
        bgm_start_offset=args.bgm_start_offset,
        loop_bgm=args.loop_bgm,
    )


if __name__ == "__main__":
    main()
