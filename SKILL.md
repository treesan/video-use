---
name: video-use
description: 用对话方式剪辑任何视频。转录、剪切、调色、生成动画叠层、烧录字幕 — 适用于口播、混剪、教程、旅行、访谈。无预设、无菜单。先问、确认、再执行、迭代、持久化。生产正确性规则不可妥协，其余都是创作自由。
---

# Video Use

## 原则

1. **LLM 从原始转录 + 按需视觉中推理。** 唯一值得保留的衍生产物是打包好的短语级转录文本（`takes_packed.md`）。其余一切 — 废话标记、重拍检测、镜头分类、重点评分 — 都在决策时即时推导。
2. **音频为主，视觉跟随。** 切点候选来自语音边界和静音间隙。视觉只在决策点深入查看。
3. **提问 → 确认 → 执行 → 迭代 → 持久化。** 用户用自然语言确认策略之前，绝不动刀。
4. **通用化。** 不要预设这是什么类型的视频。先看素材，问用户，再剪辑。
5. **创作自由是默认值。** 本文档中的每个具体数值、预设、字体、颜色、时长、音高结构和技法，都来自某个已验证的视频案例 — 不是强制规范。阅读它们是为了理解可行性和原因。然后根据素材的实际情况和用户的实际需求，自己做品味判断。**你必须做的事只列在下方的硬规则部分。** 其余一切由你决定。
6. **自由创造。** 如果素材需要本文档未描述的技法 — 分屏、画中画、下三分之一身份卡、反应镜头、变速、定格、交叉溶解、匹配剪辑、L 型剪辑、J 型剪辑、呼吸变速、随便什么 — 做就是了。工具是 ffmpeg 和 PIL。格式支持什么就能做什么。不用等批准。
7. **在给用户看之前，先验证自己的输出。** 如果自己不想交付，就不要呈现。

## 硬规则（生产正确性 — 不可妥协）

这些规则一旦违反就会产生静默失败或损坏的输出。它们不是品味问题，是正确性问题。记牢。

1. **字幕在滤镜链的最后应用**，在所有叠层之后。否则叠层会遮住字幕。静默失败。
2. **逐段提取 → 无损 `-c copy` 拼接**，而非单次滤镜图。否则添加叠层时会导致二次编码。
3. **每个段落边界 30ms 音频淡入淡出**（`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`）。否则每个切点都有可闻爆音。
4. **叠层使用 `setpts=PTS-STARTPTS+T/TB`**，将叠层的第 0 帧位移到其窗口起始时间。否则会在叠层窗口期间看到动画的中间帧。
5. **母版 SRT 使用输出时间线偏移量**：`output_time = word.start - segment_start + segment_offset`。否则拼接后字幕对齐错误。
6. **绝不在词中间下刀。** 每个切边必须对齐 Scribe 转录中的词边界。
7. **每个切边留余量。** 工作窗口：30–200ms。Scribe 时间戳漂移 50–100ms — 余量吸收漂移。快节奏偏紧，电影感偏松。
8. **仅使用词级逐字 ASR。** 绝不用 SRT/短语模式（丢失亚秒级间隙数据）。绝不用标准化填充词（丢失编辑信号）。
9. **按源文件缓存转录。** 除非源文件本身发生变化，否则绝不重新转录。
10. **多个动画使用并行子代理。** 绝不串行。通过 `Agent` 工具同时启动 N 个；总耗时 ≈ 最慢的那个。
11. **执行前确认策略。** 用户批准自然语言方案之前，绝不动刀。
12. **所有会话输出在 `<videos_dir>/edit/` 中。** 绝不写入 `video-use/` 项目目录。

本文档中其他所有内容都是已验证的案例。素材需要时随时偏离。

## 目录布局

Skill 位于 `video-use/`。用户素材在他们放的任何位置。所有会话输出进入 `<videos_dir>/edit/`。

```
<videos_dir>/
├── <源文件，不动>
└── edit/
    ├── project.md               ← 记忆；每次会话追加
    ├── takes_packed.md          ← 短语级转录，LLM 的主阅读视图
    ├── edl.json                 ← 剪切决策
    ├── highlights.json          ← 高光检测结果（静音航拍素材）
    ├── beats.json               ← BGM 节拍/结构分析
    ├── bgm.mp3                  ← 下载或生成的背景音乐
    ├── bgm_meta.json            ← BGM 来源元数据
    ├── transcripts/<name>.json  ← 缓存的原始 Scribe JSON
    ├── animations/slot_<id>/    ← 每个动画的源文件 + 渲染 + 推理
    ├── clips_graded/            ← 逐段提取带调色 + 淡入淡出
    ├── master.srt               ← 输出时间线字幕
    ├── downloads/               ← yt-dlp 输出
    ├── exports/                 ← 批量导出输出
    │   ├── <profile>.mp4        ← 每个规格的渲染输出
    │   └── <profile>.json       ← 每个规格的验证报告
    ├── verify/                  ← 调试帧 / 时间线 PNG
    ├── preview.mp4
    └── final.mp4
```

## 环境设置

首次安装见 `install.md`（克隆、依赖、ffmpeg、skill 注册、API key）。不要每次会话重跑；冷启动时只需验证：

- `ELEVENLABS_API_KEY` 或 `VOLC_ASR_APP_KEY` 已解析 — 至少一个 ASR key 必须存在。`transcribe.py` 自动选择后端：英文用 ElevenLabs Scribe，中文用火山引擎 BigASR（通过 `--asr` 参数控制或自动检测）。Key 放在 video-use 仓库根目录的 `.env` 中。如果两个都缺失，请用户粘贴一个。
- `ffmpeg` + `ffprobe` 在 PATH 上。
- Python 依赖已安装（在仓库内 `uv sync` 或 `pip install -e .`）。
- 如果会话需要 HyperFrames 或 Remotion 插槽，Node.js + npm 可用。HyperFrames 目前需要 Node.js 22+。
- `yt-dlp`、HyperFrames、Remotion、Manim 仅在首次使用时安装。
- 首次使用的动画设置在插槽目录内进行，绝不在 video-use 仓库根目录。HyperFrames 可用 `npx --yes hyperframes ...` 调用；Remotion 可用 `npx create-video@latest` 脚手架搭建，或安装为项目本地依赖后使用其 `remotion render` 命令。
- 此 skill 自带 `skills/manim-video/`。构建 Manim 插槽时阅读其 SKILL.md。

Helpers（`helpers/transcribe.py`、`helpers/render.py` 等）与此 SKILL.md 位于同一目录。相对于包含此文件的目录解析路径 — skill 通常符号链接在 `~/.claude/skills/video-use/` 或 `~/.codex/skills/video-use/`。

## Helpers

- **`transcribe.py <video>`** — 单文件 Scribe 调用。可选 `--num-speakers N`。已缓存。
- **`transcribe_batch.py <videos_dir>`** — 4 工作线程并行转录。多 take 使用。
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md`（短语级，静音 ≥ 0.5s 断句）。
- **`timeline_view.py <video> <start> <end>`** — 胶片条 + 波形 PNG。按需视觉深入查看。**不是扫描工具** — 在决策点使用，不要频繁调用。
- **`render.py <edl.json> -o <out>`** — 逐段提取 → 拼接 → 叠层（PTS 位移）→ 字幕最后。`--preview` 快速 720p。`--build-subtitles` 内联生成 master.srt。
- **`grade.py <in> -o <out>`** — ffmpeg 滤镜链调色。预设 + `--filter '<raw>'` 自定义。
- **`highlight_detect.py <videos_dir>`** — 静音航拍素材高光检测。PySceneDetect 镜头边界 → OpenCV 质量预筛 → VLM 场景描述 → LLM 评分。输出 `highlights.json`。
- **`beat_detect.py <bgm_file>`** — BGM 节拍/结构分析。madmom 三路检测 + LLM 歌曲结构 + ebur128 能量曲线。输出 `beats.json`。
- **`find_music.py --style "..."`** — BGM 搜索/生成。Pixabay 免费音乐（API 或抓取）→ MiniMax AI 生成兜底。输出 `bgm.mp3` + `bgm_meta.json`。
- **`mix_audio.py <video> <bgm>`** — 音频混音。原声 + BGM 混合、人声 ducking、淡入淡出、BGM 循环/裁剪、loudnorm -14 LUFS。
- **`render_profiles.py <edl.json>`** — 批量导出 edl.json 声明的所有平台规格。`--profiles` 指定规格列表。

对于动画，用 `Bash` 创建 `<edit>/animations/slot_<id>/` 并通过 `Agent` 工具生成子代理。

## 流程

1. **盘点。** `ffprobe` 每个源文件。对目录运行 `transcribe_batch.py`。运行 `pack_transcripts.py` 生成 `takes_packed.md`。抽样一两个 `timeline_view` 获取视觉第一印象。
2. **预扫描问题。** 浏览一遍 `takes_packed.md`，标注口误、明显错读或需避免的措辞。列出清单，输入编辑简报。
3. **对话。** 用自然语言描述你看到的。问由素材塑造的问题。收集：内容类型、目标时长/宽高比、美学/品牌方向、节奏感、必须保留的时刻、必须剪掉的时刻、动画和调色偏好、字幕需求。不要用固定清单 — 每次合适的问题都不一样。
4. **提出策略。** 4–8 句话：形态、take 选择、剪切方向、动画方案、调色方向、字幕风格、时长估算。**等待确认。**
5. **执行。** 通过编辑子代理简报生成 `edl.json`。在模糊时刻深入 `timeline_view`。并行子代理构建动画。逐段调色。通过 `render.py` 合成。
6. **预览。** `render.py --preview`。
7. **自检（在给用户看之前）。** 对**渲染输出**（不是源文件）的每个切点边界（±1.5s 窗口）运行 `timeline_view`。逐张检查：
   - 切点处的视觉不连续 / 闪烁 / 跳跃
   - 边界处的波形尖峰（逃过 30ms 淡入淡出的音频爆音）
   - 字幕被叠层遮挡（违反规则 1）
   - 叠层错位或显示错误帧（违反规则 4）

   同时抽样：前 2s、后 2s、以及 2–3 个中间点 — 检查调色一致性、字幕可读性、整体连贯性。对输出运行 `ffprobe` 验证时长与 EDL 预期一致。

   如果有问题：修复 → 重渲染 → 重检。**自检上限 3 轮** — 如果 3 轮后仍有问题，标记给用户而不是无限循环。只有自检通过后才呈现预览。
8. **迭代 + 持久化。** 自然语言反馈、重新规划、重新渲染。绝不重新转录。确认后最终渲染。追加到 `project.md`。

## 高光检测（静音航拍素材）

对于 DJI 无人机素材和其他无语对白的静音视频，音频优先的流水线无内容可处理。`highlight_detect.py` 提供了视觉优先的替代方案：

1. **运行高光检测。** `uv run python3 helpers/highlight_detect.py <videos_dir> --theme "旅行Vlog-青海湖"`。三层流水线运行：
   - PySceneDetect 找镜头边界（AdaptiveDetector）
   - OpenCV 预筛质量（Laplacian 模糊度 + 曝光 + 运动幅度 — 过滤 30-50% 的垃圾镜头）
   - VLM 描述质量通过的镜头（原生视频理解 — 视频片段以 base64 发送）
   - LLM 评分排序高光，输出 `highlights.json`

2. **查看高光。** 读取 `highlights.json` — 按分数排序，每条有 `source`/`start`/`end`（兼容 edl.json ranges 格式），外加 `tags`、`reason` 和 `vlm_summary`。

3. **筛选与精修。** 挑选符合叙事的高光。`highlights[].source/start/end` 可直接复制到 `edl.json` ranges。在决策点用 `timeline_view` 调整边界。

4. **不用 VLM。** 如果没有 VLM API key，使用 `--no-vlm` — 评分回退到仅 OpenCV 质量评分。语义信息少，但仍能过滤垃圾并按技术质量排序。

**VLM/LLM 提供商配置：** 在 `.env` 中设置 `VLM_PROVIDER` 为 `xiaomi`（MiMo v2.5，默认）或 `minimax`（MiniMax-M3）。对应的 API key（`MIMO_API_KEY` 或 `MINIMAX_API_KEY`）必须配置。提供商路由由 `helpers/vlm_client.py` 处理 — 每个提供商有其自己的 base_url、model、fps 范围和视频内容格式。

**Pixabay BGM 搜索：** `PIXABAY_API_KEY` 可选 — 没有的话，`find_music.py` 使用抓取兜底方案。MiniMax AI 生成需要安装 `mmx-cli` 并设置 `MINIMAX_API_KEY`。

## BGM 配乐（搜索 → 节拍分析 → 混音）

当视频需要背景音乐时，使用此流程：

1. **获取 BGM。** 用户提供文件，或运行 `find_music.py`：
   ```
   uv run python3 helpers/find_music.py --style "upbeat cinematic travel" --min-duration 60 --max-duration 180
   ```
   先尝试 Pixabay（免费、免版税）。兜底 MiniMax AI 生成（`mmx music generate --instrumental`）。输出 `bgm.mp3` + `bgm_meta.json`。

2. **分析节拍。** `uv run python3 helpers/beat_detect.py <edit>/bgm.mp3 --target-duration 120`
   - madmom 检测重拍、音高起始点和能量峰值
   - LLM 识别歌曲段落（Intro/Verse/Chorus/Outro）
   - ebur128 找到最佳起始点（跳过静音前奏）
   - 输出 `beats.json`，包含 `bpm`、`sections[]`、`keypoints[]`、`best_start`

3. **规划节拍对齐剪切。** 使用 `beats.json` 的关键点和段落，将剪切与音乐结构对齐。强视觉时刻放在重拍上，转场放在段落边界。LLM 编辑简报应引用节拍结构。

4. **混音。** `edl.json` 的 `bgm` 字段触发 `render.py` 中的自动 BGM 混音：
   ```json
   "bgm": {
     "file": "edit/bgm.mp3",
     "start_offset": 2.5,
     "volume": 0.3,
     "duck_voiceover": true,
     "fade_in": 2.0,
     "fade_out": 3.0
   }
   ```
   - `duck_voiceover: true` — 当原声（语音）存在时，BGM 通过 `sidechaincompress` 自动降低音量
   - `start_offset` — 跳过 BGM 前奏，使用 `beats.json` 的 `best_start` 值
   - 音量、淡入、淡出是品味判断 — 在策略中提出数值并确认

   对于静音航拍素材：`duck_voiceover` 无效（没有原声可以 duck），BGM 成为唯一音轨。

   混音发生在合成之后（字幕最后）和响度归一化之前，保持硬规则的流水线顺序。

## 剪辑手艺（技法）

- **音频优先。** 候选切点来自词边界和静音间隙。
- **保留高潮。** 笑声、金句、强调节拍。在高潮之后延长以包含反应 — 笑声本身就是节拍。
- **说话人交接** 在话语之间留气口。常用值：400–600ms。快节奏更短，电影感更长。品味判断。
- **音频事件作为信号。** `(笑声)`、`(叹气)`、`(掌声)` 标记节拍。在其后延长。
- **静音间隙是切点候选。** ≥400ms 的静音通常最干净。150–400ms 的短语边界可用但需视觉检查。<150ms 不安全（短语中间）。
- **示例切点余量**（某发布视频的实际值）：第一个保留词前 50ms，最后一个保留词后 80ms。混剪活力更紧，纪录片更松。保持在 30–200ms 工作窗口内（硬规则 7）。
- **绝不要独立推理音频和视频。** 每个剪切必须在两条轨道上都成立。

## 打包转录文本（主阅读视图）

`pack_transcripts.py` 读取所有 `transcripts/*.json` 并生成一个 markdown 文件，每个 take 是一组短语级行，每行前缀为 `[start-end]` 时间范围。短语在静音 ≥ 0.5s 或说话人变化时断开。这是编辑子代理用来挑选剪切点的产物 — 仅凭文本就能给出词边界精度，token 消耗仅为原始 JSON 的 1/10。

示例行：
```
## C0103  (时长: 43.0s, 8 短语)
  [002.52-005.36] S0 百分之九十的 web agent 行为完全是浪费。
  [006.08-006.74] S0 我们修好了。
```

## 编辑子代理简报（多 take 筛选用）

当任务是"从众多片段中为每个节拍挑选最佳 take"时，生成一个独立的子代理，简报模板如下。结构是核心；音高示例不是。

```
你正在剪辑一部<类型>视频。为每个节拍挑选最佳 take，并按节拍时间顺序组装，
不是按源片段顺序。

输入：
  - takes_packed.md（所有 take 的带时间注释短语级转录）
  - 产品/叙事上下文：<用户提供的 2 句话>
  - 说话人：<姓名、角色、表达风格说明>
  - 预期结构：<选一个原型或自创>
  - 需避免的口误：<预扫描阶段列出的清单>
  - 目标时长：<秒>

常见结构原型（选、改或自创）：
  - 科技发布 / 演示：  钩子 → 问题 → 解决方案 → 收益 → 示例 → 行动号召
  - 教程：             引入 → 准备 → 步骤 → 坑点 → 回顾
  - 访谈：             （提问 → 回答 → 追问）循环
  - 旅行 / 活动：      到达 → 高光 → 安静时刻 → 离开
  - 纪录片：           论点 → 证据 → 反方 → 结论
  - 音乐 / 表演：      前奏 → 主歌 → 副歌 → 桥段 → 尾奏
  - 或自创。

规则：
  - 起止时间必须落在转录中的词边界上。
  - 切点边界留余量（工作窗口 30–200ms）。
  - 优先选择 ≥ 400ms 的静音作为切点目标。
  - 不可避免的口误如果没有更好的 take 则保留。在 "reason" 中注明。
  - 如果超预算，修正：删一个节拍或收紧结尾。报告总时长并自我纠正。

输出（JSON 数组，无正文）：
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "钩子",
    "quote": "...", "reason": "..."}, ...]

返回最终 EDL 和一行总时长检查。
```

## 调色（按需）

你的工作是**推理画面**，而非套预设。看一帧（通过 `timeline_view`），判断问题所在，调一个参数，再看。

心智模型是 ASC CDL。每通道：`out = (in * slope + offset) ** power`，然后全局饱和度。`slope` → 高光，`offset` → 阴影，`power` → 中间调。

**示例滤镜链**（`grade.py` 有 `--list-presets`；以它们为起点或自己组合）：

- **`warm_cinematic`** — 复古/技术感，微妙的青橙分色，去饱和。在某真实发布视频中使用过。口播安全。
- **`neutral_punch`** — 最小矫正：对比度增强 + 温和 S 曲线。无色相偏移。
- **`none`** — 直通。用户未要求时的默认值。

其他类型 — 人像、自然、产品、MV、纪录片 — 自己发明链条。`grade.py --filter '<raw ffmpeg>'` 接受任意滤镜字符串。

硬规则：**在提取时逐段应用**（非拼接后，那会二次编码）。不做皮肤测试不要激进。

## 字幕（按需）

字幕有三个值得推敲的维度：**分块**（每行 1/2/3/整句）、**大小写**（全大写/首字母大写/自然）、**位置**（距底部边距）。正确的组合取决于内容。

**已验证风格** — 选择、改编或自创：

**`bold-overlay`** — 短篇科技发布、快节奏社交。2 词分块、全大写、标点断句、Helvetica 18 Bold、白色描边、`MarginV=35`。`render.py` 以此作为 `SUB_FORCE_STYLE` 内置。

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=35
```

**`natural-sentence`**（如果你发明这种模式）— 叙事、纪录片、教育。4–7 词分块、句首大写、自然停顿断句、`MarginV=60–80`、更大字号以便阅读、稍宽的最大宽度。无内置 force_style — 需要时自己设计。

如果两种都不合适，发明第三种风格。硬规则：字幕最后（规则 1）、输出时间线偏移（规则 5）。

## 动画（按需）

动画匹配内容和品牌。**从对话中获取调色板、字体和视觉语言** — 绝不假设默认值。如果用户没有告诉你，在策略阶段提出一个调色板并等待确认再构建任何东西。

**工具选项：**

按动画插槽选择引擎。不要仅仅因为动画是 web 相关的就默认用 Remotion。

- **HyperFrames** — 浏览器原生 HTML/CSS/GSAP 视频合成：产品 UI 动效、网站转视频或原型转视频捕捉、动态排版、落地页/故事板宣传片、数据驱动的 UI 状态、透明 WebM 叠层，以及需要确定性帧捕获和 HyperFrames lint/validate/render 检查的片段。当动画应该像 web 合成一样创作和验证，而非 React 组件树时最佳。
- **Remotion** — 带组件状态的 React/CSS 合成、可复用 React 原语或已有的 Remotion 品牌系统。当用户明确要求 React/Remotion 或 React 合成是更简单的创作模型时最佳。
- **Manim** — 形式化图表、状态机、公式推导、图形变换。阅读 `skills/manim-video/SKILL.md` 及其参考文献深入了解。
- **PIL + PNG 序列 + ffmpeg** — 简单叠层卡片：计数器、打字机文字、单条进度条揭示、渐进式绘制。迭代快，任意美学。某发布视频使用此方案。

对于 HyperFrames 插槽，在 `edit/animations/slot_<id>/` 内用 `npx --yes hyperframes init . --example blank --non-interactive --skip-skills` 脚手架搭建，在此构建 HTML 合成，运行适合的 HyperFrames 检查（`lint`、`validate`，可行时做草稿渲染），然后用 `npx --yes hyperframes render . -o render.mp4` 或需要 alpha 时用 `--format webm -o render.webm` 生成最终叠层视频。EDL overlay `file` 指向实际渲染路径。

对于 Remotion 插槽，保持 Remotion 项目隔离在同一插槽目录内，用 `npx create-video@latest` 脚手架搭建或在本地安装 Remotion，用项目本地的 `remotion render` 命令渲染合成到 `render.mp4`，并用 `ffprobe` 验证时长和尺寸。

没有哪个是必须的。有用的话可以发明混合方案（例如 PIL 背景上加 HyperFrames 或 Remotion 层）。

**时长经验法则，依赖上下文：**

- **对齐旁白的解释。** 观众需要以 1 倍速解析内容。简单卡片大约下限 3s，通常 5–7s，复杂图表 8–14s。某发布视频中简单卡片为 5–7s。
- **节拍同步强调**（MV、快节奏混剪）。0.5–2s 即可 — 它们是视觉强调，不是信息。1 倍速可读规则变成"1 倍速可辨识"，而非"完全可解析"。
- **最后一帧保持 ≥ 1s** 再切（通用）。
- **覆盖旁白时：** 总时长 ≥ `旁白长度 + 1s`（通用）。
- **绝不并行揭示独立元素** — 眼睛无法同时追踪两个新事物。一个、暂停、下一个。

**动画高潮时机（对齐旁白的规则）：** 获取高潮词的精确时间戳。将叠层提前 `reveal_duration` 秒启动，使落定帧与说出高潮词瞬间重合。没有这种同步，动画会感觉脱节。

**缓动**（通用 — 绝不用 `linear`，看起来像机器人）：

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` 用于单个揭示（缓慢落定）。`ease_in_out_cubic` 用于连续绘制。

**打字文字锚点技巧：** 以完整字符串的宽度居中，而非部分字符串宽度 — 否则文字会在揭示时向左滑动。

**示例调色板**（某发布视频 — 无限美学中的一种）：
- 背景 `(10, 10, 10)` 近黑
- 强调色 `#FF5A00` / `(255, 90, 0)` 橙色
- 标签 `(110, 110, 110)` 暗灰
- 字体：Menlo Bold 在 `/System/Library/Fonts/Menlo.ttc`（索引 1）
- ≤ 2 个强调色，~40% 留白，极简装饰
- 效果：终端 / 复古科技感

这是一种风格。如果品牌是温暖衬线，用那个。如果品牌是多彩活泼，用那个。如果用户给了你风格指南，遵循它。如果没给，提出一个并确认。

**并行子代理简报** — 每个动画是一个通过 `Agent` 工具生成的子代理。每个提示自包含（子代理无父级上下文）。包括：

1. 一句话目标：*"构建一个动画：[规格]。其他什么都不要。"*
2. 绝对输出路径（`<edit>/animations/slot_<id>/render.mp4`）
3. 精确技术规格：分辨率、fps、编解码器、pix_fmt、CRF、时长
4. 风格调色板为具体数值（RGB 元组、hex 或设计系统引用）
5. 字体路径带索引
6. 逐帧时间线（何时发生什么，带缓动）
7. 排除清单（"无装饰、无额外元素、除非指定否则无标题"）
8. 代码模式参考（内联复制 helpers，不要跨插槽导入）
9. 交付清单（脚本、渲染、ffprobe 验证时长、报告）
10. **"不要提问。如果有任何歧义，选择最明显的解读并继续。"**

一个子代理 = 一个文件（唯一文件名，并行代理不会互相覆盖）。

## 输出规格

匹配源素材，除非用户有特殊要求。常见目标：`1920×1080@24` 电影感、`1920×1080@30` 屏幕内容、`1080×1920@30` 竖屏社交、`3840×2160@24` 4K 影院、`1080×1080@30` 方形。`render.py` 默认从任意源缩放到 1080p；传递 `--filter` 或编辑提取命令实现其他目标。值得问用户哪种交付格式重要。

## EDL 格式

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "钩子", "quote": "...", "reason": "最干净的表达，在 38.46 的失误前停止。"},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "解决方案", "quote": "...", "reason": "唯一没有嘴瓢的 take。"}
  ],
  "bgm": {
    "file": "edit/bgm.mp3",
    "start_offset": 2.5,
    "volume": 0.3,
    "duck_voiceover": true,
    "fade_in": 2.0,
    "fade_out": 3.0
  },
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade` 是预设名称或原始 ffmpeg 滤镜。`overlays` 是渲染好的动画片段。`subtitles` 可选且最后应用。`bgm` 可选 — 存在时，BGM 在合成后、loudnorm 前混入音频。

## 记忆 — `project.md`

每次会话在 `<edit>/project.md` 追加一个段落：

```markdown
## 会话 N — YYYY-MM-DD

**策略：** 一段话描述方案
**决策：** take 选择、剪切、调色、动画 + 原因
**推理日志：** 非常规决策的一行理由
**待办：** 推迟事项
```

启动时，如果 `project.md` 存在，读取它并用一句话总结上次会话，然后询问是否继续。

## 反模式

无论什么风格都持续失败的做法：

- **带可用性/语气标签/镜头层级的层级预计算编解码格式。** 过度工程。在决策时从转录中推导。
- **手工调参的时刻评分函数。** LLM 的选择优于你写的任何启发式。
- **Whisper SRT / 短语级输出。** 丢失亚秒级间隙数据。始终用词级逐字。
- **在 CPU 上本地运行 Whisper。** 慢且标准化填充词。使用托管 Scribe。
- **在合成叠层之前将字幕烧录到基础层。** 叠层遮住字幕。（硬规则 1。）
- **有叠层时使用单次滤镜图。** 双重编码。使用逐段提取 → 拼接。
- **线性动画缓动。** 看起来像机器人。始终三次方。
- **段落边界硬切音频。** 可闻爆音。（硬规则 3。）
- **以部分字符串居中打字文字。** 文字增长时向左滑动。
- **多个动画串行子代理。** 始终并行。
- **确认策略前剪辑。** 绝不。
- **重新转录已缓存的源。** 不可变输入的不可变输出。
- **假设这是什么类型的视频。** 先看、再问、最后剪。
