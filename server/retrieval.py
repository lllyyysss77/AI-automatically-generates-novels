"""统一检索层 —— 内部记忆 + 外部搜索合并，写作时自动触发。

设计要点: 搜索不是一个"先去生成考据卡"的独立步骤, 而是 L4 召回层的一半。
写每一章时:
    ① 从本章细纲里识别出"需要事实支撑"的点 (物价 / 官职 / 年份 / 器物 / 度量衡)
    ② 内部先查 (FTS5): 这本书之前是否已经写过、已经查过
    ③ 内部查不到才走外部 (SearXNG), 结果落盘缓存并写回内部记忆
    ④ 两路结果合并成 L4 注入
这样同一个知识点全书只查一次, 之后都从内部记忆命中, 且前后写法自动一致。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .registry import registry

# 需要事实支撑的信号: 出现这些模式说明本章要写具体的、可以写错的东西
# 每项: (正则, 主题). 正则只捕获关键词本身, 不带上下文 —— 否则检索式会被噪声污染。
FACT_TRIGGERS: List[tuple] = [
    (r"(\d+\s*(?:两|贯|文|石|斗|匹|亩))", "度量衡与物价"),
    (r"(知县|知府|通判|提辖|都头|押司|县令|太师|御史|员外郎|转运使)", "官职与品级"),
    (r"(流放|刺配|杖刑|绞刑|斩首|徒刑|讼状|仵作|尸格)", "律法与量刑"),
    (r"(\d{3,4})\s*年", "年份与纪事"),
    (r"(科举|乡试|会试|殿试|童生|秀才|举人|进士)", "科举制度"),
    (r"(火药|造纸|活字印刷|水泥|玻璃|肥皂|蒸馏|织机|曲辕犁|漕运)", "工艺与器物"),
    (r"(厢军|禁军|团练|保甲|马军|步军|弓手)", "军制"),
    (r"(交子|会子|盐引|茶引|度牒|当铺|钱庄)", "货币与金融"),
]


def detect_fact_needs(text: str, era: str, limit: int = 4) -> List[Dict[str, str]]:
    """从章节细纲里识别需要查证的点, 生成检索式。"""
    seen, out = set(), []
    for pat, topic in FACT_TRIGGERS:
        for m in re.findall(pat, text or ""):
            key = topic
            if key in seen:
                continue
            seen.add(key)
            kw = str(m).strip()
            out.append({"topic": topic, "hint": kw,
                        "query": f"{era} {kw} {topic}".strip()})
            if len(out) >= limit:
                return out
    return out


class Retriever:
    """内部 FTS5 + 外部 SearXNG 的统一入口。"""

    STAGE_TOPICS: Dict[str, List[str]]

    def __init__(self, memory, project_dir: Path, era: str = "",
                 enable_web: bool = True, endpoint: Optional[str] = None,
                 summarize: Optional[Callable[[str], str]] = None,
                 topics: Optional[Dict[str, List[str]]] = None):
        self.topics = topics or {}
        self.mem = memory
        self.dir = project_dir
        self.era = era
        self.enable_web = enable_web
        self.summarize = summarize
        self.sx = (registry.searcher(endpoint).bind_cache(project_dir / "research")
                   if enable_web else None)
        self.facts_path = project_dir / "facts.json"
        self.facts: Dict[str, Any] = {}
        if self.facts_path.exists():
            try:
                self.facts = json.loads(self.facts_path.read_text(encoding="utf-8"))
            except Exception:
                self.facts = {}

    # ---------------- 事实卡 ----------------
    def _save(self):
        self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    def fact_for(self, need: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """一个知识点: 先查已建立的事实卡, 没有才联网, 查完写回记忆索引。"""
        topic = need["topic"]
        if topic in self.facts:
            return self.facts[topic]
        if not (self.enable_web and self.sx and self.sx.available()):
            return None

        hits = self.sx.search(need["query"], k=4)
        if not hits:
            self.facts[topic] = {"topic": topic, "card": "", "sources": [],
                                 "built_at": time.strftime("%F %T")}
            self._save()
            return None

        raw = "\n".join(f"- {h['title']}：{h['content'][:300]}" for h in hits)
        card = raw[:800]
        if self.summarize:
            card = self.summarize(
                f"把下面关于「{self.era} {topic}」的检索结果压成 5 条以内的写作硬事实，"
                f"每条一句话带具体数字；互相矛盾的标「存疑」；不确定的不要写。"
                f"直接输出，无前言。\n\n{raw[:4000]}") or card

        rec = {"topic": topic, "card": card,
               "sources": [h["url"] for h in hits[:3]],
               "built_at": time.strftime("%F %T")}
        self.facts[topic] = rec
        self._save()
        # 写回内部记忆 —— 下次同类问题直接内部命中, 不再联网
        self.mem.add("fact", f"fact-{topic}", f"考据·{topic}", card)
        return rec

    # ---------------- 主题落地 ----------------
    # 各创作阶段该查什么 —— 让世界观/人物/剧情都长在真实背景上
    # 各阶段该查什么由题材包 research_topics 决定 —— 都市金融该查股市融资,
    # 电竞该查赛事规则, 写死成历史题材是错的。这里只是兜底。
    STAGE_TOPICS: Dict[str, List[str]] = {
        "world": ["时代背景与社会风貌", "经济与物价", "制度与结构"],
        "cast":  ["称谓与身份", "典型职业"],
        "plot":  ["日常器物", "出行与通讯", "风俗礼仪"],
    }

    def ground(self, stage: str, extra: Optional[List[str]] = None,
               per_topic: int = 3) -> str:
        """给某个创作阶段做背景落地: 批量查该阶段该知道的常识, 压成一块可注入文本。

        结果按主题缓存进 facts.json 与记忆索引, 全书只查一次, 后续阶段直接复用。
        """
        topics = list((self.topics or {}).get(stage)
                      or self.STAGE_TOPICS.get(stage, [])) + list(extra or [])
        blocks: List[str] = []
        for t in topics:
            rec = self.facts.get(t)
            if rec is None:
                rec = self.fact_for({"topic": t, "hint": t,
                                     "query": f"{self.era} {t}"}) or {}
            card = (rec or {}).get("card")
            if card:
                blocks.append(f"【{t}】{card.strip()}")
        return "\n\n".join(blocks)

    # ---------------- 统一召回 ----------------
    def recall(self, chapter_outline: str, k: int = 6) -> Dict[str, Any]:
        """返回 {items, internal, external, needs} —— 供 L4 层直接使用。"""
        internal = self.mem.search(chapter_outline, k=k)
        needs = detect_fact_needs(chapter_outline, self.era)
        external: List[Dict[str, Any]] = []
        for nd in needs:
            rec = self.fact_for(nd)
            if rec and rec.get("card"):
                external.append({"kind": "fact", "title": f"考据·{rec['topic']}",
                                 "text": rec["card"], "sources": rec.get("sources", [])})
        # 内部命中里已有的 fact 条目去重
        seen = {e["title"] for e in external}
        items = external + [h for h in internal if h.get("title") not in seen]
        return {"items": items[:k + len(external)], "internal": len(internal),
                "external": len(external), "needs": [n["topic"] for n in needs]}
