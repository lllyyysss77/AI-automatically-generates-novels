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
# 精确词只能抓字面, 实测 30 章里"瞳孔一缩"只中 2 次, 而"瞳孔骤缩""瞳孔剧烈收缩"
# 合计 9 次全部绕过。所以套话检测必须用词根正则, 不能只做子串匹配。
CLICHE_PATTERNS = [
    (r"瞳孔(?:[一猛骤]?[缩紧])|瞳孔[^。，！？]{0,4}(?:收缩|骤缩|一缩)", "瞳孔X缩"),
    (r"(?:冷笑|嗤笑|嘲讽地笑)(?:了)?一声", "冷笑一声"),
    (r"嘴角(?:微微)?(?:勾起|上扬|扯出|噙着|挑起)", "嘴角勾起"),
    (r"缓缓(?:打开|起身|睁开|抬起|转过|站起|放下|开口|点头)", "缓缓X"),
    (r"深(?:深)?吸(?:了)?一口(?:凉)?气", "深吸一口气"),
    (r"心(?:里|中|头)(?:猛地)?咯噔", "心里咯噔"),
    (r"(?:不敢|难以)置信", "不可置信"),
    (r"眼神(?:变得)?(?:冰冷|冷冽|骤冷)|眼神(?:陡然|骤然)[^。，]{0,3}冷", "眼神变冷"),
    (r"毛骨悚然|不寒而栗|汗毛倒竖|冷意(?:从|自)脚底", "惊悚套话"),
    (r"空气(?:仿佛|似乎|像是)?(?:瞬间)?(?:凝固|凝滞)", "空气凝固"),
    (r"脸色(?:瞬间)?(?:煞白|惨白|铁青|阴沉)", "脸色X白"),
    (r"(?:冷汗|汗珠)(?:涔涔|直流|密布|渗出)", "冷汗直流"),
    (r"[一-鿿]{0,3}(?:暗道|心中暗[骂想忖]|暗自思忖)", "暗道"),
    (r"平静得(?:像是)?(?:一潭)?死水|(?:像|如)一潭死水", "一潭死水"),
]
CLICHES_NOVEL = ["不可置信", "毛骨悚然", "不寒而栗", "汗毛倒竖", "空气仿佛凝固"]
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

    # 2 通用套话 (字面匹配足够)
    hit = {w: text.count(w) for w in CLICHES_COMMON if text.count(w)}
    if hit and per_k(sum(hit.values())) > 1.0:
        issues.append({"level": "mid", "type": "通用套话", "count": sum(hit.values()),
                       "per_1k": round(per_k(sum(hit.values())), 2), "samples": list(hit)[:5]})

    # 3 小说套话 —— 词根正则, 抓变体
    pat_hits: Dict[str, int] = {}
    for pat, name in CLICHE_PATTERNS:
        c = len(re.findall(pat, text))
        if c:
            pat_hits[name] = c
    if pat_hits and per_k(sum(pat_hits.values())) > 1.5:
        issues.append({"level": "mid", "type": "小说套话", "count": sum(pat_hits.values()),
                       "per_1k": round(per_k(sum(pat_hits.values())), 2),
                       "samples": [f"{k}×{v}" for k, v in
                                   sorted(pat_hits.items(), key=lambda x: -x[1])[:5]]})

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


# ------------------------------------------------------------------ 全书体检
def book_audit(chapters: Dict[int, str], *, characters: List[str] | None = None,
               forbidden_terms: List[str] | None = None,
               protagonist: str = "") -> Dict[str, Any]:
    """跨章体检 —— 单章合格不等于全书合格。

    实测教训: 逐章 AI 味均分 95.2, 但"嘴角勾起"在 30 章里出现 40 次。
    单章 1-2 次不触发阈值, 连起来看就是灾难。以下维度只有全书视角能发现。
    """
    if not chapters:
        return {"error": "没有章节"}
    order = sorted(chapters)
    allt = "\n".join(chapters[n] for n in order)
    total_cn = len(re.findall(r"[一-鿿]", allt))
    per_10k = lambda n: round(n / max(1, total_cn / 10000), 1)
    issues: List[Dict[str, Any]] = []

    # 1 套话的全书频次
    tics = []
    for pat, name in CLICHE_PATTERNS:
        c = len(re.findall(pat, allt))
        if c >= 8:
            tics.append({"tic": name, "count": c, "per_10k": per_10k(c)})
    tics.sort(key=lambda x: -x["count"])
    if tics:
        issues.append({"level": "high" if tics[0]["count"] >= 25 else "mid",
                       "type": "口癖泛滥", "detail": tics[:8]})

    # 2 世界观术语冲突 (架空却用真朝代名之类)
    forb = {}
    for t in (forbidden_terms or []):
        c = allt.count(t)
        if c:
            forb[t] = c
    if forb:
        issues.append({"level": "high", "type": "禁用术语出现",
                       "detail": dict(sorted(forb.items(), key=lambda x: -x[1]))})

    # 3 角色出场分布 —— 主角垄断 / 配角纸片人
    dist = []
    for name in (characters or []):
        chs = [n for n in order if name in chapters[n]]
        dist.append({"name": name, "count": allt.count(name),
                     "chapters": len(chs), "coverage": round(len(chs) / len(order), 2),
                     "first": chs[0] if chs else None})
    dist.sort(key=lambda x: -x["count"])
    if dist:
        top = dist[0]
        rest = sum(d["count"] for d in dist[1:]) or 1
        ratio = top["count"] / rest
        if ratio > 1.2:
            issues.append({"level": "mid", "type": "主角戏份垄断",
                           "detail": f"{top['name']} {top['count']} 次 vs 其余全部 {rest} 次 "
                                     f"(1:{rest/top['count']:.2f})"})
        ghosts = [d["name"] for d in dist if d["chapters"] and d["coverage"] < 0.15]
        if ghosts:
            issues.append({"level": "mid", "type": "配角纸片化",
                           "detail": f"出场率低于 15% 的角色: {'、'.join(ghosts[:10])}"})
        never = [d["name"] for d in dist if not d["chapters"]]
        if never:
            issues.append({"level": "low", "type": "角色档案未落地",
                           "detail": f"档案里有但正文从未出现: {'、'.join(never[:10])}"})

    # 4 地名/主场漂移
    places = Counter(re.findall(r"([一-鿿]{2}(?:县|州|府|镇|村))", allt))
    main = places.most_common(3)
    if len(main) >= 2 and main[1][1] > main[0][1] * 0.4:
        issues.append({"level": "mid", "type": "主场地点漂移",
                       "detail": f"{main[0][0]} {main[0][1]} 次 vs {main[1][0]} {main[1][1]} 次，"
                                 f"两地都像主场，读者会混乱"})

    # 5 开篇句式重复
    heads = Counter((chapters[n].strip().split("\n")[0][:6]) for n in order)
    dup = [f"{k}×{v}" for k, v in heads.most_common(5) if v > 1]
    if dup:
        issues.append({"level": "low", "type": "章节开篇雷同", "detail": dup})

    # 6 章节字数波动
    lens = [len(re.findall(r"[一-鿿]", chapters[n])) for n in order]
    avg = sum(lens) / len(lens)
    outliers = [(order[i], l) for i, l in enumerate(lens) if l < avg * 0.6 or l > avg * 1.6]
    if outliers:
        issues.append({"level": "low", "type": "章节长度失衡",
                       "detail": f"均值 {avg:.0f} 字，偏离过大: " +
                                 "、".join(f"第{n}章{l}字" for n, l in outliers[:6])})

    weight = {"high": 18, "mid": 9, "low": 4}
    score = max(0, 100 - sum(weight[i["level"]] for i in issues))
    return {"score": score, "chapters": len(order), "total_words": total_cn,
            "issues": issues, "tics": tics[:12], "cast": dist[:20]}


# ------------------------------------------------------------------ 窗口体检
def _shingles(text: str, n: int = 5) -> set:
    """字符 n-gram 集合, 用来算相邻章节的重复度."""
    t = re.sub(r"\s+", "", text)
    return {t[i:i + n] for i in range(0, max(0, len(t) - n), 2)}


def window_audit(chapters: Dict[int, str], center: int, span: int = 3,
                 outlines: Dict[str, str] | None = None) -> Dict[str, Any]:
    """滑动窗口体检 —— 拿本章和前后几章贴在一起看。

    单章体检看不出的问题:
      * 和上一章开场方式雷同
      * 同一个桥段/比喻反复用
      * 剧情原地踏步 (三章都在同一场冲突里绕)
      * 配角突然消失或突然冒出来
      * 口癖在窗口内堆积 (每章 2 次不超标, 连着 4 章就是 8 次)
    """
    order = sorted(chapters)
    if center not in chapters:
        return {"error": f"第 {center} 章不存在"}
    lo = [n for n in order if center - span <= n < center]
    win = lo + [center]
    cur = chapters[center]
    issues: List[Dict[str, Any]] = []

    # 1 与前几章的文本重复度
    cs = _shingles(cur)
    dups = []
    for n in lo:
        ps = _shingles(chapters[n])
        if not ps or not cs:
            continue
        j = len(cs & ps) / len(cs | ps)
        dups.append({"vs": n, "overlap": round(j, 3)})
        if j > 0.16:
            issues.append({"level": "high" if j > 0.24 else "mid", "type": "与邻章重复度过高",
                           "detail": f"与第 {n} 章 {j:.1%} 重合，疑似换汤不换药"})

    # 2 开场雷同
    heads = {n: re.sub(r"\s+", "", chapters[n])[:24] for n in win}
    ch = heads[center]
    for n in lo:
        same = len(set(ch[:12]) & set(heads[n][:12])) / 12
        if heads[n][:8] == ch[:8] or same > 0.75:
            issues.append({"level": "mid", "type": "开场与邻章雷同",
                           "detail": f"第 {center} 章开场与第 {n} 章高度相似"})
            break

    # 3 口癖在窗口内堆积
    wt = "\n".join(chapters[n] for n in win)
    tics = []
    for pat, name in CLICHE_PATTERNS:
        c = len(re.findall(pat, wt))
        if c >= 4:
            tics.append({"tic": name, "count": c, "span": len(win)})
    tics.sort(key=lambda x: -x["count"])
    if tics:
        issues.append({"level": "mid", "type": "窗口内口癖堆积",
                       "detail": [f"{t['tic']}×{t['count']}（近{t['span']}章）" for t in tics[:6]]})

    # 4 剧情原地踏步: 窗口内高频实体几乎不变
    def keyset(t):
        return set(w for w, c in Counter(re.findall(r"[一-鿿]{2,4}", t)).most_common(40))
    if lo:
        prev_keys = set().union(*(keyset(chapters[n]) for n in lo))
        cur_keys = keyset(cur)
        fresh = cur_keys - prev_keys
        if len(fresh) / max(1, len(cur_keys)) < 0.25:
            issues.append({"level": "mid", "type": "剧情疑似原地踏步",
                           "detail": f"本章新元素仅占 {len(fresh)/max(1,len(cur_keys)):.0%}，"
                                     f"与前 {len(lo)} 章高度同质"})

    # 5 角色出场连续性
    if outlines:
        cast_cur = set(re.findall(r"[一-鿿]{2,4}", outlines.get(str(center), "")))
        vanished = []
        for n in lo:
            for name in re.findall(r"[一-鿿]{2,4}", outlines.get(str(n), "")):
                if name in chapters[n] and name not in cur and len(name) >= 2:
                    vanished.append(name)
        vanished = [v for v, c in Counter(vanished).items() if c >= len(lo)]
        if vanished:
            issues.append({"level": "low", "type": "角色断线",
                           "detail": f"连续出现于前 {len(lo)} 章、本章突然消失: "
                                     + "、".join(vanished[:6])})

    weight = {"high": 16, "mid": 8, "low": 3}
    return {"center": center, "window": win, "score": max(0, 100 - sum(
        weight[i["level"]] for i in issues)), "issues": issues, "overlaps": dups}
