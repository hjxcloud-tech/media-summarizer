#!/usr/bin/env python3
"""
Bilibili 评论区提取工具
使用 yt-dlp --write-comments 获取评论，按热度排序输出
"""

import json
import os
import subprocess
import sys


def fetch_comments(url: str, cookies_file: str | None = None,
                   output_dir: str = ".") -> dict | None:
    """Fetch comments via yt-dlp --write-comments and return parsed data.

    Returns dict with keys: comments, count, comment_count, info_file
    """
    args = [
        sys.executable, "-m", "yt_dlp",
        "--write-comments",
        "--skip-download",
        "--no-playlist",
        "-o", os.path.join(output_dir, "%(title)s.%(ext)s"),
        "--write-info-json",
        url,
    ]
    if cookies_file:
        args.extend(["--cookies", cookies_file])
    # YouTube NSIG bypass
    if any(d in url for d in ["youtube.com", "youtu.be"]):
        args.extend(["--remote-components", "ejs:github", "--js-runtimes", "node"])

    result = subprocess.run(args, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        print(f"[ERROR] 评论提取失败:\n{result.stderr}", file=sys.stderr)
        return None

    # Find the info.json file
    json_files = [f for f in os.listdir(output_dir) if f.endswith(".info.json")]
    if not json_files:
        print("[ERROR] 未找到 info.json 文件", file=sys.stderr)
        return None

    json_files.sort(key=lambda f: os.path.getmtime(
        os.path.join(output_dir, f)), reverse=True)
    info_file = os.path.join(output_dir, json_files[0])

    with open(info_file, "r", encoding="utf-8") as f:
        info = json.load(f)

    comments = info.get("comments", [])
    return {
        "comments": comments,
        "count": len(comments),
        "comment_count": info.get("comment_count", 0),
        "info_file": info_file,
    }


def rank_comments(comments: list, top_n: int = 10) -> list[dict]:
    """Rank comments by like_count, return top N with replies."""
    # Sort by likes descending
    sorted_comments = sorted(
        comments,
        key=lambda c: c.get("like_count", 0),
        reverse=True
    )
    return sorted_comments[:top_n]


def format_comments(comments: list, max_per_section: int = 5) -> str:
    """Format ranked comments as markdown.

    Returns sections: 热门评论, 作者回复 (if author is among commenters)
    """
    if not comments:
        return ""

    ranked = rank_comments(comments, top_n=max_per_section * 2)
    lines = []

    # Top comments section
    lines.append("## 热门评论")
    lines.append("")

    author_replies = []
    shown = 0

    for c in ranked:
        if shown >= max_per_section:
            break

        author = c.get("author", "匿名")
        text = c.get("text", "").strip()
        likes = c.get("like_count", 0)
        timestamp = c.get("timestamp", 0)

        if not text:
            continue

        lines.append(f"**{author}**（{likes} 👍）")
        lines.append(f"> {text}")
        lines.append("")

        # Check if author replied (Bilibili uploader replies labeled)
        if "UP主" in author or "up主" in author.lower():
            author_replies.append({"author": author, "text": text, "likes": likes})

        # Include top reply
        replies = c.get("replies", [])
        if replies:
            top_reply = max(replies, key=lambda r: r.get("like_count", 0))
            reply_text = top_reply.get("text", "").strip()
            if reply_text and len(reply_text) > 10:
                lines.append(f"> > 💬 **{top_reply.get('author', '?')}**: {reply_text[:150]}")
                lines.append("")

        shown += 1

    # Author interactions section (if author replied)
    if author_replies:
        lines.append("## UP主互动")
        lines.append("")
        for ar in author_replies:
            lines.append(f"> {ar['text']}")
            lines.append(f"<small>— {ar['author']} · {ar['likes']} 👍</small>")
            lines.append("")

    return "\n".join(lines)


def generate_discussion_insight(comments: list) -> str:
    """Generate a brief insight about community sentiment from comments."""
    if len(comments) < 3:
        return ""

    # Get top comments for sentiment analysis
    ranked = rank_comments(comments, top_n=10)
    texts = [c.get("text", "") for c in ranked if c.get("text")]

    # Simple keyword-based sentiment
    bull_keywords = ["看好", "买入", "加仓", "长期", "价值", "机会", "爆发", "翻倍",
                     "坚定", "持有", "定投", "低吸"]
    bear_keywords = ["泡沫", "风险", "高估", "回调", "割肉", "亏损", "暴跌",
                     "套牢", "谨慎", "观望", "减仓", "清仓"]
    question_keywords = ["怎么看", "请问", "？", "不懂", "疑惑", "为什么"]

    bull_count = sum(1 for t in texts for kw in bull_keywords if kw in t)
    bear_count = sum(1 for t in texts for kw in bear_keywords if kw in t)
    question_count = sum(1 for t in texts for kw in question_keywords if kw in t)

    lines = ["## 社区情绪", ""]

    if bull_count > bear_count:
        lines.append(f"- 偏乐观：热门评论中看好观点占优（👍 {bull_count} vs 🐻 {bear_count}）")
    elif bear_count > bull_count:
        lines.append(f"- 偏谨慎：热门评论中谨慎/看空观点更多（🐻 {bear_count} vs 👍 {bull_count}）")
    else:
        lines.append(f"- 分歧明显：看多与看空观点相当（👍 {bull_count} vs 🐻 {bear_count}）")

    if question_count > 0:
        lines.append(f"- {question_count} 条评论包含疑问，社区存在信息不对称")

    lines.append("")
    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="提取 Bilibili 视频评论")
    parser.add_argument("url", help="Bilibili 视频 URL")
    parser.add_argument("--cookies", help="Netscape 格式 Cookie 文件", default=None)
    parser.add_argument("--output-dir", default=".", help="临时输出目录")
    parser.add_argument("-o", "--output", default=None,
                        help="输出文件路径（写入格式化评论，供管线 [OUTPUT_PATH] 定位）")
    parser.add_argument("--top", type=int, default=5, help="热门评论数量")
    parser.add_argument("--max-replies", type=int, default=3, help="每条评论展示的回复数")
    parser.add_argument("--keep-raw", action="store_true",
                        help="保留下载的原始 info.json（调试用，默认解析后删除）")

    args = parser.parse_args()

    # 依赖检测
    from _common import check_dependency, emit_output_path, validate_url
    if not check_dependency("yt_dlp"):
        sys.exit(1)

    if not validate_url(args.url):
        print("[ERROR] 请提供 Bilibili 或 YouTube 视频链接", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)

    # Fetch comments
    data = fetch_comments(args.url, args.cookies, args.output_dir)
    if not data:
        sys.exit(1)

    comments = data["comments"]
    print(f"[INFO] 提取了 {len(comments)} 条评论（总计 {data['comment_count']} 条）")

    if not comments:
        print("[WARN] 未获取到评论数据")
        sys.exit(0)

    # Format
    formatted = format_comments(comments, max_per_section=args.top)
    sentiment = generate_discussion_insight(comments)
    output_text = "\n\n".join(part for part in (formatted, sentiment) if part)
    print(output_text)

    # Write output file if requested (供 run_pipeline.py 定位产物)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_text)
        emit_output_path(args.output)

    # 自动清理原始 info.json（含全量评论，体积可达数 MB；--keep-raw 保留）
    if not args.keep_raw:
        info_file = data.get("info_file")
        if info_file and os.path.exists(info_file):
            try:
                os.remove(info_file)
                print(f"[INFO] 已清理原始评论数据: {os.path.basename(info_file)}")
            except OSError as e:
                print(f"[WARN] info.json 清理失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
