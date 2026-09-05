"""自动写作编排层 —— "真正能自动写小说" 在这里.

设计要点 (借鉴 x10086 skills/long-novel 的工程架构):
  * 外部档案替代 LLM 记忆: world_bible + characters + 滚动摘要, 不依赖长 context
  * 逐章 checkpoint: 任何时候中断都能续写
  * 写完即自审: 跑 evaluator, 不合格自动重写一次
  * 上下文预算: 每次组装按优先级裁剪, 绝不撑爆 window
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Any, List, Optional, Iterator, Callable

from .registry import registry, ROOT
from .prompt_engine import render, budget, est_tokens
from .evaluator import audit
from .settings import load as load_settings
from .memory import Memory
from .memory_ctl import MemoryController

PROJECTS = ROOT / "projects"
PROJECTS.mkdir(exist_ok=True)


def slugify(t: str) -> str:
    return re.sub(r"[^\w一-鿿-]+", "_", t).strip("_")[:60]


class Project:
    def __init__(self, slug: str):
        self.slug = slug
        self.dir = PROJECTS / slug
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "chapters").mkdir(exist_ok=True)
        (self.dir / "audit").mkdir(exist_ok=True)
        (self.dir / "l2_summary").mkdir(exist_ok=True)
        self.meta: Dict[str, Any] = self._load("project.json", {})
        self.state: Dict[str, Any] = self._load("state.json",
                                                {"current": 0, "done": [], "log": []})
        # 全局配置 + 本项目覆盖
        self.cfg: Dict[str, Any] = load_settings(self.meta.get("overrides"))
        self.mem = Memory(self.dir / "memory.db")

    # ---------- io ----------
    def _load(self, name: str, default):
        p = self.dir / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return default
        return default

    def save(self):
        (self.dir / "project.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.dir / "state.json").write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def read(self, name: str) -> str:
        p = self.dir / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write(self, name: str, text: str):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def chapter_path(self, n: int) -> str:
        return f"chapters/{n:03d}.md"

    def chapter(self, n: int) -> str:
        return self.read(self.chapter_path(n))

    # ---------- 派生 ----------
    @property
    def total_words(self) -> int:
        return sum(len(re.findall(r"[一-鿿]", self.chapter(n)))
                   for n in self.state.get("done", []))

    def board(self) -> str:
        done = self.state.get("done", [])
        tw = self.total_words
        tgt = self.meta.get("target_words", 0)
        lines = [
            f"# 《{self.meta.get('title','')}》项目看板", "",
            f"- **类型**: {self.meta.get('type_id')} / **题材**: {self.meta.get('genre_id')} / **文风**: {self.meta.get('style_id')}",
            f"- **进度**: {len(done)} / {self.meta.get('target_chapters', 0)} 章",
            f"- **字数**: {tw:,} / {tgt:,} 字 ({tw/tgt*100 if tgt else 0:.1f}%)",
            f"- **模型**: {self.meta.get('model')}",
            "", "## 质量", ]
        scores = []
        for n in done:
            a = self._load(f"audit/{n:03d}.json", None)
            if a:
                scores.append(a["score"])
        if scores:
            lines += [f"- 平均 AI 味得分: **{sum(scores)/len(scores):.1f}** / 100",
                      f"- 最低分章节: 第 {done[scores.index(min(scores))]} 章 ({min(scores)} 分)"]
        lines += ["", "## 最近日志"] + [f"- {x}" for x in self.state.get("log", [])[-12:]]
        return "\n".join(lines)


# ---------------------------------------------------------------- 引擎
@dataclass
class GenResult:
    text: str
    reasoning: str = ""
    elapsed: float = 0.0
    chars: int = 0


def call(profile: str, prompt: str, on_delta: Optional[Callable[[str], None]] = None,
         system: str = "", max_tokens: Optional[int] = None,
         _retry: bool = True) -> GenResult:
    """一次生成. 自动处理 reasoning/content 三种字段 + 空 content 兜底.

    实测坑: 开思考时模型可能把全部内容留在 reasoning 里 content 为空, 或思考
    吃光 max_tokens 导致正文被截断. 空输出会自动关思考重试一次.
    """
    provider, kw = registry.resolve(profile)
    if not _retry:
        kw["thinking"] = False
    if max_tokens:
        kw["max_tokens"] = max_tokens
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    t0 = time.time()
    text, think = [], []
    for d in provider.stream(msgs, **kw):
        if d.text:
            text.append(d.text)
            if on_delta:
                on_delta(d.text)
        if d.reasoning:
            think.append(d.reasoning)
    out = "".join(text).strip()
    rsn = "".join(think)
    if not out and rsn:          # ★ 兜底 1: 答案被 <answer> 包在 reasoning 里
        out = provider.salvage(rsn)
        if on_delta and out:
            on_delta(out)
    if not out and _retry:       # ★ 兜底 2: 关思考重试, 把 token 全给正文
        print("  [call] 输出为空, 关思考重试", flush=True)
        return call(profile, prompt, on_delta, system, max_tokens, _retry=False)
    return GenResult(text=out, reasoning=rsn, elapsed=time.time() - t0, chars=len(out))


def clean(text: str) -> str:
    """去掉模型爱加的前言/标题/代码围栏."""
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
    text = re.sub(r"^(?:好的|以下是|下面是)[^\n]{0,40}[:：]\s*\n+", "", text)
    return text.strip()


class Novelist:
    """把 ContentType + GenrePack + StylePack 组装成可执行流水线."""

    def __init__(self, project: Project):
        self.p = project
        m = project.meta
        self.type = registry.types[m["type_id"]]
        self.genre = registry.genres.get(m.get("genre_id")) or {}
        self.style = registry.styles.get(m.get("style_id")) or {}
        self.common = registry.common
        self.cfg = project.cfg
        self.g = self.cfg["generation"]
        self.q = self.cfg["quality"]
        self.mcfg = self.cfg["memory"]
        self.g["context_budget"] = self._resolve_budget()

    def _resolve_budget(self) -> int:
        """context_budget=auto 时按当前网关窗口推导, 不再拍一个保守小数字.

        可用提示词预算 = 模型窗口 - 输出预留 - 安全余量
        Qwen3.8-Flash-Next 110k 窗口 -> 约 96k 可用.
        """
        lo = int(self.g.get("min_context_budget", 32000))
        hi = int(self.g.get("max_context_budget", 100000))
        v = self.g.get("context_budget")
        if isinstance(v, int) and v > 0:
            return max(lo, min(hi, v))

        gw = registry.gateways.get(registry.profiles["drafting"]["gateway"], {})
        win = int(gw.get("context_window") or 0)
        usable = win - int(self.g.get("output_reserve", 8192)) \
                     - int(self.g.get("safety_margin", 4000))
        if usable < lo:
            # 窗口撑不起下限: 仍按下限走, 并明确告警 —— 低于 32k 长篇一致性必崩
            print(f"[budget] 警告: 网关窗口 {win} 不足以支撑 {lo} 记忆体下限, "
                  f"请换用 ≥{lo + 12000} 窗口的模型")
            return lo
        return max(lo, min(hi, usable))

    # ---------- 规则文本 ----------
    def genre_rules(self, full: bool = False) -> str:
        g = self.genre
        if not g:
            return ""
        if full:
            return g.get("raw", "")[:6000]
        parts = [f"题材：{g.get('name')}"]
        if g.get("corePleasure"):
            parts.append("核心爽点：" + "；".join(g["corePleasure"][:6]))
        if g.get("pacing"):
            parts.append("节奏要求：\n" + g["pacing"][:600])
        if g.get("cast"):
            parts.append("人物配置：" + "；".join(g["cast"][:6]))
        if g.get("pitfalls"):
            parts.append("必须避开的坑：\n- " + "\n- ".join(g["pitfalls"][:8]))
        if g.get("benchmarks"):
            parts.append("对标作品：" + g["benchmarks"])
        return "\n".join(parts)

    def style_rules(self) -> str:
        s = self.style
        if not s:
            return ""
        out = [f"平台文风：{s.get('name')}（{s.get('platform','')}）"]
        out += ["- " + r for r in s.get("rules", [])]
        if s.get("banned"):
            out.append("禁止：" + "、".join(s["banned"]))
        sd = self.cfg.get("style_defaults", {})
        pref = [f"叙事视角：{sd.get('narration')}" if sd.get("narration") else "",
                f"时态：{sd.get('tense')}" if sd.get("tense") else "",
                sd.get("extra") or ""]
        pref = [x for x in pref if x]
        if pref:
            out.append("【全局写作偏好】" + "；".join(pref))
        return "\n".join(out)

    def common_rules(self) -> str:
        cats = self.common.get("categories", [])
        out = []
        for c in cats:
            out.append(f"[{c['category']}] 禁：" + "；".join(c["avoid"][:3]))
        return "\n".join(out)

    def blacklist(self) -> List[str]:
        bl = list(self.genre.get("clicheBlacklist", []))
        bl += self.style.get("banned", [])
        bl += self.cfg.get("banned_global", []) or []
        return list(dict.fromkeys([b for b in bl if b]))

    # ---------- 上下文 ----------
    def base_ctx(self) -> Dict[str, Any]:
        m = self.p.meta
        ctx = {
            "title": m.get("title", ""),
            "target_chapters": m.get("target_chapters", 0),
            "target_words": m.get("target_words", 0),
            "genre_rules": self.genre_rules(),
            "style_rules": self.style_rules(),
            "common_rules": self.common_rules(),
            "cliche_blacklist": "、".join(self.blacklist()),
        }
        ctx.update(m.get("fields", {}))
        return ctx

    # ---------- 分层记忆装配 ----------
    def build_context(self, n: int, chapter_outline: str) -> Dict[str, Any]:
        """五层记忆 → 一份带预算报告的上下文. 见 memory_ctl.py."""
        mc = MemoryController(self.g["context_budget"], self.mcfg.get("layers"))

        resident = self.p.read("world_bible.md")
        role = self.p.read("characters.md")
        if role:
            resident = (resident + "\n\n【主要角色】\n" + role)

        sm = self.p.state.get("summaries", {})
        k = self.mcfg.get("recent_chapters", 3)
        recent = [f"第{i}章：{sm[str(i)]}" for i in range(max(1, n - k), n) if str(i) in sm]

        mid = [f.read_text(encoding="utf-8")
               for f in sorted((self.p.dir / "l2_summary").glob("*.md"))]

        recall = (self.p.mem.search(chapter_outline, k=self.mcfg.get("top_k", 6))
                  if self.mcfg.get("enabled", True) else [])

        cons = []
        bl = self.blacklist()
        if bl:
            cons.append("禁用套话：" + "、".join(bl))
        pend = self.p.mem.pending_foreshadow()
        if pend:
            cons.append("未回收伏笔（可择机回收）：" + "；".join(
                f"第{f['planted']}章「{f['text'][:40]}」" for f in pend[:5]))

        return mc.assemble(outline=chapter_outline, resident=resident, recent=recent,
                           mid=mid, recall=recall, constraints="\n".join(cons))

    def prev_summary(self, n: int, k: int | None = None) -> str:
        k = k or self.mcfg.get("recent_chapters", 3)
        """最近 k 章摘要 + 所属 L2 段摘要 —— 长篇控 context 的关键."""
        out = []
        l2 = sorted((self.p.dir / "l2_summary").glob("*.md"))
        if l2:
            out.append("【前情大纲】\n" + "\n".join(f.read_text(encoding="utf-8") for f in l2[-2:]))
        sm = self.p.state.get("summaries", {})
        recent = [f"第{i}章：{sm[str(i)]}" for i in range(max(1, n - k), n) if str(i) in sm]
        if recent:
            out.append("【最近章节】\n" + "\n".join(recent))
        return "\n\n".join(out)

    def recall(self, query: str) -> str:
        """多记忆索引召回: 从世界观/角色/往期剧情/伏笔里找回相关片段.

        这是长篇写到几百章后仍能保持一致性的关键 —— 最近摘要覆盖不到的远期细节
        (第 30 章埋的伏笔、只出场过一次的配角) 只能靠检索找回来.
        """
        if not self.mcfg.get("enabled", True):
            return ""
        hits = self.p.mem.search(query, k=self.mcfg.get("top_k", 6))
        if not hits:
            return ""
        label = {"world": "世界观", "role": "角色", "plot": "往期剧情", "fore": "伏笔"}
        lines = [f"[{label.get(h['kind'], h['kind'])}] {h['title']}: {h['text'][:300]}"
                 for h in hits]
        pend = self.p.mem.pending_foreshadow()
        if pend:
            lines.append("[未回收伏笔] " + "；".join(
                f"第{f['planted']}章「{f['text'][:40]}」" for f in pend[:5]))
        return "\n".join(lines)

    def reindex(self) -> Dict[str, int]:
        """把世界观/角色档案重新灌进记忆索引."""
        wb, ch = self.p.read("world_bible.md"), self.p.read("characters.md")
        if wb:
            self.p.mem.index_document("world", "world_bible", wb)
        if ch:
            self.p.mem.index_document("role", "characters", ch)
        return self.p.mem.stats()

    # ---------- 步骤 ----------
    def step_world_bible(self, on_delta=None) -> str:
        ctx = self.base_ctx()
        prompt = render(
            "你是网文世界观设计师。为《${title}》写世界观圣经，2000 字以内，压缩、密集、可执行。\n\n"
            "【一句话故事】${premise}\n【背景】${background}\n\n【题材规范】\n${genre_rules}\n\n"
            "必须输出以下字段（用小标题分节）：\n"
            "朝代设定 / 地理与势力 / 政治军事经济 / 社会风貌与阶层 / 主角身份与金手指 / "
            "核心矛盾 / 力量或规则体系 / 关键道具与线索 / 禁忌（本书绝不出现的东西）\n\n"
            "要求：具体到能直接写正文，不要空话。直接输出，无前言。", ctx)
        r = call("planning", prompt, on_delta, max_tokens=4000)
        wb = clean(r.text)
        self.p.write("world_bible.md", wb)
        n = self.p.mem.index_document("world", "world_bible", wb)
        self._log(f"世界观 {len(wb)} 字 / {r.elapsed:.1f}s / 入索引 {n} 条")
        return wb

    def step_characters(self, on_delta=None) -> str:
        ctx = self.base_ctx()
        ctx["world_bible"] = self.p.read("world_bible.md")
        prompt = render(
            "基于世界观，为《${title}》设计角色档案。\n\n【世界观】\n${world_bible}\n\n"
            "【题材规范】\n${genre_rules}\n\n"
            "输出 8-12 个角色，每个角色一节：\n"
            "姓名｜身份｜年龄｜外貌一句话｜性格三词｜核心动机｜与主角关系｜专属口头禅或说话习惯｜结局走向\n"
            "要求：至少 2 位女性角色有独立故事线，反派要有自洽逻辑不能纯坏。直接输出，无前言。", ctx)
        r = call("planning", prompt, on_delta, max_tokens=4000)
        ch = clean(r.text)
        self.p.write("characters.md", ch)
        n = self.p.mem.index_document("role", "characters", ch)
        self._log(f"角色档案 {len(ch)} 字 / {r.elapsed:.1f}s / 入索引 {n} 条")
        return ch

    def step_outline(self, on_delta=None) -> str:
        lvl = self.type["levels"][0]
        ctx = self.base_ctx()
        ctx["world_bible"] = self.p.read("world_bible.md")
        ctx["characters"] = self.p.read("characters.md")
        prompt = render(lvl["prompt"], ctx)
        r = call("planning", prompt, on_delta, max_tokens=6000)
        ol = clean(r.text)
        self.p.write("outline.md", ol)
        self._log(f"总纲 {len(ol)} 字 / {r.elapsed:.1f}s")
        return ol

    def step_chapter_outlines(self, start: int, count: int, on_delta=None) -> List[str]:
        """分批生成章节细纲, 每批 count 章 —— 一次性生成 200 章细纲必然崩."""
        ctx = self.base_ctx()
        ctx.update({
            "outline": self.p.read("outline.md"),
            "world_bible": self.p.read("world_bible.md"),
            "characters": self.p.read("characters.md"),
            "prev_summary": self.prev_summary(start),
            "range": f"{start}-{start + count - 1}",
            "parent": "", "parent_title": f"第 {start}-{start+count-1} 章",
            "count": count,
        })
        prompt = render(
            "为《${title}》写第 ${range} 章的分章细纲。\n\n【总纲】\n${outline}\n\n"
            "【世界观摘要】\n${world_bible}\n\n【前情】\n${prev_summary}\n\n"
            "【题材规范】\n${genre_rules}\n\n【平台文风】\n${style_rules}\n\n"
            "每章严格按此格式，章与章之间用一行 ###fenge 分隔：\n"
            "第N章 章节名\n核心事件：…\n出场人物：…\n爽点：…\n章末钩子：…\n\n"
            "直接输出，无前言。", ctx)
        r = call("planning", prompt, on_delta, max_tokens=8000)
        parts = [clean(x) for x in re.split(r"###fenge", r.text) if x.strip()]
        outlines = self.p._load("chapter_outlines.json", {})
        for i, part in enumerate(parts):
            outlines[str(start + i)] = part
        self.p.write("chapter_outlines.json", json.dumps(outlines, ensure_ascii=False, indent=2))
        self._log(f"细纲 {start}-{start+len(parts)-1} 共 {len(parts)} 章 / {r.elapsed:.1f}s")
        return parts

    def step_chapter(self, n: int, on_delta=None, retry_on_low: int | None = None) -> Dict[str, Any]:
        retry_on_low = retry_on_low if retry_on_low is not None else self.q["audit_pass_score"]
        outlines = self.p._load("chapter_outlines.json", {})
        co = outlines.get(str(n), "")
        if not co:
            raise RuntimeError(f"第 {n} 章没有细纲, 先跑 step_chapter_outlines")

        target = (self.g["chapter_words_min"] + self.g["chapter_words_max"]) // 2
        lvl = [l for l in self.type["levels"] if l["id"] in ("content", "page", "shot")][0]

        ctx = self.base_ctx()
        asm = self.build_context(n, co)     # ★ 五层记忆 + 预算分配
        L = asm["layers"]
        ctx.update({
            "chapter_outline": L["L0_outline"],
            "world_bible": L["L1_resident"],
            "characters": "",               # 已并入常驻层, 避免重复占预算
            "prev_summary": MemoryController.compose(
                {k: v for k, v in L.items() if k != "L1_resident"}),
            "index": n, "target_words": target,
        })
        self.p.write(f"audit/{n:03d}.ctx.json",
                     json.dumps(asm["report"], ensure_ascii=False, indent=2))

        prompt = render(lvl["prompt"], ctx)
        r = call("drafting", prompt, on_delta, max_tokens=self.g["max_tokens_draft"])
        text = clean(r.text)

        a = audit(text, extra_blacklist=self.blacklist(), target_words=target)

        # 不合格自动重写一次 (只做一轮, 避免无限循环烧钱)
        if a["score"] < retry_on_low and text:
            probs = "；".join(f"{i['type']}{i.get('samples','')}" for i in a["issues"][:6])
            fix = (f"下面这章 AI 味检测不合格（{a['score']}分）。问题：{probs}\n"
                   f"禁用套话：{'、'.join(self.blacklist())}\n"
                   f"请重写，保持剧情完全不变，只改语言：去掉套话与 AI 腔，"
                   f"句式长短交错，段落节奏有变化，字数保持 {target} 字左右。"
                   f"直接输出正文，无前言。\n\n{text}")
            r2 = call("polishing", fix, on_delta, max_tokens=8192)
            t2 = clean(r2.text)
            a2 = audit(t2, extra_blacklist=self.blacklist(), target_words=target)
            if t2 and a2["score"] > a["score"]:
                text, a = t2, a2
                a["rewritten"] = True

        self.p.write(self.p.chapter_path(n), text)
        self.p.write(f"audit/{n:03d}.json", json.dumps(a, ensure_ascii=False, indent=2))

        # 滚动摘要
        summ = call("polishing",
                    f"用 60 字以内一句话概括这一章发生了什么，直接输出：\n\n{text[:3000]}",
                    max_tokens=200)
        one = clean(summ.text).replace("\n", " ")
        self.p.state.setdefault("summaries", {})[str(n)] = one
        if self.mcfg.get("index_chapters", True):
            self.p.mem.add("plot", f"ch{n}", f"第{n}章", one + "\n" + text[:1500])

        if n not in self.p.state["done"]:
            self.p.state["done"].append(n)
        self.p.state["current"] = n
        rep = asm["report"]
        self._log(f"第{n}章 {a['stats']['cn']}字 得分{a['score']} / {r.elapsed:.1f}s"
                  + f" / 记忆 {rep['used']}tok({rep['usage_pct']}%)"
                  + (f" 溢出:{','.join(rep['overflow'])}" if rep["overflow"] else "")
                  + ("（已重写）" if a.get("rewritten") else ""))
        self.p.save()
        self.p.write("PROJECT_BOARD.md", self.p.board())

        # 每 10 章压一次 L2 摘要
        every = self.mcfg.get("l2_every", 10)
        if n % every == 0:
            self._l2(n - every + 1, n)
        return {"chapter": n, "chars": a["stats"]["cn"], "score": a["score"],
                "elapsed": r.elapsed, "rewritten": a.get("rewritten", False)}

    def _l2(self, a: int, b: int):
        sm = self.p.state.get("summaries", {})
        body = "\n".join(f"第{i}章：{sm.get(str(i),'')}" for i in range(a, b + 1))
        r = call("polishing", f"把下面 {b-a+1} 章的剧情压缩成 500 字以内的连贯摘要，"
                              f"保留关键人物、转折、伏笔。直接输出：\n\n{body}", max_tokens=1000)
        self.p.write(f"l2_summary/{a:03d}-{b:03d}.md", clean(r.text))
        self._log(f"L2 摘要 {a}-{b} 完成")

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.p.state.setdefault("log", []).append(f"[{ts}] {msg}")
        self.p.save()
        print(f"  [{ts}] {msg}", flush=True)


def create_project(title: str, type_id: str, genre_id: str, style_id: str,
                   target_chapters: int, target_words: int,
                   fields: Dict[str, Any]) -> Project:
    p = Project(slugify(title))
    _, kw = registry.resolve("drafting")
    p.meta = {"title": title, "type_id": type_id, "genre_id": genre_id, "style_id": style_id,
              "target_chapters": target_chapters, "target_words": target_words,
              "fields": fields, "model": kw["model"],
              "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    p.save()
    return p
