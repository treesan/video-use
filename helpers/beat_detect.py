"""Beat/structure analysis on BGM audio files.

Uses madmom for DSP-based beat detection and VLM (via vlm_client) for
song structure identification. Set VLM_PROVIDER in .env to switch providers.
Outputs beats.json with keypoints, sections, energy profile, and best-start offset.

Usage:
    python helpers/beat_detect.py <bgm_file>
    python helpers/beat_detect.py <bgm_file> --output /path/beats.json
    python helpers/beat_detect.py <bgm_file> --model mimo-v2.5
    python helpers/beat_detect.py <bgm_file> --target-duration 120
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# --------------- Optional: madmom ---------------
# madmom 0.16.1 is incompatible with Python 3.10+ / NumPy 1.24+.
# Apply compatibility patches before importing.
import collections
import collections.abc
if not hasattr(collections, "MutableSequence"):
    collections.MutableSequence = collections.abc.MutableSequence  # type: ignore[attr-defined]
import numpy as _np
if not hasattr(_np, "float"):
    _np.float = float  # type: ignore[attr-defined]
if not hasattr(_np, "int"):
    _np.int = int  # type: ignore[attr-defined]
if not hasattr(_np, "bool"):
    _np.bool = bool  # type: ignore[attr-defined]
if not hasattr(_np, "complex"):
    _np.complex = complex  # type: ignore[attr-defined]
if not hasattr(_np, "object"):
    _np.object = object  # type: ignore[attr-defined]
if not hasattr(_np, "str"):
    _np.str = str  # type: ignore[attr-defined]

try:
    import madmom
except (ImportError, AttributeError):
    madmom = None  # type: ignore[assignment]

# --------------- Shared VLM client ---------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vlm_client import get_client, get_model, completion_kwargs, strip_thinking


def _default_output_path(bgm_file: Path) -> Path:
    """Derive default output path: <parent_dir>/edit/beats.json.

    If bgm_file is already inside an edit/ dir, use its parent's parent.
    Otherwise use bgm_file's parent.
    """
    bgm_dir = bgm_file.resolve().parent
    if bgm_dir.name == "edit":
        return bgm_dir / "beats.json"
    return bgm_dir / "edit" / "beats.json"


# --------------- madmom beat detection ---------------


def _run_madmom_beats(bgm_file: Path) -> tuple[float, list[float]]:
    """Run madmom three-way beat detection.

    Returns (bpm, keypoints) where keypoints are deduplicated beat times
    in seconds sorted ascending.
    """
    if madmom is None:
        print("  [beat_detect] madmom not installed, skipping beat detection", file=sys.stderr)
        return 0.0, []

    # --- 1. Beat tracking with DBN (more robust than plain BeatTrackingProcessor) ---
    print("  [beat_detect] Running DBN beat tracking ...", file=sys.stderr)
    try:
        dbn_proc = madmom.features.beats.DBNBeatTrackingProcessor(
            min_bpm=55, max_bpm=215, fps=100
        )
        dbn_act = madmom.features.beats.RNNBeatProcessor()(str(bgm_file))
        dbn_beats = dbn_proc(dbn_act)
        downbeat_times = dbn_beats.tolist()
    except Exception as e:
        print(f"  [beat_detect] DBN beat tracking failed: {e}", file=sys.stderr)
        downbeat_times = []

    # --- 2. Beat tracking (mel energy peaks) ---
    print("  [beat_detect] Running beat tracking ...", file=sys.stderr)
    try:
        beat_proc = madmom.features.beats.BeatTrackingProcessor(fps=100)
        beat_act = madmom.features.beats.RNNBeatProcessor()(str(bgm_file))
        beat_times = beat_proc(beat_act).tolist()
    except Exception as e:
        print(f"  [beat_detect] Beat tracking failed: {e}", file=sys.stderr)
        beat_times = []

    # --- 3. Pitch onset detection ---
    print("  [beat_detect] Running onset detection ...", file=sys.stderr)
    try:
        onset_proc = madmom.features.onsets.OnsetPeakPickingProcessor(
            threshold=0.3
        )
        onset_act = madmom.features.onsets.RNNOnsetProcessor()(str(bgm_file))
        onset_times = onset_proc(onset_act).tolist()
    except Exception as e:
        print(f"  [beat_detect] Onset detection failed: {e}", file=sys.stderr)
        onset_times = []

    # --- Merge & deduplicate (50ms tolerance) ---
    all_times = sorted(set(downbeat_times + beat_times + onset_times))
    keypoints: list[float] = []
    tolerance = 0.05  # 50ms
    for t in all_times:
        if not keypoints or (t - keypoints[-1]) >= tolerance:
            keypoints.append(round(t, 3))

    # --- Estimate BPM from beat intervals ---
    bpm = _estimate_bpm(beat_times) if beat_times else 0.0

    print(f"  [beat_detect] Found {len(keypoints)} keypoints, BPM ~ {bpm:.0f}", file=sys.stderr)
    return bpm, keypoints


def _estimate_bpm(beat_times: list[float]) -> float:
    """Estimate BPM from beat times using median interval."""
    if len(beat_times) < 2:
        return 0.0
    intervals = [beat_times[i + 1] - beat_times[i] for i in range(len(beat_times) - 1)]
    # Filter outliers: keep intervals within 2x of median
    intervals.sort()
    median = intervals[len(intervals) // 2]
    if median <= 0:
        return 0.0
    kept = [iv for iv in intervals if 0.5 * median <= iv <= 2.0 * median]
    if not kept:
        return 0.0
    avg_interval = sum(kept) / len(kept)
    return round(60.0 / avg_interval, 1)


# --------------- ebur128 energy analysis ---------------


def _run_ebur128(bgm_file: Path) -> tuple[list[dict[str, float]], float]:
    """Run ffmpeg ebur128 filter and parse momentary loudness over time.

    Returns (energy_profile, best_start).
    energy_profile: [{"time": ..., "loudness": ...}, ...]
    best_start: first time where momentary loudness exceeds -30 LUFS.
    """
    print("  [beat_detect] Running ebur128 energy analysis ...", file=sys.stderr)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(bgm_file),
        "-filter_complex", "ebur128",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr

    energy_profile: list[dict[str, float]] = []
    best_start = 0.0
    loudness_threshold = -30.0  # LUFS

    # Parse ebur128 summary lines. Format:
    #   t: 1.0   TARGET: -23 LUFS    M: -40.2 S: -42.1  I: -45.0  LRA:  5.0
    # We want the momentary (M:) values at each time point.
    pattern = re.compile(r"t:\s*([\d.]+).*?M:\s*([-\d.]+)")
    found_threshold = False
    for match in pattern.finditer(stderr):
        t = float(match.group(1))
        loudness = float(match.group(2))
        energy_profile.append({"time": round(t, 1), "loudness": round(loudness, 1)})

        if not found_threshold and loudness >= loudness_threshold:
            best_start = round(t, 1)
            found_threshold = True

    # Subsample if too many points (keep at most 2000)
    if len(energy_profile) > 2000:
        step = len(energy_profile) // 1000
        energy_profile = energy_profile[::step]

    print(
        f"  [beat_detect] ebur128: {len(energy_profile)} points, best_start={best_start}s",
        file=sys.stderr,
    )
    return energy_profile, best_start


# --------------- Audio metadata ---------------


def _get_duration(bgm_file: Path) -> float:
    """Get audio duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(bgm_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


# --------------- LLM song structure ---------------


def _llm_identify_sections(
    bpm: float,
    duration: float,
    keypoints: list[float],
    energy_profile: list[dict[str, float]],
    model: str,
) -> list[dict[str, str | float]]:
    """Ask LLM to identify song sections from keypoints and energy data."""
    print(f"  [beat_detect] Asking LLM ({model}) for song structure ...", file=sys.stderr)
    try:
        client = get_client()
    except Exception as exc:
        print(f"  [beat_detect] VLM client init failed: {exc} — skipping LLM", file=sys.stderr)
        return []

    # Summarize energy profile to avoid sending thousands of points
    if energy_profile:
        energy_summary = _summarize_energy(energy_profile)
    else:
        energy_summary = "no energy data available"

    # Thin keypoints for the prompt: at most 200 evenly spaced
    kp_display = keypoints
    if len(kp_display) > 200:
        step = len(kp_display) // 100
        kp_display = kp_display[::step]

    prompt = (
        "Analyze this music structure and identify song sections.\n"
        f"BPM: {bpm:.0f}, Duration: {duration:.1f}s\n\n"
        f"Keypoints (beat/onset times in seconds): {kp_display}\n\n"
        f"Energy profile summary: {energy_summary}\n\n"
        "Identify sections (Intro, Verse, Chorus, Bridge, Build-up, Drop, Outro) "
        "with start and end times in seconds.\n"
        "Output ONLY a JSON array, no markdown, no explanation. "
        'Each element: {"name": "SectionName", "start": 0.0, "end": 8.5}\n'
        "Times should align with the given keypoints where possible."
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=16384,
            **completion_kwargs("disabled"),
        )
        text = strip_thinking(resp.choices[0].message.content)
        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        sections = json.loads(text)
        if not isinstance(sections, list):
            return []
        # Validate each section has required fields
        valid = []
        for s in sections:
            if isinstance(s, dict) and "name" in s and "start" in s and "end" in s:
                valid.append({
                    "name": str(s["name"]),
                    "start": float(s["start"]),
                    "end": float(s["end"]),
                })
        return valid
    except Exception as e:
        print(f"  [beat_detect] LLM structure ID failed: {e}", file=sys.stderr)
        return []


def _summarize_energy(profile: list[dict[str, float]]) -> str:
    """Produce a short text summary of the energy profile for the LLM prompt."""
    if not profile:
        return "no energy data"
    loudnesses = [p["loudness"] for p in profile]
    min_l = min(loudnesses)
    max_l = max(loudnesses)

    # Find significant energy changes
    segments: list[str] = []
    prev_bucket = None
    for p in profile:
        # Bucket loudness into low/mid/high
        if p["loudness"] < -35:
            bucket = "low"
        elif p["loudness"] < -20:
            bucket = "mid"
        else:
            bucket = "high"
        if bucket != prev_bucket:
            segments.append(f"{bucket} at {p['time']:.0f}s")
            prev_bucket = bucket

    return f"range [{min_l:.0f} to {max_l:.0f} LUFS], transitions: {'; '.join(segments[:12])}"


# --------------- Main pipeline ---------------


def analyze(
    bgm_file: Path,
    output: Path,
    model: str | None = None,
    target_duration: float | None = None,
) -> dict:
    """Run full beat/structure analysis pipeline."""
    model = model or get_model()
    if not bgm_file.exists():
        sys.exit(f"File not found: {bgm_file}")

    if madmom is None:
        print(
            "  [beat_detect] WARNING: madmom not installed. "
            "Install with: pip install madmom",
            file=sys.stderr,
        )

    # 1. Basic metadata
    duration = _get_duration(bgm_file)
    print(f"  [beat_detect] Duration: {duration:.1f}s", file=sys.stderr)

    # 2. madmom beat detection
    bpm, keypoints = _run_madmom_beats(bgm_file)

    # 3. ebur128 energy analysis
    energy_profile, best_start = _run_ebur128(bgm_file)

    # 4. LLM structure identification
    sections = _llm_identify_sections(
        bpm=bpm,
        duration=duration,
        keypoints=keypoints,
        energy_profile=energy_profile,
        model=model,
    )

    # 5. Shortfall detection
    shortfall = None
    if target_duration is not None and duration < target_duration:
        shortfall = round(target_duration - duration, 1)
        print(
            f"  [beat_detect] SHORTFALL: BGM is {shortfall:.1f}s shorter than target {target_duration}s",
            file=sys.stderr,
        )

    result = {
        "bpm": bpm,
        "duration": round(duration, 1),
        "sections": sections,
        "keypoints": keypoints,
        "best_start": best_start,
        "energy_profile": energy_profile,
        "shortfall": shortfall,
    }

    # Write output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"  [beat_detect] Written: {output}", file=sys.stderr)

    return result


# --------------- CLI ---------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Beat/structure analysis on BGM audio files",
    )
    parser.add_argument("bgm_file", type=Path, help="Path to BGM audio file (MP3/WAV/M4A)")
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path for beats.json (default: <parent_dir>/edit/beats.json)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model for LLM structure ID (default: from VLM_PROVIDER config)",
    )
    parser.add_argument(
        "--target-duration", type=float, default=None,
        help="Target video duration in seconds (for shortfall detection)",
    )

    args = parser.parse_args()

    output = args.output or _default_output_path(args.bgm_file)
    model = args.model

    analyze(
        bgm_file=args.bgm_file,
        output=output,
        model=model,
        target_duration=args.target_duration,
    )


if __name__ == "__main__":
    main()
