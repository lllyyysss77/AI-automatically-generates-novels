"""导出插件. 加一种格式 = 加一个函数 + 注册一行."""
from __future__ import annotations

import re
from typing import Dict, Callable, List, Any


def _chapters(project) -> List[tuple]:
    out = []
    for n in sorted(project.state.get("done", [])):
        body = project.chapter(n)
        title = ""
        co = project._load("chapter_outlines.json", {}).get(str(n), "")
        m = re.search(r"第\s*\d+\s*章\s*(.+)", co)
        if m:
            title = m.group(1).strip().splitlines()[0][:30]
        out.append((n, title, body))
    return out


def to_txt(project) -> str:
    parts = [f"《{project.meta.get('title','')}》\n"]
    for n, title, body in _chapters(project):
        parts.append(f"\n第{n}章 {title}\n\n{body}\n")
    return "".join(parts)


def to_md(project) -> str:
    parts = [f"# 《{project.meta.get('title','')}》\n",
             f"> {project.meta.get('genre_id')} · {project.meta.get('style_id')} · "
             f"{project.total_words:,} 字\n"]
    wb = project.read("world_bible.md")
    if wb:
        parts.append(f"\n## 世界观\n\n{wb}\n")
    for n, title, body in _chapters(project):
        parts.append(f"\n## 第{n}章 {title}\n\n{body}\n")
    return "".join(parts)


def to_outline(project) -> str:
    """只导大纲: 总纲 + 全部章节细纲."""
    parts = [f"《{project.meta.get('title','')}》大纲\n\n{project.read('outline.md')}\n\n"
             "———— 分章细纲 ————\n"]
    ol = project._load("chapter_outlines.json", {})
    for k in sorted(ol, key=lambda x: int(x)):
        parts.append(f"\n{ol[k]}\n")
    return "".join(parts)


def to_fountain(project) -> str:
    """Fountain 剧本格式 (行业标准, Final Draft / Highland 可直接打开)."""
    m = project.meta
    parts = [f"Title: {m.get('title','')}\n", "Credit: Written by\n",
             "Author: AI Novel Studio\n\n"]
    for n, title, body in _chapters(project):
        parts.append(f"\n= 第{n}集 {title}\n\n")
        for line in body.splitlines():
            s = line.strip()
            if not s:
                parts.append("\n")
                continue
            # 场头
            if re.match(r"^\s*\d*\s*(内景|外景|INT|EXT)", s):
                parts.append(re.sub(r"^\s*\d+[、.]?\s*", "", s).upper() + "\n\n")
            # 「角色：台词」→ 角色名独立行 + 对白
            elif re.match(r"^[^\s：:]{1,8}[：:]", s):
                who, said = re.split(r"[：:]", s, 1)
                parts.append(f"{who.strip()}\n{said.strip()}\n\n")
            else:
                parts.append(s + "\n\n")
    return "".join(parts)


def to_srt(project) -> str:
    """短剧字幕. 按标点切句, 按字数估时长."""
    idx, t, out = 1, 0.0, []

    def fmt(x: float) -> str:
        h, r = divmod(x, 3600); mnt, s = divmod(r, 60)
        return f"{int(h):02d}:{int(mnt):02d}:{int(s):02d},{int((s%1)*1000):03d}"

    for n, title, body in _chapters(project):
        for raw in re.split(r"(?<=[。！？!?…])", body):
            s = re.sub(r"\s+", " ", raw).strip()
            if len(s) < 2:
                continue
            dur = max(1.2, len(s) * 0.22)
            out.append(f"{idx}\n{fmt(t)} --> {fmt(t+dur)}\n{s}\n")
            idx += 1; t += dur
    return "\n".join(out)


EXPORTERS: Dict[str, Callable[[Any], str]] = {
    "txt": to_txt, "md": to_md, "outline": to_outline,
    "fountain": to_fountain, "srt": to_srt,
}
MIME = {"txt": "text/plain", "md": "text/markdown", "outline": "text/plain",
        "fountain": "text/plain", "srt": "application/x-subrip"}
EXT = {"txt": "txt", "md": "md", "outline": "txt", "fountain": "fountain", "srt": "srt"}
