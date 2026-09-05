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
            "开头两行必须严格照抄下面的格式，把 X 换成你定的名字，"
            "括号里的说明文字不要抄进去：\n"
            "```\n国号：燕朝\n参考朝代：北宋\n```\n"
            "（国号是两字虚构名，不得以真朝代单字开头，不许写成「大宋景朝」这种真假混写；"
            "参考朝代仅内部参考，正文永不出现）\n"
            "其余要求：\n"
            "- 律法称「X律」、史书称「X史」，不得出现大宋、宋律、北宋等真实朝代词\n"
            "- 主场地点只指定一处，全书主线不换主场\n"
            "- 最后列「禁用词表」\n\n"
            "直接输出，无前言，不要复述本条要求。"),
    "real": ("【正统历史硬约束】\n"
             "1. 写的就是真实朝代，朝代名/官职/律法/纪年一律用真实名称，不要自造国号\n"
             "2. 开头两行照格式写（说明文字不要抄）：\n```\n朝代：北宋 仁宗 庆历年间\n"
             "起始年份：1043\n```\n"
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
        u = self.state.get("usage") or {}
        if u:
            lines += ["", "## 用量",
                      f"- 调用 {u.get('calls',0)} 次 / 累计 {u.get('total',0):,} token"
                      f"（入 {u.get('prompt',0):,} 出 {u.get('completion',0):,}）",
                      f"- 模型耗时 {u.get('elapsed_s',0)} 秒"]
            for k, v in (u.get("by_profile") or {}).items():
                lines.append(f"  - {k}: {v['calls']} 次 / "
                             f"{v['prompt']+v['completion']:,} token")
        lines += ["", "## 最近日志"] + [f"- {x}" for x in self.state.get("log", [])[-12:]]
        return "\n".join(lines)


# ---------------------------------------------------------------- 引擎
@dataclass
class GenResult:
    text: str
    reasoning: str = ""
    elapsed: float = 0.0
    chars: int = 0
    usage: Dict[str, Any] = field(default_factory=dict)


# 全进程用量累计. 网关不返回 usage 时按中文 1 字≈1.4token 估算, 标记 estimated。
USAGE: Dict[str, Any] = {"calls": 0, "prompt": 0, "completion": 0,
                         "elapsed": 0.0, "by_profile": {}}


def _record(profile: str, prompt_chars: int, res: "GenResult") -> None:
    u = res.usage or {}
    pt = u.get("prompt") or int(prompt_chars / 0.7)
    ct = u.get("completion") or int((len(res.text) + len(res.reasoning)) / 0.7)
    USAGE["calls"] += 1
    USAGE["prompt"] += pt
    USAGE["completion"] += ct
    USAGE["elapsed"] += res.elapsed
    b = USAGE["by_profile"].setdefault(profile, {"calls": 0, "prompt": 0,
                                                 "completion": 0, "elapsed": 0.0})
    b["calls"] += 1; b["prompt"] += pt; b["completion"] += ct; b["elapsed"] += res.elapsed
    res.usage = {"prompt": pt, "completion": ct,
                 "estimated": not (u.get("prompt") or u.get("completion"))}


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
    raw_usage = getattr(provider, "last_usage", {}) or {}
    if not out and rsn:          # ★ 兜底 1: 答案被 <answer> 包在 reasoning 里
        out = provider.salvage(rsn)
        if on_delta and out:
            on_delta(out)
    if not out and _retry:       # ★ 兜底 2: 关思考重试, 把 token 全给正文
        print("  [call] 输出为空, 关思考重试", flush=True)
        return call(profile, prompt, on_delta, system, max_tokens, _retry=False)
    res = GenResult(text=out, reasoning=rsn, elapsed=time.time() - t0,
                    chars=len(out), usage=dict(raw_usage))
    _record(profile, sum(len(m["content"]) for m in msgs), res)
    return res


# 模型经常把提示词里的括号说明当模板抄进正文, 例如
#   国号：燕朝（一个两字虚构国号，不得以真朝代单字开头…）
_ECHO = re.compile(
    r"（[^（）]{0,60}(?:不得|不许|禁止|仅内部参考|说明文字|不要抄|违反即作废)[^（）]{0,60}）")


def clean(text: str) -> str:
    """去掉模型爱加的前言/标题/代码围栏, 以及被复述的约束文字."""
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
    text = re.sub(r"^(?:好的|以下是|下面是)[^\n]{0,40}[:：]\s*\n+", "", text)
    text = _ECHO.sub("", text)
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
        from .evaluator import DEFAULT_TICS
        bl += DEFAULT_TICS                    # 冷启动就设防, 不等统计攒够
        lr = self.learned_rules()             # 自审学到的规则, 自动生效
        bl += lr.get("forbidden_terms", []) + lr.get("tics", [])
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
                topics=self.genre.get("research_topics"),
                plan=lambda q: clean(call("planning", q, max_tokens=500).text))
        return self._retriever

    def sanitize_facts(self, text: str) -> str:
        """架空模式下把参考资料里的真朝代名换成本书国号。

        考据卡会进 L1 常驻层, 里面满篇"宋代"等于把真朝代名喂回给模型 ——
        实测正文出现 28 次"宋律"就是这么来的。
        """
        if not text:
            return text
        a = self.world_anchor()
        if a.get("mode") != "alt" or not a.get("dynasty"):
            return text
        dyn = a["dynasty"][:-1] if a["dynasty"].endswith("朝") else a["dynasty"]
        for w in ("北宋", "南宋", "大宋", "宋朝", "宋代", "唐代", "唐朝", "明代",
                  "明朝", "清代", "清朝", "汉代", "元代"):
            text = text.replace(w, f"{dyn}朝")
        return re.sub(r"《宋史[^》]*》|《宋[^》]{0,4}》", f"《{dyn}史》", text)

    def ground(self, stage: str, extra=None, context: str = "") -> str:
        if not self.mcfg.get("web_search", True):
            return ""
        try:
            return self.retriever.ground(stage, extra=extra, context=context)
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
        forbidden += self.learned_rules().get("forbidden_terms", [])
        anchor["forbidden"] = list(dict.fromkeys(forbidden))
        if mode == "alt":
            from .evaluator import REAL_PEOPLE
            anchor["forbidden_people"] = REAL_PEOPLE
        return anchor

    def alias_pair(self) -> Optional[tuple]:
        """返回 (对外身份, 本名) —— 供状态抽取与约束共用。"""
        cards = self.roster()
        if not cards:
            return None
        head = cards[0]["card"]
        m = re.search(r"姓名\s*[:：]\s*([^\n（(]{1,8})(?:[（(]\s*(?:原名|本名|真名)?\s*[:：]?\s*([^）)]{1,8})[）)])?", head)
        if not m:
            return None
        a, b = m.group(1).strip(), (m.group(2) or "").strip()
        if not b or a == b:
            return None
        title = self.p.meta.get("title", "")
        # 书名里出现的那个是对外身份
        return (b, a) if b in title else (a, b)

    def protagonist_alias(self) -> str:
        """主角本名与对外身份不一致时（穿越/重生/马甲/化名）必须钉死称谓规则。

        实测: 花名册第一条是「林远」, 但书里对外身份是「西门庆」,
        没有这条约束后文会两个名字乱用。
        """
        pair = self.alias_pair()
        if not pair:
            return ""
        outer, inner = pair
        return (f"【称谓锚定】主角对外身份是「{outer}」，本名/前世名是「{inner}」。"
                f"叙述与他人称呼一律用「{outer}」；只有主角内心独白、"
                f"或明确回忆前世时才可出现「{inner}」，且不得让旁人叫出这个名字。")

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
            # 花名册首条用「对外身份」做名字, 否则模型会把本名当主名来写
            m = re.search(r"姓名\s*[:：]\s*([^\n（(]{1,8})[（(]\s*([^）)]{1,8})[）)]",
                          cards[0]["card"])
            if m:
                a, b = m.group(1).strip(), m.group(2).strip()
                title = self.p.meta.get("title", "")
                cards[0]["name"] = b if (b in title and a not in title) else a
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
        roles_all = self.p.read("characters.md")
        if roles_all:
            resident += "\n\n【全部角色档案（备查，勿引入未列出的新角色）】\n" + roles_all

        sm = self.p.state.get("summaries", {})
        k = self.mcfg.get("recent_chapters", 8)
        recent = [f"第{i}章：{sm[str(i)]}" for i in range(max(1, n - k), n) if str(i) in sm]
        # 只给一句话摘要, 模型接不住前文的文风、称谓、场景细节。
        # 窗口有近 10 万 token 而实测只用了 20%, 完全装得下最近几章原文。
        full_k = int(self.mcfg.get("recent_full", 2) or 0)
        for i in range(max(1, n - full_k), n):
            body = self.p.chapter(i)
            if body:
                recent.append(f"\n———— 第{i}章 正文（承接文风与细节）————\n{body}")

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
                        f"律法称「{anchor['dynasty']}律」，史书称「{anchor['dynasty']}史」。"
                        f"也不得出现真实历史人物（赵构、蔡京、岳飞、苏轼…），"
                        f"需要类似角色请用虚构姓名。")
        elif anchor.get("mode") == "real":
            cons.append("【史实锚定·正统历史】本书写的就是真实朝代，朝代名、官职、律法、"
                        "纪年一律用真实名称，不要自造国号。凡涉及具体年份、官职品级、"
                        "物价、器物，必须与背景资料一致；资料没有的宁可写模糊，不许编数字。")
        if anchor.get("main_place"):
            cons.append(f"【主场锚定】主角常驻地是「{anchor['main_place']}」，"
                        f"不得随意把主场换到别的县城；确需异地必须写明行程。")
        guide = self.p.read("style_guide.md")
        if guide:
            cons.append("【本书写作守则·自审沉淀】\n" + guide[:1200])
        alias = self.protagonist_alias()
        if alias:
            cons.append(alias)
        lr = self.learned_rules()
        if lr.get("must_appear"):
            cons.append("【断线角色必须回归】" + "、".join(lr["must_appear"][:5])
                        + " —— 接下来几章内安排他们出场并有实质戏份。")
        if lr.get("drop_roles"):
            cons.append("【已废弃角色，不得再提】" + "、".join(lr["drop_roles"][:6]))
        # 开篇去重 —— 自检报告「连续两章寅时三刻开篇」, 光靠事后判雷同没用,
        # 必须把上几章的开篇原样给模型看, 让它主动避开。
        done = sorted(self.p.state.get("done", []))
        heads = []
        for i in done[-3:]:
            first = next((ln.strip() for ln in self.p.chapter(i).splitlines()
                          if ln.strip()), "")
            if first:
                heads.append(f"第{i}章：{first[:36]}")
        if heads:
            cons.append("【开篇必须换花样】前几章是这样开场的——" + "；".join(heads)
                        + "。本章开篇的时间词、地点、句式、视角都不得与之雷同，"
                        "换一种切入方式（如直接对白、动作特写、他人视角）。")
        tg = self.tic_guard()
        if tg:
            cons.append("【口癖抑制】" + tg)
        cons.append("【配角配额】本章除主角外至少让 2 个配角有独立台词与动作，"
                    "配角不能只当背景板；不得给已知人物随意安排与其身份不符的官职。")
        bl = self.blacklist()
        # 两类区别对待: 穿帮词是硬禁, 用滥的表达是限频 —— 一律硬禁会误伤正常写作
        hard = [w for w in bl if w in set(REAL_DYNASTIES) | set(
            self.learned_rules().get("forbidden_terms", [])[:0] or [])]
        anchor_forb = set(anchor.get("forbidden") or [])
        hard = [w for w in bl if w in anchor_forb]
        soft = [w for w in bl if w not in anchor_forb]
        if hard:
            cons.append("【绝不能出现】" + "、".join(hard[:20]))
        if soft:
            cons.append("【已被用滥的表达，本章最多出现 1 次，含变体】"
                        + "、".join(soft[:24]))
        # 分层注入 —— 之前永远取最早 6 条(FIFO), 而最早那几条恰恰是早期抽取
        # 不准的垃圾, 于是模型永远看不到真正该收的新伏笔, 回收率卡在 4%。
        pend = self.p.mem.pending_foreshadow()
        if pend:
            cap = int(self.mcfg.get("foreshadow_show", 24))
            urgent = [f for f in pend if n - f["planted"] >= 30][:max(4, cap // 3)]
            active = [f for f in pend if n - f["planted"] < 12][-max(6, cap // 3):]
            midway = [f for f in pend if 12 <= n - f["planted"] < 30][:max(4, cap // 3)]
            show = list({f["id"]: f for f in urgent + active + midway}.values())
            if show:
                cons.append("【未回收伏笔】" + "；".join(
                    f"第{f['planted']}章「{f['text'][:36]}」" for f in show))
            if urgent:
                cons.append(f"【伏笔告急】埋了 30 章以上还没兑现，本章或接下来几章"
                            f"必须给个交代：" + "；".join(f"「{f['text'][:34]}」" for f in urgent))

        roles = self.p.state.get("roles", {})
        if roles:
            latest_roles = sorted(roles.items(), key=lambda kv: -kv[1].get("at", 0))[:8]
            cons.append("【角色当前状态·不得凭空改变】" + "；".join(
                f"{k}（第{v['at']}章）{v['state']}" for k, v in latest_roles))
        tl = self.p.state.get("timeline", {})
        if tl:
            last = [f"第{k}章:{v}" for k, v in sorted(tl.items(), key=lambda x: int(x[0]))[-4:] if v]
            if last:
                cons.append("【时间线】" + "；".join(last)
                            + "。本章必须明确交代距上一章过了多久，不得时间跳跃无说明。")

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
        bg = self.sanitize_facts(
            self.ground("world", context=ctx.get("premise", "") + "\n" + ctx.get("background", "")))
        if bg:
            prompt += ("\n\n【现实参考资料 —— 只用来保证器物、官制、物价、风俗合理，"
                       "资料里的朝代名与专有名词一律不得出现在成稿里】\n" + bg[:6000])
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
            "6. 【架空世界禁用真实历史人物】不得出现真实存在过的帝王将相文人"
            "（如赵构、蔡京、岳飞、诸葛亮、魏征…）。需要类似角色时另起名字，"
            "可保留原型气质但姓名必须虚构\n"
            "直接输出，无前言。", ctx)
        bg = self.sanitize_facts(self.ground("cast", context=self.p.read("world_bible.md")[:3000]))
        if bg:
            prompt += ("\n\n【现实参考资料 —— 姓名、称谓、职业、阶层、女性处境"
                       "须符合下列常识；资料里的朝代名不得出现在成稿里】\n" + bg[:5000])
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

    def step_volumes(self, on_delta=None) -> List[Dict[str, Any]]:
        """把全书拆成卷 —— 192 章直接从总纲跳到分章，中层节奏必然散。

        每卷给出：卷名 / 起止章 / 本卷主线 / 本卷高潮 / 卷末钩子 / 本卷主要出场角色。
        写分章细纲时只带所属卷的信息，而不是把整本总纲怼进去。
        """
        cached = self.p._load("volumes.json", None)
        if cached:
            return cached
        total = self.p.meta.get("target_chapters", 0)
        per = max(20, min(50, total // max(3, round(total / 40)) if total else 40))
        n_vol = max(2, round(total / per)) if total else 4
        anchor = self.world_anchor()
        prompt = (
            f"为《{self.p.meta.get('title','')}》做分卷。全书 {total} 章，分 {n_vol} 卷。\n\n"
            f"#总纲\n{self.p.read('outline.md')[:4000]}\n\n"
            f"#可用角色\n{'、'.join(c['name'] for c in self.roster()) or '未定'}\n\n"
            f"#题材节奏要求\n{self.genre_rules()[:800]}\n\n"
            + (f"#锚定\n朝代只叫「{anchor['dynasty']}」\n\n" if anchor.get("dynasty") else "")
            + f"每卷严格按此格式，卷之间用一行 ###fenge 分隔：\n"
              f"卷名：…\n章节范围：第X章-第Y章\n本卷主线：…\n"
              f"本卷高潮：（具体事件）\n卷末钩子：…\n主要出场：（3-6 个角色名）\n"
              f"实力/地位变化：（主角从什么状态到什么状态）\n\n"
              f"要求：卷与卷之间要有明显的格局升级，不能原地打转。直接输出，无前言。")
        r = call("planning", prompt, on_delta, max_tokens=4000)
        vols: List[Dict[str, Any]] = []
        cur = 1
        for blk in [clean(x) for x in re.split(r"###fenge", r.text) if x.strip()]:
            m = re.search(r"章节范围[^\d]*(\d+)\D+(\d+)", blk)
            a, b = (int(m.group(1)), int(m.group(2))) if m else (cur, cur + per - 1)
            name = (re.search(r"卷名\s*[:：]\s*(.+)", blk) or [None, f"第{len(vols)+1}卷"])[1]
            vols.append({"index": len(vols) + 1, "name": str(name).strip()[:30],
                         "start": a, "end": b, "text": blk})
            cur = b + 1
        if vols:
            self.p.write("volumes.json", json.dumps(vols, ensure_ascii=False, indent=2))
            self.p.mem.index_document("world", "volumes", r.text)
        self._log(f"分卷 {len(vols)} 卷 / {r.elapsed:.1f}s")
        return vols

    def volume_of(self, n: int) -> Dict[str, Any]:
        for v in (self.p._load("volumes.json", []) or []):
            if v["start"] <= n <= v["end"]:
                return v
        return {}

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
        vol = self.volume_of(start)
        if not vol:
            self.step_volumes()
            vol = self.volume_of(start)
        outline_ctx = self.p.read("outline.md")[:1600]
        if vol:
            outline_ctx = (f"【本卷：{vol['name']}（第{vol['start']}-{vol['end']}章）】\n"
                           f"{vol['text']}\n\n【全书总纲摘要】\n" + outline_ctx)
            cons.append(f"本批章节属于「{vol['name']}」，必须服务于本卷主线与卷末钩子，"
                        f"不得越出本卷进度")
        prompt = compile_outline_prompt(
            title=self.p.meta.get("title", ""), start=start, count=count,
            genre_line=f"{self.genre.get('name','')}/{self.style.get('name','')}".strip("/"),
            world_digest=self.p.read("world_bible.md")[:3000],
            roster_names=[c["name"] for c in self.roster()] or ["主角"],
            outline=outline_ctx,
            prev_summary=self.prev_summary(start),
            constraints="\n".join(cons),
            used_titles=[m.group(1).strip()[:20] for m in
                         (re.search(r"第\s*\d+\s*章\s*(.+)", v)
                          for v in self.p._load("chapter_outlines.json", {}).values())
                         if m],
            plots_per_chapter=max(3, int(
                (self.g["chapter_words_min"] + self.g["chapter_words_max"]) / 2
                / (int(self.style.get("blockWords") or 500) * 0.8))))
        # 细纲生成之前也要召回已确立的事实 —— 否则会写出自相矛盾的剧情。
        # 实测: 第 25 章把玉佩指向皇子赵琰, 第 33 章又说是东平府通判赵家之物,
        # 因为细纲生成压根没接记忆层, 模型看不到前面已经定死的结论。
        seed = self.p.read("outline.md")[:1200] + "\n" + self.prev_summary(start, k=6)
        established = ""
        try:
            hits = self.p.mem.search(seed, k=8)
            if hits:
                established = "\n".join(
                    f"- {h['title']}：{h['text'][:220]}" for h in hits)
        except Exception as e:
            print(f"[outline] 事实召回跳过: {e}")
        pend = self.p.mem.pending_foreshadow()
        if pend:
            established += "\n【未回收伏笔，本批可择机兑现】" + "；".join(
                f"第{f['planted']}章「{f['text'][:36]}」" for f in pend[:8])
        if established:
            cons.append("【已确立的事实，不得推翻或给出不同结论】\n" + established[:2500])

        bg = self.sanitize_facts(self.ground("plot", context=self.p.read("outline.md")[:2500]))
        if bg:
            prompt += ("\n\n【现实参考资料 —— 本批剧情涉及的器物、行程、礼俗须符合下列常识；"
                       "资料里的朝代名不得出现在成稿里】\n" + bg[:5000])
        r = call("planning", prompt, on_delta, max_tokens=8000)
        parts = [clean(x) for x in re.split(r"###fenge", r.text) if x.strip()]
        outlines = self.p._load("chapter_outlines.json", {})
        for i, part in enumerate(parts):
            outlines[str(start + i)] = part
        self.p.write("chapter_outlines.json", json.dumps(outlines, ensure_ascii=False, indent=2))
        self._log(f"细纲 {start}-{start+len(parts)-1} 共 {len(parts)} 章 / {r.elapsed:.1f}s")
        return parts

    def step_chapter(self, n: int, on_delta=None, retry_on_low: int | None = None) -> Dict[str, Any]:
        self._check_budget()
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
            alias_rule=self.protagonist_alias(),
            roster=rost, protagonist=((self.alias_pair() or [None])[0]
                                      or (rost[0]["name"] if rost else "")),
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
        # 按目标字数推导 max_tokens 做硬上限 —— 8192 太宽松, 实测普遍超 10~50%。
        # 中文约 1 字 1.4 token, 留 1.25 倍余量给标点与收尾。
        cap = min(self.g["max_tokens_draft"], int(target / 0.7 * 1.25))
        r = call("drafting", prompt, on_delta, max_tokens=cap)
        # 去掉模型输出里的【字数标记】—— 它只是写作时的计数脚手架, 不进成稿
        r.text = re.sub(r"【字数标记[^】]*】\s*", "", r.text)
        text = clean(r.text)

        a = audit(text, extra_blacklist=self.blacklist(), target_words=target,
                  check_modern=self.history_mode() in ("real", "alt"))
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
            over_note = ""
            if a["stats"]["cn"] > target * 1.15:
                over_note = (f"另外字数严重超标（{a['stats']['cn']}/{target}），必须压缩到 "
                             f"{target} 字左右，删掉旁枝末节与重复铺陈，保留主线与爽点。\n")
            fix = (f"下面这章 AI 味检测不合格（{a['score']}分）。问题：{probs}\n"
                   f"{over_note}"
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

        # 结构化抽取: 摘要 + 伏笔 + 角色状态 + 时间推进 —— 合并成一次调用
        one = self._extract_state(n, text)
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
        self.p.state["usage"] = self._usage_snapshot()
        self.p.save()
        self.p.write("PROJECT_BOARD.md", self.p.board())

        # 每 10 章压一次 L2 摘要
        every = self.mcfg.get("l2_every", 10)
        if n % every == 0:
            self._l2(n - every + 1, n)
        ref = int(self.q.get("reflect_every", 5) or 0)
        if ref and n % ref == 0:
            try:
                self.step_reflect()
            except Exception as e:
                self._log(f"自审失败(不阻塞写作): {e}")
        sc = int(self.q.get("selfcheck_every", 20) or 0)
        if sc and n % sc == 0:
            try:
                self.step_selfcheck()
            except Exception as e:
                self._log(f"系统自检失败(不阻塞写作): {e}")
        return {"chapter": n, "chars": a["stats"]["cn"], "score": a["score"],
                "elapsed": r.elapsed, "rewritten": a.get("rewritten", False)}

    # ---------------- 自我改进循环 ----------------
    def step_reflect(self, on_delta=None, sample: int = 3) -> Dict[str, Any]:
        """写几章就自己读一遍、批一遍，把结论沉淀成本书的写作守则。

        单纯把审查分数打出来没用 —— 必须把"哪里不好、下次怎么写"变成
        可执行的守则文本，注入后续每一章。这是全书质量能爬坡的唯一机制。
        """
        done = sorted(self.p.state.get("done", []))
        if len(done) < 3:
            return {"skipped": "章节太少"}

        chs = {n: self.p.chapter(n) for n in done}
        names = [c["name"] for c in self.roster()] or []
        anchor = self.world_anchor()
        forb = [w for w in (anchor.get("forbidden") or []) if w not in set(names)]
        ba = book_audit(chs, characters=names, forbidden_terms=forb,
                        forbidden_people=anchor.get("forbidden_people"),
                        protagonist=names[0] if names else "")
        # 全书分会被前期旧账永久拖住, 再给一个「最近 20 章」的趋势分,
        # 让自审看得见改进, 而不是每次都看到同一个分数
        recent_ids = sorted(chs)[-20:]
        ba_recent = book_audit({i: chs[i] for i in recent_ids}, characters=names,
                               forbidden_terms=forb,
                               forbidden_people=anchor.get("forbidden_people"),
                               protagonist=names[0] if names else "")
        ba["recent_score"] = ba_recent["score"]
        ba["recent_range"] = f"{recent_ids[0]}-{recent_ids[-1]}" if recent_ids else ""
        wa = window_audit(chs, done[-1], span=3,
                          outlines=self.p._load("chapter_outlines.json", {}))

        # 抽样最近几章正文给 critic 读 —— 光看指标看不出"写得好不好"
        picks = done[-sample:]
        excerpt = "\n\n".join(
            f"—— 第{n}章（节选）——\n{chs[n][:1800]}" for n in picks)

        prev_guide = self.p.read("style_guide.md")
        problems = json.dumps(ba.get("issues", []) + wa.get("issues", []),
                              ensure_ascii=False)[:2500]

        rules_now = self.p._load("rules.json", {})
        prompt = (
            f"你是网文主编，正在审读《{self.p.meta.get('title','')}》。\n\n"
            f"【机器体检结论】\n全书 {ba['score']}/100，近章窗口 {wa.get('score')}/100\n"
            f"{problems}\n\n"
            f"【最近 {len(picks)} 章正文节选】\n{excerpt}\n\n"
            + (f"【上一版写作守则】\n{prev_guide[:1500]}\n\n" if prev_guide else "")
            + "请输出**更新后的本书写作守则**，直接给后续章节的作者看。要求：\n"
              "1. 只写可执行的具体指令，不要评价、不要鼓励、不要空话\n"
              "2. 每条指令都要能被检查（写什么/不写什么/写多少）\n"
              "3. 优先解决体检里的高危问题，并针对节选里读到的实际毛病补充\n"
              "4. 保留上一版里仍然有效的条目，去掉已经解决的\n"
              "5. 12 条以内，每条一行，以「-」开头\n\n"
              "写完守则后，另起一行输出 ===RULES=== ，再输出一段 JSON（不要代码围栏），"
              "把守则里**能被程序自动检查**的部分抽出来，供检测器强制执行：\n"
              '{"forbidden_terms":["本书绝不能出现的词，如穿帮的朝代名/现代词"],'
              '"tics":["被用滥、应加入禁用的表达"],'
              '"must_appear":["接下来几章必须回归的断线角色"],'
              '"drop_roles":["建了档但一直没登场、建议删除的角色"]}\n'
              "每项最多 8 条，没有就给空数组。只抽**确定无疑**的，宁缺毋滥。\n"
              + (f"（已有规则，不要重复：{json.dumps(rules_now, ensure_ascii=False)[:600]}）\n"
                 if rules_now else "")
              + "先输出守则，再输出 ===RULES=== 与 JSON。")
        r = call("judging", prompt, on_delta, max_tokens=2200)
        body = clean(r.text)
        guide, _, rules_raw = body.partition("===RULES===")
        guide = guide.strip()
        self._merge_rules(rules_raw)
        if guide:
            self.p.write("style_guide.md", guide)
            hist = self.p._load("reflect_log.json", [])
            hist.append({"at_chapter": done[-1], "book_score": ba["score"],
                         "window_score": wa.get("score"),
                         "issues": [i["type"] for i in ba.get("issues", [])],
                         "guide_chars": len(guide)})
            self.p.write("reflect_log.json", json.dumps(hist, ensure_ascii=False, indent=2))
        self._log(f"自审@第{done[-1]}章 全书{ba['score']}(近{ba.get('recent_score')}) "
                  f"窗口{wa.get('score')} "
                  f"→ 守则 {len(guide)} 字")
        return {"book": ba, "window": wa, "guide": guide}

    # ---------------- 章节重写 ----------------
    def rewrite_chapter(self, n: int, mode: str = "polish", note: str = "",
                        on_delta=None) -> Dict[str, Any]:
        """三种重写模式（取自 x10086 novel-writing skill 的实践）。

        polish  剧情完全不动，只改语言、节奏、对白
        replace 整章重写，剧情可调，但必须衔接前后章
        fork    从本章分叉出另一条线，旧稿保留为 .alt
        旧稿一律先备份到 .ckpt/，永不覆盖丢失。
        """
        old = self.p.chapter(n)
        if not old:
            raise RuntimeError(f"第 {n} 章没有正文")
        ck = self.p.dir / ".ckpt"
        ck.mkdir(exist_ok=True)
        ver = len(list(ck.glob(f"{n:03d}_v*.md"))) + 1
        (ck / f"{n:03d}_v{ver}.md").write_text(old, encoding="utf-8")

        co = self.p._load("chapter_outlines.json", {}).get(str(n), "")
        asm = self.build_context(n, co)
        L = asm["layers"]
        target = (self.g["chapter_words_min"] + self.g["chapter_words_max"]) // 2
        common = (f"【本章细纲】\n{co}\n\n【必守约束】\n{L.get('L5_constraint','')}\n\n"
                  f"【前情】\n{L.get('L2_recent','')}\n")

        if mode == "polish":
            prompt = (f"下面这一章剧情不动，只改语言。\n{common}\n"
                      f"改写要求：{note or '句式长短交错，去掉套话与 AI 腔，对白更有性格，节奏更紧'}\n"
                      f"字数保持 {target} 字左右。剧情、人物、事件顺序一律不得改变。"
                      f"直接输出正文，无前言。\n\n{old}")
            profile = "polishing"
        elif mode == "replace":
            prompt = (f"重写《{self.p.meta.get('title','')}》第 {n} 章。\n{common}\n"
                      f"重写方向：{note or '按细纲重新组织，加强冲突与钩子'}\n"
                      f"必须与前后章衔接。字数 {target} 字左右。直接输出正文，无前言。\n\n"
                      f"【原稿供参考，可大幅改动】\n{old[:3000]}")
            profile = "drafting"
        elif mode == "fork":
            (self.p.dir / f"chapters/{n:03d}.alt.md").write_text(old, encoding="utf-8")
            prompt = (f"从第 {n} 章分叉出另一种走向。\n{common}\n"
                      f"分叉方向：{note or '让本章的关键选择走向相反的结果'}\n"
                      f"字数 {target} 字左右。直接输出正文，无前言。\n\n"
                      f"【原线供对照】\n{old[:2500]}")
            profile = "drafting"
        else:
            raise ValueError(f"未知模式 {mode}，可选 polish/replace/fork")

        r = call(profile, prompt, on_delta, max_tokens=self.g["max_tokens_draft"])
        new = re.sub(r"【字数标记[^】]*】\s*", "", clean(r.text))
        if not new or len(new) < len(old) * 0.4:
            return {"ok": False, "msg": "重写结果过短，已保留原稿",
                    "backup": f".ckpt/{n:03d}_v{ver}.md"}

        self.p.write(self.p.chapter_path(n), new)
        a = audit(new, extra_blacklist=self.blacklist(), target_words=target)
        self.p.write(f"audit/{n:03d}.json", json.dumps(a, ensure_ascii=False, indent=2))
        self.p.state.setdefault("summaries", {})[str(n)] = self._extract_state(n, new)
        self.p.mem.add("plot", f"ch{n}", f"第{n}章", new[:1500])

        # 剧情有变时提示下游受影响的章节
        affected = []
        if mode in ("replace", "fork"):
            done = sorted(self.p.state.get("done", []))
            affected = [x for x in done if x > n][:5]
        log = self.p._load("edit_log.json", [])
        log.append({"chapter": n, "mode": mode, "note": note, "backup": f"{n:03d}_v{ver}.md",
                    "score_before": None, "score_after": a["score"]})
        self.p.write("edit_log.json", json.dumps(log, ensure_ascii=False, indent=2))
        self._log(f"第{n}章 {mode} 重写 → {a['stats']['cn']}字 得分{a['score']}"
                  + (f"，可能影响后续 {affected}" if affected else ""))
        return {"ok": True, "mode": mode, "score": a["score"],
                "chars": a["stats"]["cn"], "backup": f".ckpt/{n:03d}_v{ver}.md",
                "affected": affected}

    def repair_violations(self, limit: int = 10, dry: bool = False,
                          on_delta=None) -> Dict[str, Any]:
        """按当前规则批量返修旧章 —— 规则是后来长出来的，前面的章享受不到。

        实测: 第 20 章才加的禁用词，改不了 1-19 章，于是全书分被存量永久拖住
        （全书 52 而最近 20 章 74）。这里找出违规最重的章节做 polish 重写，
        剧情不动只改语言，旧稿照例备份到 .ckpt/。
        """
        done = sorted(self.p.state.get("done", []))
        bl = self.blacklist()
        target = (self.g["chapter_words_min"] + self.g["chapter_words_max"]) // 2
        ranked = []
        for n in done:
            t = self.p.chapter(n)
            if not t:
                continue
            a = audit(t, extra_blacklist=bl, target_words=target,
                      check_modern=self.history_mode() in ("real", "alt"))
            hits = {w: t.count(w) for w in bl if w in t}
            if a["score"] < self.q["audit_pass_score"] or hits:
                ranked.append({"n": n, "score": a["score"],
                               "violations": sum(hits.values()), "hits": hits})
        ranked.sort(key=lambda x: (-x["violations"], x["score"]))
        picked = ranked[:limit]
        if dry:
            return {"candidates": ranked, "would_repair": [x["n"] for x in picked]}

        fixed = []
        for item in picked:
            n = item["n"]
            note = ("重点清除这些违规表达：" + "、".join(list(item["hits"])[:8])
                    if item["hits"] else "去掉套话与 AI 腔，句式长短交错")
            try:
                r = self.rewrite_chapter(n, mode="polish", note=note, on_delta=on_delta)
                fixed.append({"n": n, "before": item["score"],
                              "after": r.get("score"), "ok": r.get("ok")})
            except Exception as e:
                fixed.append({"n": n, "error": str(e)[:120]})
        self._log(f"批量返修 {len(fixed)} 章: " +
                  "、".join(f"{f['n']}({f.get('before')}→{f.get('after')})" for f in fixed))
        return {"repaired": fixed, "remaining": len(ranked) - len(picked)}

    def _extract_state(self, n: int, text: str) -> str:
        """一次调用抽出: 本章摘要 / 新埋与回收的伏笔 / 角色状态变化 / 时间推进。

        没有这层, 人物会瞬移、伤口会自愈、伏笔埋了永不回收、时间线会错乱 ——
        这些都是单章读起来没问题、连起来一定崩的东西。
        """
        names = [c["name"] for c in self.roster()][:14]
        alias = self.alias_pair()
        alias_note = (f"注意：「{alias[0]}」与「{alias[1]}」是同一个人，"
                      f"角色状态一律记在「{alias[0]}」名下。\n" if alias else "")
        prompt = (
            f"读下面这一章，抽取结构化信息。已知角色：{'、'.join(names) or '未知'}\n"
            f"{alias_note}\n"
            f"严格按下面格式输出，没有内容的写「无」，不要任何多余文字：\n"
            f"摘要：（60 字以内一句话概括本章发生了什么）\n"
            f"时间：（本章相对上一章过了多久，如「当天下午」「三日后」）\n"
            f"埋伏笔：（**只记真正的悬念**：被刻意隐藏、以后必须专门写一段来解开的东西。\n"
            f"  算：身份秘密、来历不明的物证、没说破的承诺、可疑的人、被打断的话、"
            f"莫名消失的东西。\n"
            f"  不算：人物性格（如「某人贪婪」）、当前处境（如「某人心态转变」）、"
            f"已经讲明白的事、单纯的剧情推进。\n"
            f"  最多 2 条，宁可写「无」也不要凑数）\n"
            f"收伏笔：（本章解开了之前埋的哪些线索，分号分隔）\n"
            f"角色状态：（格式 姓名=所在地/身体状态/关键持有物，分号分隔，只列本章出场的）\n\n"
            f"{text[:4000]}")
        r = call("polishing", prompt, max_tokens=600)
        out = clean(r.text)

        def field(k: str) -> str:
            m = re.search(rf"^{k}\s*[:：]\s*(.*)$", out, re.M)
            v = (m.group(1).strip() if m else "")
            return "" if v in ("无", "None", "-") else v

        summary = field("摘要") or out.splitlines()[0][:60]
        st = self.p.state
        st.setdefault("timeline", {})[str(n)] = field("时间")

        for f in [x.strip() for x in re.split(r"[;；]", field("埋伏笔")) if len(x.strip()) > 3][:2]:
            self.p.mem.add_foreshadow(n, f[:60])
        self._resolve_foreshadow(n, text)

        roles = st.setdefault("roles", {})
        for item in [x.strip() for x in re.split(r"[;；]", field("角色状态")) if "=" in x]:
            name, _, val = item.partition("=")
            name = name.strip()
            if alias and name == alias[1]:      # 别名归一, 免得主角被记成两个人
                name = alias[0]
            if name:
                roles[name] = {"at": n, "state": val.strip()[:80]}
        if alias and alias[1] in roles and alias[0] in roles:
            roles.pop(alias[1], None)
        self.p.save()
        return summary.replace("\n", " ")

    def step_selfcheck(self, on_delta=None) -> Dict[str, Any]:
        """系统级自检 —— 区分「内容问题」与「系统问题」。

        自审守则只能改内容。但有些毛病守则治不了:
          * 检测器漏检 (「瞳孔骤缩」不在黑名单里, 写了 9 次没人管)
          * 检测器误报 (「西门府」被当成行政区, 天天报主场漂移)
          * 约束没被遵守 (称谓规则埋在末尾, 模型压根不看)
          * 配额设错 (10 条剧情写 2600 字, 必然超字数)
        这些要改代码或配置。让系统自己识别出来写成待办, 而不是靠人一章章读。
        """
        done = sorted(self.p.state.get("done", []))
        if len(done) < 5:
            return {"skipped": "章节太少"}
        chs = {n: self.p.chapter(n) for n in done}
        names = [c["name"] for c in self.roster()]
        anchor = self.world_anchor()
        ba = book_audit(chs, characters=names, forbidden_terms=anchor.get("forbidden"),
                        forbidden_people=anchor.get("forbidden_people"),
                        protagonist=names[0] if names else "")
        per = []
        for n in done[-8:]:
            a = self.p._load(f"audit/{n:03d}.json", {})
            if a:
                per.append({"n": n, "score": a.get("score"),
                            "cn": (a.get("stats") or {}).get("cn"),
                            "issues": [i["type"] for i in a.get("issues", [])]})
        guide = self.p.read("style_guide.md")
        rules = self.learned_rules()
        target = (self.g["chapter_words_min"] + self.g["chapter_words_max"]) // 2

        prompt = (
            "你是这套 AI 写作系统的架构师，正在做系统级自检。\n\n"
            f"【目标单章字数】{target}\n"
            f"【逐章检测结果】{json.dumps(per, ensure_ascii=False)}\n\n"
            f"【全书体检】总分 {ba['score']}；最近 {ba.get('recent_range')} 章 "
            f"{ba.get('recent_score')} 分（分差说明前期旧账，重点看后者的趋势）\n问题："
            f"{json.dumps([{'type': i['type'], 'detail': str(i.get('detail'))[:160]} for i in ba['issues']], ensure_ascii=False)}\n\n"
            f"【当前机器规则】{json.dumps(rules, ensure_ascii=False)[:800]}\n\n"
            f"【当前写作守则】\n{guide[:1200]}\n\n"
            f"【最近一章正文节选】\n{chs[done[-1]][:2000]}\n\n"
            "请判断：哪些问题**靠改提示词/守则解决不了**，是系统本身的缺陷？只看这四类：\n"
            "1. 漏检 —— 正文里明显有问题但检测器没报出来\n"
            "2. 误报 —— 检测器报了但其实不是问题\n"
            "3. 约束失效 —— 约束写了但模型明显没遵守（说明位置或写法有问题）\n"
            "4. 配额错误 —— 字数/条数/预算之类的参数设得不合理\n\n"
            "严格输出 JSON（不要代码围栏，没有就给空数组）：\n"
            '{"issues":[{"kind":"漏检|误报|约束失效|配额错误","what":"一句话说清现象",'
            '"evidence":"引用具体证据","fix":"建议怎么改（改哪个模块/参数）"}]}\n'
            "只报**证据确凿**的，最多 5 条。纯内容问题（写得不好看、人物不立体）不要报。")
        r = call("judging", prompt, on_delta, max_tokens=1600)
        m = re.search(r"\{.*\}", clean(r.text), re.S)
        found = []
        if m:
            try:
                found = (json.loads(m.group(0)).get("issues") or [])[:5]
            except Exception:
                pass
        if found:
            log = self.p._load("system_issues.json", [])
            log.append({"at_chapter": done[-1], "book_score": ba["score"], "issues": found})
            self.p.write("system_issues.json", json.dumps(log, ensure_ascii=False, indent=2))
            md = [f"# 系统级待办（自动生成）\n\n> 由 step_selfcheck 产出，"
                  f"这些是**改提示词解决不了**、需要动代码或配置的问题。\n"]
            for e in log[-6:]:
                md.append(f"\n## @第{e['at_chapter']}章（全书 {e['book_score']} 分）\n")
                for i in e["issues"]:
                    md.append(f"- **[{i.get('kind')}]** {i.get('what')}\n"
                              f"  - 证据：{i.get('evidence')}\n"
                              f"  - 建议：{i.get('fix')}\n")
            self.p.write("SYSTEM_ISSUES.md", "".join(md))
        self._log(f"系统自检@第{done[-1]}章 → {len(found)} 条系统级问题")
        return {"issues": found, "book_score": ba["score"]}

    # 规则守门 —— 判断交给模型，正则只做廉价预筛。
    # 之前用一堆正则启发式（拟声词/感官名词/词频…），结果先误杀「脸色铁青」这种
    # 真套话，又漏掉「蔡知县」这种角色称呼，来回调都调不准。这类判断本就需要
    # 理解上下文，应该让模型做。
    _CHEAP_REJECT = re.compile(
        r"[（(].*?[）)]"                                # 带括号的条件说明
        r"|作为|用作|无铺垫|不得|禁止|应当|时候|场景|之类|等等"  # 描述句而非词条
        r"|[A-Za-z]"                                    # 占位符 X/N 或英文
        r"|^\W*$")

    def _cheap_ok(self, w: str) -> bool:
        return bool(w) and 2 <= len(w) <= 12 and not self._CHEAP_REJECT.search(w)

    def gate_rules(self, cands: List[str]) -> Dict[str, List[str]]:
        """让模型判定每个候选词该硬禁、该限频、还是该丢弃。

        返回 {"hard": [...], "soft": [...], "drop": [{"w":…, "why":…}]}
          hard 穿帮词（真朝代名/现代词/真实历史人物）—— 出现即违规
          soft 被用滥的套话 —— 限频，超密度才扣分
          drop 角色称呼 / 剧情道具 / 正常描写 —— 禁掉会误伤，不进规则
        """
        cands = [w.strip() for w in cands if w and w.strip()]
        pre_drop = [{"w": w, "why": "格式不合（含条件说明/占位符/长度异常）"}
                    for w in cands if not self._cheap_ok(w)]
        rest = [w for w in cands if self._cheap_ok(w)]
        if not rest:
            return {"hard": [], "soft": [], "drop": pre_drop}

        roster = "、".join(c["name"] for c in self.roster()) or "（未知）"
        done = sorted(self.p.state.get("done", []))
        sample = "\n".join(self.p.chapter(n)[:700] for n in done[-2:])
        anchor = self.world_anchor()
        prompt = (
            f"你在给一套 AI 写作系统做「禁用词守门」。下面是候选词，"
            f"请判断每个词该怎么处理。\n\n"
            f"【本书】《{self.p.meta.get('title','')}》"
            f"{'（架空世界，国号「'+anchor['dynasty']+'」）' if anchor.get('dynasty') else ''}\n"
            f"【出场角色】{roster}\n"
            f"【近两章正文节选】\n{sample[:2500]}\n\n"
            f"【候选词】{'、'.join(rest)}\n\n"
            f"分三类：\n"
            f"hard = 穿帮词，出现即错。真实朝代名、现代词汇、真实历史人物名、"
            f"与本书设定冲突的词。\n"
            f"soft = 被用滥的套话或固定搭配（如「脸色铁青」「目光如刀」），"
            f"偶尔用没问题、频繁用才是毛病，应当限频。\n"
            f"drop = **禁掉会误伤正常写作**的：角色的姓名/职务称呼/别号、"
            f"本书的剧情道具与专有名词、拟声词、必要的感官描写。\n\n"
            f"只输出 JSON（不要代码围栏）：\n"
            f'{{"hard":["…"],"soft":["…"],"drop":[{{"w":"…","why":"一句话理由"}}]}}\n'
            f"每个候选词必须且只能出现在一类里。拿不准就归 drop —— "
            f"误禁一个角色名的代价远大于漏禁一个套话。")
        try:
            r = call("judging", prompt, max_tokens=1200)
            m = re.search(r"\{.*\}", clean(r.text), re.S)
            d = json.loads(m.group(0)) if m else {}
        except Exception as e:
            print(f"[gate] 模型判定失败, 全部保守丢弃: {e}")
            return {"hard": [], "soft": [],
                    "drop": pre_drop + [{"w": w, "why": "守门模型不可用"} for w in rest]}

        hard = [w for w in (d.get("hard") or []) if w in rest]
        soft = [w for w in (d.get("soft") or []) if w in rest and w not in hard]
        judged = set(hard) | set(soft)
        drop = pre_drop + [x if isinstance(x, dict) else {"w": x, "why": ""}
                           for x in (d.get("drop") or [])]
        drop += [{"w": w, "why": "模型未归类，保守丢弃"}
                 for w in rest if w not in judged
                 and w not in {x.get("w") for x in drop if isinstance(x, dict)}]
        return {"hard": hard, "soft": soft, "drop": drop}

    def _merge_rules(self, raw: str) -> Dict[str, Any]:
        """把自审抽出的结构化规则并进 rules.json —— 让模型的发现变成机器强制。

        这是闭环的关键: 自审如果只产出一段文字守则, 模型下次照样犯;
        只有把「严禁出现大汉一词」变成检测器里的 forbidden_terms,
        才会在生成后被真正拦下来。
        """
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return {}
        try:
            new = json.loads(m.group(0))
        except Exception:
            return {}
        cur = self.p._load("rules.json", {})
        changed = {}
        # 禁用类候选统一交给模型守门, 分成硬禁与限频两档
        cands = [str(x).strip() for k in ("forbidden_terms", "tics")
                 for x in (new.get(k) or []) if str(x).strip()][:16]
        g = self.gate_rules(cands) if cands else {"hard": [], "soft": [], "drop": []}
        for k, add in (("forbidden_terms", g["hard"]), ("tics", g["soft"])):
            old = cur.get(k, [])
            merged = list(dict.fromkeys(old + add))[:40]
            if merged != old:
                changed[k] = [x for x in add if x not in old]
            cur[k] = merged
        for k in ("must_appear", "drop_roles"):
            add = [str(x).strip() for x in (new.get(k) or []) if str(x).strip()][:8]
            old = cur.get(k, [])
            merged = list(dict.fromkeys(old + add))[:40]
            if merged != old:
                changed[k] = [x for x in add if x not in old]
            cur[k] = merged
        if g["drop"]:
            self._log("守门丢弃: " + "；".join(
                f"{x.get('w')}（{x.get('why','')[:20]}）" for x in g["drop"][:5]))
        if changed:
            self.p.write("rules.json", json.dumps(cur, ensure_ascii=False, indent=2))
            self._log("自审新增机器规则: " + json.dumps(changed, ensure_ascii=False))
        return changed

    def learned_rules(self) -> Dict[str, Any]:
        """读取时二次校验 —— 只在写入时把关不够。

        实测: 加了守门器却没重启长跑, 旧进程照样把「蔡德茂」(出场 496 次的核心
        配角) 写进禁用词表, 于是提示词里赫然写着「禁用: 蔡德茂」。
        规则是外部数据, 每次使用前都要验, 不能信任落盘内容。
        """
        cur = self.p._load("rules.json", {})
        if not cur:
            return cur
        # 读取时只做零成本的格式预筛（模型守门在写入时已做过语义判断）,
        # 防止旧版本进程或手改文件塞进畸形条目
        names = {c["name"] for c in self.roster()}
        dirty = False
        for k in ("forbidden_terms", "tics"):
            ok = [w for w in cur.get(k, [])
                  if self._cheap_ok(w) and w not in names]
            if len(ok) != len(cur.get(k, [])):
                dirty = True
            cur[k] = ok
        if dirty:
            self.p.write("rules.json", json.dumps(cur, ensure_ascii=False, indent=2))
        return cur

    def _resolve_foreshadow(self, n: int, text: str) -> int:
        """让模型判断本章兑现了哪些伏笔 —— 靠文本匹配永远对不上。

        之前用 2-gram 重叠匹配, 模型报的措辞和埋设时的措辞几乎不重合,
        回收率卡在 4%（216 埋 9 收）。改成把候选清单编号给模型勾选。
        """
        pend = self.p.mem.pending_foreshadow()
        if not pend:
            return 0
        # 优先问最可能被兑现的: 埋得久的 + 最近的
        cands = list({f["id"]: f for f in
                      [x for x in pend if n - x["planted"] >= 20][:8]
                      + [x for x in pend if n - x["planted"] < 20][-10:]}.values())
        listing = "\n".join(f"{i+1}. （第{f['planted']}章）{f['text'][:50]}"
                             for i, f in enumerate(cands))
        prompt = (
            f"下面是一部小说里**尚未兑现的伏笔清单**，以及刚写完的第 {n} 章正文。\n"
            f"判断本章**明确兑现或解开**了哪几条（真相被揭示、承诺被履行、"
            f"疑点被解释、埋下的人或物起了作用）。\n"
            f"只是提到、只是继续铺垫、只是相关，都**不算**兑现。\n\n"
            f"【伏笔清单】\n{listing}\n\n【第{n}章正文】\n{text[:4500]}\n\n"
            f"只输出被兑现的编号，逗号分隔，如：2,7。一条都没有就输出：无")
        try:
            out = clean(call("polishing", prompt, max_tokens=120).text)
        except Exception:
            return 0
        if "无" in out and not re.search(r"\d", out):
            return 0
        done_n = 0
        for idx in re.findall(r"\d+", out)[:6]:
            i = int(idx) - 1
            if 0 <= i < len(cands):
                self.p.mem.resolve_foreshadow(cands[i]["id"], n)
                done_n += 1
        return done_n

    def _l2(self, a: int, b: int):
        sm = self.p.state.get("summaries", {})
        body = "\n".join(f"第{i}章：{sm.get(str(i),'')}" for i in range(a, b + 1))
        r = call("polishing", f"把下面 {b-a+1} 章的剧情压缩成 500 字以内的连贯摘要，"
                              f"保留关键人物、转折、伏笔。直接输出：\n\n{body}", max_tokens=1000)
        self.p.write(f"l2_summary/{a:03d}-{b:03d}.md", clean(r.text))
        self._log(f"L2 摘要 {a}-{b} 完成")

    def _usage_snapshot(self) -> Dict[str, Any]:
        return {"calls": USAGE["calls"], "prompt": USAGE["prompt"],
                "completion": USAGE["completion"],
                "total": USAGE["prompt"] + USAGE["completion"],
                "elapsed_s": round(USAGE["elapsed"], 1),
                "by_profile": {k: dict(v) for k, v in USAGE["by_profile"].items()}}

    def _check_budget(self) -> None:
        """daily_call_budget 之前是空壳没人读。0 = 不限。"""
        lim = int(self.cfg["limits"].get("daily_call_budget") or 0)
        if lim and USAGE["calls"] >= lim:
            raise RuntimeError(f"已达调用预算上限 {lim} 次（config/settings.yaml → "
                               f"limits.daily_call_budget），本次停止")

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
