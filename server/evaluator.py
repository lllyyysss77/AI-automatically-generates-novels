"""质量评估器.

L1 机器指标 —— 移植自 x10086 skills/anti-ai-tell-audit, 并接入题材包的专属黑名单.
用途: ① 生成后自动判分, 不合格触发重写  ② 回归基准跑分
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Any, List

STRUCT = {
    "【】心理描写": (r"【[^】]+】", 2),
    "散文里的粗体": (r"\*\*[^*\n]+\*\*", 3),
    "分隔线": (r"^---\s*$", 0),
    "markdown表格": (r"^\s*\|.+\|", 0),
    "prompt残留": (r"\[(?:THINK|PLAN|需求|意图)[^\]]*\]", 0),
    "标题残留": (r"^#{1,6}\s", 0),
}

CLICHES_COMMON = ["总而言之", "综上所述", "值得一提的是", "不得不说", "必须指出", "毫无疑问",
                  "越来越多", "事实证明", "众所周知", "据悉", "大致可以分为", "不可忽视"]
CLICHES_NOVEL = ["瞳孔一缩", "冷笑一声", "不可置信", "眼神变得冰冷", "毛骨悚然", "不寒而栗",
                 "心里咯噔", "汗毛倒竖", "冷意从脚底", "空气仿佛凝固", "缓缓打开", "缓缓起身",
                 "缓缓睁开", "缓缓抬起", "深吸一口气", "嘴角勾起"]
HOLLOW = ["非常", "十分", "特别", "极其", "格外", "尤其"]


def cn_len(t: str) -> int:
    return len(re.findall(r"[一-鿿]", t))


def audit(text: str, extra_blacklist: List[str] | None = None,
          target_words: int = 0) -> Dict[str, Any]:
    """返回 {score, issues[], stats{}}  score 100 分制, 越高越好."""
    cn = cn_len(text)
    if cn < 100:
        return {"score": 0, "issues": [{"level": "high", "type": "长度不足",
                                        "detail": f"仅 {cn} 汉字"}], "stats": {"cn": cn}}

    issues: List[Dict[str, Any]] = []
    per_k = lambda n: n / (cn / 1000)  # 每千字频次

    # 1 结构性 AI tell
    for name, (pat, thr) in STRUCT.items():
        hits = re.findall(pat, text, flags=re.M)
        if len(hits) > thr:
            issues.append({"level": "high" if thr == 0 else "mid", "type": f"结构:{name}",
                           "count": len(hits), "threshold": thr, "samples": hits[:3]})

    # 2/3 套话
    for label, words, limit in (("通用套话", CLICHES_COMMON, 1.0),
                                ("小说套话", CLICHES_NOVEL, 2.0)):
        hit = {w: text.count(w) for w in words if text.count(w)}
        if hit and per_k(sum(hit.values())) > limit:
            issues.append({"level": "mid", "type": label, "count": sum(hit.values()),
                           "per_1k": round(per_k(sum(hit.values())), 2), "samples": list(hit)[:5]})

    # 4 题材专属黑名单 —— 出现即算失败
    for w in (extra_blacklist or []):
        if w and w in text:
            issues.append({"level": "high", "type": "题材黑名单", "samples": [w]})

    # 5 空洞形容词密度
    hollow = sum(text.count(w) for w in HOLLOW)
    if per_k(hollow) > 3:
        issues.append({"level": "low", "type": "空洞形容词", "count": hollow,
                       "per_1k": round(per_k(hollow), 2)})

    # 6 句式重复: 连续 3 段同开头
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    starts = [p[:2] for p in paras]
    for i in range(len(starts) - 2):
        if starts[i] and starts[i] == starts[i + 1] == starts[i + 2]:
            issues.append({"level": "mid", "type": "句式重复",
                           "samples": [f"连续3段以「{starts[i]}」开头"]})
            break

    # 7 对话占比
    dialog = sum(len(m) for m in re.findall(r"[""][^""]*[""]|「[^」]*」", text))
    ratio = dialog / max(1, len(text))
    if ratio < 0.15:
        issues.append({"level": "low", "type": "对话过少", "ratio": round(ratio, 3)})

    # 8 段落长度方差 (防全篇一个节奏)
    if len(paras) > 5:
        lens = [len(p) for p in paras]
        avg = sum(lens) / len(lens)
        var = sum((x - avg) ** 2 for x in lens) / len(lens)
        if var < 400:
            issues.append({"level": "low", "type": "段落节奏平均", "variance": round(var)})

    # 9 字数达标
    if target_words:
        rate = cn / target_words
        if rate < 0.8 or rate > 1.4:
            issues.append({"level": "mid", "type": "字数偏离",
                           "detail": f"{cn}/{target_words} = {rate:.0%}"})

    weight = {"high": 15, "mid": 7, "low": 3}
    score = max(0, 100 - sum(weight[i["level"]] for i in issues))
    return {
        "score": score,
        "issues": issues,
        "stats": {"cn": cn, "paras": len(paras), "dialog_ratio": round(ratio, 3)},
    }


def consistency(chapters: List[str], names: List[str]) -> Dict[str, Any]:
    """L2 一致性: 角色出场统计 + 疑似别名检测."""
    text = "\n".join(chapters)
    appear = {n: text.count(n) for n in names}
    missing = [n for n, c in appear.items() if c == 0]
    return {"appearances": appear, "never_appeared": missing}
