#!/usr/bin/env python3
"""
video-summarizer 一键流程整合入口

串起 字幕/转录(extract_subs) → 评论(extract_comments) → 画面OCR(extract_frames)，
并输出结构化 JSON 摘要，供 AI 进一步生成总结笔记。

支持平台：Bilibili / YouTube / 小宇宙（音频播客，无字幕→自动 Whisper 转录；
小宇宙无公开评论接口、无画面，管线自动跳过评论与 OCR）。

用法:
  run_pipeline.py <URL> [--cookies FILE] [--output-dir DIR] [--no-ocr] [--no-comments]
                  [--keep-files] [--stt-model NAME] [--keyframes-dir DIR]
                  [--max-keyframes N] [--no-keyframes]

示例:
  python "06 skill&agent/media-summarizer/scripts/run_pipeline.py" \
      "https://www.bilibili.com/video/BV1xxx" --cookies cookies.txt
  python "06 skill&agent/media-summarizer/scripts/run_pipeline.py" \
      "https://www.xiaoyuzhoufm.com/episode/xxx"

执行顺序与降级逻辑:
  1. 校验 URL（bilibili.com / b23.tv / youtube.com / youtu.be / xiaoyuzhoufm.com）
  2. 获取元数据（yt-dlp --dump-json）→ title/uploader/upload_date/duration；
     小宇宙由 yt-dlp 通用提取器拿不到 upload_date/duration/播客名，
     自动补充解析单集页面 JSON-LD
  3. extract_subs.py → 字幕文本（无字幕自动降级为 STT 转录）
  4. extract_comments.py → 评论产物（失败不阻塞主流程；--no-comments 跳过；
     小宇宙无公开评论接口自动跳过）
  5. extract_frames.py → OCR 画面文字 + 数据/演示关键帧导出（检测到 ffmpeg 自动执行，
     缺失则跳过；--no-ocr 显式关闭；音频内容自动跳过；关键帧默认按**文稿倒推**：
     读 extract_subs 生成的 <字幕输出>.ts.txt 时间戳行，找"这张图/公式/数据"等画面
     线索 → 定点抓帧 → OCR 验证（屏幕无文字则丢弃），无线索时回退全帧 OCR；
     图片导出至 --keyframes-dir（默认 attachments/），命名 <视频ID>_<HHMMSS>.png，
     为交付物不随临时目录清理；--no-keyframes 关闭导出）
  6. 将各产物文件内容内嵌进 JSON 的 contents 字段，keyframes 清单置于 JSON
     （字幕/转录全文由 AI 校对整理后**并入总结笔记同一文件**，不单独生成原文文件；
     关键帧图片由 AI 以 ![[文件名.png]] 插入笔记「字幕原文（校对整理版）」对应时间段处）
  7. 自动清理整个临时目录（--keep-files 可保留；转录失败需恢复时自动跳过清理并提示）

  各子脚本运行中产生的瞬时文件（原始字幕/评论 info.json/低清视频/帧/音频）
  由子脚本自行清理；本管线结束时再整体清理临时目录，无需人工阶段。
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

from _common import (
    add_cookies_arg,
    check_dependency,
    cleanup_temp,
    derive_video_id,
    fetch_xiaoyuzhou_meta,
    find_ffmpeg,
    is_xiaoyuzhou,
    validate_url,
)

# 本脚本所在目录（与各提取脚本同处 scripts/ 下）
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# 各步骤超时（秒）
# 注：字幕/STT 转录的实际超时由 extract_subs.py 按音频时长自动估算
#     （上限 3.5h，--stt-timeout 可覆盖），会先于本外层兜底触发；
#     此处 4h 仅作极端情况下的紧急上限。
TIMEOUT_SUBS = 14400     # 字幕/STT 转录，外层兜底 4 小时
TIMEOUT_COMMENTS = 600   # 评论提取，10 分钟
TIMEOUT_FRAMES = 1800    # OCR，30 分钟（按视频时长动态放宽，见 main）
TIMEOUT_METADATA = 120   # 元数据，2 分钟


def _find_audio_file(output_dir: str) -> str | None:
    """在管线工作目录中查找残留音频文件（STT 失败后用于恢复转录）。"""
    if not output_dir or not os.path.isdir(output_dir):
        return None
    for pattern in ("*.m4a", "*.m4s", "*.mp3", "*.webm", "*.opus", "*.aac"):
        hits = sorted(glob.glob(os.path.join(output_dir, pattern)),
                      key=os.path.getmtime, reverse=True)
        if hits:
            return hits[0]
    return None


def _read_artifact(path: str | None) -> str | None:
    """读取产物文件内容（供 JSON contents 内嵌；缺失/读失败返回 None）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# 纯音频扩展名（用于识别"无画面"内容，自动跳过 OCR）
AUDIO_EXTS = {"m4a", "mp3", "aac", "opus", "ogg", "wav", "flac", "m4b"}


def _auto_cleanup(output_dir: str, artifacts: dict, keep_files: bool) -> bool:
    """管线结束时自动清理临时目录。

    规则（返回 True 表示已清理）：
      - --keep-files：保留全部文件，不清理；
      - 正文（字幕/转录）缺失且存在残留音频：恢复模式，保留文件供
        --local-audio 串行重转录，打印手动清理命令；
      - 其余情况：删除临时目录内全部瞬时产物（保留 cookies*.txt）。
    """
    if keep_files:
        print(f"[INFO] --keep-files 已指定，保留临时目录: {output_dir}")
        return False

    need_recovery = (
        not artifacts.get("subtitle") and not artifacts.get("transcript")
        and _find_audio_file(output_dir) is not None
    )
    if need_recovery:
        print("[WARN] 转录失败且检测到残留音频，保留临时目录供恢复转录", file=sys.stderr)
        print("[HINT] 恢复完成后手动清理: ", file=sys.stderr)
        # 正斜杠避免 Windows 反斜杠在 python -c 字符串字面量中被当作转义
        py = SCRIPTS_DIR.replace("\\", "/")
        out = output_dir.replace("\\", "/")
        print(f'       python -c "import sys; sys.path.insert(0, \'{py}\'); '
              f'from _common import cleanup_temp; cleanup_temp(\'{out}\')"',
              file=sys.stderr)
        return False

    cleanup_temp(output_dir, keep_cookies=True, keep_comments=False)
    # 评论内容已内嵌进 JSON：补充删除 cleanup_temp 扩展名规则外的 comments.*
    # （如 comments.md），其余 comments_*.txt 已由 keep_comments=False 覆盖
    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            if fname.startswith("comments"):
                try:
                    os.remove(os.path.join(root, fname))
                except OSError:
                    pass
    print(f"[INFO] 已自动清理临时目录: {output_dir}")
    return True


def run_script(script_name: str, args: list, timeout: int) -> dict:
    """运行 scripts/ 下的子脚本，捕获输出并解析 [OUTPUT_PATH] / [TRANSCRIPT_TYPE]。

    Returns:
        {
          "returncode": int, "stdout": str, "stderr": str,
          "output_path": str | None, "transcript_type": str | None,
          "error": str | None,
        }
    """
    script = os.path.join(SCRIPTS_DIR, script_name)
    try:
        result = subprocess.run(
            [sys.executable, script] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1, "stdout": "", "stderr": "",
            "output_path": None, "transcript_type": None,
            "error": f"{script_name} 执行超时（>{timeout}s）",
        }

    combined = (result.stdout or "") + "\n" + (result.stderr or "")

    # 解析 [OUTPUT_PATH]（取最后一个；路径可能含空格，捕获整行并去除首尾空白）
    output_path = None
    m = re.findall(r"\[OUTPUT_PATH\]\s*(.+)", combined)
    if m:
        output_path = m[-1].strip()

    # 解析 [TRANSCRIPT_TYPE]
    transcript_type = None
    m2 = re.search(r"\[TRANSCRIPT_TYPE\]\s*(\w+)", combined)
    if m2:
        transcript_type = m2.group(1)

    # 提取错误摘要（取 stderr 最后一个非空行）
    error = None
    if result.returncode != 0:
        err_lines = [ln for ln in (result.stderr or "").splitlines() if ln.strip()]
        error = (err_lines[-1][:500] if err_lines
                 else f"{script_name} 退出码 {result.returncode}")

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "output_path": output_path,
        "transcript_type": transcript_type,
        "error": error,
    }


def fetch_metadata(url: str, cookies_file: str | None) -> dict:
    """通过 yt-dlp --dump-json 获取视频元数据。失败返回 {}。"""
    args = ["--dump-json", "--no-download", "--no-playlist"]
    add_cookies_arg(args, cookies_file)
    args.append(url)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp"] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_METADATA,
        )
    except subprocess.TimeoutExpired:
        return {}
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="video-summarizer 一键流程：字幕/转录 + 评论 + 画面OCR（支持 B站/YouTube/小宇宙）"
    )
    parser.add_argument("url", help="Bilibili / YouTube / 小宇宙 URL")
    parser.add_argument("--cookies", default=None,
                        help="Netscape 格式 Cookie 文件（B 站字幕/评论推荐）")
    parser.add_argument("--output-dir", default=None,
                        help="临时工作目录（默认在系统临时目录下自动创建）")
    parser.add_argument("--no-ocr", action="store_true",
                        help="跳过画面文字 OCR 提取")
    parser.add_argument("--no-comments", action="store_true",
                        help="跳过评论提取")
    parser.add_argument("--keep-files", action="store_true",
                        help="保留临时目录与全部产物文件（调试用，默认自动清理）")
    parser.add_argument("--stt-model", default="base",
                        choices=["tiny", "base", "small", "medium"],
                        help="Whisper 转录模型大小（播客长音频可用 small 提质量，默认 base）")
    parser.add_argument("--keyframes-dir", default="attachments",
                        help="数据/演示关键帧图片导出目录（默认 vault 根 attachments/；"
                             "纯音频/--no-ocr/--no-keyframes 时跳过）")
    parser.add_argument("--no-keyframes", action="store_true",
                        help="不导出关键帧图片（仍做画面 OCR 文字）")
    parser.add_argument("--max-keyframes", type=int, default=12,
                        help="关键帧导出上限（默认 12）")
    parser.add_argument("--no-bili-api", action="store_true",
                        help="禁用 B 站 player API 直取 AI 字幕（默认在 yt-dlp 取不到时启用）")
    parser.add_argument("--min-relevance", type=float, default=0.05,
                        help="字幕↔标题/简介内容相关性下限（默认 0.05，实测正例≈0.4、"
                             "CDN 串片≈0.00）；低于则弃用字幕并降级 STT")
    parser.add_argument("--force-subtitle", action="store_true",
                        help="跳过内容相关性门禁，强行采用已取到的字幕（仅供人工核对调试）")
    args = parser.parse_args()

    # 依赖检测
    if not check_dependency("yt_dlp"):
        sys.exit(1)

    # 1. 校验 URL
    if not validate_url(args.url):
        print("[ERROR] 请提供 Bilibili / YouTube / 小宇宙 链接", file=sys.stderr)
        sys.exit(1)

    # 工作目录
    output_dir = args.output_dir
    if not output_dir:
        output_dir = tempfile.mkdtemp(prefix="video_summarizer_")
        print(f"[INFO] 临时工作目录: {output_dir}")
    os.makedirs(output_dir, exist_ok=True)

    errors: list[str] = []

    # 2. 元数据
    print("[INFO] 步骤 1/4: 获取元数据...")
    info = fetch_metadata(args.url, args.cookies)
    metadata = {
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("uploader_id"),
        "publish_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "url": args.url,
    }

    # 小宇宙：yt-dlp 通用提取器无 upload_date/duration/播客名，从页面 JSON-LD 补充
    # （uploader 例外：yt-dlp 给的是域名 www.xiaoyuzhoufm.com，播客名始终以页面为准）
    if is_xiaoyuzhou(args.url):
        xyz = fetch_xiaoyuzhou_meta(args.url)
        if xyz:
            for key in ("title", "uploader", "publish_date", "duration"):
                if key == "uploader" or not metadata.get(key):
                    if xyz.get(key):
                        metadata[key] = xyz[key]
            print(f"[INFO] 小宇宙页面元数据: 播客={metadata.get('uploader')} "
                  f"日期={metadata.get('publish_date')} "
                  f"时长={metadata.get('duration')}s")
        else:
            errors.append("xiaoyuzhou_meta: 页面元数据解析失败（无日期/时长，文件名将用当天日期）")

    if metadata["title"]:
        print(f"[INFO] 内容: {metadata['title']}")

    # 纯音频内容（小宇宙播客 / 无视频轨）→ 跳过画面 OCR
    audio_only = (
        is_xiaoyuzhou(args.url)
        or (info.get("vcodec") == "none")
        or (info.get("ext") or "").lower() in AUDIO_EXTS
    )

    # 3. 字幕 / 转录
    print("[INFO] 步骤 2/4: 提取字幕 / 转录...")
    sub_output = os.path.join(output_dir, "subtitle.txt")
    sub_args = [args.url, "-o", sub_output, "--output-dir", output_dir]
    add_cookies_arg(sub_args, args.cookies)
    # 按音频时长估算 STT 内层超时（长播客避免默认 1h 提前触发）
    if metadata.get("duration"):
        sub_args += ["--stt-timeout",
                     str(min(max(1800, int(metadata["duration"]) * 5 + 600), 12600))]
    if args.stt_model != "base":
        sub_args += ["--stt-model", args.stt_model]
    if args.no_bili_api:
        sub_args.append("--no-bili-api")
    if args.force_subtitle:
        sub_args.append("--force-subtitle")
    if abs(args.min_relevance - 0.05) > 1e-9:
        sub_args += ["--min-relevance", str(args.min_relevance)]
    sub_res = run_script("extract_subs.py", sub_args, TIMEOUT_SUBS)

    artifacts = {"subtitle": None, "transcript": None, "comments": None, "frames": None}
    transcript_type = sub_res["transcript_type"]

    if sub_res["returncode"] == 0 and sub_res["output_path"] and os.path.exists(sub_res["output_path"]):
        # 产物存在但为空 = 失败（历史坑：空文件仍 exit 0，下游臆造内容）
        try:
            body_size = os.path.getsize(sub_res["output_path"])
        except OSError:
            body_size = 0
        if body_size < 16:
            errors.append(f"extract_subs: 正文产物为空（{body_size} 字节），按失败处理")
        else:
            if transcript_type == "stt":
                artifacts["transcript"] = sub_res["output_path"]
            else:
                artifacts["subtitle"] = sub_res["output_path"]
            print(f"[INFO] 字幕/转录完成: {sub_res['output_path']}")
    else:
        errors.append(f"extract_subs: {sub_res['error'] or '无输出产物'}")
        # 转录失败：若音频已下载，给出可直接执行的串行恢复命令
        leftover = _find_audio_file(output_dir)
        if leftover:
            print(f"[WARN] 转录步骤失败，检测到已下载音频: {leftover}", file=sys.stderr)
            print("[HINT] 串行恢复转录（避免多任务 CPU 争抢再次超时）:", file=sys.stderr)
            print(f'       python "{os.path.join(SCRIPTS_DIR, "transcribe.py")}" '
                  f'--local-audio "{leftover}" -o "{sub_output}" -m base', file=sys.stderr)

    # 4. 评论（失败不阻塞；小宇宙无公开评论接口）
    if args.no_comments:
        print("[INFO] 步骤 3/4: 已跳过评论提取（--no-comments）")
    elif is_xiaoyuzhou(args.url):
        print("[INFO] 步骤 3/4: 小宇宙无公开评论接口，跳过评论提取")
    else:
        print("[INFO] 步骤 3/4: 提取评论...")
        comments_work = os.path.join(output_dir, "comments_work")
        comments_out = os.path.join(output_dir, "comments.md")
        com_args = [args.url, "--output-dir", comments_work, "-o", comments_out]
        add_cookies_arg(com_args, args.cookies)
        com_res = run_script("extract_comments.py", com_args, TIMEOUT_COMMENTS)
        if com_res["returncode"] == 0 and com_res["output_path"] and os.path.exists(com_res["output_path"]):
            artifacts["comments"] = com_res["output_path"]
            print(f"[INFO] 评论完成: {com_res['output_path']}")
        elif com_res["returncode"] != 0:
            errors.append(f"extract_comments: {com_res['error']}")
        else:
            print("[INFO] 未获取到评论数据")

    # 5. 画面 OCR + 关键帧导出（默认检测到 ffmpeg 才执行；纯音频内容自动跳过）
    keyframes: list[dict] = []
    if args.no_ocr:
        print("[INFO] 步骤 4/4: 已跳过画面 OCR（--no-ocr）")
    elif audio_only:
        print("[INFO] 步骤 4/4: 音频内容（无画面），跳过画面 OCR 与关键帧")
    elif find_ffmpeg():
        print("[INFO] 步骤 4/4: 提取画面文字（OCR）+ 数据/演示关键帧...")
        frames_out = os.path.join(output_dir, "ocr.txt")
        frames_work = os.path.join(output_dir, "frames_work")
        fr_args = [args.url, "-o", frames_out, "--output-dir", frames_work]
        add_cookies_arg(fr_args, args.cookies)
        if not args.no_keyframes and args.keyframes_dir:
            fr_args += ["--keyframes-dir", args.keyframes_dir,
                        "--video-id", derive_video_id(args.url),
                        "--max-keyframes", str(args.max_keyframes)]
            # 文稿倒推：有带时间戳字幕副本（extract_subs 生成的 <输出>.ts.txt）
            # 则按讲稿画面线索定点抓帧，远快于全帧 OCR；缺文件自动回退全帧模式
            ts_file = sub_output + ".ts.txt"
            if os.path.exists(ts_file):
                fr_args += ["--transcript-file", ts_file]
        fr_res = run_script("extract_frames.py", fr_args,
                            min(max(TIMEOUT_FRAMES,
                                    int((metadata.get("duration") or 0) * 1.0) + 900),
                                7200))
        # 解析 [KEYFRAMES] json 行（交付图片清单，独立于临时目录存活）
        km_m = re.search(r"\[KEYFRAMES\]\s*(\[.+\])", fr_res["stdout"] or "")
        if km_m:
            try:
                keyframes = json.loads(km_m.group(1))
                print(f"[INFO] 关键帧导出 {len(keyframes)} 张 -> {args.keyframes_dir}")
            except json.JSONDecodeError:
                errors.append("extract_frames: [KEYFRAMES] 解析失败")
        if fr_res["returncode"] == 0 and fr_res["output_path"] and os.path.exists(fr_res["output_path"]):
            artifacts["frames"] = fr_res["output_path"]
            print(f"[INFO] 画面OCR完成: {fr_res['output_path']}")
        elif fr_res["returncode"] != 0:
            errors.append(f"extract_frames: {fr_res['error']}")
        else:
            print("[INFO] 未检测到画面文字")
    else:
        print("[INFO] 未检测到 ffmpeg，跳过画面 OCR（可 pip install imageio-ffmpeg）")

    # 6. 输出结构化 JSON 摘要（含各产物全文，供 AI 总结；随后自动清理临时目录）
    if transcript_type is None:
        transcript_type = "subtitle" if artifacts["subtitle"] else None

    # 先读取产物全文（清理后文件即被删除），再自动清理临时目录
    # （--keep-files / 恢复模式除外）。
    # 注意：字幕/转录全文仅供 AI 校对整理后并入总结笔记同一文件，
    # 管线**不单独生成原文文件**。
    contents = {
        "subtitle": _read_artifact(artifacts["subtitle"]),
        "transcript": _read_artifact(artifacts["transcript"]),
        "comments": _read_artifact(artifacts["comments"]),
        "frames": _read_artifact(artifacts["frames"]),
    }

    cleaned = _auto_cleanup(output_dir, artifacts, args.keep_files)

    # 退出码判定：管线设计为"步骤失败不中断"，但**正文（字幕/转录）缺失**
    # 意味着本次没有任何可用交付，必须非零退出，避免调用方按"exit 0"误判成功。
    body_missing = not ((contents.get("subtitle") or "").strip()
                        or (contents.get("transcript") or "").strip())

    result = {
        "metadata": metadata,
        "artifacts": artifacts,
        "contents": contents,
        "transcript_type": transcript_type,
        "keyframes": keyframes,
        "errors": errors,
        "body_missing": body_missing,
        "output_dir": output_dir,
        "cleaned": cleaned,
    }
    print("\n" + json.dumps(result, ensure_ascii=False, indent=2))

    # 失败步骤醒目提示（避免"管线退出码 0 但正文缺失"被忽略）
    if errors or body_missing:
        print("\n" + "!" * 60, file=sys.stderr)
        print("[WARN] 管线存在失败步骤，产物可能不完整：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        if body_missing:
            print("  - **正文缺失**：本次无可用转录/字幕，"
                  "笔记不得基于标题简介臆造；修复后重跑本管线", file=sys.stderr)
        print("详情见上方 HINT / 按需补跑对应提取脚本。", file=sys.stderr)
        print("!" * 60, file=sys.stderr)

    if body_missing:
        sys.exit(3)


if __name__ == "__main__":
    main()
