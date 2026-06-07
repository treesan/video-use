"""Shared VLM/LLM client with provider routing.

Reads VLM_PROVIDER from env (default: "xiaomi") and routes to the
corresponding API endpoint, model name, and API key.

Supported providers:
  - xiaomi  : MiMo v2.5 (https://api.xiaomimimo.com/v1)
  - minimax : MiniMax-M3 (https://api.minimaxi.com/v1)

Usage:
    from helpers.vlm_client import get_client, get_model, get_provider_config

    client = get_client()       # OpenAI-compatible client
    model = get_model()         # e.g. "mimo-v2.5" or "MiniMax-M3"
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --------------- Provider definitions ---------------

PROVIDERS: dict[str, dict] = {
    "xiaomi": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5",
        "api_key_env": "MIMO_API_KEY",
        "fps_range": (0.1, 10.0),
        "detail_key": "media_resolution",   # key name in video_url content part
        "detail_default": "default",
        "thinking_support": False,
    },
    "minimax": {
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M3",
        "api_key_env": "MINIMAX_API_KEY",
        "fps_range": (0.2, 5.0),
        "detail_key": "detail",             # key name inside video_url dict
        "detail_default": "default",
        "thinking_support": True,
        "thinking_default": "disabled",
    },
}


@dataclass
class ProviderConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    fps_range: tuple[float, float]
    detail_key: str
    detail_default: str
    thinking_support: bool
    thinking_default: str | None = None


# --------------- Env loading ---------------


def load_env() -> None:
    """Load .env file into os.environ if not already loaded."""
    dotenv_path = Path(__file__).resolve().parent.parent / ".env"
    if dotenv_path.exists():
        for line in dotenv_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def load_env_value(key: str) -> str:
    """Load a single value from .env files or environment."""
    repo_root = Path(__file__).resolve().parent.parent
    for candidate in [repo_root / ".env"]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return os.environ.get(key, "")


# --------------- Provider resolution ---------------


def _resolve_provider() -> str:
    """Get provider name from env, default 'xiaomi'."""
    return os.environ.get("VLM_PROVIDER", "xiaomi").lower()


def get_provider_config() -> ProviderConfig:
    """Resolve current provider and return its config."""
    load_env()
    name = _resolve_provider()
    if name not in PROVIDERS:
        raise ValueError(
            f"Unknown VLM_PROVIDER '{name}'. Choose from: {', '.join(PROVIDERS)}"
        )
    p = PROVIDERS[name]
    api_key = os.environ.get(p["api_key_env"], "")
    if not api_key:
        raise ValueError(f"{p['api_key_env']} not set (check .env or environment)")

    return ProviderConfig(
        provider=name,
        base_url=p["base_url"],
        model=p["model"],
        api_key=api_key,
        fps_range=p["fps_range"],
        detail_key=p["detail_key"],
        detail_default=p["detail_default"],
        thinking_support=p["thinking_support"],
        thinking_default=p.get("thinking_default"),
    )


def get_client():
    """Create an OpenAI-compatible client for the current provider."""
    from openai import OpenAI
    cfg = get_provider_config()
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)


def get_model() -> str:
    """Return the default model name for the current provider."""
    return get_provider_config().model


def completion_kwargs(thinking: str | None = None) -> dict:
    """Return provider-specific extra request kwargs for chat completions."""
    cfg = get_provider_config()
    if not cfg.thinking_support:
        return {}
    thinking_type = thinking or cfg.thinking_default
    if not thinking_type:
        return {}
    return {"extra_body": {"thinking": {"type": thinking_type}}}


def build_video_content(
    video_data_uri: str,
    fps: float = 2.0,
    detail: str | None = None,
) -> dict:
    """Build a video_url content part compatible with the current provider.

    Xiaomi puts fps/detail at content-part level.
    MiniMax puts fps/detail inside the video_url dict.
    """
    cfg = get_provider_config()
    fps = max(cfg.fps_range[0], min(fps, cfg.fps_range[1]))
    detail = detail or cfg.detail_default

    if cfg.provider == "xiaomi":
        return {
            "type": "video_url",
            "video_url": {"url": video_data_uri},
            "fps": fps,
            "media_resolution": detail,
        }
    else:
        # MiniMax: fps and detail go inside video_url
        return {
            "type": "video_url",
            "video_url": {
                "url": video_data_uri,
                "fps": fps,
                "detail": detail,
            },
        }


def strip_thinking(content: str | None) -> str:
    """Strip thinking/reasoning blocks from model response.

    MiniMax-M3 includes <think reasoning>...</think reasoning> in content.
    Xiaomi MiMo uses reasoning_content separately (already stripped).
    """
    if not content:
        return ""
    import re
    # Remove <think reasoning>...</think reasoning> blocks
    content = re.sub(r"<think[^>]*>.*?</think[^>]*>", "", content, flags=re.DOTALL)
    return content.strip()
