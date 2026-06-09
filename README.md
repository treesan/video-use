<p align="center">
  <img src="static/video-use-banner.png" alt="video-use" width="100%">
</p>

# video-use

**video-use** — 用 Claude Code 剪视频。100% 开源。

把原始素材丢进文件夹，跟 Claude Code 对话，拿回 `final.mp4`。适用于任何内容 — 口播、混剪、教程、旅行、访谈 — 没有预设，没有菜单。

## 它能做什么

- **剪掉废话**（`嗯`、`啊`、口误重试）和素材间的空白
- **自动调色**，每一段都上色（暖色电影、中性冲击，或任意自定义 ffmpeg 滤镜链）
- **30ms 音频淡入淡出**，每个切点都无爆音
- **烧录字幕**，你的风格 — 默认 2 词大写分块，完全可定制
- **生成动画叠层**，通过 [HyperFrames](https://github.com/heygen-com/hyperframes)、[Remotion](https://www.remotion.dev/)、[Manim](https://www.manim.community/) 或 PIL — 并行子代理生成，每个动画一个
- **高光检测**，静音航拍素材 — PySceneDetect + OpenCV 质量筛选 + VLM 场景描述（mimo/doubao/Qwen）
- **BGM 自动搜索**，从 Pixabay（无需 API key，反爬绕过），MiniMax AI 生成作为兜底
- **节拍感知剪辑** — madmom 三路节拍检测（downbeat/pitch/mel_energy）+ LLM 歌曲结构分析（Intro/Verse/Chorus）
- **音频混音** — BGM + 人声自动 ducking、淡入淡出、循环/裁剪、-14 LUFS 响度归一化
- **自检渲染输出**，在每个切点边界检查后才给你看
- **持久化会话记忆** 在 `project.md`，下周的会话从上次断点继续

## 设置提示词

粘贴到 Claude Code、Codex、Hermes、Openclaw 或任何有 shell 访问的代理：

```text
Set up https://github.com/browser-use/video-use for me.

Read install.md first to install this repo, wire up ffmpeg, register the skill with whichever agent you're running under, and set up the ElevenLabs API key — ask me to paste it when you need it. Then read SKILL.md for daily usage, and always read helpers/ because that's where the editing scripts live. After install, don't transcribe anything on your own — just tell me it's ready and wait for me to drop footage into a folder.
```

代理会处理克隆、依赖安装、技能注册，并提示你一次 ElevenLabs API key（在 [elevenlabs.io/app/settings/api-keys](https://elevenlabs.io/app/settings/api-keys) 获取）。

然后把代理指向原始素材文件夹：

```bash
cd /path/to/your/videos
claude    # 或 codex, hermes 等
```

如果需要从 VPS 或 Telegram 常驻剪辑，通过 [Browser Use Box](https://browser-use.com/bux) 运行代理。[15 秒演示](https://www.tiktok.com/@browser_use/video/7639824093721758989)。

在会话中：

> 把这些剪成一个发布视频

它会盘点素材、提出策略、等你确认，然后在素材旁生成 `edit/final.mp4`。所有输出都在 `<videos_dir>/edit/` — 项目目录保持干净。

## 手动安装

如果你想自己来：

```bash
# 1. 克隆并符号链接到代理的技能目录
git clone https://github.com/browser-use/video-use ~/Developer/video-use
ln -sfn ~/Developer/video-use ~/.claude/skills/video-use        # Claude Code
# ln -sfn ~/Developer/video-use ~/.codex/skills/video-use       # Codex

# 2. 安装依赖
cd ~/Developer/video-use
uv sync                         # 或: pip install -e .
brew install ffmpeg             # 必须
brew install yt-dlp             # 可选，用于下载在线素材

# 3. 添加 API keys
cp .env.example .env
$EDITOR .env
```

`.env` 中的 API keys：

| 变量 | 用途 | 获取方式 |
|------|------|---------|
| `ELEVENLABS_API_KEY` | 英文语音转录（ElevenLabs Scribe） | [elevenlabs.io/app/settings/api-keys](https://elevenlabs.io/app/settings/api-keys) |
| `VOLC_ASR_APP_KEY` | 中文语音转录（火山引擎 BigASR） | 火山引擎控制台 → 语音识别 |
| `VLM_PROVIDER` | 视觉描述提供商，`xiaomi`（默认）或 `minimax` | — |
| `MIMO_API_KEY` | 小米 MiMo VLM（当 `VLM_PROVIDER=xiaomi`） | 小米开放平台 |
| `MINIMAX_API_KEY` | MiniMax VLM + AI 音乐生成（当 `VLM_PROVIDER=minimax`） | [platform.minimaxi.com](https://platform.minimaxi.com) |
| `PIXABAY_API_KEY` | Pixabay 免版税音乐搜索（可选） | [pixabay.com/api/docs](https://pixabay.com/api/docs/) |

> `ELEVENLABS_API_KEY` 和 `VOLC_ASR_APP_KEY` 至少配一个；`transcribe.py` 会自动选择可用后端。

## 工作原理

LLM 从不"看"视频。它**读**视频 — 通过两层接口，合在一起给它词级精度的剪辑能力。

<p align="center">
  <img src="static/timeline-view.svg" alt="timeline_view 合成图 — 胶片条 + 说话人轨道 + 波形 + 词标签 + 静音间隙切点候选" width="100%">
</p>

**第一层 — 音频转录（始终加载）。** 每个源文件一次 ElevenLabs Scribe 调用，获得词级时间戳、说话人分离和音频事件（`(笑声)`、`(掌声)`、`(叹气)`）。所有素材打包成一个 ~12KB 的 `takes_packed.md` — LLM 的主阅读视图。

```
## C0103  (时长: 43.0s, 8 短语)
  [002.52-005.36] S0 百分之九十的 web agent 行为完全是浪费。
  [006.08-006.74] S0 我们修好了。
```

**第二层 — 视觉合成图（按需）。** `timeline_view` 为任意时间范围生成胶片条 + 波形 + 词标签 PNG。只在决策点调用 — 模糊停顿、重拍对比、切点合理性检查。

> 朴素方案：30,000 帧 × 1,500 tokens = **45M tokens 的噪声**。
> video-use：**12KB 文本 + 几张 PNG**。

跟 browser-use 给 LLM 结构化 DOM 而非截图是同一个思路 — 只是换成了视频。

## 流水线

### 标准流程（口播、访谈）

```
转录 ──> 打包 ──> LLM 推理 ──> EDL ──> 渲染 ──> 自检
                                                    │
                                                    └─ 有问题？修复 + 重渲染（最多 3 次）
```

### 静音航拍素材（DJI 无人机、旅行 Vlog）

```
高光检测 ──> 找音乐 ──> 节拍检测 ──> LLM 推理 ──> EDL ──> 渲染+混音 ──> 自检
    │            │           │                                    │
    │            │           │                                    └─ 有问题？修复 + 重渲染
    │            │           └─ madmom 三路节拍检测
    │            └─ Pixabay 搜索 / MiniMax 生成
    └─ PySceneDetect + OpenCV + VLM
```

自检循环在_渲染输出_的每个切点边界运行 `timeline_view` — 捕捉视觉跳帧、音频爆音、隐藏字幕。只有通过检查后你才看到预览。

## 新功能：高光检测 & BGM 音乐

针对静音航拍素材（DJI 无人机、旅行 Vlog），video-use 现在包含：

### 高光检测（`helpers/highlight_detect.py`）

```bash
# 检测静音航拍素材中的高光片段
python helpers/highlight_detect.py /path/to/videos --theme "旅行Vlog"

# 跳过 VLM，仅使用 OpenCV 质量评分
python helpers/highlight_detect.py /path/to/videos --no-vlm
```

**4 层流水线：**
1. **PySceneDetect** — 镜头边界检测（AdaptiveDetector）
2. **OpenCV 质量预筛** — 模糊（Laplacian）、曝光、运动、黑帧检测
3. **VLM 场景描述** — mimo-v2.5 / doubao-seed / Qwen3.5，通过 OpenAI 兼容 API
4. **LLM 评分** — 按美学质量和主题相关性排序高光

### BGM 音乐（`helpers/find_music.py`）

```bash
# 从 Pixabay 搜索免费免版税音乐
python helpers/find_music.py --style "cinematic travel" --provider pixabay

# 通过 MiniMax AI 生成（兜底）
python helpers/find_music.py --style "upbeat electronic" --provider minimax

# 自动：先试 Pixabay，不行再 MiniMax
python helpers/find_music.py --style "chill lo-fi" --provider auto
```

**提供商：**
- **Pixabay** — 免费免版税音乐，无需 API key（反爬绕过）
- **MiniMax** — AI 生成器乐曲，通过 `mmx-cli`

### 节拍检测（`helpers/beat_detect.py`）

```bash
# 分析 BGM 节拍和结构
python helpers/beat_detect.py /path/to/bgm.mp3
```

**输出：** `beats.json`，包含 BPM、关键点、歌曲段落（Intro/Verse/Chorus）、能量曲线、最佳起始偏移。

### 音频混音（`helpers/mix_audio.py`）

```bash
# 混合视频与 BGM
python helpers/mix_audio.py video.mp4 bgm.mp3 -o output.mp4

# 带人声 ducking
python helpers/mix_audio.py video.mp4 bgm.mp3 --duck-voiceover -o output.mp4
```

**功能：** amix、sidechaincompress ducking、淡入淡出、循环/裁剪、-14 LUFS 响度归一化。

## 导出规格

`render.py` 可以按单个平台规格渲染，`render_profiles.py` 可以批量渲染 `edl.json` 中声明的所有规格：

```bash
uv run python helpers/render.py <videos_dir>/edit/edl.json \
  -o <videos_dir>/edit/final.mp4 \
  --profile bilibili_1080p60_landscape

uv run python helpers/render_profiles.py <videos_dir>/edit/edl.json \
  --profiles bilibili_4k60_landscape,douyin_1080p60_portrait
```

内置规格包括 B站 4K/1080p 横屏、抖音竖屏、小红书竖屏/3:4、以及 1080p120 横屏/竖屏变体。批量导出写入：

```text
<videos_dir>/edit/exports/<profile>.mp4
<videos_dir>/edit/exports/<profile>.json
```

JSON 报告验证分辨率、帧率、EDL 时长、音频存在、黑帧和长静音。EDL 可以声明默认值：

```json
{
  "export": {
    "profiles": ["bilibili_4k60_landscape", "douyin_1080p60_portrait"],
    "default_profile": "bilibili_1080p60_landscape",
    "audio_policy": "bgm_only"
  }
}
```

`audio_policy` 支持 `bgm_only`、`duck`、`mix`、`source_only` 和 `silent`。对于航拍/旅行混剪，使用 `bgm_only` 确保源相机音频被丢弃，BGM 循环覆盖完整输出时长。

## 设计原则

1. **文本 + 按需视觉。** 不倒帧。转录文本是主界面。
2. **音频优先，视觉跟随。** 切点来自语音边界和静音间隙。
3. **提问 → 确认 → 执行 → 自检 → 持久化。** 没有策略确认不动刀。
4. **对内容类型零假设。** 先看、先问、再剪。
5. **12 条硬规则，其余自由发挥。** 生产正确性不可妥协，审美不设限。

详见 [`SKILL.md`](./SKILL.md) 获取完整的生产规则和剪辑手艺。
