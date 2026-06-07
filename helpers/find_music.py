"""Search and download background music from Pixabay or generate via MiniMax.

Two providers:
  1. Pixabay — free royalty-free music search and download (primary)
  2. MiniMax — AI music generation via mmx-cli (fallback)

Auto mode tries Pixabay first, falls back to MiniMax on failure.

Usage:
    python helpers/find_music.py --style "upbeat travel vlog"
    python helpers/find_music.py --style "chill lo-fi" --min-duration 60 --max-duration 120
    python helpers/find_music.py --style "epic orchestral" --provider minimax
    python helpers/find_music.py --style "corporate" --output /path/to/edit/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# --------------- Env helpers ---------------


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


# --------------- Pixabay provider ---------------

PIXABAY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

PIXABAY_BROWSER_HEADERS = {
    "User-Agent": PIXABAY_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _pixabay_build_opener() -> urllib.request.OpenerDirector:
    import http.cookiejar

    cookie_jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))


def _urllib_get_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: dict[str, str],
    timeout: int = 30,
) -> str:
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _pixabay_api_search(
    api_key: str, style: str, min_duration: int, max_duration: int
) -> list[dict]:
    """Search Pixabay via official API. Returns list of hit dicts."""
    url = "https://pixabay.com/api/"
    params = {
        "key": api_key,
        "q": style,
        "category": "music",
        "min_duration": min_duration,
        "max_duration": max_duration,
    }
    log.info("Pixabay API search: q=%r  duration=%d-%d", style, min_duration, max_duration)
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits", [])
    log.info("Pixabay API returned %d hits", len(hits))
    return hits


def _pixabay_scrape_search(
    style: str, min_duration: int, max_duration: int
) -> list[dict]:
    """Scrape Pixabay music search page for results without an API key."""
    slug = re.sub(r"\s+", "-", style.strip().lower())
    query = urllib.parse.quote(slug, safe="-")
    url = f"https://pixabay.com/music/search/{query}/"
    log.info("Pixabay scraping: %s", url)

    opener = _pixabay_build_opener()
    html = _urllib_get_text(opener, url, PIXABAY_BROWSER_HEADERS)

    if "cf-challenge" in html.lower() or "challenge-platform" in html.lower():
        log.warning("Pixabay scraping blocked by Cloudflare challenge")
        return []

    results = _parse_bootstrap_audio(opener, html, url, min_duration, max_duration)
    if not results:
        results = _parse_next_data_from_html(html, min_duration, max_duration)
    if not results:
        results = _parse_embedded_audio(html, min_duration, max_duration)

    log.info("Pixabay scraping found %d results", len(results))
    return results


def _parse_bootstrap_audio(
    opener: urllib.request.OpenerDirector,
    html: str,
    referer: str,
    min_duration: int,
    max_duration: int,
) -> list[dict]:
    """Extract tracks from Pixabay's bootstrap JSON endpoint."""
    match = re.search(r'window\.__BOOTSTRAP_URL__\s*=\s*["\']([^"\']+)["\']', html)
    if not match:
        return []

    bootstrap_url = urllib.parse.urljoin("https://pixabay.com", match.group(1))
    headers = {
        "User-Agent": PIXABAY_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    try:
        data = json.loads(_urllib_get_text(opener, bootstrap_url, headers, timeout=15))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to fetch Pixabay bootstrap JSON: %s", exc)
        return []

    results = []
    for item in data.get("page", {}).get("results", []):
        sources = item.get("sources", {}) or {}
        audio_url = sources.get("src")
        if not audio_url:
            continue
        duration = item.get("duration")
        if duration is not None and not _duration_in_range(duration, min_duration, max_duration):
            continue
        user = item.get("user", {}) or {}
        results.append({
            "title": item.get("name") or sources.get("filename", "Pixabay Track"),
            "duration": duration or 0,
            "artist": user.get("username", "Pixabay User"),
            "download_url": audio_url,
        })
    return results


def _parse_next_data_from_html(
    html: str, min_duration: int, max_duration: int
) -> list[dict]:
    next_data_match = re.search(
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.+?)</script>',
        html,
        re.DOTALL,
    )
    if not next_data_match:
        return []
    try:
        return _parse_next_data(json.loads(next_data_match.group(1)), min_duration, max_duration)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("Failed to parse __NEXT_DATA__: %s", exc)
        return []


def _duration_in_range(duration: float | int | str, min_duration: int, max_duration: int) -> bool:
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return False
    return min_duration <= value <= max_duration


def _parse_next_data(
    data: dict, min_duration: int, max_duration: int
) -> list[dict]:
    """Extract audio results from __NEXT_DATA__ JSON."""
    results = []
    try:
        # Navigate common Pixabay Next.js data structures
        props = data.get("props", {}).get("pageProps", {})
        # The exact key varies; try common patterns
        for key in ("hits", "results", "media", "items"):
            hits = props.get(key, [])
            if hits:
                break
        else:
            hits = []

        for hit in hits:
            if hit.get("type") not in ("film", "music", "audio", None):
                continue
            duration = hit.get("duration") or hit.get("length", 0)
            if isinstance(duration, str):
                try:
                    duration = float(duration)
                except ValueError:
                    continue
            if not duration or duration < min_duration or duration > max_duration:
                continue
            download_url = (
                hit.get("videoUrl")
                or hit.get("audioUrl")
                or hit.get("downloadUrl")
                or hit.get("urls", {}).get("download", "")
            )
            if not download_url:
                continue
            results.append({
                "title": hit.get("title", hit.get("name", "Unknown")),
                "duration": duration,
                "artist": hit.get("user", hit.get("artist", "Pixabay User")),
                "download_url": download_url,
            })
    except Exception as exc:
        log.warning("Error parsing __NEXT_DATA__ results: %s", exc)
    return results


def _parse_embedded_audio(
    html: str, min_duration: int, max_duration: int
) -> list[dict]:
    """Fallback: scan HTML for audio URLs and metadata."""
    results = []
    # Look for CDN audio URLs in the page source
    audio_urls = re.findall(
        r'https?://cdn\.pixabay\.com/audio[^\s"\'<>]+\.mp3[^\s"\'<>]*',
        html,
    )
    seen = set()
    for url in audio_urls:
        # Clean URL — strip trailing query artifacts from regex
        url = url.split('"')[0].split("'")[0].rstrip("\\")
        if url in seen:
            continue
        seen.add(url)
        results.append({
            "title": "Pixabay Track",
            "duration": 0,  # unknown, accept anyway
            "artist": "Pixabay User",
            "download_url": url,
        })
    return results


def _pixabay_download(
    session: requests.Session, url: str, output_path: Path
) -> bool:
    """Download an MP3 from Pixabay CDN. Returns True on success."""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = urllib.parse.urljoin("https://pixabay.com", url)

    log.info("Downloading: %s", url[:80] + "..." if len(url) > 80 else url)
    try:
        headers = {
            "User-Agent": PIXABAY_USER_AGENT,
            "Accept": "audio/mpeg,audio/*,*/*;q=0.8",
            "Referer": "https://pixabay.com/music/",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "")
            if "audio" not in content_type and "octet-stream" not in content_type:
                log.warning("Unexpected content type: %s", content_type)
            output_path.write_bytes(response.read())
        size_kb = output_path.stat().st_size / 1024
        log.info("Downloaded %.0f KB to %s", size_kb, output_path)
        return size_kb > 10  # sanity check
    except OSError as exc:
        log.warning("Download failed: %s", exc)
        return False


def find_pixabay(
    style: str,
    min_duration: int,
    max_duration: int,
    output_dir: Path,
) -> dict | None:
    """Try Pixabay (API then scraping). Returns metadata dict or None."""
    api_key = _load_env_value("PIXABAY_API_KEY")

    # --- Try API first if key available ---
    if api_key:
        try:
            hits = _pixabay_api_search(api_key, style, min_duration, max_duration)
            if hits:
                session = requests.Session()
                for hit in hits:
                    # Pixabay API hits use "pageURL" for preview or download
                    download_url = hit.get("videoURL") or hit.get("audioURL", "")
                    if not download_url:
                        # Some hits provide a preview; try that
                        download_url = hit.get("previewURL", "")
                    if not download_url:
                        continue
                    out_file = output_dir / "bgm.mp3"
                    if _pixabay_download(session, download_url, out_file):
                        return {
                            "title": hit.get("tags", "Unknown")[:60],
                            "artist": hit.get("user", "Pixabay User"),
                            "duration": hit.get("duration", 0),
                            "source": "pixabay",
                            "license": "Pixabay License",
                            "file": "bgm.mp3",
                        }
                log.warning("No downloadable track from Pixabay API hits")
            else:
                log.warning("Pixabay API returned no hits")
        except requests.RequestException as exc:
            log.warning("Pixabay API error: %s", exc)
    else:
        log.info("No PIXABAY_API_KEY set, skipping API search")

    # --- Try scraping ---
    try:
        results = _pixabay_scrape_search(style, min_duration, max_duration)
        if results:
            session = requests.Session()
            session.headers.update(PIXABAY_BROWSER_HEADERS)
            for result in results:
                url = result["download_url"]
                out_file = output_dir / "bgm.mp3"
                if _pixabay_download(session, url, out_file):
                    return {
                        "title": result.get("title", "Unknown"),
                        "artist": result.get("artist", "Pixabay User"),
                        "duration": result.get("duration", 0),
                        "source": "pixabay",
                        "license": "Pixabay License",
                        "file": "bgm.mp3",
                    }
            log.warning("Scraping found results but all downloads failed")
        else:
            log.warning("Pixabay scraping returned no results")
    except requests.RequestException as exc:
        log.warning("Pixabay scraping error: %s", exc)

    return None


# --------------- MiniMax provider ---------------


def _extract_bpm(style: str) -> int:
    """Extract BPM from style string if mentioned, else return 120."""
    match = re.search(r'(\d{2,3})\s*bpm', style, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 120


def find_minimax(
    style: str,
    min_duration: int,
    max_duration: int,
    output_dir: Path,
) -> dict | None:
    """Generate BGM via MiniMax mmx-cli. Returns metadata dict or None."""
    mmx_path = shutil.which("mmx")
    if not mmx_path:
        log.warning("mmx-cli not found on PATH")
        return None

    bpm = _extract_bpm(style)
    # Strip explicit BPM from prompt to avoid redundancy
    prompt = re.sub(r'\d{2,3}\s*bpm', '', style, flags=re.IGNORECASE).strip()
    if not prompt:
        prompt = style

    with tempfile.TemporaryDirectory(prefix="find_music_") as tmpdir:
        out_path = Path(tmpdir) / "output.mp3"
        cmd = [
            mmx_path,
            "music", "generate",
            "--instrumental",
            "--prompt", prompt,
            "--bpm", str(bpm),
            "--use-case", "background music for video",
            "--out", str(out_path),
        ]
        log.info("MiniMax generating: prompt=%r  bpm=%d", prompt, bpm)
        log.info("  cmd: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min max for generation
            )
        except subprocess.TimeoutExpired:
            log.warning("MiniMax generation timed out")
            return None
        except FileNotFoundError:
            log.warning("mmx-cli not found at %s", mmx_path)
            return None

        if result.returncode != 0:
            log.warning("mmx failed (exit %d): %s", result.returncode, result.stderr[:500])
            return None

        # mmx may write to a different path — check output and parse stdout
        actual_path = out_path
        if not actual_path.exists():
            # Try to find the generated file from mmx output
            path_match = re.search(r'(/?\S+\.mp3)', result.stdout)
            if path_match:
                candidate = Path(path_match.group(1))
                if candidate.exists():
                    actual_path = candidate
                else:
                    # Maybe relative to cwd
                    candidate = Path.cwd() / path_match.group(1)
                    if candidate.exists():
                        actual_path = candidate

        if not actual_path.exists():
            log.warning("MiniMax output file not found: %s", actual_path)
            log.warning("mmx stdout: %s", result.stdout[:500])
            return None

        # Copy to output directory
        dest = output_dir / "bgm.mp3"
        shutil.copy2(actual_path, dest)
        size_kb = dest.stat().st_size / 1024
        log.info("MiniMax output: %.0f KB -> %s", size_kb, dest)

        # Try to get duration via ffprobe
        duration = 0.0
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries",
                 "format=duration", "-of", "csv=p=0", str(dest)],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                duration = float(probe.stdout.strip())
        except Exception:
            pass

        return {
            "title": f"AI Generated - {prompt[:40]}",
            "artist": "MiniMax",
            "duration": duration,
            "source": "minimax",
            "license": "MiniMax Generated",
            "file": "bgm.mp3",
        }


# --------------- Main ---------------


def find_music(
    style: str,
    min_duration: int = 30,
    max_duration: int = 180,
    output: Path | None = None,
    provider: str = "auto",
) -> dict | None:
    """Search/generate BGM. Returns metadata dict or None on failure."""
    output = output or Path.cwd() / "edit"
    output.mkdir(parents=True, exist_ok=True)

    if provider in ("pixabay", "auto"):
        log.info("Trying Pixabay (provider=%s)...", provider)
        meta = find_pixabay(style, min_duration, max_duration, output)
        if meta:
            return meta
        if provider == "pixabay":
            log.error("Pixabay failed and provider is pinned to pixabay")
            return None
        log.warning("Pixabay failed, falling back to MiniMax")

    if provider in ("minimax", "auto"):
        log.info("Trying MiniMax (provider=%s)...", provider)
        meta = find_minimax(style, min_duration, max_duration, output)
        if meta:
            return meta
        if provider == "minimax":
            log.error("MiniMax failed and provider is pinned to minimax")
            return None

    log.error("All providers failed. Consider adding BGM manually.")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search and download background music (Pixabay / MiniMax)",
    )
    parser.add_argument(
        "--style", required=True,
        help="Style description for BGM search/generation",
    )
    parser.add_argument(
        "--min-duration", type=int, default=30,
        help="Minimum BGM duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--max-duration", type=int, default=180,
        help="Maximum BGM duration in seconds (default: 180)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output directory (default: <cwd>/edit/)",
    )
    parser.add_argument(
        "--provider", choices=["pixabay", "minimax", "auto"], default="auto",
        help="Force provider or auto (default: auto)",
    )
    args = parser.parse_args()

    meta = find_music(
        style=args.style,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        output=args.output,
        provider=args.provider,
    )

    if not meta:
        print("ERROR: No BGM found/generated.", file=sys.stderr)
        sys.exit(1)

    # Write metadata
    output = args.output or Path.cwd() / "edit"
    meta_path = output / "bgm_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    log.info("Metadata written to %s", meta_path)

    print(f"BGM: {meta['title']} by {meta['artist']} ({meta['duration']:.1f}s)")
    print(f"Source: {meta['source']}  License: {meta['license']}")
    print(f"File: {output / meta['file']}")


if __name__ == "__main__":
    main()
