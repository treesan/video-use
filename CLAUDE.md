# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Conversation-driven video editing for AI coding agents. The LLM never processes video frames directly — it reads word-level transcripts (from ElevenLabs Scribe or Volcengine BigASR) and on-demand visual composites (filmstrip + waveform PNGs). Users drop raw footage into a folder, chat with the agent, and get a finished `final.mp4`.

## Commands

```bash
# Install
uv sync                          # preferred
pip install -e .                  # fallback

# Transcription
python helpers/transcribe.py <video>                    # single file, auto-selects ASR backend
python helpers/transcribe_batch.py <videos_dir>         # 4-worker parallel batch
python helpers/pack_transcripts.py --edit-dir <dir>     # JSON transcripts → takes_packed.md

# Visual inspection
python helpers/timeline_view.py <video> <start> <end>   # filmstrip + waveform PNG

# Render
python helpers/render.py <edl.json> -o <output>         # full pipeline
python helpers/render.py <edl.json> -o <output> --preview  # 720p fast preview
python helpers/render.py <edl.json> -o <output> --profile bilibili_1080p60_landscape  # platform profile

# Batch export
python helpers/render_profiles.py <edl.json>             # all profiles from edl.export.profiles
python helpers/render_profiles.py <edl.json> --profiles bilibili_4k60_landscape,douyin_1080p60_portrait

# Color grade
python helpers/grade.py <input> -o <output>             # auto mode
python helpers/grade.py <input> -o <output> --preset warm_cinematic

# Highlight detection (silent aerial footage)
python helpers/highlight_detect.py <videos_dir>                      # auto-detect highlights
python helpers/highlight_detect.py <videos_dir> --theme "旅行Vlog"    # with theme keywords
python helpers/highlight_detect.py <videos_dir> --no-vlm             # skip VLM, quality-only

# BGM beat analysis
python helpers/beat_detect.py <bgm.mp3>                              # analyze beats + structure
python helpers/beat_detect.py <bgm.mp3> --target-duration 120        # with shortfall detection

# BGM search/generation
python helpers/find_music.py --style "upbeat travel"                 # search Pixabay, fallback MiniMax
python helpers/find_music.py --style "cinematic" --provider minimax  # force MiniMax

# Audio mixing
python helpers/mix_audio.py <video> <bgm.mp3> -o output.mp4          # basic mix
python helpers/mix_audio.py <video> <bgm.mp3> --duck-voiceover       # with voiceover ducking

# Tests (unittest, no pytest)
python helpers/test_transcribe_unified.py
python helpers/test_transcribe_volc.py
```

## Architecture

### Two-Layer LLM Interface

- **Layer 1 (always loaded)**: `takes_packed.md` — phrase-level transcript with word-boundary timestamps. Breaks on silence ≥ 0.5s or speaker change. ~12KB for a typical project.
- **Layer 2 (on demand)**: `timeline_view.py` PNGs — filmstrip + waveform + word labels for any time range. Use only at decision points, not as a scan tool.

### Pipeline

```
Transcribe → Pack → LLM reasons → EDL.json → Render → Self-Eval
                                                        └→ fix + re-render (max 3)
```

For silent aerial footage (no speech), the pipeline starts with highlight detection:
```
Highlight Detect → LLM reasons → EDL.json (+ bgm) → Render → Self-Eval
```

### Dual ASR Backend

`transcribe.py` is the unified entry point. Both backends produce Scribe-compatible JSON so downstream tools work unchanged:
- **Volcengine BigASR Turbo** (`transcribe_volc.py`): Preferred for CJK languages. Chinese-first default.
- **ElevenLabs Scribe** (internal to `transcribe.py`): Preferred for English.

### Render Pipeline (`render.py` — most complex helper)

1. Per-segment extract with grade + 30ms audio fades
2. Lossless `-c copy` concat into `base.mp4`
3. Overlay composition with PTS shifting (if overlays exist)
4. Subtitles applied **LAST**
5. BGM mix (if `edl.json` has `bgm` field): original audio + BGM via amix/sidechaincompress
6. Two-pass loudness normalization (-14 LUFS / -1 dBTP / LRA 11)
7. HDR (HLG/PQ) → SDR tone-mapping via zscale

### Output Layout

All outputs go into `<videos_dir>/edit/`, never inside this project directory:
```
<videos_dir>/edit/
├── project.md               # session memory, appended each session
├── takes_packed.md          # primary LLM reading view
├── edl.json                 # cut decisions
├── highlights.json          # highlight detection results (silent aerial)
├── beats.json               # BGM beat/structure analysis
├── bgm.mp3                  # background music (downloaded or generated)
├── bgm_meta.json            # BGM source metadata
├── transcripts/<name>.json  # cached raw ASR JSON
├── animations/slot_<id>/    # per-animation source + render
├── clips_graded/            # per-segment extracts with grade + fades
├── master.srt               # output-timeline subtitles
├── exports/                 # batch profile export outputs
│   ├── <profile>.mp4        # per-profile rendered output
│   └── <profile>.json       # per-profile validation report
├── verify/                  # debug frames / timeline PNGs
├── preview.mp4
└── final.mp4
```

## Hard Rules (production correctness — non-negotiable)

These are correctness constraints, not style preferences. Deviation produces silent failures or broken output.

1. **Subtitles LAST** in the filter chain, after every overlay
2. **Per-segment extract → lossless `-c copy` concat**, never single-pass filtergraph with overlays
3. **30ms audio fades** at every segment boundary
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift frame 0 to window start
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`
6. **Never cut inside a word** — snap to word boundaries from transcript
7. **Pad every cut edge** (30–200ms working window, absorbs ASR timestamp drift)
8. **Word-level verbatim ASR only** — never SRT/phrase mode (loses sub-second gap data)
9. **Cache transcripts per source** — never re-transcribe unless source file changed
10. **Parallel sub-agents for multiple animations** — never sequential
11. **Strategy confirmation before execution** — never touch the cut until user approves the plan
12. **All session outputs in `<videos_dir>/edit/`**

## EDL Format

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "..."}
  ],
  "bgm": {
    "file": "edit/bgm.mp3",
    "start_offset": 2.5,
    "volume": 0.3,
    "duck_voiceover": true,
    "fade_in": 2.0,
    "fade_out": 3.0
  },
  "export": {
    "profiles": ["bilibili_1080p60_landscape", "douyin_1080p60_portrait"],
    "default_profile": "bilibili_1080p60_landscape",
    "audio_policy": "bgm_only"
  },
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade`: preset name (`warm_cinematic`, `neutral_punch`, `subtle`, `none`) or raw ffmpeg filter string.
`overlays`: optional rendered animation clips.
`subtitles`: optional, applied LAST per Hard Rule 1.
`bgm`: optional, BGM mix after compositing, before loudnorm. `duck_voiceover` auto-lowers BGM during speech via sidechaincompress.
`export`: optional, declares target profiles and audio policy for batch export. `audio_policy` supports `bgm_only`, `duck`, `mix`, `source_only`, `silent`.

## Animation Engines

Pick per slot, don't default to any single one:
- **HyperFrames** — browser-native HTML/CSS/GSAP, `npx --yes hyperframes`
- **Remotion** — React/CSS, `npx create-video@latest`
- **Manim** — math/technical diagrams, see `skills/manim-video/SKILL.md`
- **PIL + PNG sequence + ffmpeg** — simple overlay cards

## External Dependencies

- `ffmpeg` + `ffprobe` — **required**, all video/audio processing
- `yt-dlp` — optional, for downloading online sources
- `mmx-cli` — optional, MiniMax AI music generation (`mmx music generate --instrumental`)
- API keys in `.env`: `ELEVENLABS_API_KEY`, `VOLCENGINE_ACCESS_TOKEN` / `VOLCENGINE_APPID`, `PIXABAY_API_KEY` (optional), `VLM_PROVIDER` (optional, default `xiaomi`), `MIMO_API_KEY` / `MINIMAX_API_KEY` (per provider)
- VLM provider routing via `helpers/vlm_client.py` — set `VLM_PROVIDER=xiaomi` (MiMo v2.5) or `VLM_PROVIDER=minimax` (MiniMax-M3). Each provider has its own base_url, model, and api_key_env.

## Project Memory

`<edit>/project.md` is appended each session with strategy, decisions, reasoning log, and outstanding items. On startup, read it and summarize the last session before asking whether to continue.

## 四个原则

### 1. 编码前思考
**不要假设。不要隐藏困惑。呈现权衡。**
- 明确说明假设，呈现多种解释，适时提出异议，困惑时停下来

### 2. 简洁优先
**用最少的代码解决问题。不要过度推测。**
- 不要添加要求之外的功能，不要为一次性代码创建抽象
- 检验标准：资深工程师会觉得这过于复杂吗？如果是，简化。

### 3. 精准修改
**只碰必须碰的。只清理自己造成的混乱。**
- 不要"改进"相邻的代码，不要重构没坏的东西，匹配现有风格
- 检验标准：每一行修改都应该能直接追溯到用户的请求。

### 4. 目标驱动执行
**定义成功标准。循环验证直到达成。**
- 将指令式任务转化为可验证的目标
- 对于多步骤任务，说明一个简短的计划