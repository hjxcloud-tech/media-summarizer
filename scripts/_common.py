#!/usr/bin/env python3
"""
video-summarizer 共享工具模块
提供 URL 校验、Cookie 参数、输出路径标记、依赖检测、ffmpeg 定位、临时文件清理。
被 scripts/ 下各脚本与 run_pipeline.py 复用，保证行为一致。
"""

import json
import os
import re
import subprocess
import sys
import urllib.request

# ─── URL 校验 ────────────────────────────────────────────────

# 受支持的视频/播客平台域名片段
SUPPORTED_DOMAINS = ("bilibili.com", "b23.tv", "youtube.com", "youtu.be",
                     "xiaoyuzhoufm.com")


def validate_url(url: str) -> bool:
    """校验 URL 是否属于受支持平台。"""
    return any(d in url for d in SUPPORTED_DOMAINS)


def is_xiaoyuzhou(url: str) -> bool:
    """判断是否为小宇宙播客链接。"""
    return "xiaoyuzhoufm.com" in url


def is_bilibili(url: str) -> bool:
    """判断是否为 B 站链接（含 b23.tv 短链）。"""
    return "bilibili.com" in url or "b23.tv" in url


def remove_quiet(path: str | None) -> None:
    """删除文件，忽略不存在/权限异常（清理瞬时产物用）。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# ─── 小宇宙页面元数据 ─────────────────────────────────────────

# 小宇宙单集页为 Next.js 渲染，yt-dlp 通用提取器只能拿到音频直链与标题，
# upload_date / duration / 播客名需从页面 JSON-LD（schema.org PodcastEpisode）解析。
_XIAOYUZHOU_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
}


def _parse_iso8601_duration(value: str) -> int | None:
    """解析 ISO 8601 时长（如 PT69M / PT1H30M / PT1H30M5S）为秒。失败返回 None。"""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", (value or "").strip())
    if not m:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def fetch_xiaoyuzhou_meta(url: str, timeout: int = 60) -> dict:
    """抓取小宇宙单集页面元数据（JSON-LD），失败返回 {}。

    Returns:
        {"title": 单集标题, "publish_date": "YYYY-MM-DD",
         "duration": 秒, "uploader": 播客名, "description": 简介}
    """
    try:
        req = urllib.request.Request(url, headers=_XIAOYUZHOU_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except OSError:
        return {}

    pattern = re.compile(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        re.DOTALL,
    )
    for m in pattern.finditer(html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        # JSON-LD 可能为单对象或数组
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict) or item.get("@type") != "PodcastEpisode":
                continue
            meta: dict = {}
            if item.get("name"):
                meta["title"] = item["name"]
            dp = item.get("datePublished")
            if dp:
                meta["publish_date"] = str(dp)[:10]
            dur = _parse_iso8601_duration(item.get("timeRequired") or "")
            if dur:
                meta["duration"] = dur
            series = item.get("partOfSeries")
            if isinstance(series, dict) and series.get("name"):
                meta["uploader"] = series["name"]
            if item.get("description"):
                meta["description"] = item["description"]
            if meta:
                return meta
    return {}


# ─── Cookie 参数 ─────────────────────────────────────────────

def add_cookies_arg(args: list, cookies_file: str | None) -> None:
    """向 yt-dlp 命令行参数列表追加 --cookies <file>。"""
    if cookies_file:
        args.extend(["--cookies", cookies_file])


# ─── 视频 ID（关键帧图片命名用） ─────────────────────────────

def derive_video_id(url: str) -> str:
    """从 URL 推导短视频 ID，用于关键帧图片唯一命名。

    B站 → BV号；YouTube → video id；小宇宙 → eid 前 8 位；兜底 "vid"。
    """
    m = re.search(r'BV[\w]{5,}', url)
    if m:
        return m.group(0)
    m = re.search(r'(?:v=|youtu\.be/)([\w-]{6,})', url)
    if m:
        return m.group(1)[:12]
    m = re.search(r'/episode/([0-9a-f]{6,})', url)
    if m:
        return m.group(1)[:8]
    return "vid"


# ─── 输出路径标记 ────────────────────────────────────────────

OUTPUT_PATH_MARKER = "[OUTPUT_PATH]"
TRANSCRIPT_TYPE_MARKER = "[TRANSCRIPT_TYPE]"


def emit_output_path(path: str) -> None:
    """输出机器可解析的产物路径标记：[OUTPUT_PATH] <path>。"""
    print(f"{OUTPUT_PATH_MARKER} {path}")


# ─── 依赖检测 ───────────────────────────────────────────────

def check_dependency(module_name: str, pip_name: str | None = None,
                     extra_hint: str | None = None) -> bool:
    """检测 Python 模块是否可用；缺失时输出 pip install 指引。

    Args:
        module_name: import 名（如 yt_dlp、faster_whisper、rapidocr_onnxruntime）
        pip_name:    pip 安装名（默认由 module_name 转换，如 yt_dlp -> yt-dlp）
        extra_hint:  附加提示文本

    Returns:
        True 表示模块可用，False 表示缺失。
    """
    try:
        __import__(module_name)
        return True
    except ImportError:
        pkg = pip_name or module_name.replace("_", "-")
        msg = f"[ERROR] 缺少依赖: {module_name}。请运行: pip install {pkg}"
        if extra_hint:
            msg += f"（{extra_hint}）"
        print(msg, file=sys.stderr)
        return False


# ─── ffmpeg 定位 ────────────────────────────────────────────

_ffmpeg_path: str | None = None


def find_ffmpeg() -> str | None:
    """定位 ffmpeg 可执行文件。

    优先使用 imageio_ffmpeg 自带的二进制（免额外下载），其次搜索系统 PATH。
    返回 ffmpeg 路径；未找到返回 None。
    """
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path

    # 1. imageio_ffmpeg 自带二进制
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            _ffmpeg_path = exe
            return _ffmpeg_path
    except ImportError:
        pass

    # 2. 系统 PATH 搜索（Windows 用 where，类 Unix 用 which）
    for name in ("ffmpeg", "ffmpeg.exe"):
        for cmd in (("where", name), ("which", name)):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    _ffmpeg_path = result.stdout.strip().splitlines()[0]
                    return _ffmpeg_path
            except OSError:
                continue

    return None


# ─── 临时文件清理 ───────────────────────────────────────────

# 需要删除的扩展名集合（不含 .info.json，单独处理）。
# 覆盖：文本/音频/视频/帧图片/原始字幕（vtt/srt/ass/lrc/json）
_CLEANUP_EXTS = {
    ".txt", ".m4a", ".m4s", ".mp3", ".mp4", ".webm", ".opus", ".aac",
    ".vtt", ".srt", ".ass", ".lrc", ".json", ".mkv", ".flv",
    ".jpg", ".jpeg", ".png",
}


def cleanup_temp(output_dir: str, keep_cookies: bool = True,
                 keep_comments: bool = True) -> None:
    """删除临时产物，保留 cookie 文件与评论产物（可按需关闭）。

    删除：文本/音频/视频/帧/原始字幕等临时文件以及 *.info.json
    保留：cookies*.txt 与 comments.*（评论 JSON / Markdown）

    Args:
        output_dir:     管线临时工作目录
        keep_cookies:   是否保留 cookies*.txt（默认 True）
        keep_comments:  是否保留 comments.*（默认 True；管线自动清理时
                        内容已内嵌进 JSON，可传 False 一并删除）
    """
    if not os.path.isdir(output_dir):
        return

    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            if fname.startswith("cookies") and keep_cookies:
                continue
            if fname.startswith("comments") and keep_comments:
                continue

            # 删除 yt-dlp 元数据（含原始评论的 info.json）
            if fname.endswith(".info.json"):
                _remove_file(root, fname)
                continue

            ext = os.path.splitext(fname)[1].lower()
            if ext in _CLEANUP_EXTS:
                _remove_file(root, fname)

    # 清理空目录
    for root, dirs, _files in os.walk(output_dir, topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass


def _remove_file(dirpath: str, fname: str) -> None:
    """安全删除单个文件（忽略错误）。"""
    try:
        os.remove(os.path.join(dirpath, fname))
    except OSError:
        pass
