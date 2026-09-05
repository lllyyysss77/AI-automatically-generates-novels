#!/usr/bin/env python3
"""命令行自动写作器 —— 可断点续跑.

  python3 run_novel.py init  --title "重生之我成了西门庆" --genre lishi-jiakong \
                             --style fanqie-shuangwen --words 500000
  python3 run_novel.py run   --title "重生之我成了西门庆" --chapters 5
  python3 run_novel.py status --title "重生之我成了西门庆"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.orchestrator import Project, Novelist, create_project, slugify
from server.settings import load as load_settings


def cmd_init(a):
    cfg = load_settings()
    avg = (cfg["generation"]["chapter_words_min"] + cfg["generation"]["chapter_words_max"]) // 2
    chapters = a.chapters or max(1, round(a.words / avg))
    limit = cfg["limits"]["max_chapters"]
    if chapters > limit:
        print(f"! 章节数 {chapters} 超过全局上限 {limit}, 已截断")
        chapters = limit
    p = create_project(
        title=a.title, type_id=a.type, genre_id=a.genre, style_id=a.style,
        target_chapters=chapters, target_words=a.words,
        fields={"premise": a.premise or a.title, "background": a.background or "",
                "characters": "", "relationships": "", "kb": "", "style": a.extra or ""},
    )
    print(f"✓ 建项目 {p.dir}")
    print(f"  目标 {chapters} 章 / {a.words:,} 字 (单章 {avg} 字)")
    print(f"  题材={a.genre} 文风={a.style} 模型={p.meta['model']}")


def cmd_run(a):
    p = Project(slugify(a.title))
    if not p.meta:
        sys.exit("项目不存在, 先跑 init")
    nv = Novelist(p)
    t0 = time.time()

    if not p.read("world_bible.md"):
        print("[1/4] 世界观圣经"); nv.step_world_bible()
    if not p.read("characters.md"):
        print("[2/4] 角色档案"); nv.step_characters()
    if not p.read("outline.md"):
        print("[3/4] 总纲"); nv.step_outline()

    print("[4/4] 逐章生成")
    batch = p.cfg["generation"]["outline_batch"]
    start = (p.state.get("current") or 0) + 1
    end = min(start + a.chapters - 1, p.meta["target_chapters"])
    n = start
    while n <= end:
        outlines = p._load("chapter_outlines.json", {})
        if str(n) not in outlines:
            print(f"  → 生成第 {n}-{n+batch-1} 章细纲")
            nv.step_chapter_outlines(n, batch)
        r = nv.step_chapter(n)
        print(f"  ✓ 第{r['chapter']}章 {r['chars']}字 得分{r['score']}"
              f"{' [已重写]' if r['rewritten'] else ''} {r['elapsed']:.1f}s")
        n += 1

    done = len(p.state["done"])
    tw = p.total_words
    el = time.time() - t0
    print(f"\n本次 {end-start+1} 章 / {el:.0f}s  累计 {done} 章 {tw:,} 字")
    if done:
        speed = tw / el if el else 0
        left = p.meta["target_words"] - tw
        print(f"速率 {speed:.0f} 字/秒  剩余 {left:,} 字 预计 {left/speed/3600:.1f} 小时" if speed else "")


def cmd_status(a):
    p = Project(slugify(a.title))
    if not p.meta:
        sys.exit("项目不存在")
    print(p.board())
    try:
        nv = Novelist(p)
        print("\n## 记忆索引\n" + json.dumps(p.mem.stats(), ensure_ascii=False))
    except Exception as e:
        print("memory:", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init"); i.set_defaults(f=cmd_init)
    i.add_argument("--title", required=True)
    i.add_argument("--type", default="novel")
    i.add_argument("--genre", default="lishi-jiakong")
    i.add_argument("--style", default="fanqie-shuangwen")
    i.add_argument("--words", type=int, default=500000)
    i.add_argument("--chapters", type=int, default=0)
    i.add_argument("--premise", default="")
    i.add_argument("--background", default="")
    i.add_argument("--extra", default="")

    r = sub.add_parser("run"); r.set_defaults(f=cmd_run)
    r.add_argument("--title", required=True)
    r.add_argument("--chapters", type=int, default=3)

    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    s.add_argument("--title", required=True)

    a = ap.parse_args()
    a.f(a)
