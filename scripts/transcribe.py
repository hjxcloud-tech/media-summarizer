#!/usr/bin/env python3
"""
Bilibili 音频下载 + 本地 Whisper 语音转文本
用于无 AI 字幕视频的降级方案，使用 faster-whisper 在 CPU 上运行
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def download_audio(url: str, output_dir: str,
                   cookies_file: str | None = None) -> str | None:
    """Download audio from Bilibili video via yt-dlp. Returns audio file path."""
    os.makedirs(output_dir, exist_ok=True)
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    args = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio[ext=m4a]/bestaudio/best[ext=mp4]/best",
        "-o", output_template,
        "--no-playlist",
        "--no-mtime",
    ]
    if cookies_file:
        args.extend(["--cookies", cookies_file])
    args.append(url)

    print("[INFO] 下载音频中...")
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        print(f"[ERROR] 音频下载失败:\n{result.stderr}", file=sys.stderr)
        return None

    # Find downloaded audio file
    audio_files = []
    for ext in ["*.m4a", "*.m4s", "*.mp3", "*.mp4", "*.webm", "*.opus", "*.aac"]:
        audio_files.extend(Path(output_dir).glob(ext))
    # Exclude .info.json
    audio_files = [f for f in audio_files if not f.name.endswith(".info.json")]

    if not audio_files:
        print("[ERROR] 未找到下载的音频文件", file=sys.stderr)
        return None

    audio_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(audio_files[0])


def _fmt_ts(sec: float) -> str:
    """秒数转 mm:ss / hh:mm:ss（供行首时间戳 tag）。"""
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# 模型缓存：分块转录时避免反复加载（同一进程内复用）
_MODEL_CACHE: dict = {}

# HF 缓存候选根（按顺序探测）：本机曾出现 HF_HOME 指向空目录、模型实际装在
# 默认用户缓存的情况 → 加载报 LocalEntryNotFoundError 但脚本仍可能被判成功。
_HF_MODEL_REPO_TMPL = "models--Systran--faster-whisper-{size}"


def _hf_cache_candidates() -> list[str]:
    """列出 HF 缓存根候选（hub 目录形态），按优先级去重。"""
    raw: list[str] = []
    if os.environ.get("HF_HUB_CACHE"):
        raw.append(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        raw.append(os.path.join(os.environ["HF_HOME"], "hub"))
        raw.append(os.environ["HF_HOME"])
    home = os.path.expanduser("~")
    raw.append(os.path.join(home, ".cache", "huggingface", "hub"))
    raw.append(os.path.join(home, ".cache", "huggingface"))
    out, seen = [], set()
    for p in raw:
        if p and os.path.isdir(p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _find_local_model_root(model_size: str) -> str | None:
    """返回包含 faster-whisper <size> 快照的缓存根目录；未命中返回 None。"""
    repo = _HF_MODEL_REPO_TMPL.format(size=model_size)
    for root in _hf_cache_candidates():
        d = os.path.join(root, repo)
        if not os.path.isdir(d):
            continue
        snap = os.path.join(d, "snapshots")
        try:
            for name in os.listdir(snap):
                if glob.glob(os.path.join(snap, name, "model.bin")):
                    return root
        except OSError:
            continue
    return None


def _get_model(model_size: str):
    """按尺寸缓存 Whisper 模型（int8/CPU）。

    先试本地已缓存快照（download_root + local_files_only，避开 HF_HOME 指错
    目录、离线/网络受限导致的加载失败），未命中再走在线下载（hf-mirror）。
    """
    if model_size not in _MODEL_CACHE:
        from faster_whisper import WhisperModel
        root = _find_local_model_root(model_size)
        if root:
            print(f"[INFO] 加载 Whisper 模型: {model_size} (本地缓存: {root})")
            try:
                _MODEL_CACHE[model_size] = WhisperModel(
                    model_size, device="cpu", compute_type="int8",
                    download_root=root, local_files_only=True)
                return _MODEL_CACHE[model_size]
            except Exception as e:  # 本地快照损坏/字段缺失 → 回退在线下载
                print(f"[WARN] 本地缓存加载失败（{type(e).__name__}: "
                      f"{str(e)[:120]}），回退在线下载", file=sys.stderr)
        else:
            print(f"[INFO] 加载 Whisper 模型: {model_size} "
                  f"(未找到本地缓存，首次运行需下载 ~150MB-1.5GB)")
        _MODEL_CACHE[model_size] = WhisperModel(model_size, device="cpu",
                                                compute_type="int8")
    return _MODEL_CACHE[model_size]


def _find_ffmpeg_exe() -> str | None:
    """定位 ffmpeg（优先 imageio_ffmpeg 自带二进制）。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except ImportError:
        pass
    for name in ("ffmpeg", "ffmpeg.exe"):
        try:
            r = subprocess.run(["where", name], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[0]
        except OSError:
            continue
    return None


def _probe_duration(ffmpeg_exe: str, audio_path: str) -> float:
    """用 ffmpeg -i 解析音频时长（秒）；失败返回 0。"""
    try:
        r = subprocess.run([ffmpeg_exe, "-i", audio_path],
                           capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d+)", r.stderr or "")
    if not m:
        return 0.0
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            + float("0." + m.group(4)))


def _split_audio(ffmpeg_exe: str, audio_path: str, out_dir: str,
                 chunk_sec: int) -> list[str]:
    """按固定时长切片（-c copy，秒级近似边界），返回切片路径列表。"""
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(audio_path)[1] or ".m4a"
    pattern = os.path.join(out_dir, f"chunk_%03d{ext}")
    try:
        r = subprocess.run([
            ffmpeg_exe, "-y", "-i", audio_path, "-f", "segment",
            "-segment_time", str(chunk_sec), "-c", "copy",
            "-loglevel", "error", pattern,
        ], capture_output=True, text=True, timeout=1800)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[WARN] 音频分块失败: {e}", file=sys.stderr)
        return []
    if r.returncode != 0:
        print(f"[WARN] 音频分块失败: {(r.stderr or '')[-200:]}", file=sys.stderr)
        return []
    return sorted(glob.glob(os.path.join(out_dir, f"chunk_*{ext}")))


def _shift_timestamps(text: str, offset_sec: float) -> str:
    """把行首 [mm:ss]/[hh:mm:ss] 时间戳整体平移 offset 秒。"""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]\s*(.*)$", line)
        if m:
            sec = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
                   + int(m.group(3)) + offset_sec)
            out.append(f"[{_fmt_ts(sec)}] {m.group(4)}")
        else:
            out.append(line)
    return "\n".join(out)


def transcribe_chunked(audio_path: str, model_size: str = "base",
                       language: str = "zh", timestamps: bool = True,
                       chunk_sec: int = 1200,
                       tmp_dir: str | None = None) -> str:
    """长音频分块转录：整段 mel 频谱 1 次性分配会 OOM（如 85min≈1.5GB），
    按时长切片后逐块转录（模型复用），时间戳按块偏移合并。

    短音频（≤1.5×chunk_sec）直接单次转录。
    """
    ffmpeg_exe = _find_ffmpeg_exe()
    duration = _probe_duration(ffmpeg_exe, audio_path) if ffmpeg_exe else 0.0
    if not ffmpeg_exe or duration <= chunk_sec * 1.5:
        return transcribe_audio(audio_path, model_size, language, None, timestamps)

    tmp_dir = tmp_dir or os.path.join(tempfile.gettempdir(), "whisper_chunks")
    print(f"[INFO] 长音频 {duration/60:.1f} 分钟 → 按 {chunk_sec//60} 分钟分块转录"
          f"（规避整段 mel 频谱内存峰值）")
    chunks = _split_audio(ffmpeg_exe, audio_path, tmp_dir, chunk_sec)
    if not chunks:
        print("[WARN] 分块失败，回退整段转录", file=sys.stderr)
        return transcribe_audio(audio_path, model_size, language, None, timestamps)

    parts: list[str] = []
    try:
        for i, chunk in enumerate(chunks):
            print(f"[INFO] --- 分块 {i+1}/{len(chunks)}: "
                  f"{os.path.basename(chunk)} ---")
            part = transcribe_audio(chunk, model_size=model_size,
                                    language=language, output_path=None,
                                    timestamps=timestamps)
            parts.append(_shift_timestamps(part, i * chunk_sec)
                         if timestamps else part)
    finally:
        for chunk in chunks:
            if os.path.exists(chunk):
                try:
                    os.remove(chunk)
                except OSError:
                    pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
    return "\n".join(parts)


def transcribe_audio(audio_path: str, model_size: str = "base",
                     language: str = "zh",
                     output_path: str | None = None,
                     timestamps: bool = True) -> str:
    """Transcribe audio file using faster-whisper.

    Args:
        audio_path: Path to audio file
        model_size: Whisper model size (tiny, base, small, medium, large)
        language: Language code (zh for Chinese)
        output_path: Optional output text file path
        timestamps: 行首加段级时间戳 [mm:ss]/[hh:mm:ss]（默认开启，
                    供存疑标注/定位核对；--no-timestamps 关闭保持纯文本）

    Returns:
        Transcribed text
    """
    # Use HF mirror for users in China
    if "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    t_start = time.time()
    model = _get_model(model_size)

    t_load = time.time()
    print(f"[INFO] 模型就绪 ({t_load - t_start:.1f}s)，开始转录...")

    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,  # filter out silence
        vad_parameters=dict(
            min_silence_duration_ms=500,
        ),
    )

    lines = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            line = f"[{_fmt_ts(segment.start)}] {text}" if timestamps else text
            lines.append(line)

        # Progress indicator
        if len(lines) % 20 == 0 and len(lines) > 0:
            elapsed = time.time() - t_load
            duration_sec = info.duration if info else 0
            progress_sec = segment.end
            if duration_sec > 0:
                eta = (elapsed / progress_sec) * (duration_sec - progress_sec)
                print(f"[INFO] 进度: {progress_sec:.0f}s/{duration_sec:.0f}s "
                      f"({progress_sec/duration_sec*100:.0f}%) ETA: {eta:.0f}s")

    result = "\n".join(lines)
    t_end = time.time()

    audio_duration = info.duration if info else 0
    speed = audio_duration / (t_end - t_load) if (t_end - t_load) > 0 else 0
    print(f"[INFO] 转录完成: {len(lines)} 行, {len(result)} 字符")
    print(f"[INFO] 耗时: {t_end - t_start:.0f}s | 速度: {speed:.1f}x 实时 "
          f"(音频 {audio_duration:.0f}s)")

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"[INFO] 已保存: {output_path}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="下载 Bilibili 视频音频并转录为文本"
    )
    parser.add_argument("url", nargs="?", help="Bilibili 视频 URL（使用 --local-audio 时可省略）")
    parser.add_argument("--local-audio", help="直接转录本地音频文件（跳过下载，用于恢复失败步骤）", default=None)
    parser.add_argument("--cookies", help="Netscape 格式 Cookie 文件", default=None)
    parser.add_argument("-o", "--output", help="输出文本文件路径", default=None)
    parser.add_argument("-m", "--model", default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper 模型大小 (default: base)")
    parser.add_argument("-l", "--language", default="zh",
                        help="音频语言 (default: zh)")
    parser.add_argument("--output-dir", default=".",
                        help="音频下载临时目录")
    parser.add_argument("--audio-only", action="store_true",
                        help="仅下载音频，不转录")
    parser.add_argument("--keep-audio", action="store_true",
                        help="保留下载的音频文件（默认自动删除）")
    parser.add_argument("--no-timestamps", action="store_true",
                        help="行首不带段级时间戳 [mm:ss]（默认带，供存疑定位核对）")
    parser.add_argument("--chunk-minutes", type=int, default=20,
                        help="长音频分块转录的块长（分钟，默认 20；规避整段 mel OOM）")
    parser.add_argument("--no-chunk", action="store_true",
                        help="禁用分块，整段一次性转录音频")

    args = parser.parse_args()

    # 依赖检测
    from _common import check_dependency, validate_url
    if args.local_audio:
        # 恢复路径：直接转录已下载/已存在的音频，无需 URL 与 yt-dlp
        if not os.path.exists(args.local_audio):
            print(f"[ERROR] 音频文件不存在: {args.local_audio}", file=sys.stderr)
            sys.exit(1)
        if not check_dependency("faster_whisper"):
            sys.exit(1)
        audio_path = args.local_audio
        print(f"[INFO] 使用本地音频: {audio_path} ({os.path.getsize(audio_path)/1024/1024:.1f}MB)")
    else:
        if not args.url:
            print("[ERROR] 需要提供视频 URL 或 --local-audio", file=sys.stderr)
            sys.exit(1)
        if not check_dependency("yt_dlp"):
            sys.exit(1)
        if not check_dependency("faster_whisper"):
            sys.exit(1)

        if not validate_url(args.url):
            print("[ERROR] 请提供 Bilibili / YouTube / 小宇宙 链接", file=sys.stderr)
            sys.exit(1)

        # Step 1: Download audio
        audio_path = download_audio(args.url, args.output_dir, args.cookies)
        if not audio_path:
            sys.exit(1)

        print(f"[INFO] 音频文件: {audio_path} ({os.path.getsize(audio_path)/1024/1024:.1f}MB)")

    if args.audio_only:
        sys.exit(0)

    try:
        # Step 2: Transcribe
        if not args.output:
            args.output = str(Path(audio_path).with_suffix(".txt"))

        if args.no_chunk:
            text = transcribe_audio(
                audio_path,
                model_size=args.model,
                language=args.language,
                output_path=args.output,
                timestamps=not args.no_timestamps,
            )
        else:
            text = transcribe_chunked(
                audio_path,
                model_size=args.model,
                language=args.language,
                timestamps=not args.no_timestamps,
                chunk_sec=max(300, args.chunk_minutes * 60),
                tmp_dir=os.path.join(args.output_dir, "chunks"),
            )
            os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[INFO] 已保存: {args.output}")

        # 空产物即失败：非零退出，且绝不打印 OUTPUT_PATH（防"假成功"）
        stripped = (text or "").strip()
        out_size = os.path.getsize(args.output) if os.path.exists(args.output) else 0
        if not stripped or out_size < 16:
            print(f"[ERROR] 转录产物为空（文本 {len(stripped)} 字符 / 文件 {out_size} 字节），"
                  f"判定为失败：请确认音频非纯静音、模型加载成功", file=sys.stderr)
            sys.exit(2)

        print(f"\n--- 转录文本预览 (前500字) ---")
        print(text[:500])
        print(f"[TRANSCRIPT_TYPE] stt")
        print(f"\n[OUTPUT_PATH] {args.output}")
    except SystemExit:
        raise
    except Exception as e:
        # 关键：模型加载失败/转录中断必须非零退出，避免"产物存在但为空、
        # 调用方却按成功处理"的假成功（2026-09-29 实测踩过）。
        print(f"[ERROR] 转录失败: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        if not _find_local_model_root(args.model):
            print("[HINT] 未找到本地模型缓存。可设 HF_HOME 指向已含模型的缓存目录"
                  "（如 C:\\Users\\<user>\\.cache\\huggingface），"
                  "或联网下载后重试；已缓存过仍报错请检查磁盘/快照完整性。",
                  file=sys.stderr)
        sys.exit(2)
    finally:
        # 清理下载的音频缓存（成功/失败路径均执行；
        # --local-audio 指向用户已有文件，绝不删除）
        if not args.keep_audio and not args.local_audio and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
                print(f"[INFO] 已清理音频缓存: {audio_path}")
            except OSError as e:
                print(f"[WARN] 音频缓存清理失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
