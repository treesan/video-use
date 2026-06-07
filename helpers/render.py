"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
    from export_profiles import ExportProfile, get_profile, profile_summary_rows, valid_profile_names
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}

    class ExportProfile:  # type: ignore
        pass

    def get_profile(name: str):  # type: ignore
        raise ValueError(f"export profiles unavailable; cannot resolve '{name}'")

    def valid_profile_names() -> list[str]:  # type: ignore
        return []

    def profile_summary_rows() -> list[dict[str, str | int]]:  # type: ignore
        return []


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

# zscale-based HDR→SDR chain. Requires ffmpeg built with --enable-libzimg
# (Homebrew: `brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-zimg`).
# Pipeline:
#   1. zscale t=linear:        move HLG/PQ transfer into linear light
#   2. format gbrpf32le:       feed 32-bit float to tonemap
#   3. zscale p=bt709:         set primaries to BT.709 (target gamut)
#   4. tonemap=hable:desat=0:  map HDR to SDR (Hable is robust for HLG)
#   5. zscale t=bt709 ... r=tv:force BT.709 transfer with TV range
#   6. format yuv420p:         8-bit for libx264
TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def is_portrait_source(video: Path) -> bool:
    """Return True if the video's height > width (portrait / vertical)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        )
        w, h = map(int, out.stdout.strip().split(","))
        return h > w
    except Exception:
        return False


def video_encode_args(profile: ExportProfile, preview: bool = False, draft: bool = False) -> list[str]:
    if draft:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p", "-r", str(profile.fps)]
    if preview:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(profile.fps)]
    return profile.ffmpeg_video_args()


def audio_encode_args(profile: ExportProfile) -> list[str]:
    return profile.ffmpeg_audio_args()


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    profile: ExportProfile,
    crop_center: dict[str, float] | None = None,
    preview: bool = False,
    draft: bool = False,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf_parts: list[str] = []
    if is_hdr_source(source) and TONEMAP_CHAIN:
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(profile.fit_filter(crop_center))
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(p for p in vf_parts if p)

    fade_out_start = max(0.0, duration - 0.03)
    af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-af", af,
        *video_encode_args(profile, preview=preview, draft=draft),
        *audio_encode_args(profile),
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    profile: ExportProfile,
    preview: bool,
    draft: bool = False,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(src_path, start, duration, seg_filter, out_path, profile, r.get("crop_center"), preview=preview, draft=draft)
        seg_paths.append(out_path)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - 2-word chunks (break on any punctuation in between)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # Group into 2-word chunks, break on punctuation
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            # Break if the current text ends in punctuation or we hit 2 words
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
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


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    profile: ExportProfile,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            *audio_encode_args(profile),
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, profile, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

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
        *audio_encode_args(profile),
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    profile: ExportProfile,
    preview: bool = False,
    draft: bool = False,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        *video_encode_args(profile, preview=preview, draft=draft),
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def resolve_audio_policy(edl: dict, cli_policy: str | None) -> str:
    policy = cli_policy or (edl.get("export") or {}).get("audio_policy")
    if policy is None:
        bgm_config = edl.get("bgm") or {}
        return "duck" if bgm_config.get("duck_voiceover", True) else "mix"
    valid = {"bgm_only", "duck", "mix", "source_only", "silent"}
    if policy not in valid:
        raise ValueError(f"unknown audio policy '{policy}'. Valid policies: {', '.join(sorted(valid))}")
    return policy


def strip_audio(video_path: Path, out_path: Path) -> None:
    run(["ffmpeg", "-y", "-i", str(video_path), "-map", "0:v:0", "-c:v", "copy", "-an", str(out_path)], quiet=True)


def mix_bgm(
    video_path: Path,
    bgm_config: dict,
    out_path: Path,
    edit_dir: Path,
    profile: ExportProfile,
    audio_policy: str,
) -> None:
    """Mix BGM into the video's audio track.

    If the video has no audio stream (e.g. silent drone footage), BGM becomes
    the sole audio track. Otherwise, original audio and BGM are mixed with
    optional voiceover ducking (sidechaincompress).

    BGM config schema (from edl.json "bgm" field):
        file: str           — path to BGM audio file
        start_offset: float — seconds to skip in BGM (default 0)
        volume: float       — BGM volume 0.0-1.0 (default 0.3)
        duck_voiceover: bool— lower BGM when original audio is present (default True)
        fade_in: float      — BGM fade-in duration in seconds (default 2.0)
        fade_out: float     — BGM fade-out duration in seconds (default 3.0)
    """
    bgm_file = resolve_path(bgm_config["file"], edit_dir)
    if not bgm_file.exists():
        print(f"  warning: BGM file not found: {bgm_file}, skipping BGM mix")
        run(["ffmpeg", "-y", "-i", str(video_path), "-c", "copy", str(out_path)], quiet=True)
        return

    volume = bgm_config.get("volume", 0.3)
    start_offset = bgm_config.get("start_offset", 0.0)
    fade_in = bgm_config.get("fade_in", 2.0)
    fade_out = bgm_config.get("fade_out", 3.0)
    duck = bgm_config.get("duck_voiceover", True)

    # Get video duration
    dur_out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    video_dur = float(dur_out.stdout.strip())

    # Check if video has audio
    has_audio_out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    has_audio = bool(has_audio_out.stdout.strip())

    # Build BGM audio filter chain
    # Clamp fades so they never overlap
    actual_fade_in = min(fade_in, video_dur / 2)
    actual_fade_out = min(fade_out, video_dur / 2)
    bgm_af_parts: list[str] = []
    if start_offset > 0:
        bgm_af_parts.append(f"atrim=start={start_offset}")
    bgm_af_parts.append(f"afade=t=in:st=0:d={actual_fade_in}")
    fade_out_start = max(0.0, video_dur - actual_fade_out)
    bgm_af_parts.append(f"afade=t=out:st={fade_out_start:.3f}:d={actual_fade_out}")
    bgm_af_parts.append(f"volume={volume}")
    bgm_af = ",".join(bgm_af_parts)

    if audio_policy == "bgm_only" or not has_audio:
        filter_complex = f"[1:a]{bgm_af}[bgm]"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-stream_loop", "-1", "-i", str(bgm_file),
            "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[bgm]",
            "-c:v", "copy",
            *audio_encode_args(profile),
            "-t", f"{video_dur:.3f}",
            "-movflags", "+faststart",
            str(out_path),
        ]
    elif audio_policy == "duck":
        filter_complex = (
            f"[0:a]volume=1.0[orig];"
            f"[1:a]{bgm_af}[bgm];"
            f"[bgm][orig]sidechaincompress=threshold=0.1:ratio=10"
            f":attack=0.01:release=0.5:makeup=1[ducked];"
            f"[orig][ducked]amix=inputs=2:duration=first:dropout_transition=3[mixed]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-stream_loop", "-1", "-i", str(bgm_file),
            "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[mixed]",
            "-c:v", "copy",
            *audio_encode_args(profile),
            "-t", f"{video_dur:.3f}",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        filter_complex = (
            f"[0:a]volume=1.0[orig];"
            f"[1:a]{bgm_af}[bgm];"
            f"[orig][bgm]amix=inputs=2:duration=first:dropout_transition=3[mixed]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-stream_loop", "-1", "-i", str(bgm_file),
            "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[mixed]",
            "-c:v", "copy",
            *audio_encode_args(profile),
            "-t", f"{video_dur:.3f}",
            "-movflags", "+faststart",
            str(out_path),
        ]

    print(f"  mixing BGM: policy={audio_policy}, volume={volume}, fade_in={fade_in}s, fade_out={fade_out}s")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--profile",
        default=None,
        help="Export profile name. Default: EDL export.default_profile or legacy_1080p24_landscape.",
    )
    ap.add_argument(
        "--audio-policy",
        choices=["bgm_only", "duck", "mix", "source_only", "silent"],
        default=None,
        help="Override EDL export.audio_policy.",
    )
    ap.add_argument(
        "--list-profiles",
        action="store_true",
        help="List available export profiles and exit.",
    )
    args = ap.parse_args()

    if args.list_profiles:
        print("name\tresolution\tfps\tcodec\torientation\tplatform")
        for row in profile_summary_rows():
            print(
                f"{row['name']}\t{row['resolution']}\t{row['fps']}\t"
                f"{row['codec']}\t{row['orientation']}\t{row['platform']}"
            )
        return

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()
    export_config = edl.get("export") or {}
    profile_name = args.profile or export_config.get("default_profile") or "legacy_1080p24_landscape"
    try:
        profile = get_profile(profile_name)
        audio_policy = resolve_audio_policy(edl, args.audio_policy)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"export profile: {profile.name} ({profile.resolution}, {profile.fps}fps, {profile.codec})")
    print(f"audio policy: {audio_policy}")

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, profile, preview=args.preview, draft=args.draft
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + subtitles LAST)
    overlays = edl.get("overlays") or []
    bgm_config = edl.get("bgm")

    # Use a consistent temp path for the pre-loudnorm intermediate
    tmp_composite = out_path.with_suffix(".prenorm.mp4")

    if audio_policy == "silent":
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir, profile, preview=args.preview, draft=args.draft)
        strip_audio(tmp_composite, out_path)
        tmp_composite.unlink(missing_ok=True)
        loudnorm_src = None
    elif bgm_config and audio_policy != "source_only":
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir, profile, preview=args.preview, draft=args.draft)
        tmp_mixed = edit_dir / "_bgm_mixed.mp4"
        print("mixing BGM …")
        mix_bgm(tmp_composite, bgm_config, tmp_mixed, edit_dir, profile, audio_policy)
        tmp_composite.unlink(missing_ok=True)
        loudnorm_src = tmp_mixed
    else:
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir, profile, preview=args.preview, draft=args.draft)
        loudnorm_src = tmp_composite

    # 5. Loudness normalization
    if loudnorm_src is not None:
        if args.no_loudnorm:
            run(["ffmpeg", "-y", "-i", str(loudnorm_src), "-c", "copy", str(out_path)], quiet=True)
            loudnorm_src.unlink(missing_ok=True)
        else:
            print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
            apply_loudnorm_two_pass(loudnorm_src, out_path, profile, preview=args.draft)
            loudnorm_src.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
