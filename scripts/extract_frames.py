#!/usr/bin/env python3
"""
Bilibili 视频关键帧提取 + OCR 画面文字识别
提取视频画面中的图表标注、PPT文字、演示内容，补充纯音频/字幕的信息缺口
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


# ─── ffmpeg 定位 ──────────────────────────────────────────────

_ffmpeg_path: str | None = None
_ffprobe_path: str | None = None


def _find_ffmpeg() -> tuple[str, str]:
    """Locate ffmpeg and ffprobe binaries. Tries imageio_ffmpeg first, then system PATH."""
    global _ffmpeg_path, _ffprobe_path
    if _ffmpeg_path and _ffprobe_path:
        return _ffmpeg_path, _ffprobe_path

    # 1. Try imageio_ffmpeg bundled binary (fast, no extra download)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            _ffmpeg_path = exe
            _ffprobe_path = exe.replace("ffmpeg", "ffprobe")
            if os.path.exists(_ffprobe_path):
                return _ffmpeg_path, _ffprobe_path
            # ffprobe not found, use ffmpeg for probe too
            _ffprobe_path = exe
    except ImportError:
        pass

    # 2. Fallback to system PATH
    for name in ["ffmpeg", "ffmpeg.exe"]:
        result = subprocess.run(["where", name], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            _ffmpeg_path = result.stdout.strip().splitlines()[0]
            break

    for name in ["ffprobe", "ffprobe.exe"]:
        result = subprocess.run(["where", name], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            _ffprobe_path = result.stdout.strip().splitlines()[0]
            break

    if not _ffmpeg_path:
        raise RuntimeError(
            "ffmpeg 未找到。请安装: pip install imageio-ffmpeg"
        )

    return _ffmpeg_path, _ffprobe_path or _ffmpeg_path


# ─── 视频下载 ────────────────────────────────────────────────

def download_video_lowres(url: str, output_dir: str,
                          cookies_file: str | None = None) -> str | None:
    """Download video at low resolution (360p min) for OCR. Returns video path."""
    os.makedirs(output_dir, exist_ok=True)
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    args = [
        sys.executable, "-m", "yt_dlp",
        "-f", "worstvideo[height>=360][ext=mp4]/worst[height>=360]/worst",
        "-o", output_template,
        "--no-playlist",
        "--no-mtime",
    ]
    if cookies_file:
        args.extend(["--cookies", cookies_file])
    args.append(url)

    print("[INFO] 下载低清视频用于OCR (≤360p)...")
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        print(f"[ERROR] 视频下载失败:\n{result.stderr}", file=sys.stderr)
        return None

    # Find downloaded file
    video_files = []
    for ext in ["*.mp4", "*.webm", "*.mkv", "*.flv"]:
        video_files.extend(Path(output_dir).glob(ext))
    video_files = [f for f in video_files if not f.name.endswith(".info.json")]

    if not video_files:
        print("[ERROR] 未找到下载的视频文件", file=sys.stderr)
        return None

    video_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    path = str(video_files[0])
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"[INFO] 视频文件: {path} ({size_mb:.1f}MB)")
    return path


# ─── 帧提取 ──────────────────────────────────────────────────

def get_video_duration(video_path: str) -> float:
    """Get video duration in seconds by parsing ffmpeg stderr output.

    Uses ffmpeg (not ffprobe) because imageio_ffmpeg only bundles ffmpeg.
    """
    ffmpeg_path, _ = _find_ffmpeg()
    result = subprocess.run(
        [ffmpeg_path, "-i", video_path],
        capture_output=True, text=True, timeout=30,
    )
    # ffmpeg prints duration info to stderr: "Duration: HH:MM:SS.ms, ..."
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr or "")
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    return 0


def extract_frames(video_path: str, output_dir: str,
                   interval: float = 8.0) -> list[dict]:
    """Extract frames at regular intervals using ffmpeg.

    Returns list of {path, timestamp_sec} dicts.
    """
    os.makedirs(output_dir, exist_ok=True)

    duration = get_video_duration(video_path)
    if duration <= 0:
        print("[WARN] 无法获取视频时长，使用默认间隔")
        duration = 600  # assume 10 min

    # Extract one frame every `interval` seconds, at low resolution for OCR speed
    output_pattern = os.path.join(output_dir, "frame_%04d.jpg")
    ffmpeg_path, _ = _find_ffmpeg()
    # 解码全片耗时与时长成正比（长视频可超数分钟），按视频时长放宽超时
    result = subprocess.run([
        ffmpeg_path, "-y",
        "-i", video_path,
        "-vf", f"fps=1/{interval},scale=640:-1",
        "-q:v", "5",
        "-loglevel", "error",
        output_pattern,
    ], capture_output=True, text=True, timeout=max(600, int(duration * 0.5) + 300))

    if result.returncode != 0:
        print(f"[ERROR] ffmpeg 帧提取失败:\n{result.stderr}", file=sys.stderr)
        return []

    # Collect generated frames with timestamps
    frames = []
    frame_files = sorted(Path(output_dir).glob("frame_*.jpg"))
    for i, fp in enumerate(frame_files):
        timestamp = i * interval
        frames.append({"path": str(fp), "timestamp_sec": timestamp})

    print(f"[INFO] 提取 {len(frames)} 帧 (间隔 {interval}s, 分辨率 640px)")
    return frames


# ─── OCR 文字识别 ─────────────────────────────────────────────

def ocr_frame(frame_path: str) -> list[dict]:
    """Run RapidOCR on a single frame. Returns list of {text, confidence, box}."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        print("[ERROR] rapidocr-onnxruntime 未安装，pip install rapidocr-onnxruntime",
              file=sys.stderr)
        return []

    engine = RapidOCR()
    result, _ = engine(frame_path)

    if not result:
        return []

    items = []
    for box, text, confidence in result:
        text = text.strip()
        if text and len(text) >= 2:  # skip single-char noise
            items.append({"text": text, "confidence": confidence, "box": box})
    return items


# Global OCR engine instance (lazy init)
_ocr_engine = None


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def ocr_frame_cached(frame_path: str) -> list[dict]:
    """Run RapidOCR with cached engine instance."""
    engine = _get_ocr_engine()
    result, _ = engine(frame_path)
    if not result:
        return []
    items = []
    for box, text, confidence in result:
        text = text.strip()
        if text and len(text) >= 2:
            items.append({"text": text, "confidence": confidence, "box": box})
    return items


# ─── 文本去重与合并 ───────────────────────────────────────────

def _text_similarity(a: str, b: str) -> float:
    """Simple Jaccard-like similarity on character bigrams."""
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s) - 1))
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def deduplicate_ocr_results(frame_results: list[dict],
                            sim_threshold: float = 0.75) -> list[dict]:
    """Deduplicate OCR text across frames.

    frame_results: list of {timestamp_sec, texts: [{text, confidence}]}
    Returns deduplicated list with unique text items and their first appearance.
    """
    seen: list[dict] = []  # [{text, timestamp, confidence}]

    for fr in frame_results:
        for item in fr.get("texts", []):
            text = item["text"]
            # Check similarity against all seen texts
            is_dup = False
            for s in seen:
                if _text_similarity(text, s["text"]) >= sim_threshold:
                    is_dup = True
                    break
            if not is_dup:
                seen.append({
                    "text": text,
                    "timestamp_sec": fr["timestamp_sec"],
                    "confidence": item["confidence"],
                })

    return seen


def format_ocr_output(deduped: list[dict]) -> str:
    """Format deduplicated OCR results as markdown text."""
    if not deduped:
        return "（未检测到画面文字）"

    # Format timestamp as MM:SS
    def fmt_ts(sec):
        m, s = divmod(int(sec), 60)
        return f"{m:02d}:{s:02d}"

    lines = []
    current_minute = -1
    for item in deduped:
        minute = int(item["timestamp_sec"] // 60)
        if minute != current_minute:
            current_minute = minute
            lines.append(f"\n### {fmt_ts(item['timestamp_sec'])}")
        lines.append(f"- {item['text']}")

    return "\n".join(lines).strip()


# ─── 关键帧筛选与导出 ─────────────────────────────────────────

# 数据/图表/演示特征词（OCR 文本含数字且命中任一词 → 判定为关键帧候选）
_DATA_KEYWORDS_RE = re.compile(
    r'(?:%|％|亿|万[元美]|增长|下降|同比|环比|营收|利润|市值|估值|份额|渗透率|'
    r'token|参数|架构|模型|路线图|roadmap|演示|demo|对比|数据|图表|统计|'
    r'规模|收入|毛利率|增速|CAGR|DAU|MAU|GMV|ARR)',
    re.I)
_HAS_DIGIT_RE = re.compile(r'\d')


def is_data_frame(texts: list[dict]) -> bool:
    """OCR 文本判定是否属于"数据/图表/PPT/演示"类关键帧。

    规则：存在含数字的文本行，且任一文本命中数据特征词；
    或文本总长≥60 字符（信息密集画面，如整页 PPT/表格）。
    """
    if not texts:
        return False
    joined = ' '.join(t['text'] for t in texts)
    has_digit = any(_HAS_DIGIT_RE.search(t['text']) for t in texts)
    if has_digit and _DATA_KEYWORDS_RE.search(joined):
        return True
    return len(joined) >= 60


def _fmt_hhmmss(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}{m:02d}{s:02d}"


def export_keyframes(frames_with_ocr: list[dict], keyframes_dir: str,
                     video_id: str, max_keyframes: int = 12,
                     min_gap_sec: float = 30.0) -> list[dict]:
    """从 OCR 结果中筛选数据/演示关键帧，复制图片到 keyframes_dir。

    frames_with_ocr: [{path, timestamp_sec, texts:[{text,...}]}]
    规则：is_data_frame 命中为候选 → 相邻候选按 min_gap_sec（默认 30s）去同屏连拍
    → 若仍超 max_keyframes 张，按时间轴**均匀采样**保留（防长标题/片头吃满配额，
    保证全片覆盖）。
    命名：<video_id>_<HHMMSS>.png（冲突自动加 _2/_3 后缀）。
    返回：[{path, timestamp_sec, ocr_summary}]，path 为交付图片绝对路径。
    """
    candidates = [f for f in frames_with_ocr
                  if f.get("texts") and is_data_frame(f["texts"])]
    if not candidates:
        return []
    candidates.sort(key=lambda f: f["timestamp_sec"])

    # 第一步：同屏连拍去重（与前一个保留帧时间间隔 < min_gap_sec 则跳过）
    spaced: list[dict] = []
    for cand in candidates:
        if spaced and cand["timestamp_sec"] - spaced[-1]["timestamp_sec"] < min_gap_sec:
            continue
        spaced.append(cand)

    # 第二步：超量则按时间轴均匀采样（覆盖全片）
    if len(spaced) > max_keyframes:
        idx_n = len(spaced) - 1
        picks = {round(i * idx_n / (max_keyframes - 1)) for i in range(max_keyframes)} \
            if max_keyframes > 1 else {0}
        spaced = [spaced[i] for i in sorted(picks)]

    kept: list[dict] = []
    for cand in spaced:
        cand_text = ' '.join(t['text'] for t in cand["texts"])
        try:
            os.makedirs(keyframes_dir, exist_ok=True)
            base = os.path.join(keyframes_dir,
                                f"{video_id}_{_fmt_hhmmss(cand['timestamp_sec'])}")
            target = base + ".png"
            n = 2
            while os.path.exists(target):
                target = f"{base}_{n}.png"
                n += 1
            shutil.copyfile(cand["path"], target)
        except OSError as e:
            print(f"[WARN] 关键帧导出失败 @ {cand['timestamp_sec']}s: {e}",
                  file=sys.stderr)
            continue
        kept.append({
            "path": os.path.abspath(target),
            "timestamp_sec": cand["timestamp_sec"],
            "ocr_summary": cand_text[:80],
        })
    return kept


# ─── 主流程 ──────────────────────────────────────────────────

# ─── 文稿倒推关键帧（text-driven cue mode） ───────────────────

# 画面指涉词：讲稿说到"这张图/大家看/如下公式…"→ 此刻屏幕大概率有画面
_CUE_VISUAL_RE = re.compile(
    r'(?:这[张个道]?\s*[图表示页公架]|大家看|看这|如[下上]图|示意图|架构图|'
    r'PPT|slide|公式|演示|展示|左边|右边|屏幕|曲线|金字塔|清单|目录|'
    r'第[一二三四五六七八九十]步|这一步|这一章|章节)')
_CUE_DATA_NUM_RE = re.compile(r'\d+(?:\.\d+)?')
_CUE_DATA_CTX_RE = re.compile(
    r'(?:亿|万|%|％|元|增长|下降|下跌|占比|同比|环比|倍|数据|规模|估值|市值|'
    r'成本|价格|收入|利润|渗透率|参数|token|数量)')


def parse_ts_lines(path: str) -> list[dict]:
    """解析 `[mm:ss] 文本` / `[hh:mm:ss] 文本` 行 → [{sec, text}]。"""
    out: list[dict] = []
    ts_prefix = re.compile(r'^\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]\s*(.*)$')
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = ts_prefix.match(line)
            if m and m.group(4).strip():
                h = int(m.group(1) or 0)
                out.append({"sec": h * 3600 + int(m.group(2)) * 60 + int(m.group(3)),
                            "text": m.group(4).strip()})
    return out


def select_cues(ts_lines: list[dict], max_keyframes: int = 12,
                gap_sec: float = 45.0, lead_sec: float = 2.0,
                duration: float = 0.0) -> list[float]:
    """从带时间戳文稿挑画面线索时刻（秒）。

    命中画面指涉词，或（数字≥2 且含量纲词）→ 线索；+lead_sec（话音先起、
    画面稍显）；相邻线索 gap_sec 去重；超上限时全片均匀采样（防片头密集
    线索吃满配额、丢失后半程关键页）。
    """
    hits: list[float] = []
    for item in ts_lines:
        text = item["text"]
        visual = bool(_CUE_VISUAL_RE.search(text))
        nums = len(_CUE_DATA_NUM_RE.findall(text))
        dataish = nums >= 2 and bool(_CUE_DATA_CTX_RE.search(text))
        if visual or dataish:
            hits.append(item["sec"] + lead_sec)
    hits.sort()
    spaced: list[float] = []
    for h in hits:
        if duration and h > duration - 1:
            continue
        if spaced and h - spaced[-1] < gap_sec:
            continue
        spaced.append(h)
    if max_keyframes > 0 and len(spaced) > max_keyframes:
        n = len(spaced) - 1
        picks = sorted({round(i * n / (max_keyframes - 1))
                        for i in range(max_keyframes)})
        spaced = [spaced[i] for i in picks]
    return spaced


def capture_frame_at(ffmpeg_path: str, video_path: str, sec: float,
                     out_path: str, width: int = 960) -> bool:
    """按时间戳精确抓取单帧（-ss 在 -i 前，seek 快）。成功返回 True。"""
    try:
        r = subprocess.run([
            ffmpeg_path, "-y", "-ss", f"{sec:.2f}", "-i", video_path,
            "-frames:v", "1", "-vf", f"scale={width}:-1", "-q:v", "3",
            "-loglevel", "error", out_path,
        ], capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and os.path.exists(out_path) and \
            os.path.getsize(out_path) > 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def extract_by_cues(url: str, transcript_file: str, output_dir: str,
                    cookies_file: str | None, keyframes_dir: str,
                    video_id: str, max_keyframes: int = 12,
                    gap_sec: float = 45.0,
                    keep_video: bool = False) -> tuple[str, list[dict]]:
    """文稿倒推模式：字幕线索 → 定点抓帧 → OCR 验证（屏幕有文字才留）。

    返回 (画面文字 markdown, keyframes 清单)。线索为空/验证全空/任一环节
    失败时返回 ("", [])，由调用方回退全帧 OCR 模式。
    """
    video_dir = os.path.join(output_dir, "video")
    frames_dir = os.path.join(output_dir, "cue_frames")
    video_path = None
    try:
        ts_lines = parse_ts_lines(transcript_file)
        if not ts_lines:
            return "", []
        video_path = download_video_lowres(url, video_dir, cookies_file)
        if not video_path:
            return "", []
        duration = get_video_duration(video_path)
        cues = select_cues(ts_lines, max_keyframes=max_keyframes,
                           gap_sec=gap_sec, duration=duration)
        if not cues:
            return "", []
        ffmpeg_path, _ = _find_ffmpeg()
        os.makedirs(frames_dir, exist_ok=True)
        print(f"[INFO] 文稿倒推关键帧：{len(cues)} 个画面线索时刻 "
              f"{[round(c) for c in cues]}")
        picked: list[dict] = []
        for i, sec in enumerate(cues):
            fp = os.path.join(frames_dir, f"cue_{i:02d}.jpg")
            if not capture_frame_at(ffmpeg_path, video_path, sec, fp):
                continue
            texts = ocr_frame_cached(fp)
            if not texts:
                continue  # 屏幕无文字（口播/人脸画面）→ 丢弃
            joined = ' '.join(t['text'] for t in texts)
            picked.append({"path": fp, "timestamp_sec": sec,
                           "text": joined})
        if not picked:
            return "", []
        os.makedirs(keyframes_dir, exist_ok=True)
        kept: list[dict] = []
        for cand in picked:
            base = os.path.join(
                keyframes_dir,
                f"{video_id}_{_fmt_hhmmss(cand['timestamp_sec'])}")
            target = base + ".png"
            n = 2
            while os.path.exists(target):
                target = f"{base}_{n}.png"
                n += 1
            try:
                shutil.copyfile(cand["path"], target)
            except OSError:
                continue
            kept.append({
                "path": os.path.abspath(target),
                "timestamp_sec": round(cand["timestamp_sec"], 1),
                "ocr_summary": cand["text"][:80],
            })
        text_out = format_ocr_output([
            {"timestamp_sec": k["timestamp_sec"], "text": k["ocr_summary"]}
            for k in kept])
        return text_out, kept
    finally:
        if video_path and not keep_video and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass
        shutil.rmtree(frames_dir, ignore_errors=True)
        shutil.rmtree(video_dir, ignore_errors=True)


def extract_frame_text(url: str, output_dir: str,
                       cookies_file: str | None = None,
                       interval: float = 8.0,
                       keep_frames: bool = False,
                       keep_video: bool = False,
                       keyframes_dir: str | None = None,
                       video_id: str = "vid",
                       max_keyframes: int = 12) -> tuple[str, list[dict]]:
    """Main pipeline: download → extract frames → OCR → deduplicate → format.

    Returns (formatted markdown text of on-screen text, exported keyframes list).
    keyframes_dir 不为空时，另将数据/演示关键帧图片复制至该目录（交付物，不随临时清理删除）。
    无论成功或失败（含下载/帧提取/OCR 异常），finally 中都会清理
    低清视频与帧文件（除非 --keep-video / --keep-frames 调试保留）。
    """
    video_dir = os.path.join(output_dir, "video")
    frames_dir = os.path.join(output_dir, "frames")
    video_path = None
    frames: list[dict] = []
    keyframes: list[dict] = []

    try:
        # Step 1: Download low-res video
        video_path = download_video_lowres(url, video_dir, cookies_file)
        if not video_path:
            return "", []

        # Step 2: Extract frames
        frames = extract_frames(video_path, frames_dir, interval)
        if not frames:
            return "", []

        # Step 3: OCR each frame
        print(f"[INFO] 开始OCR识别 (RapidOCR)...")
        t_start = time.time()
        frame_results = []
        for i, f in enumerate(frames):
            texts = ocr_frame_cached(f["path"])
            if texts:
                frame_results.append({
                    "path": f["path"],
                    "timestamp_sec": f["timestamp_sec"],
                    "texts": texts,
                })
            # Progress
            if (i + 1) % 10 == 0:
                elapsed = time.time() - t_start
                fps = (i + 1) / elapsed
                eta = (len(frames) - i - 1) / fps if fps > 0 else 0
                print(f"[INFO] OCR进度: {i+1}/{len(frames)} 帧 "
                      f"({fps:.1f} fps, ETA: {eta:.0f}s)")

        t_ocr = time.time()
        print(f"[INFO] OCR完成: {len(frame_results)}/{len(frames)} 帧有文字 "
              f"({t_ocr - t_start:.1f}s)")

        # Step 4: Deduplicate
        deduped = deduplicate_ocr_results(frame_results)
        print(f"[INFO] 去重后: {len(deduped)} 条唯一文本")

        # Step 4.5: Export data/demo keyframes (交付到 keyframes_dir，原帧随后清理)
        if keyframes_dir:
            keyframes = export_keyframes(frame_results, keyframes_dir,
                                         video_id, max_keyframes)
            if keyframes:
                print(f"[INFO] 导出关键帧 {len(keyframes)} 张 -> {keyframes_dir}")

        # Step 5: Format output
        return format_ocr_output(deduped), keyframes
    finally:
        # 无论成功/失败，清理帧、视频与空的 work 目录（调试 flag 除外）
        if not keep_frames:
            for f in frames:
                try:
                    os.remove(f["path"])
                except OSError:
                    pass
            try:
                shutil.rmtree(frames_dir, ignore_errors=True)
            except Exception:
                pass
        if not keep_video:
            try:
                shutil.rmtree(video_dir, ignore_errors=True)
            except Exception:
                pass
        try:
            os.rmdir(output_dir)  # 目录非空时静默失败
        except OSError:
            pass


# ─── CLI ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="提取B站视频画面中的文字（图表/PPT/演示内容）"
    )
    parser.add_argument("url", help="Bilibili 视频 URL")
    parser.add_argument("--cookies", help="Netscape 格式 Cookie 文件", default=None)
    parser.add_argument("-o", "--output", help="输出文本文件路径", default=None)
    parser.add_argument("-i", "--interval", type=float, default=8.0,
                        help="帧提取间隔（秒），默认 8s")
    parser.add_argument("--output-dir", default=".",
                        help="临时文件目录")
    parser.add_argument("--keep-frames", action="store_true",
                        help="保留提取的帧图片（调试用）")
    parser.add_argument("--keep-video", action="store_true",
                        help="保留下载的低清视频（调试用）")
    parser.add_argument("--keyframes-dir", default=None,
                        help="数据/演示关键帧图片导出目录（交付物，如 attachments；不传则不导出）")
    parser.add_argument("--video-id", default=None,
                        help="关键帧图片命名前缀（缺省从 URL 推导 BV号/视频id/eid）")
    parser.add_argument("--max-keyframes", type=int, default=12,
                        help="关键帧导出上限（默认 12）")
    parser.add_argument("--transcript-file", default=None,
                        help="带时间戳文稿（[mm:ss] 行，即 <字幕输出>.ts.txt）；"
                             "提供时优先走文稿倒推模式（定点抓帧，远快于全帧OCR）")
    parser.add_argument("--cue-gap", type=float, default=45.0,
                        help="文稿线索最小时间间隔（秒），默认 45")

    args = parser.parse_args()

    # 依赖检测
    from _common import check_dependency, derive_video_id, validate_url
    if not check_dependency("yt_dlp"):
        sys.exit(1)
    if not check_dependency("rapidocr_onnxruntime"):
        sys.exit(1)

    if not validate_url(args.url):
        print("[ERROR] 请提供 Bilibili / YouTube / 小宇宙 视频链接", file=sys.stderr)
        sys.exit(1)

    work_dir = os.path.join(args.output_dir, "ocr_temp")
    vid = args.video_id or derive_video_id(args.url)
    result, keyframes = "", []

    # 优先：文稿倒推模式（字幕线索 → 定点抓帧 → OCR 验证）
    if args.transcript_file and args.keyframes_dir and \
            os.path.exists(args.transcript_file):
        result, keyframes = extract_by_cues(
            args.url, args.transcript_file, work_dir,
            cookies_file=args.cookies,
            keyframes_dir=args.keyframes_dir,
            video_id=vid,
            max_keyframes=args.max_keyframes,
            gap_sec=args.cue_gap,
            keep_video=args.keep_video,
        )
        if not keyframes:
            print("[INFO] 文稿倒推无线索/未通过 OCR 验证，回退全帧 OCR 模式")

    # 回退/默认：全帧 OCR 模式（含仅要画面文字不要关键帧的场景）
    if not result:
        result, kf2 = extract_frame_text(
            args.url, work_dir,
            cookies_file=args.cookies,
            interval=args.interval,
            keep_frames=args.keep_frames,
            keep_video=args.keep_video,
            keyframes_dir=args.keyframes_dir,
            video_id=vid,
            max_keyframes=args.max_keyframes,
        )
        if not keyframes:
            keyframes = kf2

    if not result:
        print("[WARN] 未检测到画面文字", file=sys.stderr)
        sys.exit(1)

    # Output
    if args.output:
        output_path = args.output
    else:
        # Derive output path from URL (BV号)
        bv_match = re.search(r'BV[\w]+', args.url)
        bv_id = bv_match.group(0) if bv_match else "video"
        output_path = f"ocr_{bv_id}.txt"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)

    print(f"\n[INFO] 画面文字提取完成")
    print(f"[INFO] 输出文件: {output_path}")
    print(f"\n--- 画面文字预览 (前500字) ---")
    print(result[:500])
    if keyframes:
        print(f"\n[KEYFRAMES] {json.dumps(keyframes, ensure_ascii=False)}")
    print(f"\n[OUTPUT_PATH] {output_path}")
    # 视频/帧已由 extract_frame_text 的 finally 自动清理（导出的关键帧在交付目录，不受影响）


if __name__ == "__main__":
    main()
