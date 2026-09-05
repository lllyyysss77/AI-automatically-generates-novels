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
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Any, List, Optional, Iterator, Callable

from .registry import registry, ROOT
from .prompt_engine import render, budget, est_tokens
from .evaluator import audit, book_audit, window_audit
from .retrieval import Retriever
from .prompt_compiler import (compile_chapter_prompt, compile_outline_prompt,
                               to_plot_list)
from .settings import load as load_settings
from .memory import Memory
from .memory_ctl import MemoryController

PROJECTS = ROOT / "projects"
PROJECTS.mkdir(exist_ok=True)

# 架空题材的穿帮词: 实测生成 30 章后"宋律"出现 28 次、"大宋"20 次,
# 而自定的架空朝代"景朝"只有 2 次 —— 模型会稳定回落到训练数据里的真朝代。
MODE_RULES = {
    "alt": ("【架空硬约束，违反即作废】\n"
            "1. 第一行必须是：国号：X朝（一个两字虚构国号，不得以真朝代单字开头，"
            "绝不许写成「大宋景朝」这种真假混写）\n"
            "2. 另起一行：参考朝代（仅内部参考，正文永不出现）：北宋/唐/明…\n"
            "3. 律法称「X律」、史书称「X史」，不得出现大宋、宋律、北宋等真实朝代词\n"
            "4. 主场地点只指定一处，全书主线不换主场\n"
            "5. 最后列「禁用词表」\n\n直接输出，无前言。"),
    "real": ("【正统历史硬约束】\n"
             "1. 写的就是真实朝代，朝代名/官职/律法/纪年一律用真实名称，不要自造国号\n"
             "2. 第一行写：朝代：X（具体到帝号与年号），第二行写：故事起始年份\n"
             "3. 官职品级、俸禄、物价、度量衡必须真实；不确定的写模糊，不许编数字\n"
             "4. 主场地点只指定一处\n5. 最后列「易错点」\n\n直接输出，无前言。"),
    "none": ("【设定约束】\n1. 力量/规则体系必须自洽且可执行\n"
             "2. 主场地点只指定一处\n3. 最后列「禁忌」：本书绝不出现的东西\n\n"
             "直接输出，无前言。"),
}

REAL_DYNASTIES = ["大宋", "宋律", "宋朝", "北宋", "南宋", "大唐", "唐朝", "大明", "明朝",
                  "大清", "清朝", "大汉", "汉朝", "秦朝", "元朝", "民国", "大元",
                  "宋史", "唐律", "明律", "大周"]
FAKE_DYN_RE = re.compile(r"(?:虚构|架空)?(?:朝代|王朝|国号)[^\n。]{0,8}?[「\"'']?([一-鿿]{1,2}朝)")


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
        self._retriever: Optional[Retriever] = None
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

    # ---------- 统一检索 ----------
    @property
    def retriever(self) -> Retriever:
        """内部记忆 + 外部搜索合一。搜索不是独立步骤, 是召回层的一半。"""
        if self._retriever is None:
            wb = self.p.read("world_bible.md")
            m = re.search(r"参考朝代[^\n]*?[:：]\s*([^\n，,。]{1,10})", wb) or \
                re.search(r"(?:朝代|时代)\s*[:：]\s*([^\n，,。]{1,10})", wb)
            era = (self.p.meta.get("era") or (m.group(1).strip() if m else "")
                   or self.genre.get("name", ""))
            self._retriever = Retriever(
                self.p.mem, self.p.dir, era=era,
                enable_web=bool(self.mcfg.get("web_search", True)),
                summarize=lambda q: clean(call("polishing", q, max_tokens=800).text),
                topics=self.genre.get("research_topics"))
        return self._retriever

    def ground(self, stage: str, extra=None) -> str:
        if not self.mcfg.get("web_search", True):
            return ""
        try:
            return self.retriever.ground(stage, extra=extra)
        except Exception as e:
            print(f"[ground] 背景落地跳过: {e}")
            return ""

    def history_mode(self) -> str:
        """real = 写真实朝代（宋朝就叫宋朝，用真名是对的，要校的是史实准不准）
           alt  = 架空（必须自定国号，出现真朝代名才算穿帮）
           none = 与真实历史无关的题材（玄幻/都市/科幻…），两条都不校
        """
        m = (self.p.meta.get("history_mode") or "auto").lower()
        if m != "auto":
            return m
        gid = self.p.meta.get("genre_id", "")
        if gid in ("lishi", "gongdou", "junshi"):
            return "real"
        if gid in ("lishi-jiakong", "chuanyue", "wuxia"):
            return "alt"
        return "none"

    # ---------- 世界观锚定 ----------
    def world_anchor(self) -> Dict[str, Any]:
        """从世界观里抽出必须钉死的硬设定, 并推导禁用词。

        架空题材如果自定了"景朝", 就必须禁掉"大宋/宋律/北宋"这类真朝代词,
        否则模型会一路写成同人。这些进 L5 硬约束层, 每章必带。
        """
        wb = self.p.read("world_bible.md")
        meta = self.p.meta
        anchor = dict(meta.get("anchor") or {})

        if not anchor.get("dynasty"):
            m = re.search(r"国号\s*[:：]\s*([一-鿿]{1,3}朝)", wb) or FAKE_DYN_RE.search(wb)
            cand = m.group(1) if m else ""
            if not cand:
                # 兜底: 从"朝代设定：大宋景朝"这类混写里剥出虚构部分
                m2 = re.search(r"朝代设定\s*[:：]\s*([一-鿿]{2,6}朝)", wb)
                cand = m2.group(1) if m2 else ""
            # 逐层剥掉真朝代前缀: "大宋景朝" -> "宋景朝" -> "景朝"
            for _ in range(3):
                for real in ("大宋", "北宋", "南宋", "大唐", "大明", "大清", "大汉", "大元",
                             "宋", "唐", "明", "清", "汉", "元", "秦", "晋", "隋"):
                    if cand.startswith(real) and len(cand) > len(real) + 1:
                        cand = cand[len(real):]
                        break
                else:
                    break
            if cand and cand not in REAL_DYNASTIES and cand.endswith("朝"):
                anchor["dynasty"] = cand
        if not anchor.get("main_place"):
            places = Counter(re.findall(r"([一-鿿]{2}(?:县|州|府|城|镇))", wb))
            if places:
                anchor["main_place"] = places.most_common(1)[0][0]

        mode = self.history_mode()
        anchor["mode"] = mode
        forbidden = list(anchor.get("forbidden") or [])
        # 只有架空模式才禁真朝代名; 正统历史里「宋朝」「宋律」本来就是正确写法
        if mode == "alt" and anchor.get("dynasty"):
            forbidden += [w for w in REAL_DYNASTIES if w not in forbidden]
        elif mode != "alt":
            anchor.pop("dynasty", None)
        anchor["forbidden"] = forbidden
        return anchor

    # ---------- 角色花名册 ----------
    def roster(self) -> List[Dict[str, str]]:
        """把 characters.md 解析成结构化角色卡, 供逐章定向注入。"""
        cached = self.p._load("roster.json", None)
        if cached:
            return cached
        txt = self.p.read("characters.md")
        cards: List[Dict[str, str]] = []
        # 只在「### N. 姓名：X」这种角色标题上切块; 用 **粗体** 切会把字段名当成角色
        for blk in re.split(r"\n(?=#{1,4}\s*\d*\.?\s*姓名)", txt):
            blk = blk.strip()
            if len(blk) < 40:
                continue
            head = blk.splitlines()[0]
            m = re.search(r"姓名\s*[:：]\s*([^（(｜|，,\n]{1,8})", head)
            if not m:
                m = re.search(r"[#\d.\s]*([一-鿿]{2,6})", head)
            if not m:
                continue
            name = m.group(1).strip()
            if not re.search(r"[一-鿿]", name) or name in ("身份", "年龄", "外貌", "性格"):
                continue
            cards.append({"name": name, "card": blk[:700]})
        if cards:
            self.p.write("roster.json", json.dumps(cards, ensure_ascii=False, indent=2))
        return cards

    def cast_for(self, chapter_outline: str) -> str:
        """只注入本章会出场的角色卡 —— 全量塞进去既费预算又让模型抓瞎。"""
        cards = self.roster()
        if not cards:
            return ""
        hit = [c for c in cards if c["name"] and c["name"] in chapter_outline]
        # 主角永远带上
        if cards and cards[0] not in hit:
            hit.insert(0, cards[0])
        if len(hit) < 3:                       # 出场太少时补几个近期活跃角色
            for c in cards:
                if c not in hit:
                    hit.append(c)
                if len(hit) >= 4:
                    break
        return "\n\n".join(f"【{c['name']}】{c['card']}" for c in hit[:6])

    # ---------- 口癖抑制 ----------
    def tic_guard(self, n: Optional[int] = None) -> str:
        """把已写章节的问题反馈给下一章 —— 全书 + 邻章窗口双重视角。

        单章体检看不出问题 (1-2 次不触发阈值), 30 章连起来"嘴角勾起"43 次;
        而窗口视角能抓到"这三章都在同一场冲突里绕""开场和上一章雷同"这类
        全书统计也看不出的毛病。两者的结论都必须回流到生成端。
        """
        done = sorted(self.p.state.get("done", []))
        if len(done) < 3:
            return ""
        lines: List[str] = []

        chs = {i: self.p.chapter(i) for i in done[-40:]}
        tics = [t for t in book_audit(chs).get("tics", []) if t["count"] >= 6]
        if tics:
            lines.append("全书已用滥、本章禁止再出现的表达："
                         + "、".join(f"{t['tic']}({t['count']}次)" for t in tics[:10]))

        # 邻章窗口: 拿最近写完的一章做中心, 看它和前几章贴在一起有什么毛病
        last = done[-1]
        w = window_audit(chs, last, span=3,
                         outlines=self.p._load("chapter_outlines.json", {}))
        for i in w.get("issues", []):
            if i["type"] in ("与邻章重复度过高", "开场与邻章雷同"):
                lines.append(f"上一章已被判「{i['type']}」——本章必须换一种开场方式与场景切入点。")
            elif i["type"] == "剧情疑似原地踏步":
                lines.append("最近几章被判「原地踏步」——本章必须推进主线，"
                             "引入新地点/新人物/新矛盾，不许在同一场冲突里绕。")
            elif i["type"] == "角色断线":
                lines.append(f"注意角色连续性：{i['detail']}")
        return "\n".join(dict.fromkeys(lines))

    # ---------- 分层记忆装配 ----------
    def build_context(self, n: int, chapter_outline: str) -> Dict[str, Any]:
        """五层记忆 → 一份带预算报告的上下文. 见 memory_ctl.py."""
        mc = MemoryController(self.g["context_budget"], self.mcfg.get("layers"))

        resident = self.p.read("world_bible.md")
        brief = self.p.read("research_brief.md")
        if brief:
            resident += "\n\n【考据卡·硬事实，写到相关内容必须照此写】\n" + brief
        cast = self.cast_for(chapter_outline)
        if cast:
            resident += "\n\n【本章出场角色档案】\n" + cast

        sm = self.p.state.get("summaries", {})
        k = self.mcfg.get("recent_chapters", 3)
        recent = [f"第{i}章：{sm[str(i)]}" for i in range(max(1, n - k), n) if str(i) in sm]

        mid = [f.read_text(encoding="utf-8")
               for f in sorted((self.p.dir / "l2_summary").glob("*.md"))]

        recall, retr_info = [], {}
        if self.mcfg.get("enabled", True):
            try:
                rr = self.retriever.recall(chapter_outline, k=self.mcfg.get("top_k", 6))
                recall, retr_info = rr["items"], rr
            except Exception as e:            # 外部搜索挂了不能拖垮写作
                print(f"[retrieval] 降级为纯内部召回: {e}")
                recall = self.p.mem.search(chapter_outline, k=self.mcfg.get("top_k", 6))

        cons = []
        anchor = self.world_anchor()
        if anchor.get("mode") == "alt" and anchor.get("dynasty"):
            cons.append(f"【朝代锚定·架空】本书朝代只叫「{anchor['dynasty']}」。"
                        f"绝对禁止出现真实朝代词：{'、'.join(anchor['forbidden'][:14])}。"
                        f"律法称「{anchor['dynasty']}律」，史书称「{anchor['dynasty']}史」。")
        elif anchor.get("mode") == "real":
            cons.append("【史实锚定·正统历史】本书写的就是真实朝代，朝代名、官职、律法、"
                        "纪年一律用真实名称，不要自造国号。凡涉及具体年份、官职品级、"
                        "物价、器物，必须与背景资料一致；资料没有的宁可写模糊，不许编数字。")
        if anchor.get("main_place"):
            cons.append(f"【主场锚定】主角常驻地是「{anchor['main_place']}」，"
                        f"不得随意把主场换到别的县城；确需异地必须写明行程。")
        tg = self.tic_guard()
        if tg:
            cons.append("【口癖抑制】" + tg)
        cons.append("【配角配额】本章除主角外至少让 2 个配角有独立台词与动作，"
                    "配角不能只当背景板；不得给已知人物随意安排与其身份不符的官职。")
        bl = self.blacklist()
        if bl:
            cons.append("禁用套话：" + "、".join(bl))
        pend = self.p.mem.pending_foreshadow()
        if pend:
            cons.append("未回收伏笔（可择机回收）：" + "；".join(
                f"第{f['planted']}章「{f['text'][:40]}」" for f in pend[:5]))

        out = mc.assemble(outline=chapter_outline, resident=resident, recent=recent,
                          mid=mid, recall=recall, constraints="\n".join(cons))
        out["retrieval"] = {k: retr_info.get(k) for k in ("internal", "external", "needs")}
        return out

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
            "核心矛盾 / 力量或规则体系 / 关键道具与线索 / 禁用词表\n\n"
            + MODE_RULES[self.history_mode()], ctx)
        bg = self.ground("world")
        if bg:
            prompt += ("\n\n【真实背景资料 —— 世界观必须与之吻合，"
                       "器物、官制、物价、风俗不得违背】\n" + bg[:6000])
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
            "输出 10-14 个角色，格式严格如下（每人一节，标题行必须是「### N. 姓名：某某」）：\n"
            "### 1. 姓名：某某\n"
            "**身份**：…\n**年龄**：…\n**外貌一句话**：…\n**性格三词**：…\n"
            "**核心动机**：…\n**与主角关系**：…\n**专属口头禅或说话习惯**：…\n**结局走向**：…\n\n"
            "【硬性约束】\n"
            "1. 口头禅与说话习惯里绝对不许出现真实朝代名（大宋/宋律/大唐/大明…），"
            "要用世界观里的虚构国号\n"
            "2. 必须有 4 位以上**戏份仅次于主角的核心配角**，各自有独立目标与故事线，"
            "不是主角的应声筒\n"
            "3. 至少 2 位女性角色有独立故事线，不围绕男主转\n"
            "4. 反派要有自洽逻辑，不能纯坏\n"
            "5. 若借用了知名作品的人物，其身份职业必须与原作一致，"
            "不得随意安排不符身份的官职\n"
            "直接输出，无前言。", ctx)
        bg = self.ground("cast")
        if bg:
            prompt += ("\n\n【真实背景资料 —— 姓名、称谓、职业、阶层、"
                       "女性处境必须符合下列常识】\n" + bg[:5000])
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
        anchor = self.world_anchor()
        cons = []
        if anchor.get("dynasty"):
            cons.append(f"朝代只叫「{anchor['dynasty']}」，禁用：{'、'.join(anchor['forbidden'][:10])}")
        if anchor.get("main_place"):
            cons.append(f"主场固定在「{anchor['main_place']}」")
        cons.append("每章主角之外必须有 2 个以上配角有独立戏份")
        cons.append(self.genre_rules()[:800])
        prompt = compile_outline_prompt(
            title=self.p.meta.get("title", ""), start=start, count=count,
            genre_line=f"{self.genre.get('name','')}/{self.style.get('name','')}".strip("/"),
            world_digest=self.p.read("world_bible.md")[:3000],
            roster_names=[c["name"] for c in self.roster()] or ["主角"],
            outline=self.p.read("outline.md")[:3000],
            prev_summary=self.prev_summary(start),
            constraints="\n".join(cons))
        bg = self.ground("plot")
        if bg:
            prompt += ("\n\n【真实背景资料 —— 本批剧情涉及的器物、行程、"
                       "礼俗必须符合下列常识，不要写出不属于该时代的东西】\n" + bg[:5000])
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

        # 用编译器产出网文作者实战格式的提示词 (块状标记 / 人物卡出场标注 /
        # 正负词库 / 字数标记 / 编号剧情), 而不是长段落描述式提示词。
        st = self.style
        f = self.p.meta.get("fields", {})
        rost = self.roster()
        prompt = compile_chapter_prompt(
            title=self.p.meta.get("title", ""), index=n, target_words=target,
            genre_line=f"{self.genre.get('name','')}/{st.get('name','')}".strip("/"),
            manner=st.get("manner") or "口语化，节奏明快",
            background=f.get("background", ""),
            world_digest=L["L1_resident"][:4000],
            roster=rost, protagonist=(rost[0]["name"] if rost else ""),
            relations=f.get("relationships", ""),
            mainline=self.p.read("outline.md")[:1500],
            chapter_outline=co,
            positive=st.get("positive", []),
            negative=self.blacklist(),
            constraints=L.get("L5_constraint", ""),
            memory=ctx.get("prev_summary", "")[:6000],
            block_words=int(st.get("blockWords") or 500),
        )
        self.p.write(f"audit/{n:03d}.prompt.txt", prompt)
        r = call("drafting", prompt, on_delta, max_tokens=self.g["max_tokens_draft"])
        # 去掉模型输出里的【字数标记】—— 它只是写作时的计数脚手架, 不进成稿
        r.text = re.sub(r"【字数标记[^】]*】\s*", "", r.text)
        text = clean(r.text)

        a = audit(text, extra_blacklist=self.blacklist(), target_words=target)
        pos = st.get("positive", [])
        if pos:
            used = [w for w in pos if w in text]
            a["positive_hits"] = len(used)
            a["positive_samples"] = used[:12]
            if len(used) < max(2, len(pos) // 25):
                a["issues"].append({"level": "low", "type": "网感不足",
                                    "detail": f"正向词库仅命中 {len(used)} 个"})
                a["score"] = max(0, a["score"] - 3)

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
        # 窗口体检: 本章 + 前 3 章贴在一起看
        done_now = sorted(set(self.p.state.get("done", [])) | {n})
        wchs = {i: self.p.chapter(i) for i in done_now[-8:]}
        w = window_audit(wchs, n, span=3,
                         outlines=self.p._load("chapter_outlines.json", {}))
        a["window"] = {"score": w.get("score"), "issues": w.get("issues", [])}
        self.p.write(f"audit/{n:03d}.json", json.dumps(a, ensure_ascii=False, indent=2))

        rep = asm["report"]
        rt = asm.get("retrieval") or {}
        self._log(f"第{n}章 {a['stats']['cn']}字 单章{a['score']} 窗口{a['window']['score']} / {r.elapsed:.1f}s"
                  + (f" / 召回 内{rt.get('internal',0)}+外{rt.get('external',0)}"
                     + (f"({'/'.join(rt.get('needs') or [])})" if rt.get("needs") else "")
                     if rt else "")
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
                   fields: Dict[str, Any], history_mode: str = "auto") -> Project:
    p = Project(slugify(title))
    _, kw = registry.resolve("drafting")
    p.meta = {"title": title, "type_id": type_id, "genre_id": genre_id, "style_id": style_id,
              "target_chapters": target_chapters, "target_words": target_words,
              "fields": fields, "model": kw["model"], "history_mode": history_mode,
              "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    p.save()
    return p
