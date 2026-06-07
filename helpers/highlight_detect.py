"""Highlight detection for silent aerial footage (e.g., DJI drone videos).

3-layer pipeline:
  1. PySceneDetect — shot boundary detection (AdaptiveDetector)
  2. OpenCV quality pre-screen — Laplacian blur + exposure + motion + black frame
  3. VLM scene description via native video understanding + LLM scoring

Uses the shared vlm_client module for provider routing (xiaomi/minimax).
Set VLM_PROVIDER in .env to switch between MiMo v2.5 and MiniMax-M3.

Output is highlights.json compatible with edl.json ranges — each highlight's
source/start/end can be copied directly into edl.json.

Usage:
    python helpers/highlight_detect.py <videos_dir>
    python helpers/highlight_detect.py <videos_dir> --theme "旅行Vlog-重庆"
    python helpers/highlight_detect.py <videos_dir> --no-vlm
    python helpers/highlight_detect.py <videos_dir> --min-score 0.6
    python helpers/highlight_detect.py <videos_dir> --output /path/to/highlights.json
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

# Allow imports from helpers/ when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vlm_client import (
    get_client,
    get_model,
    get_provider_config,
    build_video_content,
    completion_kwargs,
    strip_thinking,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".MP4", ".MOV"}


# -------- Layer 1: Shot boundary detection -----------------------------------


def detect_shots(videos_dir: Path) -> list[dict]:
    """Detect shot boundaries across all videos in the directory.

    Returns list of {source: stem, start: float, end: float}.
    """
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import AdaptiveDetector

    shots: list[dict] = []
    video_files = sorted(
        p for p in videos_dir.iterdir() if p.suffix in VIDEO_EXTS
    )
    if not video_files:
        log.warning("No video files found in %s", videos_dir)
        return shots

    for vf in video_files:
        log.info("Detecting shots in %s", vf.name)
        try:
            video = open_video(str(vf))
            sm = SceneManager()
            sm.add_detector(AdaptiveDetector())
            sm.detect_scenes(video)
            scene_list = sm.get_scene_list()
        except Exception as exc:
            log.error("Failed to detect shots in %s: %s", vf.name, exc)
            continue

        if not scene_list:
            # Single-shot video — use full duration
            dur = _video_duration(vf)
            if dur > 0:
                shots.append({"source": vf.stem, "start": 0.0, "end": dur})
            continue

        for i, (start_tc, end_tc) in enumerate(scene_list):
            shots.append({
                "source": vf.stem,
                "start": start_tc.get_seconds(),
                "end": end_tc.get_seconds(),
            })

        log.info("  Found %d shots in %s", len(scene_list), vf.name)

    return shots


def _video_duration(path: Path) -> float:
    """Get video duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


# -------- Layer 2: OpenCV quality pre-screen ---------------------------------


def quality_prescreen(
    videos_dir: Path,
    shots: list[dict],
    blur_threshold: int = 100,
) -> list[dict]:
    """Score each shot for quality. Adds quality_score (0-1) to each shot dict."""
    video_cache: dict[str, cv2.VideoCapture] = {}

    for shot in shots:
        source = shot["source"]
        if source not in video_cache:
            vf = _find_video(videos_dir, source)
            if vf is None:
                shot["quality_score"] = 0.0
                continue
            video_cache[source] = cv2.VideoCapture(str(vf))

        cap = video_cache[source]
        shot["quality_score"] = _score_shot(
            cap, shot["start"], shot["end"], blur_threshold
        )

    # Release captures
    for cap in video_cache.values():
        cap.release()

    return shots


def _find_video(videos_dir: Path, stem: str) -> Path | None:
    """Find video file matching stem in directory."""
    for ext in VIDEO_EXTS:
        p = videos_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def _extract_frame(cap: cv2.VideoCapture, time_s: float) -> np.ndarray | None:
    """Seek to time_s and read one frame."""
    cap.set(cv2.CAP_PROP_POS_MSEC, time_s * 1000)
    ret, frame = cap.read()
    return frame if ret else None


def _score_shot(
    cap: cv2.VideoCapture,
    start: float,
    end: float,
    blur_threshold: int,
) -> float:
    """Compute quality score for a single shot.

    Weighted: 0.4 * blur + 0.3 * exposure + 0.3 * motion
    """
    duration = end - start
    if duration <= 0:
        return 0.0

    # Sample every ~1s or 5 frames, whichever is less
    n_samples = max(2, min(int(duration), 5))
    sample_times = [
        start + i * duration / (n_samples - 1) for i in range(n_samples)
    ]

    frames: list[np.ndarray] = []
    for t in sample_times:
        f = _extract_frame(cap, t)
        if f is not None:
            frames.append(f)

    if not frames:
        return 0.0

    # Blur score
    blur_scores = []
    for f in frames:
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        blur_scores.append(1.0 if lap_var >= blur_threshold else 0.0)
    blur_score = sum(blur_scores) / len(blur_scores)

    # Exposure score
    exposure_scores = []
    for f in frames:
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        mean_bright = gray.mean()
        if mean_bright < 10 or mean_bright > 245:
            exposure_scores.append(0.0)
        else:
            exposure_scores.append(1.0)
    exposure_score = sum(exposure_scores) / len(exposure_scores)

    # Motion score
    motion_score = 0.5  # default for single frame
    if len(frames) >= 2:
        diffs = []
        for i in range(1, len(frames)):
            g1 = cv2.cvtColor(frames[i - 1], cv2.COLOR_BGR2GRAY)
            g2 = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY)
            # Resize to small for fast comparison
            g1 = cv2.resize(g1, (160, 90))
            g2 = cv2.resize(g2, (160, 90))
            diff = np.abs(g1.astype(float) - g2.astype(float)).mean()
            diffs.append(diff)
        avg_diff = sum(diffs) / len(diffs)
        # Good motion: 3-30 mean pixel diff. Too low = static, too high = shaky.
        if avg_diff < 1.0:
            motion_score = 0.3
        elif avg_diff < 3.0:
            motion_score = 0.6
        elif avg_diff <= 30.0:
            motion_score = 1.0
        elif avg_diff <= 50.0:
            motion_score = 0.7
        else:
            motion_score = 0.3

    return 0.4 * blur_score + 0.3 * exposure_score + 0.3 * motion_score


# -------- Layer 3a: VLM scene description (native video understanding) --------


def _extract_clip_base64(video: Path, start: float, end: float) -> str:
    """Extract a video clip segment and return as base64 string with data URI prefix."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = Path(tmpdir) / "clip.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(video),
            "-t", f"{end - start:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-vf", "scale=480:-2",
            "-an",  # strip audio — VLM only needs visuals
            "-movflags", "+faststart",
            str(clip_path),
        ]
        subprocess.run(
            cmd, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        data = clip_path.read_bytes()
        b64 = base64.b64encode(data).decode()
        return f"data:video/mp4;base64,{b64}"


def vlm_describe(
    videos_dir: Path,
    shots: list[dict],
    model: str,
    min_score: float,
) -> list[dict]:
    """Add vlm_summary to quality-passing shots via native video understanding."""
    try:
        client = get_client()
    except Exception as exc:
        log.warning("VLM client init failed: %s — skipping VLM", exc)
        return shots

    passing = [s for s in shots if s["quality_score"] >= min_score]
    if not passing:
        log.info("No shots pass quality threshold for VLM")
        return shots

    log.info("Describing %d shots with VLM (%s)", len(passing), model)

    for shot in passing:
        try:
            shot["vlm_summary"] = _describe_shot_native(videos_dir, shot, model, client)
        except Exception as exc:
            log.warning(
                "VLM failed for %s [%.1f-%.1f]: %s",
                shot["source"], shot["start"], shot["end"], exc,
            )
            shot["vlm_summary"] = ""

    return shots


def _describe_shot_native(
    videos_dir: Path,
    shot: dict,
    model: str,
    client,
) -> str:
    """Send a video clip to VLM's native video understanding API."""
    vf = _find_video(videos_dir, shot["source"])
    if vf is None:
        return ""

    video_data_uri = _extract_clip_base64(vf, shot["start"], shot["end"])

    video_part = build_video_content(video_data_uri, fps=2.0)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    video_part,
                    {
                        "type": "text",
                        "text": (
                            "Describe this video clip in 2-3 sentences. Include: "
                            "scene type (aerial/ground/water/etc), main visual elements, "
                            "aesthetic qualities (lighting, color, composition), "
                            "and motion intensity (1-10)."
                        ),
                    },
                ],
            }
        ],
        max_completion_tokens=16384,
        **completion_kwargs("disabled"),
    )
    return strip_thinking(response.choices[0].message.content)


# -------- Layer 3b: LLM comprehensive scoring --------------------------------


def llm_score(
    shots: list[dict],
    model: str,
    theme: str | None = None,
    min_score: float = 0.5,
) -> list[dict]:
    """Use LLM to score shots and generate highlights.

    Returns list of highlight dicts sorted by score descending.
    """
    try:
        client = get_client()
    except Exception as exc:
        log.warning("VLM client init failed: %s — using quality_score only", exc)
        return _quality_only_highlights(shots, min_score)

    # Only score shots that passed quality pre-screen
    passing = [s for s in shots if s["quality_score"] >= min_score]
    if not passing:
        log.info("No shots pass quality threshold for LLM scoring")
        return []

    log.info("LLM scoring %d shots with %s", len(passing), model)

    # Build shot catalog for LLM
    catalog_lines: list[str] = []
    for i, s in enumerate(passing):
        vlm_desc = s.get("vlm_summary", "")
        line = (
            f"Shot {i}: {s['source']} [{s['start']:.2f}-{s['end']:.2f}] "
            f"quality={s['quality_score']:.2f}"
        )
        if vlm_desc:
            line += f" | VLM: {vlm_desc}"
        catalog_lines.append(line)

    catalog = "\n".join(catalog_lines)

    theme_instruction = ""
    if theme:
        theme_instruction = (
            f"Theme keywords: '{theme}'. "
            "Prioritize shots that match this theme — scenery, mood, or content."
        )

    prompt = f"""You are a highlight detector for aerial drone footage. Score each shot 0-1 based on visual quality and content interest.

{theme_instruction}

Shots:
{catalog}

For each shot, output a JSON array of objects with keys:
- "index": shot index number
- "score": float 0-1 (combined quality + interest)
- "tags": list of 2-4 descriptive tags (e.g. ["mountain", "aerial", "sunset"])
- "reason": one-sentence explanation

Only include shots with score >= 0.5. Sort by score descending.
Output ONLY the JSON array, no other text."""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=16384,
            **completion_kwargs("disabled"),
        )
        text = strip_thinking(response.choices[0].message.content)
        highlights = _parse_llm_response(text, passing)
    except Exception as exc:
        log.error("LLM scoring failed: %s — falling back to quality_score only", exc)
        return _quality_only_highlights(passing, min_score)

    return sorted(highlights, key=lambda h: h["score"], reverse=True)


def _parse_llm_response(text: str, shots: list[dict]) -> list[dict]:
    """Parse LLM JSON response into highlight dicts."""
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (```json and ```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        log.error("Failed to parse LLM response as JSON")
        return _quality_only_highlights(shots, 0.5)

    highlights: list[dict] = []
    for item in items:
        idx = item.get("index")
        if idx is None or idx >= len(shots):
            continue
        shot = shots[idx]
        highlights.append({
            "source": shot["source"],
            "start": shot["start"],
            "end": shot["end"],
            "score": float(item.get("score", shot["quality_score"])),
            "tags": item.get("tags", []),
            "reason": item.get("reason", ""),
            "vlm_summary": shot.get("vlm_summary", ""),
        })

    return highlights


def _quality_only_highlights(
    shots: list[dict], min_score: float
) -> list[dict]:
    """Fallback: generate highlights from quality_score only, no LLM."""
    highlights: list[dict] = []
    for shot in shots:
        if shot["quality_score"] >= min_score:
            highlights.append({
                "source": shot["source"],
                "start": shot["start"],
                "end": shot["end"],
                "score": round(shot["quality_score"], 2),
                "tags": [],
                "reason": "Quality-passing shot (no VLM/LLM description)",
                "vlm_summary": shot.get("vlm_summary", ""),
            })
    return sorted(highlights, key=lambda h: h["score"], reverse=True)


# -------- Main pipeline ------------------------------------------------------


def run_pipeline(
    videos_dir: Path,
    output: Path | None = None,
    theme: str | None = None,
    model: str | None = None,
    min_score: float = 0.5,
    blur_threshold: int = 100,
    no_vlm: bool = False,
) -> Path:
    """Run the full highlight detection pipeline. Returns output path."""
    if output is None:
        output = videos_dir / "edit" / "highlights.json"

    model = model or get_model()

    log.info("=== Highlight Detection Pipeline ===")
    log.info("Videos dir: %s", videos_dir)
    log.info("Output: %s", output)
    log.info("Model: %s (provider: %s)", model, get_provider_config().provider)
    if theme:
        log.info("Theme: %s", theme)

    # Layer 1: Shot boundary detection
    log.info("--- Layer 1: Shot boundary detection ---")
    shots = detect_shots(videos_dir)
    if not shots:
        log.error("No shots detected — aborting")
        sys.exit(1)
    log.info("Detected %d shots", len(shots))

    # Layer 2: OpenCV quality pre-screen
    log.info("--- Layer 2: Quality pre-screen ---")
    shots = quality_prescreen(videos_dir, shots, blur_threshold)
    passing = [s for s in shots if s["quality_score"] >= min_score]
    log.info(
        "%d / %d shots pass quality threshold (%.1f)",
        len(passing), len(shots), min_score,
    )

    # Layer 3a: VLM scene description
    if not no_vlm:
        log.info("--- Layer 3a: VLM scene description ---")
        shots = vlm_describe(videos_dir, shots, model, min_score)
    else:
        log.info("--- VLM skipped (--no-vlm) ---")

    # Layer 3b: LLM comprehensive scoring
    log.info("--- Layer 3b: LLM scoring ---")
    highlights = llm_score(shots, model, theme, min_score)

    if not highlights:
        log.warning("No highlights found")

    # Write output
    result = {"highlights": highlights}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %d highlights to %s", len(highlights), output)

    return output


# -------- CLI ----------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Highlight detection for silent aerial footage",
    )
    parser.add_argument(
        "videos_dir",
        type=Path,
        help="Directory containing source video files (MP4/MOV)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path for highlights.json (default: <videos_dir>/edit/highlights.json)",
    )
    parser.add_argument(
        "--theme", type=str, default=None,
        help="Theme keywords for LLM scoring (e.g. '旅行Vlog-重庆')",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Model for VLM+LLM (default: from VLM_PROVIDER config)",
    )
    parser.add_argument(
        "--min-score", type=float, default=0.5,
        help="Minimum quality_score to pass OpenCV pre-screen (default: 0.5)",
    )
    parser.add_argument(
        "--blur-threshold", type=int, default=100,
        help="Laplacian variance threshold for blur detection (default: 100)",
    )
    parser.add_argument(
        "--no-vlm", action="store_true",
        help="Skip VLM, use only OpenCV quality_score (degradation mode)",
    )

    args = parser.parse_args()

    if not args.videos_dir.is_dir():
        log.error("Not a directory: %s", args.videos_dir)
        sys.exit(1)

    out = run_pipeline(
        videos_dir=args.videos_dir,
        output=args.output,
        theme=args.theme,
        model=args.model,
        min_score=args.min_score,
        blur_threshold=args.blur_threshold,
        no_vlm=args.no_vlm,
    )
    print(f"\nDone: {out}")


if __name__ == "__main__":
    main()
