#!/usr/bin/env python3
"""media-summarizer 自检：字幕门禁、HF 缓存回退、空产物退出码（可选：管线正文缺失退出码）。

用法：
    python scripts/selftest_gate.py            # 离线用例（纯逻辑 + 本地模型缓存 + 空音频）
    python scripts/selftest_gate.py --online --cookies <cookies.txt> \
        --bvid BV1xxxxxxxxxx                   # 追加真实视频端到端用例（需网络）

退出码：0 全通过；1 有失败。修改 extract_subs.py / transcribe.py / run_pipeline.py 后先跑本脚本。
"""
import argparse
import os
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import extract_subs as es  # noqa: E402

REF = ("华为海思光电子 CPO和NPO的真心话大冒险 拆解Ai时代光互连的选择 "
       "光互联 CPO NPO 硅光 光模块 超节点 海思")
DUR = 1554.0
RICH = ("华为海思光电子 CPO 和 NPO 的真心话大冒险 拆解 Ai 时代光互连的选择 "
        "光互联 CPO NPO 硅光 光模块 超节点 海思 确定性 比单点性能更重要")
GOOD_TS = "\n".join(f"[{m:02d}:{s:02d}] 讲一下 CPO 与 NPO 的硅光方案"
                    for m in (1, 5, 12, 20) for s in (0, 30)) + "\n[25:40] 确定性比单点性能更重要"
WRONG_TS = "[01:00] 今天吃几碗烧烤的总结就是两个字\n[05:00] 学做菜 会吃菜 老瑞\n[08:00] 济南的烧烤"
TRUNC_TS = "[00:30] CPO 硅光 NPO 光模块 超节点\n[04:40] 确定性很重要"
MIXED_TS = "\n".join(f"[{m:02d}:{s:02d}] CPO 硅光 NPO"
                     for m in (10, 30, 50) for s in (0, 30))

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    if not cond:
        fails.append(name)


def test_gate() -> None:
    cases = [
        ("正确字幕放行", GOOD_TS, RICH, True, "ok"),
        ("串片字幕拦下", WRONG_TS, "烧烤 做菜 老瑞 济南", False, "relevance"),
        ("截断字幕拦下", TRUNC_TS, RICH, False, "coverage"),
        ("混拼字幕拦下", MIXED_TS, RICH, False, "coverage"),
    ]
    for name, ts, text, want_ok, want_why in cases:
        ok, why, rel, cov = es.subtitle_gate(text, ts, REF, DUR, es.RELEVANCE_MIN, False)
        check(name, ok is want_ok and want_why in why,
              f"ok={ok} rel={rel:.3f} cov={cov:.2f} reason={why}")
    ok_f, why_f, _, _ = es.subtitle_gate("烧烤 做菜", WRONG_TS, REF, DUR,
                                         es.RELEVANCE_MIN, True)
    check("--force-subtitle 放行", ok_f and why_f == "forced", why_f)
    ok_s, _, _, cov_s = es.subtitle_gate("内容", "[25:40] 内容", "短标题", DUR,
                                         es.RELEVANCE_MIN, False)
    check("词项过少时只判覆盖率", ok_s and cov_s > 0.85, f"cov={cov_s:.2f}")


def test_cookie_header(cookies: str | None) -> None:
    if not cookies:
        print("[SKIP] 未给 --cookies，跳过 Netscape Cookie 解析用例")
        return
    hdr = es.load_netscape_cookie_header(cookies)
    check("Netscape cookie → 请求头", bool(hdr) and "=" in hdr, f"len={len(hdr or '')}")
    check("缺失 Cookie 文件返回 None",
          es.load_netscape_cookie_header(os.path.join(tempfile.gettempdir(), "nope_ms.txt")) is None)


def test_hf_and_empty() -> None:
    """把 HF_HOME 指到空目录（复现历史故障），验证本地缓存回退与空产物非零退出。"""
    import transcribe as t
    root = t._find_local_model_root("base")
    check("探测到本地 Whisper 缓存", bool(root) and os.path.isdir(str(root)), f"root={root}")

    env = dict(os.environ)
    empty = os.path.join(tempfile.gettempdir(), "ms_empty_hf")
    os.makedirs(empty, exist_ok=True)
    env["HF_HOME"] = empty
    env.pop("HF_HUB_CACHE", None)
    code = (f"import sys; sys.path.insert(0, r'{SCRIPT_DIR}'); import transcribe as t;"
            "t._get_model('base'); print('LOADED')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, timeout=1200)
    check("HF_HOME 指空目录仍能加载模型", r.returncode == 0 and "LOADED" in r.stdout,
          (r.stderr or "").strip().splitlines()[-1][:120] if r.returncode else "")

    ff = None
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if not ff:
        print("[SKIP] 无 ffmpeg，跳过空产物用例")
        return
    tmp = tempfile.mkdtemp(prefix="ms_selftest_")
    wav = os.path.join(tmp, "silence.wav")
    subprocess.run([ff, "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1", wav],
                   capture_output=True, timeout=180)
    r2 = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "transcribe.py"),
                         "--local-audio", wav, "-o", os.path.join(tmp, "o.txt"),
                         "-m", "tiny", "--no-chunk"],
                        capture_output=True, text=True, env=env, timeout=1800)
    check("空转录产物退出码 2", r2.returncode == 2, f"rc={r2.returncode}")
    check("空产物不打印 OUTPUT_PATH", "OUTPUT_PATH" not in (r2.stdout or ""))


def test_pipeline_body_missing(cookies: str, bvid: str) -> None:
    """无字幕 + 无可用模型 → 管线必须非零退出（历史坑：exit 0 假成功）。"""
    tmp = tempfile.mkdtemp(prefix="ms_pl_")
    fake_home = os.path.join(tmp, "fakehome")
    os.makedirs(fake_home, exist_ok=True)
    env = dict(os.environ)
    env.update(HF_HOME=os.path.join(tmp, "emptyhf"), HF_HUB_OFFLINE="1",
               HOME=fake_home, USERPROFILE=fake_home,
               HF_ENDPOINT="http://127.0.0.1:1")
    os.makedirs(env["HF_HOME"], exist_ok=True)
    r = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "run_pipeline.py"),
                        f"https://www.bilibili.com/video/{bvid}/",
                        "--cookies", cookies, "--no-ocr", "--no-comments",
                        "--stt-model", "tiny", "--output-dir", os.path.join(tmp, "pl")],
                       capture_output=True, text=True, env=env, timeout=2400)
    body_missing = '"body_missing": true' in (r.stdout or "")
    check("正文缺失时管线非零退出", r.returncode != 0, f"rc={r.returncode}")
    check("JSON body_missing=true", body_missing, "")


def main() -> int:
    ap = argparse.ArgumentParser(description="media-summarizer 自检")
    ap.add_argument("--cookies", default=None, help="B站 Netscape Cookie 文件（--online 必需）")
    ap.add_argument("--bvid", default=None, help="用于端到端用例的 BV 号（--online 必需）")
    ap.add_argument("--online", action="store_true", help="追加需要网络的真实视频用例")
    args = ap.parse_args()

    test_gate()
    test_cookie_header(args.cookies)
    test_hf_and_empty()
    if args.online:
        if not (args.cookies and args.bvid):
            print("[SKIP] --online 需同时给 --cookies 与 --bvid")
        else:
            test_pipeline_body_missing(args.cookies, args.bvid)

    print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
