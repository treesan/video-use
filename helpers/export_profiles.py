"""Export profile registry for platform-specific renders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FitMode = Literal["contain", "cover_center", "cover_left", "cover_right"]


@dataclass(frozen=True)
class ExportProfile:
    name: str
    width: int
    height: int
    fps: int
    codec: str
    crf: int | None
    video_bitrate: str | None
    audio_bitrate: str
    pixel_format: str
    preset: str
    fit: FitMode
    platform: str
    orientation: Literal["landscape", "portrait"]
    loudnorm_i: float = -14.0
    loudnorm_tp: float = -1.0
    loudnorm_lra: float = 11.0
    audio_codec: str = "aac"

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def ffmpeg_video_args(self) -> list[str]:
        args = ["-c:v", self.codec, "-preset", self.preset]
        if self.crf is not None:
            args += ["-crf", str(self.crf)]
        elif self.video_bitrate:
            args += ["-b:v", self.video_bitrate]
        args += ["-pix_fmt", self.pixel_format, "-r", str(self.fps)]
        return args

    def ffmpeg_audio_args(self) -> list[str]:
        return ["-c:a", self.audio_codec, "-b:a", self.audio_bitrate, "-ar", "48000", "-ac", "2"]

    def fit_filter(self, crop_center: dict[str, float] | None = None) -> str:
        if self.fit == "contain":
            return (
                f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
                f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2"
            )
        if crop_center:
            x = min(1.0, max(0.0, float(crop_center.get("x", 0.5))))
            y = min(1.0, max(0.0, float(crop_center.get("y", 0.5))))
            crop_x = f"(iw-ow)*{x:.4f}"
            crop_y = f"(ih-oh)*{y:.4f}"
        elif self.fit == "cover_left":
            crop_x = "0"
            crop_y = "(ih-oh)/2"
        elif self.fit == "cover_right":
            crop_x = "iw-ow"
            crop_y = "(ih-oh)/2"
        else:
            crop_x = "(iw-ow)/2"
            crop_y = "(ih-oh)/2"
        return (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
            f"crop={self.width}:{self.height}:{crop_x}:{crop_y}"
        )


BUILTIN_PROFILES: dict[str, ExportProfile] = {
    "legacy_1080p24_landscape": ExportProfile(
        name="legacy_1080p24_landscape",
        width=1920,
        height=1080,
        fps=24,
        codec="libx264",
        crf=20,
        video_bitrate=None,
        audio_bitrate="192k",
        pixel_format="yuv420p",
        preset="fast",
        fit="contain",
        platform="video-use legacy default",
        orientation="landscape",
    ),
    "bilibili_4k60_landscape": ExportProfile(
        name="bilibili_4k60_landscape",
        width=3840,
        height=2160,
        fps=60,
        codec="libx265",
        crf=None,
        video_bitrate="50M",
        audio_bitrate="320k",
        pixel_format="yuv420p10le",
        preset="slow",
        fit="contain",
        platform="Bilibili 4K",
        orientation="landscape",
    ),
    "bilibili_1080p60_landscape": ExportProfile(
        name="bilibili_1080p60_landscape",
        width=1920,
        height=1080,
        fps=60,
        codec="libx264",
        crf=None,
        video_bitrate="20M",
        audio_bitrate="320k",
        pixel_format="yuv420p",
        preset="medium",
        fit="contain",
        platform="Bilibili horizontal",
        orientation="landscape",
    ),
    "bilibili_1080p30_landscape": ExportProfile(
        name="bilibili_1080p30_landscape",
        width=1920,
        height=1080,
        fps=30,
        codec="libx264",
        crf=None,
        video_bitrate="18M",
        audio_bitrate="320k",
        pixel_format="yuv420p",
        preset="medium",
        fit="contain",
        platform="Bilibili horizontal",
        orientation="landscape",
    ),
    "douyin_1080p60_portrait": ExportProfile(
        name="douyin_1080p60_portrait",
        width=1080,
        height=1920,
        fps=60,
        codec="libx264",
        crf=None,
        video_bitrate="12M",
        audio_bitrate="256k",
        pixel_format="yuv420p",
        preset="medium",
        fit="cover_center",
        platform="Douyin vertical",
        orientation="portrait",
    ),
    "douyin_1080p30_portrait": ExportProfile(
        name="douyin_1080p30_portrait",
        width=1080,
        height=1920,
        fps=30,
        codec="libx264",
        crf=None,
        video_bitrate="10M",
        audio_bitrate="256k",
        pixel_format="yuv420p",
        preset="medium",
        fit="cover_center",
        platform="Douyin vertical",
        orientation="portrait",
    ),
    "xiaohongshu_1080p30_portrait": ExportProfile(
        name="xiaohongshu_1080p30_portrait",
        width=1080,
        height=1920,
        fps=30,
        codec="libx264",
        crf=None,
        video_bitrate="10M",
        audio_bitrate="256k",
        pixel_format="yuv420p",
        preset="medium",
        fit="cover_center",
        platform="Xiaohongshu vertical",
        orientation="portrait",
    ),
    "xiaohongshu_1080x1440_30": ExportProfile(
        name="xiaohongshu_1080x1440_30",
        width=1080,
        height=1440,
        fps=30,
        codec="libx264",
        crf=None,
        video_bitrate="10M",
        audio_bitrate="256k",
        pixel_format="yuv420p",
        preset="medium",
        fit="cover_center",
        platform="Xiaohongshu 3:4",
        orientation="portrait",
    ),
    "landscape_1080p120": ExportProfile(
        name="landscape_1080p120",
        width=1920,
        height=1080,
        fps=120,
        codec="libx264",
        crf=None,
        video_bitrate="25M",
        audio_bitrate="320k",
        pixel_format="yuv420p",
        preset="medium",
        fit="contain",
        platform="High-frame-rate horizontal",
        orientation="landscape",
    ),
    "portrait_1080p120": ExportProfile(
        name="portrait_1080p120",
        width=1080,
        height=1920,
        fps=120,
        codec="libx264",
        crf=None,
        video_bitrate="15M",
        audio_bitrate="256k",
        pixel_format="yuv420p",
        preset="medium",
        fit="cover_center",
        platform="High-frame-rate vertical",
        orientation="portrait",
    ),
}


def list_profiles() -> list[ExportProfile]:
    return list(BUILTIN_PROFILES.values())


def valid_profile_names() -> list[str]:
    return sorted(BUILTIN_PROFILES)


def get_profile(name: str) -> ExportProfile:
    try:
        return BUILTIN_PROFILES[name]
    except KeyError as exc:
        valid = ", ".join(valid_profile_names())
        raise ValueError(f"unknown export profile '{name}'. Valid profiles: {valid}") from exc


def profile_summary_rows() -> list[dict[str, str | int]]:
    return [
        {
            "name": profile.name,
            "resolution": profile.resolution,
            "fps": profile.fps,
            "codec": profile.codec,
            "orientation": profile.orientation,
            "platform": profile.platform,
        }
        for profile in list_profiles()
    ]
