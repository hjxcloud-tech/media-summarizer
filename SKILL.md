---
name: media-summarizer
description: 媒体内容（视频/播客/访谈）总结工具，优先直取官方转录稿/文字稿。触发词：总结视频、B站总结、YouTube总结、小宇宙播客总结、播客笔记、视频摘要、视频笔记。四阶段流程：获取原文（官方转录稿＞平台字幕＞STT）→评论提取→AI总结→生成Obsidian笔记；播客/访谈按「核心观点+对谈精编（校对整理）」单文件框架，视频总结稿底部附校对整理的字幕原文，均不单独生成原稿文件。
---

# 媒体总结器

基于 yt-dlp 字幕提取 + Claude 智能总结，将 B 站、YouTube 长视频和小宇宙播客等媒体内容转化为结构化 Obsidian 笔记（**单文件交付**：原文校对、整理后并入笔记，不单独生成原稿文件）。**优先直取官方转录稿/文字稿，其次才是平台字幕与本地转录。**

## 核心能力

- **三平台支持**：Bilibili、YouTube 视频与小宇宙播客均可处理
- **一键流程**：`run_pipeline.py` 串起字幕/转录、评论、画面 OCR，输出结构化 JSON 摘要
- **字幕自动提取**：yt-dlp 下载 CC 字幕/AI 字幕，无需下载视频文件。YouTube 自动字幕覆盖率极高，多数视频无需 STT 降级
- **智能语言选择**：自动优选中文手动字幕 → 中文自动字幕 → 其他语言（YouTube 增加英文自动字幕回退）
- **文本清洗**：去除时间戳、HTML 标签、重复行，合并零碎语句
- **画面文字识别**：下载低清视频 → 关键帧提取 → RapidOCR 识别图表/PPT 中的文字，整合进关键观点（纯音频内容自动跳过）
- **数据/演示关键帧（文稿倒推）**：视频类总结默认**按讲稿倒推**——从带时间戳字幕中识别「这张图/公式/数据」等画面线索 → 在对应时刻定点抓帧 → OCR 验证（屏幕无文字则丢弃），无线索回退全帧 OCR；图片导出至 `attachments/<视频ID>_<HHMMSS>.png`（默认上限 12 张），AI 以 `![[文件名.png]]` 插入「字幕原文（校对整理版）」对应时间段（`--no-keyframes` 关闭）
- **结构化输出（双框架）**：
  - **播客/访谈类**：仿照**「核心观点 + 对谈精编」**框架（参考 `D:\Software\Obsidian\3Blue1Brown 对谈 Jane Street：深度学习如何成为系统化交易的最大驱动力.md`）——TL;DR 形式的核心观点 + 按主题分节、标明发言人的对谈精编，**必须经过校对、整理**
  - **视频类**：原有总结框架（核心摘要/关键客观数据/关键观点/时间线/金句/社区讨论），字幕原文**校对、整理后附于总结稿下方同一文件**
- **评论区提取**：自动抓取热门评论和创作者互动，总结社区情绪与核心议题
- **语音转文本降级**：无字幕时自动触发，下载音频轨用本地 Whisper 转录（小宇宙播客必走此路径）
- **单文件交付**：字幕/转录原文一律**校对、整理后并入总结笔记同一文件**，不单独生成原稿文件

## 使用流程

### 原文稿优先级（执行任何转录前必查）

**顺序：官方转录稿/文字稿 ＞ 平台字幕 ＞ 本地 STT**——先在网上找 transcript 稿，再决定要不要转录（实测：直取稿质量更高、省数小时 CPU；Sam Altman 集 podhood 官方稿 103K 字符、张小珺152 走 scripod API 6.1 万字，均无需本地 Whisper）：

1. **官方转录稿**：
   - 英文播客：节目官方托管站（如 podhood，episode 页 URL 加 `.md` 即全文 Markdown 转录）；转录站 podscripts.co / scripod.com 的收录页
   - scripod.com：单集转录正文需登录，**公开 API** 为 `https://scripod.com/api/transcript/<episode-id>`（返回 JSON：`segments[].speaker/.sentences[]`，句级时间戳；说话人分离经常缺失，需按内容推断），登录 cookie 由用户浏览器扩展导出（JSON 格式：顶层 `{url, cookies:[{name,value,domain,path,secure,httpOnly}]}`）
2. **媒体官方文字稿**：中文商业播客常与媒体合作发布对谈整理稿（硅谷101→界面新闻/钛媒体等），搜索「节目名+期号+文字稿」命中，全文即「对谈精编」底稿
3. 以上均无 → 走管线（平台字幕 → Whisper STT 降级）

> **校对基准（非底稿）**：若只找到节目**同题的非逐字媒体文章**（如晚点聊单集对应《晚点 LatePost》公众号 / 腾讯新闻的同题整理稿），它不足以充当「对谈精编」底稿，但**必须用作 STT 校对基准**——人名、公司名、英文术语与关键数据一律以该文为准。实测（晚点聊 148 期）：small 模型输出为繁体且同音误识别密集（「巨生智能/段道端/现数/零销手」），靠同题文章校回「具身智能/端到端/线束/灵巧手」等，并在笔记「转录说明」中附对照表。

### 平台字幕完整性校验（B 站必读，2026-09-29 固化）

**B 站 AI 字幕不可盲信**，实测两类故障（同一视频在不同时刻表现不同，属间歇性）：

| 故障 | 现象 | 实测样本 |
|---|---|---|
| **串片/错轨** | 取回的字幕整段是**别的视频**的内容（`player/v2` 列出的 `ai-zh` 轨，`aisubtitle.hdslb.com` 正文与本片无关）；yt-dlp 与 API 两条路径都可能中招 | 海思 CPO 演讲取到"烧烤探店"；VCSEL-CPO 取到"健身五分化"；均多次重试稳定复现，隔数小时后同片又恢复正常 |
| **截断** | 字幕只返回开头一段（35:34 的片子只给到 04:46；末句时间 ÷ 时长 < 0.85） | 同批次实测 |
| **混拼** | 末句时间**超出**本片时长（coverage>1.15） | coverage=1.71 样本 |

`extract_subs.py` 已内置**双判据门禁**（`subtitle_gate`），不合格即弃用字幕、自动降级 STT：

1. **内容相关性**：从标题+简介抽内容词（拉丁 token + CJK 2/3-gram，去停用词），算其在字幕中的命中率。实测正确字幕 `0.29~0.51`，串片 `0.000~0.007`；阈值 `--min-relevance`（默认 0.20）。标题+简介词项 < 8 时不判此项（避免误杀）。
2. **时间覆盖率**：末句时间 ÷ 视频时长，须落在 `0.85~1.15`（`COVERAGE_MIN/MAX`）。
3. 判定结果打印 **`[SUB_GATE] relevance=… coverage=… verdict=ok|reject reason=…`**；两路（yt-dlp 与 API）都过同一门禁。
4. 另有 **API 直取回退**：yt-dlp `--list-subs` 只列 danmaku 时（B 站 AI 字幕常见表现），自动用 `x/web-interface/view` + `x/player/v2` 取轨（需 `--cookies` 登录态）；`--no-bili-api` 关闭。
5. 调试需强行保留被判不合格的字幕：`--force-subtitle`（**仅用于人工核对，不得直接进总结**）。

> 交付前若 `[SUB_GATE] verdict=reject`，说明本次正文来自 STT；此时**必须**在笔记「转录说明」写"基于语音转录（xx 模型），术语可能存在误差"，并逐处标注「（转录存疑 @mm:ss）」。

### 方式一：一键流程（推荐）


统一入口 `scripts/run_pipeline.py` 串起 字幕/转录 → 评论 → 画面 OCR 三步，输出结构化 JSON 摘要。

```bash
python "06 skill&agent/media-summarizer/scripts/run_pipeline.py" "<URL>" \
    [--cookies <cookies.txt>] [--output-dir <目录>] [--no-ocr] [--no-comments] \
    [--keep-files] [--stt-model <base|small>] [--keyframes-dir <目录>] [--max-keyframes 12] [--no-keyframes] \
    [--no-bili-api] [--min-relevance 0.20] [--force-subtitle]
```

**退出码**：`0` 正常；`1` URL/依赖问题；`3` **正文（字幕+转录）缺失**——此时 JSON `body_missing=true`，不得据标题简介臆造总结，须先修字幕/STT 再重跑。

执行顺序与降级：
1. 校验 URL（bilibili.com / b23.tv / youtube.com / youtu.be / xiaoyuzhoufm.com）。
2. 获取元数据（yt-dlp `--dump-json`）：title / uploader / upload_date / duration。**小宇宙**的 yt-dlp 通用提取器拿不到日期/时长/播客名，自动解析单集页面 JSON-LD 补充（`publish_date` / `duration` / 播客名）。
3. 运行 `extract_subs.py` → 字幕文本（无字幕自动降级 STT 转录；小宇宙无字幕必走 Whisper 转录）。
4. 运行 `extract_comments.py` → 评论产物（失败不阻塞主流程；**小宇宙无公开评论接口自动跳过**）。
5. 运行 `extract_frames.py` → OCR 画面文字 + **数据/演示关键帧导出**。**检测到 ffmpeg 即自动执行，缺失则跳过并提示**；`--no-ocr` 显式关闭；**纯音频内容（播客）自动跳过**。关键帧默认**文稿倒推**（读 `extract_subs` 生成的 `<字幕输出>.ts.txt` 时间戳行 → 画面线索 → 定点抓帧 → OCR 验证；无线索回退全帧），命名 `<视频ID>_<HHMMSS>.png`，导出至 vault 根 `attachments/`（`--keyframes-dir` 改目录，`--no-keyframes` 关闭，`--max-keyframes` 限量，默认 12）——图片为交付物，不随临时目录清理。
6. **不单独生成原文文件**：字幕/转录全文内嵌 JSON `contents` 字段（`transcript_type` 区分 subtitle/stt），由 AI 校对整理后**并入总结笔记同一文件**（播客/访谈按「核心观点+对谈精编」框架，视频附于总结稿底部）；关键帧清单在 JSON `keyframes` 字段（`[{path, timestamp_sec, ocr_summary}]`）。
7. **自动清理临时目录**（`--keep-files` 可保留调试；转录失败需恢复时自动跳过清理并打印恢复/清理命令）。

输出 JSON 摘要（stdout）：

```json
{
  "metadata": {"title": "...", "uploader": "...", "publish_date": "YYYY-MM-DD", "duration": 4140, "url": "..."},
  "artifacts": {"subtitle": "path", "transcript": "path|null", "comments": "path|null", "frames": "path|null"},
  "contents": {"subtitle": "全文", "transcript": "全文|null", "comments": "全文|null", "frames": "全文|null"},
  "transcript_type": "subtitle|stt",
  "keyframes": [{"path": "attachments/BVxxx_001234.png", "timestamp_sec": 754, "ocr_summary": "画面文字要点"}],
  "errors": [],
  "body_missing": false,
  "output_dir": "...",
  "cleaned": true
}
```

- `artifacts` 为各产物文件路径（绝对路径，清理后路径失效）；**`contents` 为各产物文件全文**，AI 总结直接读取该字段即可，无需再访问磁盘文件。
- `transcript_type` 区分原生字幕与语音转录；步骤失败记入 `errors` 字段，不中断整个流程。
- **`body_missing=true` 时管线以退出码 3 结束**（正文是总结的唯一依据，缺失即"无交付"，不得按成功处理）。
- **管线运行结束自动清理整个临时目录**（保留 `cookies*.txt`）；`cleaned` 字段标记是否已清理。仅当正文（字幕/转录）缺失且残留音频（恢复模式）或指定 `--keep-files` 时保留文件。

### 方式二：分步执行（进阶/调试）

各脚本可单独调用，便于调试或跳过某个环节。以下脚本均自动输出 `[OUTPUT_PATH] <路径>` 标记供程序解析，且运行中产生的瞬时文件（原始字幕/评论 info.json/低清视频/帧/音频）均**自动清理**（调试 flag 如 `--keep-raw`/`--keep-video`/`--keep-frames`/`--keep-audio` 可保留）。

#### 阶段 1：字幕提取

```bash
python "06 skill&agent/media-summarizer/scripts/extract_subs.py" "<视频URL>" --cookies <cookies.txt> -o "<临时文件.txt>"
```

脚本自动完成：① 验证链接 → ② 列出可用字幕 → ③ 选择最佳语言 → ④ 下载 → ⑤ 清洗 → ⑥ 输出纯文本

**平台差异**：
| 特性 | Bilibili | YouTube | 小宇宙 |
|------|----------|---------|--------|
| 内容形态 | 视频 | 视频 | **纯音频播客** |
| 字幕获取 | AI 字幕需登录（Cookie） | 无需登录 | 无字幕 → 必走 Whisper 转录 |
| 评论区 | 需登录 | 无需登录 | **无公开评论接口**（自动跳过） |
| 画面 OCR | 可用 | 可用 | **无画面**（自动跳过） |
| 元数据 | yt-dlp 完整 | yt-dlp 完整 | yt-dlp 无日期/时长，自动解析页面 JSON-LD 补充 |
| 下载速度 | 国内直连快 | 可能需代理 | 国内直连快 |

**认证方式**（仅 B 站需要，YouTube 无需认证）：
| 参数 | 说明 |
|------|------|
| `--cookies <file>` | Netscape 格式 Cookie 文件（B站推荐） |
| `--cookies-from <browser>` | 从浏览器读取 Cookie（B站备选，需关闭浏览器） |
| `-u` / `-p` | B站不支持密码登录，忽略此选项 |

#### 阶段 1.5：评论提取

```bash
python "06 skill&agent/media-summarizer/scripts/extract_comments.py" "<视频URL>" --cookies <cookies.txt> --output-dir <临时目录> -o "<评论输出.md>"
```

提取热门评论（按点赞排序）、创作者回复。YouTube 评论无需 Cookie 即可提取。`-o` 可选，写入格式化评论文件并输出 `[OUTPUT_PATH]`。

**失败处理**：
- 无可用字幕 → **自动降级到阶段 1b（语音转文本）**，下载音频轨并用本地 Whisper 转录
- yt-dlp 报错 → 检查链接有效性，确认网络正常

#### 阶段 1.6：画面文字提取（OCR）

```bash
python "06 skill&agent/media-summarizer/scripts/extract_frames.py" "<视频URL>" --cookies <cookies.txt> -o "<临时文件.txt>"
```

脚本自动完成（默认**文稿倒推模式**）：读带时间戳字幕（`<字幕输出>.ts.txt`）→ 找「这张图/公式/数据」等画面线索 → 对应时刻定点抓帧（960px）→ RapidOCR 校验（屏幕无文字丢弃）→ 数据/演示关键帧导出至 `attachments/`；无线索时回退**全帧模式**：① 下载低清视频(≤360p) → ② ffmpeg 每 8 秒提取关键帧 → ③ RapidOCR 中文文字识别 → ④ 跨帧去重 → ⑤ 输出带时间戳的画面文字 → ⑥ 自动清理视频和帧文件

- 补充字幕/语音无法覆盖的**图表标注、PPT 文字、演示内容**
- 首次运行需安装: `pip install rapidocr-onnxruntime` + `pip install imageio-ffmpeg`
- 26 分钟视频约需下载 30-50MB 低清视频 + 2-3 分钟 OCR 处理
- OCR 结果有噪声（如误识别的背景文字），AI 总结时需交叉验证

#### 阶段 1b：语音转文本（无字幕时自动触发）

```bash
python "06 skill&agent/media-summarizer/scripts/transcribe.py" "<视频URL>" --cookies <cookies.txt> -o "<输出.txt>" -m base
```

脚本自动完成：① 下载视频音频轨（m4a/mp4/webm/opus/aac/mp3）→ ② faster-whisper 转录 → ③ 输出纯文本 → ④ 自动清理音频文件

- 首次运行需下载 Whisper 模型 (~150MB base / ~500MB small)，使用 hf-mirror.com 镜像
- CPU 转录速度约 5-10x 实时（26 分钟视频约 3-5 分钟）
- 转录质量低于原生字幕，关键术语可能有误，建议在备注中标注
- **长音频默认分片转录**：`--chunk-minutes <分钟>`（默认 20）用 ffmpeg 无损切分（`-c copy`）后逐片转录，再按 `+ 片序 × 分片时长` 回填全局时间戳；`--no-chunk` 关闭。原因：整段长音频的 mel 频谱数组会一次性吃满内存（实测 85 分钟节目在本机 15.7GB 内存下报 `_ArrayMemoryError: Unable to allocate 1.52 GiB`），分片后单次峰值大幅下降。分片音视频源文件在转录结束后统一清理

### 阶段 2：AI 总结

提取成功后，读取管线 JSON 摘要的 `contents` 字段（字幕/转录 + 画面OCR + 评论全文），按以下结构生成详细总结：

```markdown
## 核心摘要（≤300字）
概括视频核心内容、论证主线、最终结论

## 关键客观数据（表格，必填）

将视频中的**硬数据**汇总为表格，三列：类别 / 指标 / 数值 / 口径备注。
与关键观点的区别：这里是"是什么"，**不含观点判断**；口径差异必须并列标注
（归母 vs 扣非、整体 vs 分部毛利率、静态/TTM/前瞻 PE、出量与金额口径等）。

## 关键观点（≥5条，每条含详细论证）

对每个观点，遵循"论点 → 论据链 → 结论"结构：

1. **观点主题**：一句话概括
   - 支撑论据 1（引用视频中的具体数据、案例或推理）
   - 支撑论据 2
   - 支撑论据 3
   - 若有反方观点或局限，一并记录

记录原则：
- 视频中出现的**具体数字**必须记录（百分比、价格、时间、容量等）
- 视频中提到的**具体产品/公司/人物**必须记录
- 视频中的**论证逻辑链**完整保留（"因为A所以B，而B导致C"）
- UP 主明确表达的**个人判断和预测**单独标注

## 作者分析过程（如有，必填）

概括作者的分析框架与推理链：按什么顺序拆解（如"业绩→护城河→现金流→
资产负债表→估值→资本动作"）、每步论证逻辑（"因为 A 所以 B"）、多方/空方
如何交锋与让步。若为纯口播观点类视频无明确框架，标注"无明确分析框架"。

## 内容时间线

按 2-3 分钟粒度分段，记录每段的核心议程和关键信息：

| 时间段 | 主题 | 关键信息 |
|--------|------|----------|
| 00:00-03:00 | 问题引入 | ... |

若视频为多P合并，在每个 P 的开头时间点标注。

## 金句摘录

- "原文引用" —— 标注大致时间点

## 💬 社区讨论

**禁止逐条罗列评论。** 必须总结为以下结构：

### 核心议题
概括评论区讨论的几个主要方向（2-4 个），每个方向用一段话总结代表性观点和论据。

### 共识与分歧
- 共识：大多数评论认同的观点
- 分歧：评论中存在明显对立的问题

### UP 主互动
UP 主在评论区的回复要点（如有）。

### 情绪基调
整体评论情绪（乐观/悲观/务实/娱乐化等），一两句话概括。
```

**写作原则**：
- 忠实原文，不凭空添加观点
- 关键数据、人名、术语必须与字幕一致
- 保留视频中的论证逻辑链
- 引文用双引号标注，可定位到原文
- **详细优先**：宁可多记不可遗漏。数据、案例、推理步骤必须记录
- **去口语化**：关键观点用正式书面语重述，不使用 UP 主的原文口语风格。有保留价值的 UP 主原话仅放入金句
- **画面 OCR 整合**：OCR 提取的文字信息整合进关键观点中，不单独成章。若视频为纯口播无画面信息，直接省略
- **关键帧嵌入（视频类）**：管线 JSON `keyframes` 非空时，把图片 `![[<视频ID>_<HHMMSS>.png]]` + 一行画面说明（`*画面 @mm:ss：…*`）**插入文末「📄 字幕原文（校对整理版）」中时间上对应的那个时间段小节**（按 `### mm:ss` 小节起始时间归档——说话提到该图/公式/数据的那一段即归属处）；关键观点/数据表区不散插图片（用文字叙述即可）。图片原件统一在 vault `attachments/`，不复制进笔记目录

**内容框架分流（重要）**——按内容形态选框架，与视频/播客分开处理：

| 内容形态 | 笔记框架 | 原文（校对整理后）去向 |
|---------|---------|----------------------|
| **播客/访谈类**（小宇宙播客、YouTube 对谈/访谈、圆桌讨论等多人对话形式） | **「核心观点 + 对谈精编」**：`## 核心观点`（TL;DR 形式，5-9 条，每条一句话概括+要点展开，参考 `3Blue1Brown 对谈 Jane Street` 笔记的 `## TL;DR`）+ `## 对谈精编`（按主题 `###` 分节，对话用 `**发言人：**` 标注，整理成连贯可读的对谈） | 对谈精编即校对整理后的原文，**不需要再附原始转录全文** |
| **视频类**（单人口播/课程/讲解，非对话访谈） | 原有总结框架（核心摘要/关键客观数据表/关键观点/作者分析过程/内容时间线/金句/社区讨论） | **`## 📄 字幕原文（校对整理版）`** 附于总结稿底部同一文件 |

**原文校对、整理规范（两类均适用，**必须执行**）**：
- 将 STT 转录/字幕逐句清洗为连贯文本：合并零碎短句、规范标点与分段、删重
- **修正明显同音误识别**（依上下文推断，如「遇训」→「预训练」、「吃饺」→「支教」）；无法确认处保留原样并标注「（转录存疑）」
- **存疑处/关键引文标注时间 tag**：STT/官方转录稿行首的 `[mm:ss]`（或 `[hh:mm:ss]`）为段级时间戳，存疑标注写成「（转录存疑 @22:15）」，让用户可以跳转到对应时刻自行核对；对谈精编每节可标注起止时间段
- 访谈/对话明确标注发言人（主持人/嘉宾），按主题逻辑组织，非机械逐句照搬
- 关键数据、人名、术语须与转录一致，**不得虚构**、不得补写原文没有的内容
- 基于 STT 的在笔记中注明「基于语音转录（xx 模型），术语可能存在误差」

### 阶段 3：生成笔记

使用 `templates/summary-note.md` 模板，填充元数据和总结内容，保存到 `01 inbox/视频总结/`。

> **入库后的源文件落位（ingest-router §1c-2，2026-09-11 用户定案）**：播客/视频总结稿属"无项目归属的通用知识/观点类资料"（如世界模型、具身智能等主题长访谈），**经 RAG 入库后须移动至 `03 area/<主题>/`**（按主导主题判定，一篇只落一个主题目录）——顺序是**先移动文件、再执行入库**（`file_registry.json` 以移动后的新路径为键）；已入库后再移动时，只需改注册表路径键（+ 知识卡 `source`），无需重新嵌入（Chroma/BM25 按文件名索引）。本目录 `01 inbox/视频总结/` 只是**生成时的落地目录**。

**笔记生成（单文件交付）**：
- **播客/访谈类**：使用 `templates/interview-podcast-note.md` 模板——核心观点（TL;DR）+ 对谈精编（按主题分节、标注发言人），原文已融入精编，**不再单独生成原稿文件**
- **视频类**：使用 `templates/summary-note.md` 模板，并在文末追加 `## 📄 字幕原文（校对整理版）` 一节（见"校对、整理规范"），**不单独生成原稿文件**

**文件命名规范（必须遵守）**：文件名必须以内容发布日期为前缀，便于按时间排序：

```
YYYY-MM-DD-标题-BV号.md
```

- 日期取自 `publish_date`（B站/YouTube 为 yt-dlp 的 `upload_date`；小宇宙为页面 `datePublished`），统一为 `YYYY-MM-DD` 格式
- 示例：`2026-08-06-大宗商品之王——铜-BV1QNup66EE6.md`
- YouTube 视频：`YYYY-MM-DD-标题-视频ID.md`
- 小宇宙播客（无 BV 号）：`YYYY-MM-DD-标题-小宇宙.md`
- 本地音频（无 BV 号）：`YYYY-MM-DD-标题-本地音频.md`
- 笔记 frontmatter 的 `publish_date` 必须为 `YYYY-MM-DD` 格式

### 阶段 4：清理缓存

**管线方式无需手动清理**——`run_pipeline.py` 运行结束时已自动删除整个临时目录（保留 `cookies*.txt`），产物全文已内嵌在 JSON `contents` 字段中。分步执行各脚本也会自动清理自身产生的瞬时文件：

- `extract_subs.py`：清洗后自动删除原始字幕（`subs/*.vtt/.srt/.ass/.json/.lrc`）
- `extract_comments.py`：解析后自动删除原始 `*.info.json`（含全量评论，体积可达数 MB；`--keep-raw` 保留）
- `extract_frames.py`：无论成功/失败均自动清理低清视频与帧（`--keep-video` / `--keep-frames` 保留）
- `transcribe.py`：转录成功/失败均自动清理音频（`--keep-audio` 保留；`--local-audio` 指向用户文件绝不删除）

仅以下情况需要手动清理（调用 `_common.cleanup_temp` 规则）：

```bash
python -c "import sys; sys.path.insert(0, '06 skill&agent/media-summarizer/scripts'); from _common import cleanup_temp; cleanup_temp('<输出目录>')"
```

- 分步执行后留下的最终文本产物（如 `subtitle.txt`、`ocr_*.txt`、`comments.md`）
- 管线恢复模式保留的临时目录（转录失败需 `--local-audio` 重转时，管线会打印本清理命令）
- 删除：文本/音频/视频/帧/原始字幕等瞬时文件与 `*.info.json`；保留：`cookies*.txt` 与 `comments.*`

## 常见故障与恢复

> 改动 `extract_subs.py` / `transcribe.py` / `run_pipeline.py` 后**先跑自检**：
> `python "06 skill&agent/media-summarizer/scripts/selftest_gate.py"`（离线 11 项：字幕门禁四态、`--force-subtitle`、HF 缓存回退、空产物退出码）；
> 加 `--online --cookies <txt> --bvid BVxxxx` 追加真实视频端到端（正文缺失必须非零退出）。自检不绿不得交付笔记。

| 症状 | 原因 | 处理 |
|------|------|------|
| 管线退出码 3、JSON `body_missing=true`（或 `contents.frames` 为 null 而 `errors` 含 `extract_frames`） | 正文（字幕+转录）全取不到＝无交付；或仅 OCR 失败 | 正文问题必须按下方"转录失败恢复"修复后**重跑管线**，不得据标题/简介臆造；仅 OCR 失败可单独补跑 `extract_frames.py` |
| 取回的字幕内容与视频无关（**串片**），或只覆盖开头一小段（**截断**） | B 站 `aisubtitle.hdslb.com` 间歇性返回别的视频的字幕/截断正文（yt-dlp 与 player API 两路都会遇到） | `extract_subs.py` 的 `[SUB_GATE]` 双判据已自动弃用并降级 STT；若需强行保留用 `--force-subtitle`（**仅限人工核对，不得直接进总结**）；判据依据见「平台字幕完整性校验」 |
| STT 报 `LocalEntryNotFoundError` / 模型加载失败 | `HF_HOME` 指向空目录或网络不可达，模型实际缓存在别处（如 `~/.cache/huggingface/hub`） | 已内置自动回退：`transcribe.py` 探测候选缓存根并以 `download_root + local_files_only` 加载本地快照，失败再走 hf-mirror 在线下载。仍失败则确认模型已缓存（`tiny/base/small` 均在 `models--Systran--faster-whisper-*`）或临时联网 |
| 转录文件存在但内容为空、脚本却"成功" | 历史缺陷（空产物未拦截） | 已修：空/过小产物 → `transcribe.py` 退出码 2 且不打印 `[OUTPUT_PATH]`；`run_pipeline.py` 把 <16 字节正文按失败处理并以 3 退出 |
| STT 转录超时（`TimeoutExpired`） | 长视频 + 多任务并行抢 CPU，base 模型实际慢于预期 | 串行执行；`--stt-timeout <秒>` 调大；或用 `--local-audio` 对已下载音频重试（此时管线自动保留临时目录，恢复完成后按打印的命令清理） |
| STT 报 `numpy._core._exceptions._ArrayMemoryError`（长音频） | 整段音频的 mel 频谱数组超出可用内存 | 分片转录已默认开启（`--chunk-minutes 20`）；仍报错则调小分片，或手动切片后逐片 `--local-audio` 运行（逐片产物可断点续做，已生成的 `part*.txt` 直接跳过） |
| 批量总结多个视频时多个转录互相拖垮 | 同时跑多个 Whisper 进程，CPU 争抢 | **逐个串行**运行管线，同一时刻只保留一个转录进程 |
| OCR 步骤超时（`>1800s`） | 长视频帧数多 | 单独补跑 `extract_frames.py`（进程被杀时帧/视频保留在 `frames_work/`，可复用） |

### 转录失败恢复（重要）

管线默认"步骤失败不中断"，转录失败时评论/OCR 可能仍完成，但缺正文。此时管线**自动跳过清理**并打印恢复提示，临时目录保留以供恢复。恢复步骤：

1. 检查管线工作目录是否残留音频：`ls <output_dir>/*.m4a`
2. 若存在，直接用 `--local-audio` 串行转录（不重新下载、不并发）：
   ```bash
   python "06 skill&agent/media-summarizer/scripts/transcribe.py" \
     --local-audio "<残留音频.m4a>" -o "<output_dir>/subtitle.txt" -m base
   ```
3. 转录完成后按需补 OCR（`extract_frames.py`），再按阶段 2 生成总结；随后手动清理该临时目录（管线已打印清理命令，或调用 `_common.cleanup_temp`）。

## 硬性约束

```
NO SUBTITLES → AUTO STT FALLBACK  → IF STT FAILS → ASK USER
SUBTITLE GATE → B站字幕必过 [SUB_GATE] 双判据（相关性≥阈值、覆盖率 0.85~1.15）→ reject 即弃用并降级 STT
EMPTY TRANSCRIPT → 正文缺失即失败：transcribe.py 退出码 2 / run_pipeline.py 退出码 3（body_missing=true）→ 禁止据标题简介臆造
PIPELINE END → AUTO CLEANUP TEMP FILES
NO COMMENT-BY-COMMENT LISTING → SUMMARIZE COMMUNITY THEMES
DETAIL FIRST → RECORD ALL DATA POINTS, ARGUMENT CHAINS, NAMES
FORMAL REWRITE → KEY POINTS IN WRITTEN LANGUAGE, NOT SPOKEN STYLE
NO SEPARATE OCR SECTION → INTEGRATE INTO KEY POINTS
NO MISSING DATA TABLE → KEY OBJECTIVE DATA MUST BE A TABLE WITH CALIBER NOTES
NO PROCESS SKIPPING → SUMMARIZE AUTHOR'S ANALYSIS CHAIN (OR STATE NONE)
NO PARALLEL STT → RUN BATCH TRANSCRIPTIONS SEQUENTIALLY
FILENAME DATE PREFIX → SAVE AS YYYY-MM-DD-TITLE-BVxxx.md (USE PUBLISH DATE)
TRANSCRIPT → PROOFREAD & MERGE INTO SAME NOTE → 播客/访谈: 核心观点+对谈精编框架；视频: 附于总结稿底部；不单独生成原稿文件
UNCERTAIN → TAG WITH TIMESTAMP (@mm:ss) → 存疑/关键引文标时间 tag 供用户自行核对
KEYFRAMES → 文稿倒推定点抓帧 → attachments/<视频ID>_<HHMMSS>.png → INSERT INTO 字幕原文对应时间段 (![[…]] + *画面 @mm:ss*；视频类；纯音频自动跳过)
```
- 字幕为空时，自动尝试语音转文本（阶段 1b）
- **B 站字幕（`ai-zh` 与手动轨）一律经 `[SUB_GATE]` 双判据**：相关性（标题+简介内容词命中率，阈值 `--min-relevance` 默认 0.20）+ 时间覆盖率（0.85~1.15）。串片/截断属 B 站侧**间歇性**故障，同一片不同时刻可能一次坏一次好，因此**不得**凭"上次取到过正确字幕"跳过校验；`verdict=reject` 的字幕严禁进入总结（`--force-subtitle` 仅供人工核对）
- **正文缺失必须失败退出**：`transcribe.py` 空/超小产物 → 退出码 2 且不打印 `[OUTPUT_PATH]`；`run_pipeline.py` → 退出码 3 + `body_missing=true`。历史缺陷是"exit 0 假成功"，差点据标题臆造整篇笔记
- **Whisper 模型加载自动探测本地 HF 缓存**（`HF_HUB_CACHE`/`HF_HOME`/默认用户缓存），`HF_HOME` 指向空目录不再致命
- 转录文本质量低于原生字幕，在备注中标注"基于语音转录"
- 不根据标题和简介自行推断内容
- 画面 OCR 为可选增强，无 ffmpeg 时跳过，结果整合进关键观点，不阻塞主流程
- **管线结束自动清理临时文件**（分步执行产生的瞬时文件由各脚本自动清理；仅恢复模式/`--keep-files` 保留）
- **禁止逐条罗列评论**，必须总结为社区议题、共识/分歧、情绪基调
- 总结**必须详细**：数据、论证链、具体命名不可省略
- **关键观点用正式书面语重述**，不保留 UP 主原文口语风格。有意义的原话放入金句
- **笔记 frontmatter 必须包含 `publish_date`**，格式统一为 `YYYY-MM-DD`
- **不设 📊 画面关键信息独立章节**，OCR 结果融入关键观点
- **笔记文件名必须以 `YYYY-MM-DD-` 发布日期前缀**（见"阶段 3：生成笔记"命名规范）
- **原文必须校对、整理后并入总结笔记同一文件，不单独生成原稿文件**：播客/访谈类按「核心观点 + 对谈精编」框架（精编即整理后的对谈原文）；视频类在总结稿底部附「📄 字幕原文（校对整理版）」

## 技能分工

| 组件 | 职责 |
|------|------|
| `scripts/run_pipeline.py` | 一键流程编排：字幕/转录→评论→OCR+关键帧导出（纯音频自动跳过后两者），小宇宙页面元数据补充，产物全文内嵌 JSON（AI 校对整理后并入笔记，不生成独立原文文件；关键帧清单在 `keyframes` 字段），**正文缺失时 `body_missing=true` 且退出码 3**，结束自动清理临时目录；透传 `--no-bili-api/--min-relevance/--force-subtitle` |
| `scripts/extract_frames.py` | 低清视频下载 + **文稿倒推关键帧**（字幕线索→定点抓帧→OCR 验证，`--transcript-file/--cue-gap`）或全帧 OCR 回退 + 数据/演示关键帧导出（`--keyframes-dir/--max-keyframes/--no-keyframes`）；成功/失败均自动清理视频/临时帧 |
| `scripts/_common.py` | 共享工具：URL 校验（含小宇宙/B站）、小宇宙页面 JSON-LD 元数据解析、依赖检测、ffmpeg 定位、Netscape Cookie 头、临时文件清理 |
| `scripts/extract_subs.py` | 字幕提取与清洗 + **B 站字幕完整性门禁 `[SUB_GATE]`**（内容相关性 + 时间覆盖率双判据，拦"串片/截断/混拼"）+ player API 直取回退（`--no-bili-api` 关闭）+ 自动 STT 降级 + 自动清理原始字幕 |
| `scripts/transcribe.py` | 音频下载（m4a/mp4/webm/opus/aac/mp3）+ 长音频自动分片 + Whisper 语音转文本（**自动探测本地 HF 缓存**，`HF_HOME` 指错可回退）+ **空产物退出码 2** + 成功/失败均自动清理音频 |
| `scripts/extract_comments.py` | 评论区抓取与分析 + 自动清理原始 info.json |
| `scripts/export_cookies.py` | 浏览器 Cookie 导出工具 |
| `scripts/selftest_gate.py` | **技能自检**：字幕门禁四态（正确/串片/截断/混拼）、`--force-subtitle` 放行、HF 缓存回退、空转录退出码 2、管线正文缺失退出码 3（`--online` 追加真实视频用例）；改脚本后必跑 |
| Claude | 文本理解、校对整理原文、双框架总结生成、对谈精编、笔记创建 |
| `templates/summary-note.md` | 视频类笔记结构模板（总结 + 底部字幕原文附录） |
| `templates/interview-podcast-note.md` | 播客/访谈类笔记结构模板（核心观点 + 对谈精编） |

## 依赖

安装全部依赖：

```bash
pip install -r "06 skill&agent/media-summarizer/requirements.txt"
```

| 包 | 用途 |
|------|------|
| yt-dlp | 视频元数据 / 字幕 / 音频 / 评论提取 |
| rapidocr-onnxruntime | 画面文字 OCR |
| faster-whisper | 语音转文本降级（无字幕时） |
| imageio-ffmpeg | ffmpeg 二进制（帧提取） |
| pywin32 | Windows 浏览器 Cookie 导出 |
