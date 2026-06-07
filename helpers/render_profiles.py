"""Render one EDL to multiple export profiles."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from export_profiles import ExportProfile, get_profile


def parse_profiles(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_profiles(edl: dict, cli_profiles: str | None) -> list[str]:
    selected = parse_profiles(cli_profiles)
    if not selected:
        selected = list((edl.get("export") or {}).get("profiles") or [])
    if not selected:
        default_profile = (edl.get("export") or {}).get("default_profile") or "legacy_1080p24_landscape"
        selected = [default_profile]

    for name in selected:
        get_profile(name)
    return selected


def resolve_audio_policy(edl: dict, cli_policy: str | None) -> str:
    policy = cli_policy or (edl.get("export") or {}).get("audio_policy")
    if policy:
        return policy
    bgm_config = edl.get("bgm") or {}
    return "duck" if bgm_config.get("duck_voiceover", True) else "mix"


def expected_duration(edl: dict) -> float | None:
    if edl.get("total_duration_s") is not None:
        return float(edl["total_duration_s"])
    ranges = edl.get("ranges") or []
    if not ranges:
        return None
    return sum(float(r["end"]) - float(r["start"]) for r in ranges)


def probe_output(output_path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=index,codec_type,width,height,r_frame_rate,codec_name",
            "-of", "json",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def fps_value(raw: str) -> float:
    try:
        return float(Fraction(raw))
    except (ValueError, ZeroDivisionError):
        return 0.0


def detect_black_frames(output_path: Path) -> list[dict[str, float]]:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(output_path),
            "-vf", "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    intervals: list[dict[str, float]] = []
    for match in re.finditer(r"black_start:(?P<start>[0-9.]+) black_end:(?P<end>[0-9.]+) black_duration:(?P<duration>[0-9.]+)", proc.stderr):
        intervals.append({
            "start": float(match.group("start")),
            "end": float(match.group("end")),
            "duration": float(match.group("duration")),
        })
    return intervals


def detect_long_silence(output_path: Path, threshold_db: float = -45.0, min_duration: float = 2.0) -> list[dict[str, float]]:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(output_path),
            "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}", "-vn", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    starts: list[float] = []
    intervals: list[dict[str, float]] = []
    for line in proc.stderr.splitlines():
        start_match = re.search(r"silence_start: (?P<start>[0-9.]+)", line)
        if start_match:
            starts.append(float(start_match.group("start")))
            continue
        end_match = re.search(r"silence_end: (?P<end>[0-9.]+) \| silence_duration: (?P<duration>[0-9.]+)", line)
        if end_match:
            end = float(end_match.group("end"))
            duration = float(end_match.group("duration"))
            start = starts.pop(0) if starts else max(0.0, end - duration)
            intervals.append({"start": start, "end": end, "duration": duration})
    return intervals


def validate_output(profile: ExportProfile, output_path: Path, edl: dict, audio_policy: str | None) -> dict:
    checks: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []

    def add_check(name: str, ok: bool, **data) -> None:
        checks.append({"name": name, "ok": ok, **data})
        if not ok:
            errors.append(name)

    exists = output_path.exists() and output_path.stat().st_size > 0
    add_check("file_exists", exists, bytes=output_path.stat().st_size if output_path.exists() else 0)
    if not exists:
        return {"status": "failed", "checks": checks, "errors": errors, "warnings": warnings}

    probe = probe_output(output_path)
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    duration = float((probe.get("format") or {}).get("duration") or 0.0)

    add_check(
        "resolution",
        bool(video) and int(video.get("width", 0)) == profile.width and int(video.get("height", 0)) == profile.height,
        expected={"width": profile.width, "height": profile.height},
        actual={"width": int(video.get("width", 0)) if video else 0, "height": int(video.get("height", 0)) if video else 0},
    )
    actual_fps = fps_value(video.get("r_frame_rate", "0/1") if video else "0/1")
    add_check("fps", abs(actual_fps - profile.fps) <= 0.5, expected=profile.fps, actual=actual_fps)

    expected = expected_duration(edl)
    if expected is not None:
        add_check("duration", abs(duration - expected) <= 1.0, expected=expected, actual=duration)

    expects_audio = audio_policy != "silent"
    add_check("audio_stream", bool(audio_streams) == expects_audio, expected=expects_audio, actual=bool(audio_streams))

    black_intervals = detect_black_frames(output_path)
    checks.append({"name": "black_frames", "ok": not black_intervals, "intervals": black_intervals})
    if black_intervals:
        warnings.append("black_frames")

    silence_intervals: list[dict[str, float]] = []
    if expects_audio and audio_streams:
        silence_intervals = detect_long_silence(output_path)
        checks.append({"name": "long_silence", "ok": not silence_intervals, "intervals": silence_intervals})
        if silence_intervals:
            warnings.append("long_silence")

    status = "failed" if errors else ("warning" if warnings else "ok")
    return {
        "status": status,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "probe": {"duration": duration, "video": video, "audio_streams": audio_streams},
    }


def report_for(profile_name: str, output_path: Path, edl: dict, audio_policy: str | None, render_error: str | None = None) -> dict:
    profile = get_profile(profile_name)
    report = {
        "profile": profile.name,
        "output": str(output_path),
        "expected": {
            "resolution": {"width": profile.width, "height": profile.height},
            "fps": profile.fps,
            "codec": profile.codec,
            "orientation": profile.orientation,
            "platform": profile.platform,
        },
    }
    if render_error:
        report["status"] = "failed"
        report["render_error"] = render_error
        return report

    validation = validate_output(profile, output_path, edl, audio_policy)
    report["status"] = validation["status"]
    report["validation"] = validation
    return report


def render_profile(
    edl_path: Path,
    output_path: Path,
    profile_name: str,
    extra_args: list[str],
    edl: dict,
    audio_policy: str | None,
) -> dict:
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("render.py")),
        str(edl_path),
        "-o", str(output_path),
        "--profile", profile_name,
        *extra_args,
    ]
    try:
        subprocess.run(cmd, check=True)
        return report_for(profile_name, output_path, edl, audio_policy)
    except subprocess.CalledProcessError as exc:
        return report_for(profile_name, output_path, edl, audio_policy, str(exc))


def main() -> None:
    ap = argparse.ArgumentParser(description="Render multiple export profiles from one EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("--profiles", default=None, help="Comma-separated profile names. Overrides edl.export.profiles.")
    ap.add_argument("--preview", action="store_true", help="Pass preview mode to render.py")
    ap.add_argument("--draft", action="store_true", help="Pass draft mode to render.py")
    ap.add_argument("--no-subtitles", action="store_true", help="Pass --no-subtitles to render.py")
    ap.add_argument("--no-loudnorm", action="store_true", help="Pass --no-loudnorm to render.py")
    ap.add_argument("--audio-policy", choices=["bgm_only", "duck", "mix", "source_only", "silent"], default=None)
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    try:
        profile_names = resolve_profiles(edl, args.profiles)
        audio_policy = resolve_audio_policy(edl, args.audio_policy)
    except ValueError as exc:
        sys.exit(str(exc))

    exports_dir = edl_path.parent / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    extra_args: list[str] = []
    if args.preview:
        extra_args.append("--preview")
    if args.draft:
        extra_args.append("--draft")
    if args.no_subtitles:
        extra_args.append("--no-subtitles")
    if args.no_loudnorm:
        extra_args.append("--no-loudnorm")
    if args.audio_policy:
        extra_args += ["--audio-policy", args.audio_policy]

    reports: list[dict] = []
    for profile_name in profile_names:
        output_path = exports_dir / f"{profile_name}.mp4"
        print(f"rendering {profile_name} → {output_path}")
        report = render_profile(edl_path, output_path, profile_name, extra_args, edl, audio_policy)
        report_path = exports_dir / f"{profile_name}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        reports.append(report)

    print("\nprofile\tstatus\twarnings\terrors\toutput")
    for report in reports:
        validation = report.get("validation") or {}
        warnings = ",".join(validation.get("warnings") or [])
        errors = ",".join(validation.get("errors") or [])
        print(f"{report['profile']}\t{report['status']}\t{warnings}\t{errors}\t{report['output']}")

    failures = [report for report in reports if report["status"] == "failed"]
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
