#!/usr/bin/env python3
"""
Bilibili 字幕提取与清洗工具
使用 yt-dlp 提取字幕，清洗为纯文本供 LLM 总结
"""

import argparse
import re
import subprocess
import sys
import json
import os
from pathlib import Path


# ─── 非字幕"语言"黑名单 ────────────────────────────────────────
# yt-dlp 可能将弹幕、特殊格式等误列为字幕语言
NON_SUBTITLE_LANGS = {"danmaku", "xml", "json", "raw"}


def run_ytdlp(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run yt-dlp with given arguments."""
    return subprocess.run(
        [sys.executable, "-m", "yt_dlp"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _add_cookies_arg(args: list[str], cookies_browser: str | None, cookies_file: str | None):
    """Add cookie-related arguments to the yt-dlp command line."""
    if cookies_file:
        args.extend(["--cookies", cookies_file])
    elif cookies_browser:
        args.extend(["--cookies-from-browser", cookies_browser])


def _add_auth_args(args: list[str], username: str | None, password: str | None):
    """Add username/password arguments to the yt-dlp command line."""
    if username:
        args.extend(["--username", username])
    if password:
        args.extend(["--password", password])


def list_subtitles(url: str, cookies_browser: str | None = None,
                   cookies_file: str | None = None,
                   username: str | None = None,
                   password: str | None = None) -> dict:
    """List available subtitles for a video.

    Returns dict with keys:
      - "manual": list of valid language codes
      - "auto": list of valid language codes
      - "need_login": bool, True if login is required for AI subs
    """
    args = ["--list-subs", "--no-download"]
    _add_cookies_arg(args, cookies_browser, cookies_file)
    _add_auth_args(args, username, password)
    args.append(url)

    result = run_ytdlp(args, timeout=30)

    subs = {"manual": [], "auto": [], "need_login": False}
    current_section = None  # None, "manual", "auto"

    # Check for login-required warning
    stderr_text = result.stderr if result.stderr else ""
    output_text = result.stdout if result.stdout else ""
    combined = stderr_text + output_text

    if "only available when logged in" in combined or "Subtitles are only available" in combined:
        subs["need_login"] = True

    if result.returncode != 0:
        print(f"[ERROR] yt-dlp 执行失败:\n{stderr_text}", file=sys.stderr)
        return subs

    # Parse the subtitle listing output
    # Format:
    #   Available subtitles for BV...:
    #   Language Formats
    #   zh-Hans  vtt, srt, ass
    #   en       vtt
    #
    #   Available automatic captions for BV...:
    #   Language Formats
    #   zh-Hans  vtt, srt, ass

    for line in output_text.splitlines():
        line = line.strip()

        # Detect section headers
        if "Available automatic captions" in line or "Available auto-generated" in line:
            current_section = "auto"
            continue
        if "Available subtitles" in line:
            current_section = "manual"
            continue

        # Skip metadata lines
        if not line or line.startswith("[") or line.startswith("has no"):
            continue
        if line in ("Language Formats", "Language", "Formats"):
            continue
        if "WARNING" in line:
            continue

        if current_section is None:
            continue

        # Parse language line: "zh-Hans  vtt, srt, ass"
        # First token is the language code, rest are formats
        parts = line.split()
        if not parts:
            continue

        lang = parts[0].rstrip(":")
        # Skip non-language entries (danmaku, xml, etc.)
        if lang.lower() in NON_SUBTITLE_LANGS:
            continue
        # Only accept valid language codes (letters, hyphens only)
        if not re.match(r'^[a-zA-Z][a-zA-Z\-]*$', lang):
            continue

        if lang not in subs[current_section]:
            subs[current_section].append(lang)

    return subs


def pick_best_subtitle(subs: dict) -> tuple[str, str] | None:
    """Pick the best subtitle: prefer zh-Hans manual > zh-Hans auto > first available."""
    # Priority order for Chinese subtitles
    zh_variants = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW"]

    for variant in zh_variants:
        if variant in subs["manual"]:
            return (variant, "manual")
        if variant in subs["auto"]:
            return (variant, "auto")

    # Fallback to first available
    for source in ["manual", "auto"]:
        if subs[source]:
            return (subs[source][0], source)

    return None


def download_subtitle(url: str, lang: str, subtitles_type: str,
                      output_dir: str, cookies_browser: str | None = None,
                      cookies_file: str | None = None,
                      username: str | None = None,
                      password: str | None = None) -> str | None:
    """Download subtitle and return the file path."""
    subs_dir = os.path.join(output_dir, "subs")
    os.makedirs(subs_dir, exist_ok=True)

    args = [
        "--skip-download",
        "--write-subs" if subtitles_type == "manual" else "--write-auto-subs",
        "--sub-lang", lang,
        "--output", os.path.join(subs_dir, "%(title)s.%(ext)s"),
        "--no-playlist",
    ]
    _add_cookies_arg(args, cookies_browser, cookies_file)
    _add_auth_args(args, username, password)
    args.append(url)

    print(f"[INFO] 下载字幕: lang={lang}, type={subtitles_type}")
    result = run_ytdlp(args, timeout=120)

    if result.returncode != 0:
        print(f"[ERROR] 字幕下载失败:\n{result.stderr}", file=sys.stderr)
        return None

    # Find the downloaded subtitle file (could be .vtt, .srt, .json, .ass)
    sub_files = []
    for ext in ["*.vtt", "*.srt", "*.json", "*.ass", "*.lrc"]:
        sub_files.extend(Path(subs_dir).glob(ext))
    if not sub_files:
        print("[ERROR] 未找到下载的字幕文件", file=sys.stderr)
        return None

    # Return the most recently created file
    sub_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(sub_files[0])


def _cleanup_raw_subs(subs_dir: str) -> None:
    """删除原始字幕文件（.vtt/.srt/.ass/.json/.lrc），保留清洗后的 .txt 产物。

    清洗产物默认输出在 subs/ 内（同名 .txt），因此仅删除原始字幕扩展名，
    目录空后尝试移除整个 subs/。
    """
    if not os.path.isdir(subs_dir):
        return
    for fname in os.listdir(subs_dir):
        if fname.lower().endswith((".vtt", ".srt", ".ass", ".json", ".lrc")):
            try:
                os.remove(os.path.join(subs_dir, fname))
            except OSError:
                pass
    try:
        os.rmdir(subs_dir)
    except OSError:
        pass


def clean_json_subtitle(json_path: str) -> str:
    """Clean Bilibili JSON subtitle format to plain text.

    Bilibili AI subtitles use JSON format:
    {"body": [{"from": 0.0, "to": 1.5, "content": "text"}, ...]}
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    body = data.get("body", [])
    if not body:
        return ""

    lines = []
    for item in body:
        content = item.get("content", "").strip()
        if content:
            lines.append(content)

    # Remove consecutive duplicates (common in AI subtitles)
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    return "\n".join(deduped)


# ─── 带时间戳副本（供关键帧文稿倒推） ─────────────────────────

def _fmt_mmss(sec: float) -> str:
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def json_subtitle_to_ts_lines(json_path: str) -> str:
    """B站 JSON 字幕 → `[mm:ss] 文本` 行（保留起始时间，供画面线索倒推）。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    lines, prev = [], ""
    for item in data.get("body", []):
        content = item.get("content", "").strip()
        if content and content != prev:
            lines.append(f"[{_fmt_mmss(item.get('from', 0))}] {content}")
            prev = content
    return "\n".join(lines)


def vtt_to_ts_lines(vtt_path: str) -> str:
    """VTT/SRT 字幕 → `[mm:ss] 文本` 行（每个 cue 取其起始时间）。"""
    with open(vtt_path, "r", encoding="utf-8") as f:
        raw = f.read()
    ts_re = re.compile(
        r"^(\d{1,3})[:\,](\d{2})[:\,](\d{2})[\.\,]\d{2,3}\s*-->")
    tag_re = re.compile(r"<[^>]*>")
    number_only_re = re.compile(r"^\d+$")
    lines: list[str] = []
    cur_ts = ""
    prev_text = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        m = ts_re.match(line)
        if m:
            sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            cur_ts = _fmt_mmss(sec)
            continue
        if number_only_re.match(line):
            continue
        text = tag_re.sub("", line).strip()
        if text and text != prev_text:
            lines.append(f"[{cur_ts}] {text}")
            prev_text = text
    return "\n".join(lines)


def clean_vtt(vtt_path: str) -> str:
    """Clean VTT subtitle file to plain text.

    Removes:
    - WEBVTT headers
    - Timestamps (00:00:01.000 --> 00:00:05.000)
    - Inline timing tags <00:00:01.000>
    - Style tags <c.bg_transparent>, </c>, <b>, </b>, etc.
    - Speaker labels like <v SpeakerName>
    - Empty/duplicate lines
    """
    with open(vtt_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    cleaned: list[str] = []
    prev_line = ""

    # Regex patterns
    timestamp_re = re.compile(r"^\d{1,3}[:\,]\d{2}[:\,]\d{2}[\.\,]\d{2,3}\s*-->\s*\d{1,3}[:\,]\d{2}[:\,]\d{2}[\.\,]\d{2,3}")
    tag_re = re.compile(r"<[^>]*>")
    number_only_re = re.compile(r"^\d+$")

    for line in lines:
        line = line.strip()

        # Skip empty lines, headers, timestamps, pure numbers
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if timestamp_re.match(line):
            continue
        if number_only_re.match(line):
            continue

        # Remove all HTML-like tags (including <c.bg_transparent>, <v ...>, etc.)
        line = tag_re.sub("", line).strip()

        if not line:
            continue

        # Merge with previous line if it's short (likely a continuation)
        if prev_line and len(prev_line) < 50 and not prev_line.endswith(("。", "！", "？", ".", "!", "?")):
            prev_line += line
        elif prev_line:
            cleaned.append(prev_line)
            prev_line = line
        else:
            prev_line = line

    # Don't forget the last line
    if prev_line:
        cleaned.append(prev_line)

    # Remove consecutive duplicates (common in auto subtitles)
    deduped = []
    for line in cleaned:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    return "\n".join(deduped)


# ─── B 站 AI 字幕（平台侧不稳定，须做内容相关性校验） ───────────
# 实测（2026-09-29）：
#   ① yt-dlp `--list-subs` 对 B 站只列 danmaku，ai-zh 轨取不到（带登录 Cookie 同样取不到）；
#   ② `x/player/v2` 接口能列出 ai-zh，但 `aisubtitle.hdslb.com` 的正文**会稳定返回
#      别的视频的字幕**（串片），多次重试不复现正确内容——盲信即产出错误总结。
# 因此本脚本提供 API 直取路径，并强制做字幕↔标题/简介的内容相关性校验：
# 校验不通过 → 弃用字幕、降级 STT（`--force-subtitle` 可强行保留，仅调试用）。

BILI_API_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 相关性阈值：实测正确字幕 FORWARD=0.29~0.51，串片/错轨=0.000~0.007；
# 取 0.20 作分界（负例余量大，正例最低 0.29 仍有缓冲）
RELEVANCE_MIN = 0.20
# 覆盖率（末句时间 ÷ 视频时长）区间：正确字幕实测 0.98~1.00。
#   < 0.85 → 字幕被截断（实测有 35:34 的视频只返回到 04:46 的样本），
#            用它会静默丢内容 → 弃用，改走 STT；
#   > 1.15 → 时间轴超出本片长度，必为别的视频/混拼 → 弃用。
COVERAGE_MIN, COVERAGE_MAX = 0.85, 1.15

_STOP_TERMS = {
    "的", "了", "是", "和", "与", "在", "等", "及", "或", "也", "就", "都", "对",
    "从", "到", "为", "以", "上", "下", "中", "里", "个", "一", "我们", "大家",
    "今天", "视频", "内容", "讲解", "分享", "介绍", "一个", "这个", "那些",
    "什么", "如何", "为什么", "以及", "通过", "进行", "可以", "主要", "相关",
    "方面", "影响", "情况", "部分", "一下", "带来", "本期", "这次", "之后",
}


def load_netscape_cookie_header(cookies_file: str | None) -> str | None:
    """Netscape cookies.txt → `name=value; ...` 请求头。失败返回 None。"""
    if not cookies_file or not os.path.exists(cookies_file):
        return None
    pairs: list[str] = []
    try:
        with open(cookies_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                p = line.split("\t")
                if len(p) >= 7 and p[5]:
                    pairs.append(f"{p[5]}={p[6]}")
    except OSError:
        return None
    return "; ".join(pairs) if pairs else None


def _bili_api_json(url: str, cookie_header: str | None, referer: str,
                   timeout: int = 30) -> dict:
    """GET B 站接口并解析 JSON。异常抛出由调用方兜住。"""
    import urllib.request
    if url.startswith("//"):
        url = "https:" + url
    headers = {"User-Agent": BILI_API_UA, "Referer": referer}
    if cookie_header:
        headers["Cookie"] = cookie_header
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def relevance_terms(ref_text: str) -> set:
    """从标题+简介抽「内容词」：拉丁 token（≥2 字符，大写）+ CJK 2/3-gram（去停用词）。"""
    terms: set = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9./+-]{1,}", ref_text or ""):
        if len(w) >= 2:
            terms.add(w.upper())
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", ref_text or ""):
        for n in (2, 3):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if g not in _STOP_TERMS:
                    terms.add(g)
    return terms


def subtitle_relevance(sub_text: str, ref_text: str) -> tuple[float, int]:
    """字幕对标题+简介的正向覆盖率：命中的内容词 / 内容词总数。

    返回 (score, n_terms)。n_terms < 8 时样本过少，分数不可信（由调用方放行）。
    """
    terms = relevance_terms(ref_text)
    if not terms or not sub_text:
        return 0.0, len(terms)
    upper = sub_text.upper()
    hits = sum(1 for t in terms if t.upper() in upper)
    return hits / len(terms), len(terms)


def _ts_coverage(ts_text: str, duration: float) -> float:
    """从 `[mm:ss] 文本` 行取末句时间 / 视频时长；无法判定时返回 1.0。"""
    times = list(re.finditer(r"^\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]",
                             ts_text or "", re.M))
    if not times or not duration:
        return 1.0
    last = times[-1]
    sec = (int(last.group(1) or 0) * 3600 + int(last.group(2)) * 60
           + int(last.group(3)))
    return sec / float(duration)


def subtitle_gate(text: str, ts_text: str, ref_text: str, duration: float,
                  min_relevance: float, force: bool) -> tuple[bool, str, float, float]:
    """字幕可用性门禁（双判据），返回 (ok, reason, relevance, coverage)。

    实测（2026-09-29，B 站 yt-dlp 与 player API 两路都出现过异常）：
      正确字幕  relevance 0.29~0.51  coverage 0.98~1.00
      串片字幕  relevance 0.000~0.007（整段内容是别的视频）
      混拼字幕  coverage 1.71（末句时间轴超出本片长度）
      截断字幕  coverage 0.16~0.45（只返回开头几分钟，静默丢内容）
    只判相关性拦不住混拼，只判覆盖率拦不住"时长吻合但整段错片"，故双判据。
    标题+简介词项 < 8 时相关性不可信，只判覆盖率。
    """
    rel, n_terms = subtitle_relevance(text, ref_text)
    cov = _ts_coverage(ts_text, duration)
    if force:
        return True, "forced", rel, cov
    if n_terms >= 8 and rel < min_relevance:
        return False, f"relevance {rel:.3f} < {min_relevance}", rel, cov
    if cov > COVERAGE_MAX:
        return False, f"coverage {cov:.2f} > {COVERAGE_MAX}（时间轴超出本片）", rel, cov
    if duration and cov < COVERAGE_MIN:
        return False, (f"coverage {cov:.2f} < {COVERAGE_MIN}（字幕被截断，"
                       f"用它会丢内容）"), rel, cov
    return True, "ok", rel, cov


def fetch_bilibili_ai_subtitle(bvid: str, cookies_file: str | None,
                               output_dir: str,
                               tries: int = 6) -> tuple[str, str, float] | None:
    """B 站 AI 字幕 API 直取 → 落盘 JSON。

    Args: bvid 形如 `BV1xxxxxxxxx`（b23.tv 短链需先由 yt-dlp 元数据解析出真实 BV 号）

    Returns: (json_path, lan, coverage) 或 None（无轨/接口失败）。
    coverage = 末句起始时间 / 视频时长。多次抓取取分段最多的一次（CDN 存在
    只返回前段的截断抖动）；**相关性判定不在此处**，由调用方用
    subtitle_relevance 决定采用还是弃用（串片防护）。
    """
    if not bvid or not re.fullmatch(r"BV\w{5,}", bvid):
        return None
    cookie_header = load_netscape_cookie_header(cookies_file)
    referer = f"https://www.bilibili.com/video/{bvid}/"
    try:
        view = _bili_api_json(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            cookie_header, referer)
        data = view.get("data") or {}
        cid, duration = data.get("cid"), float(data.get("duration") or 0)
        if not cid:
            return None
        player = _bili_api_json(
            f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}",
            cookie_header, referer)
        tracks = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    except (OSError, ValueError) as e:
        print(f"[DEBUG] B 站字幕接口失败: {repr(e)[:120]}")
        return None
    if not tracks:
        return None

    pick = None
    for pref in ("zh-Hans", "zh-CN", "ai-zh"):
        for t in tracks:
            if t.get("lan") == pref and t.get("subtitle_url"):
                pick = t
                break
        if pick:
            break
    if not pick:
        pick = next((t for t in tracks if t.get("subtitle_url")), None)
    if not pick:
        return None

    best_body: list = []
    for i in range(max(1, tries)):
        try:
            raw = _bili_api_json(pick["subtitle_url"], cookie_header, referer)
        except (OSError, ValueError):
            break
        body = raw.get("body") or []
        if len(body) > len(best_body):
            best_body = body
        last_from = float(body[-1].get("from", 0)) if body else 0.0
        # 覆盖到全片 85% 以上即够用；否则重试（防截断）
        if duration and last_from >= duration * 0.85:
            break
    if not best_body:
        return None

    last_from = float(best_body[-1].get("from", 0))
    coverage = (last_from / duration) if duration else 1.0
    subs_dir = os.path.join(output_dir, "subs")
    os.makedirs(subs_dir, exist_ok=True)
    json_path = os.path.join(subs_dir, f"{bvid}.{pick.get('lan', 'ai')}.api.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"body": best_body}, f, ensure_ascii=False)
    return json_path, pick.get("lan", "ai-zh"), coverage


def stt_fallback_and_exit(args, info: dict, cookies_file: str | None,
                          reason: str) -> None:
    """无可用字幕（或字幕被判串片）→ 走 Whisper STT 并结束进程。

    成功：打印 [TRANSCRIPT_TYPE] stt + [OUTPUT_PATH]，exit 0。
    失败：打印恢复指引，exit 1。
    """
    if args.no_stt:
        print(f"\n[ERROR] {reason}，且已禁用STT降级", file=sys.stderr)
        print("解决方法:", file=sys.stderr)
        print("  1. 提供Cookie获取AI字幕: --cookies cookies.txt", file=sys.stderr)
        print("  2. 移除 --no-stt 以启用语音转文本降级", file=sys.stderr)
        sys.exit(1)

    # Determine output path for STT
    bv_match = re.search(r'BV[\w]+', args.url)
    bv_id = bv_match.group(0) if bv_match else "video"
    stt_output = args.output or os.path.join(
        args.output_dir, f"transcript_{bv_id}.txt"
    )

    stt_exit = fallback_to_stt(
        args.url, stt_output, cookies_file,
        args.output_dir, args.stt_model,
        duration=info.get("duration", 0) if info else 0,
        timeout=args.stt_timeout or None,
    )

    if stt_exit != 0:
        print("\n[ERROR] 语音转文本失败（退出码 %s）。" % stt_exit, file=sys.stderr)
        print("处理方式：", file=sys.stderr)
        print("  1. 若已下载音频，用 transcribe.py --local-audio 串行重试（见上方 HINT）", file=sys.stderr)
        print("  2. 调大超时：extract_subs.py --stt-timeout <秒>", file=sys.stderr)
        print("  3. 或手动提供转录文本文件", file=sys.stderr)
        sys.exit(1)

    # STT succeeded — use its output instead of subtitle
    with open(stt_output, "r", encoding="utf-8") as f:
        cleaned = f.read()

    if not cleaned.strip():
        print("[ERROR] 语音转录结果为空", file=sys.stderr)
        sys.exit(1)

    output_path = stt_output
    char_count = len(cleaned)
    line_count = len(cleaned.splitlines())
    # STT 输出行首本已带 [mm:ss] 时间戳 → 直接作为时间戳副本（供关键帧倒推）
    try:
        with open(stt_output + ".ts.txt", "w", encoding="utf-8") as f:
            f.write(cleaned)
    except OSError:
        pass
    print(f"[INFO] STT转录完成: {line_count} 行, {char_count} 字符")
    print(f"[INFO] 输出文件: {output_path}")
    print(f"\n--- 转录文本预览 (前500字) ---")
    print(cleaned[:500])
    print(f"[TRANSCRIPT_TYPE] stt")
    print(f"\n[OUTPUT_PATH] {output_path}")
    sys.exit(0)


def get_video_info(url: str, cookies_browser: str | None = None,
                   cookies_file: str | None = None,
                   username: str | None = None,
                   password: str | None = None) -> dict:
    """Get video metadata using yt-dlp."""
    args = ["--dump-json", "--no-download", "--no-playlist"]
    _add_cookies_arg(args, cookies_browser, cookies_file)
    _add_auth_args(args, username, password)
    args.append(url)

    result = run_ytdlp(args, timeout=30)
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def detect_browser() -> str | None:
    """Auto-detect available browser for cookies. Returns browser name or None."""
    # Known error patterns that indicate browser cookie access failure
    fail_patterns = [
        "could not find",
        "unsupported",
        "could not copy",
        "failed to decrypt",
        "unable to decrypt",
        "permission denied",
        "access denied",
    ]
    candidates = ["edge", "chrome", "firefox", "brave", "chromium"]
    for browser in candidates:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--cookies-from-browser", browser,
             "--no-download", "--print", "title",
             "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],  # 通用测试视频，无需登录
            capture_output=True, text=True, timeout=15
        )
        stderr_lower = result.stderr.lower()
        # If no known failure patterns matched, assume it works
        if not any(p in stderr_lower for p in fail_patterns):
            return browser
        else:
            # Log the failure reason for debugging
            first_error = stderr_lower.splitlines()[0] if stderr_lower.strip() else "unknown error"
            print(f"[DEBUG] {browser} 不可用: {first_error[:80]}")
    return None


def _default_stt_timeout(duration_sec: float) -> int:
    """按音频时长估算转录超时（秒）。

    faster-whisper base 模型单进程 CPU 转录约 3-5x 实时；但多任务并行或机器
    繁忙时可能劣化到 2x 以下。取 5x 实时 + 10 分钟模型加载/系统开销余量，
    并设上限（3.5h）保证**内层超时先于 run_pipeline 外层兜底触发**，
    从而打印优雅的错误与恢复提示。
    """
    if duration_sec and duration_sec > 0:
        return min(max(1800, int(duration_sec * 5) + 600), 12600)
    return 3600  # 未知时长：默认 1 小时


def fallback_to_stt(url: str, output_path: str, cookies_file: str | None,
                     output_dir: str, model: str = "base",
                     duration: int = 0, timeout: int | None = None) -> int:
    """Invoke transcribe.py as fallback when no subtitles are available.

    Returns exit code (0 = success；2 = 转录超时)。
    """
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    transcribe_script = os.path.join(scripts_dir, "transcribe.py")

    print("\n" + "=" * 60)
    print("[INFO] 🔄 自动降级：无可用字幕 → 语音转文本 (Whisper)")
    print("=" * 60)

    cmd = [
        sys.executable, transcribe_script, url,
        "-o", output_path,
        "-m", model,
        "--output-dir", output_dir,
    ]
    if cookies_file:
        cmd.extend(["--cookies", cookies_file])

    if timeout is None:
        timeout = _default_stt_timeout(duration)
    print(f"[INFO] STT 转录超时上限: {timeout}s（按音频时长自动估算，可用 --stt-timeout 覆盖）")

    try:
        result = subprocess.run(cmd, timeout=timeout)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"[ERROR] 语音转文本超时（>{timeout}s）。", file=sys.stderr)
        print(f"[HINT] 音频可能已下载。请串行重试（避免多任务 CPU 争抢再次超时）：",
              file=sys.stderr)
        print(f'       python "{transcribe_script}" --local-audio "<已下载音频.m4a>" '
              f'-o "{output_path}" -m {model}', file=sys.stderr)
        return 2


def main():
    parser = argparse.ArgumentParser(description="提取 Bilibili 视频字幕并清洗为纯文本")
    parser.add_argument("url", help="Bilibili 视频 URL")
    parser.add_argument("-o", "--output", help="输出文本文件路径", default=None)
    parser.add_argument("-l", "--lang", help="指定字幕语言（默认自动选择最佳）", default=None)
    parser.add_argument("--list-only", action="store_true", help="仅列出可用字幕")
    parser.add_argument("--cookies-from", help="浏览器名称，用于读取登录Cookie（如 edge, chrome）", default=None)
    parser.add_argument("--cookies", help="Netscape格式Cookie文件路径（优先级高于 --cookies-from）", default=None)
    parser.add_argument("--no-cookies", action="store_true", help="不使用任何Cookie（默认自动检测浏览器）")
    parser.add_argument("-u", "--username", help="B站用户名/邮箱", default=None)
    parser.add_argument("-p", "--password", help="B站密码", default=None)
    parser.add_argument("--output-dir", default=".", help="字幕下载临时目录")
    parser.add_argument("--no-stt", action="store_true", help="禁用语音转文本降级（无字幕时直接报错）")
    parser.add_argument("--stt-model", default="base",
                        choices=["tiny", "base", "small", "medium"],
                        help="Whisper 模型大小 (default: base)")
    parser.add_argument("--stt-timeout", type=int, default=0,
                        help="STT 转录超时秒数（0=按音频时长自动估算，见 --help 上方说明）")
    parser.add_argument("--no-bili-api", action="store_true",
                        help="禁用 B 站 player API 直取 AI 字幕（yt-dlp 取不到字幕时的回退路径）")
    parser.add_argument("--min-relevance", type=float, default=RELEVANCE_MIN,
                        help=f"字幕↔标题/简介内容相关性下限（默认 {RELEVANCE_MIN}）；"
                             f"低于此值判为 CDN 串片/错轨，弃用字幕并降级 STT")
    parser.add_argument("--force-subtitle", action="store_true",
                        help="跳过内容相关性门禁，强行采用已取到的字幕（仅供人工核对调试）")

    args = parser.parse_args()

    # 依赖检测
    from _common import (check_dependency, validate_url, is_bilibili,
                         derive_video_id, emit_output_path, remove_quiet)
    if not check_dependency("yt_dlp"):
        sys.exit(1)

    # Validate URL
    if not validate_url(args.url):
        print("[ERROR] 请提供 Bilibili / YouTube / 小宇宙 链接", file=sys.stderr)
        sys.exit(1)

    # Auth info
    username = args.username
    password = args.password
    if username:
        print(f"[INFO] 使用账号登录: {username}")

    # Determine cookie source (skip if using username/password)
    cookies_browser = None
    cookies_file = args.cookies

    if cookies_file:
        if not os.path.exists(cookies_file):
            print(f"[ERROR] Cookie文件不存在: {cookies_file}", file=sys.stderr)
            sys.exit(1)
        print(f"[INFO] 使用Cookie文件: {cookies_file}")
    elif not username and not args.no_cookies:
        if args.cookies_from:
            cookies_browser = args.cookies_from
        else:
            print("[INFO] 自动检测浏览器Cookie...")
            cookies_browser = detect_browser()
            if cookies_browser:
                print(f"[INFO] 使用 {cookies_browser} 浏览器Cookie")
            else:
                print("[INFO] 未检测到可用浏览器Cookie，尝试无登录模式")

    # Get video info
    info = get_video_info(args.url, cookies_browser, cookies_file, username, password)
    if info:
        duration_min = info.get('duration', 0) // 60
        print(f"[INFO] 视频: {info.get('title', 'Unknown')}")
        print(f"[INFO] 时长: {duration_min}分钟 | 上传者: {info.get('uploader', 'Unknown')}")

    # List subtitles
    subs = list_subtitles(args.url, cookies_browser, cookies_file, username, password)

    manual_ok = [l for l in subs["manual"] if l not in NON_SUBTITLE_LANGS]
    auto_ok = [l for l in subs["auto"] if l not in NON_SUBTITLE_LANGS]

    print(f"[INFO] 手动字幕: {', '.join(manual_ok) if manual_ok else '(无)'}")
    print(f"[INFO] 自动字幕: {', '.join(auto_ok) if auto_ok else '(无)'}")

    if args.list_only:
        if subs["need_login"]:
            print("\n[WARNING] B站要求登录才能获取AI字幕。")
            print("  1. 用浏览器扩展导出 cookies.txt: --cookies cookies.txt")
            print("  2. 或指定浏览器: --cookies-from edge")
        sys.exit(0)

    # Check login requirement (warning only — STT may still work)
    need_stt = False
    stt_reason = ""

    if subs["need_login"] and not manual_ok and not auto_ok:
        if not cookies_file and not cookies_browser and not username:
            print("\n[WARNING] B站要求登录才能获取AI字幕，且未提供Cookie。")
            print("  将尝试语音转文本 (STT) 降级方案（音频下载不需要登录）")
        need_stt = True
        stt_reason = "B站要求登录且无Cookie"

    if not need_stt and not manual_ok and not auto_ok:
        need_stt = True
        stt_reason = "该视频无任何可用字幕"

    # ── B 站专用回退：yt-dlp 取不到 AI 字幕时，试 player API 直取 ──
    # （取回后必须过内容相关性门禁，防 aisubtitle CDN「串片」）
    if need_stt and not args.no_bili_api and is_bilibili(args.url):
        bvid_m = re.search(r"BV\w{5,}", str((info or {}).get("id") or "") + args.url)
        got = fetch_bilibili_ai_subtitle(bvid_m.group(0) if bvid_m else None,
                                         cookies_file, args.output_dir)
        if got:
            api_json, api_lan, coverage = got
            api_text = clean_json_subtitle(api_json)
            api_ts = json_subtitle_to_ts_lines(api_json)
            ref = f"{(info or {}).get('title', '')} {(info or {}).get('description', '')}"
            passed, why, score, coverage2 = subtitle_gate(
                api_text, api_ts, ref, float((info or {}).get("duration") or 0),
                args.min_relevance, args.force_subtitle)
            print(f"[SUB_GATE] source=bilibili_api lan={api_lan} "
                  f"relevance={score:.3f} coverage={coverage2:.2f} "
                  f"verdict={'ok' if passed else 'reject'} reason={why}")
            print(f"[INFO] B 站 API 字幕 {api_lan}: {len(api_text)} 字符")
            if passed:
                out_path = args.output or os.path.join(
                    args.output_dir,
                    f"subtitle_{bvid_m.group(0) if bvid_m else derive_video_id(args.url)}.txt")
                os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(api_text)
                if api_ts.strip():
                    with open(out_path + ".ts.txt", "w", encoding="utf-8") as f:
                        f.write(api_ts)
                _cleanup_raw_subs(os.path.join(args.output_dir, "subs"))
                print(f"[INFO] 字幕（B站 API）清洗完成: "
                      f"{len(api_text.splitlines())} 行, {len(api_text)} 字符")
                print(f"[INFO] 输出文件: {out_path}")
                print("\n--- 字幕文本预览 (前500字) ---")
                print(api_text[:500])
                print("[TRANSCRIPT_TYPE] subtitle")
                emit_output_path(out_path)
                sys.exit(0)
            print(f"[WARN] B站 AI 字幕与标题/简介内容不符（相关性 {score:.3f} < "
                  f"{args.min_relevance}），疑为 CDN 串片/错轨 → **弃用字幕，降级 STT**。"
                  f"（--force-subtitle 可强行保留，仅供调试）", file=sys.stderr)
            stt_reason = "B站 AI 字幕内容相关性过低（疑串片）"
            remove_quiet(api_json)
        else:
            print("[INFO] B站 player API 未返回字幕轨（该视频无 AI 字幕或 Cookie 失效）")

    if need_stt:
        stt_fallback_and_exit(args, info, cookies_file, stt_reason)

    # Pick subtitle language
    if args.lang:
        if args.lang in manual_ok:
            chosen_lang, sub_type = args.lang, "manual"
        elif args.lang in auto_ok:
            chosen_lang, sub_type = args.lang, "auto"
        else:
            print(f"[ERROR] 字幕 '{args.lang}' 不可用", file=sys.stderr)
            sys.exit(1)
    else:
        choice = pick_best_subtitle({"manual": manual_ok, "auto": auto_ok})
        if not choice:
            print("[ERROR] 无可用的中文字幕", file=sys.stderr)
            sys.exit(1)
        chosen_lang, sub_type = choice

    print(f"[INFO] 选择: {chosen_lang} ({sub_type})")

    # Download subtitle
    vtt_path = download_subtitle(args.url, chosen_lang, sub_type, args.output_dir,
                                 cookies_browser, cookies_file, username, password)
    if not vtt_path:
        sys.exit(1)

    print(f"[INFO] 字幕文件: {vtt_path}")

    # Clean (dispatch by file type)
    ext = os.path.splitext(vtt_path)[1].lower()
    if ext == ".json":
        cleaned = clean_json_subtitle(vtt_path)
    else:
        cleaned = clean_vtt(vtt_path)
    if not cleaned.strip():
        print("[ERROR] 清洗后字幕为空", file=sys.stderr)
        sys.exit(1)

    # 字幕可用性门禁：内容相关性 + 时间覆盖率双判据（防串片与截断）
    ref = f"{(info or {}).get('title', '')} {(info or {}).get('description', '')}"
    try:
        gate_ts = (json_subtitle_to_ts_lines(vtt_path) if ext == ".json"
                   else vtt_to_ts_lines(vtt_path))
    except Exception:
        gate_ts = ""
    ok_gate, why, score, cov = subtitle_gate(
        cleaned, gate_ts, ref, float((info or {}).get("duration") or 0),
        args.min_relevance, args.force_subtitle)
    print(f"[SUB_GATE] relevance={score:.3f} coverage={cov:.2f} "
          f"verdict={'ok' if ok_gate else 'reject'} reason={why}")
    if not ok_gate:
        print(f"[WARN] 字幕未过可用性门禁（{why}）→ 弃用字幕，降级 STT"
              f"（--force-subtitle 可强行保留）", file=sys.stderr)
        _cleanup_raw_subs(os.path.join(args.output_dir, "subs"))
        stt_fallback_and_exit(args, info, cookies_file, f"字幕未过门禁（{why}）")

    # Output
    if args.output:
        output_path = args.output
    else:
        output_path = str(Path(vtt_path).with_suffix(".txt"))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(cleaned)

    # 带时间戳副本 <output>.ts.txt（原始字幕此时尚未清理）
    try:
        ts_text = (json_subtitle_to_ts_lines(vtt_path) if ext == ".json"
                   else vtt_to_ts_lines(vtt_path))
        if ts_text.strip():
            with open(output_path + ".ts.txt", "w", encoding="utf-8") as f:
                f.write(ts_text)
    except Exception as e:
        print(f"[WARN] 时间戳副本生成失败（关键帧将退回全帧模式）: {e}",
              file=sys.stderr)

    # 自动清理原始字幕文件（保留清洗后的 .txt 产物）
    _cleanup_raw_subs(os.path.join(args.output_dir, "subs"))

    # Print stats
    char_count = len(cleaned)
    line_count = len(cleaned.splitlines())
    print(f"[INFO] 清洗完成: {line_count} 行, {char_count} 字符")
    print(f"[INFO] 输出文件: {output_path}")
    print(f"\n--- 字幕文本预览 (前500字) ---")
    print(cleaned[:500])

    # Return output path for machine parsing
    print(f"[TRANSCRIPT_TYPE] subtitle")
    print(f"\n[OUTPUT_PATH] {output_path}")


if __name__ == "__main__":
    main()
