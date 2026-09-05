"""AI 叙事内容生产线 —— Flask 入口.

只有一个页面路由 `/`。老版的 /bingte 已废弃。
所有模型地址与密钥来自 .env，代码与配置里不含任何真实凭据。
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterator

from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.registry import registry, ROOT                       # noqa: E402
from server.settings import load as load_settings, save as save_settings  # noqa: E402
from server.orchestrator import (Project, Novelist, create_project,        # noqa: E402
                                 slugify, call, PROJECTS)
from server.exporters import EXPORTERS, MIME, EXT                 # noqa: E402
from server.evaluator import audit                                # noqa: E402

WEB = ROOT / "web"
app = Flask(__name__, static_folder=None)

# 后台自动写作任务: slug -> 状态
JOBS: Dict[str, Dict[str, Any]] = {}


def sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ----------------------------------------------------------------- 页面
@app.route("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.route("/<path:f>")
def assets(f: str):
    p = WEB / f
    if p.is_file():
        return send_from_directory(WEB, f)
    return send_from_directory(WEB, "index.html")   # SPA 回退


# ----------------------------------------------------------------- 元信息
@app.get("/api/health")
def health():
    return jsonify({"ok": True, "gateways": len(registry.gateways),
                    "types": len(registry.types), "genres": len(registry.genres)})


@app.post("/api/probe")
def probe():
    """接入自检: 打一发真实请求, 报告该网关实际用的字段名与耗时.

    接新模型时最常见的坑是"OpenAI 兼容"但字段名不一样, 表现为前端一片空白。
    先跑这个, 一眼看出它把内容放在 content 还是 reasoning / result / parts。
    """
    b = request.json or {}
    gw = b.get("gateway") or next(iter(registry.gateways), None)
    if gw not in registry.gateways:
        return jsonify({"ok": False, "error": f"未知网关 {gw}"}), 400
    provider = registry.provider(gw)
    provider.seen_fields = set()
    model = b.get("model") or registry.gateways[gw].get("default_model")
    t0 = time.time()
    text, reason, chunks, first = [], [], 0, None
    try:
        for d in provider.stream(
                [{"role": "user", "content": b.get("prompt") or "只回复两个字：收到"}],
                model=model, thinking=bool(b.get("thinking")), max_tokens=64):
            chunks += 1
            if d.text:
                if first is None:
                    first = round(time.time() - t0, 2)
                text.append(d.text)
            if d.reasoning:
                reason.append(d.reasoning)
    except Exception as e:
        return jsonify({"ok": False, "gateway": gw, "model": model,
                        "error": str(e)[:400]}), 200

    body = "".join(text)
    rsn = "".join(reason)
    salvaged = ""
    if not body and rsn:
        salvaged = provider.salvage(rsn)
    return jsonify({
        "ok": bool(body or salvaged),
        "gateway": gw, "model": model,
        "fields_seen": sorted(provider.seen_fields) or ["(未命中任何已知字段)"],
        "declared_reasoning_field": provider.reasoning_field,
        "chunks": chunks,
        "first_token_s": first,
        "elapsed_s": round(time.time() - t0, 2),
        "content_chars": len(body),
        "reasoning_chars": len(rsn),
        "salvaged_from_answer_tag": bool(salvaged),
        "sample": (body or salvaged)[:120],
        "diagnosis": (
            "正常：内容走 content" if body and not rsn else
            "正常：内容走 content，另有思考流（创作类建议关思考）" if body and rsn else
            "内容被包在 <answer> 里，已抢救" if salvaged else
            "该网关未返回可用正文 —— 检查 reasoning_field 声明或换模型"),
    })


@app.get("/api/catalog")
def catalog():
    c = registry.catalog()
    c["typeDetail"] = registry.types
    c["shortcuts"] = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in (ROOT / "packs" / "shortcuts").glob("*.json")
    } if (ROOT / "packs" / "shortcuts").exists() else {}
    return jsonify(c)


@app.get("/api/genre/<gid>")
def genre_detail(gid: str):
    g = registry.genres.get(gid)
    return (jsonify(g), 200) if g else (jsonify({"error": "no such genre"}), 404)


@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    if request.method == "GET":
        return jsonify(load_settings())
    save_settings(request.json or {})
    return jsonify({"ok": True, "settings": load_settings()})


# ----------------------------------------------------------------- 项目
@app.get("/api/projects")
def list_projects():
    out = []
    for d in sorted(PROJECTS.iterdir()) if PROJECTS.exists() else []:
        if not (d / "project.json").exists():
            continue
        p = Project(d.name)
        out.append({"slug": d.name, **p.meta,
                    "done": len(p.state.get("done", [])),
                    "words": p.total_words})
    return jsonify(out)


@app.post("/api/projects")
def new_project():
    b = request.json or {}
    cfg = load_settings()
    avg = (cfg["generation"]["chapter_words_min"] + cfg["generation"]["chapter_words_max"]) // 2
    words = int(b.get("target_words") or 100000)
    chapters = int(b.get("target_chapters") or max(1, round(words / avg)))
    chapters = min(chapters, cfg["limits"]["max_chapters"])
    p = create_project(title=b.get("title", "未命名"), type_id=b.get("type_id", "novel"),
                       genre_id=b.get("genre_id", ""), style_id=b.get("style_id", ""),
                       target_chapters=chapters, target_words=words,
                       fields=b.get("fields") or {})
    return jsonify({"slug": p.slug, **p.meta})


@app.get("/api/projects/<slug>")
def project_detail(slug: str):
    p = Project(slug)
    if not p.meta:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "slug": slug, "meta": p.meta, "state": p.state, "board": p.board(),
        "words": p.total_words,
        "world_bible": p.read("world_bible.md"),
        "characters": p.read("characters.md"),
        "outline": p.read("outline.md"),
        "chapter_outlines": p._load("chapter_outlines.json", {}),
        "memory": p.mem.stats(),
        "job": JOBS.get(slug, {}),
    })


@app.get("/api/projects/<slug>/chapter/<int:n>")
def get_chapter(slug: str, n: int):
    p = Project(slug)
    return jsonify({"n": n, "text": p.chapter(n),
                    "audit": p._load(f"audit/{n:03d}.json", {})})


@app.post("/api/projects/<slug>/chapter/<int:n>")
def save_chapter(slug: str, n: int):
    """保存人工/右键改写后的正文, 并重新评分与重建该章记忆索引."""
    p = Project(slug)
    text = (request.json or {}).get("text", "")
    if not text.strip():
        return jsonify({"error": "内容为空"}), 400
    p.write(p.chapter_path(n), text)
    nv = Novelist(p)
    a = audit(text, nv.blacklist(),
              (p.cfg["generation"]["chapter_words_min"] +
               p.cfg["generation"]["chapter_words_max"]) // 2)
    p.write(f"audit/{n:03d}.json", json.dumps(a, ensure_ascii=False, indent=2))
    if p.cfg["memory"].get("index_chapters", True):
        p.mem.add("plot", f"ch{n}", f"第{n}章",
                  p.state.get("summaries", {}).get(str(n), "") + "\n" + text[:1500])
    return jsonify({"ok": True, "audit": a})


@app.get("/api/projects/<slug>/memory")
def mem_search(slug: str):
    p = Project(slug)
    q = request.args.get("q", "")
    return jsonify({"hits": p.mem.search(q, k=int(request.args.get("k", 6))),
                    "pending_foreshadow": p.mem.pending_foreshadow(),
                    "stats": p.mem.stats()})


@app.get("/api/projects/<slug>/context/<int:n>")
def context_report(slug: str, n: int):
    """返回写第 n 章时五层记忆的实际占用 —— 用户据此调配比."""
    p = Project(slug)
    cached = p._load(f"audit/{n:03d}.ctx.json", None)
    if cached:
        return jsonify(cached)
    co = p._load("chapter_outlines.json", {}).get(str(n), "")
    if not co:
        return jsonify({"error": f"第 {n} 章还没有细纲"}), 404
    return jsonify(Novelist(p).build_context(n, co)["report"])


@app.get("/api/projects/<slug>/export")
def export(slug: str):
    p = Project(slug)
    fmt = request.args.get("fmt", "txt")
    if fmt not in EXPORTERS:
        return jsonify({"error": f"不支持的格式 {fmt}"}), 400
    body = EXPORTERS[fmt](p)
    name = f"{p.meta.get('title', 'novel')}.{EXT[fmt]}"
    # 中文文件名必须按 RFC 5987 百分号编码, 否则 WSGI 写 header 时 latin-1 编码失败
    return Response(body, mimetype=MIME[fmt] + "; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=\"export.%s\"; filename*=UTF-8''%s"
                               % (EXT[fmt], quote(name, safe=""))})


# ----------------------------------------------------------------- 生成
@app.post("/api/gen")
def gen():
    """通用单次生成 (右键菜单 / 自由提问). 流式返回 text 与 reasoning 分离."""
    b = request.json or {}
    prompt = b.get("prompt", "")
    profile = b.get("profile", "drafting")

    def stream() -> Iterator[str]:
        try:
            provider, kw = registry.resolve(profile)
            if b.get("model"):
                kw["model"] = b["model"]
            got = False
            for d in provider.stream([{"role": "user", "content": prompt}], **kw):
                if d.text:
                    got = True
                    yield sse({"t": d.text})
                if d.reasoning:
                    yield sse({"r": d.reasoning})
            if not got:
                yield sse({"t": "（模型未返回正文，已记录）"})
            yield sse({"done": True})
        except Exception as e:
            yield sse({"error": str(e)})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/projects/<slug>/step")
def step(slug: str):
    """执行流水线单步, SSE 实时吐字."""
    p = Project(slug)
    if not p.meta:
        return jsonify({"error": "not found"}), 404
    b = request.json or {}
    what = b.get("step")
    n = int(b.get("n") or 0)

    def stream() -> Iterator[str]:
        q: list = []
        try:
            nv = Novelist(p)
            emit = lambda t: q.append(t)
            if what == "world_bible":
                nv.step_world_bible(emit)
            elif what == "characters":
                nv.step_characters(emit)
            elif what == "outline":
                nv.step_outline(emit)
            elif what == "chapter_outlines":
                nv.step_chapter_outlines(n or 1, int(b.get("count") or
                                                     p.cfg["generation"]["outline_batch"]), emit)
            elif what == "chapter":
                nv.step_chapter(n, emit)
            else:
                yield sse({"error": f"未知步骤 {what}"}); return
            for t in q:
                yield sse({"t": t})
            yield sse({"done": True, "board": p.board()})
        except Exception as e:
            traceback.print_exc()
            yield sse({"error": str(e)})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _auto_worker(slug: str, upto: int):
    p = Project(slug)
    nv = Novelist(p)
    job = JOBS[slug]
    try:
        if not p.read("world_bible.md"):
            job["stage"] = "世界观"; nv.step_world_bible()
        if not p.read("characters.md"):
            job["stage"] = "角色档案"; nv.step_characters()
        if not p.read("outline.md"):
            job["stage"] = "总纲"; nv.step_outline()
        batch = p.cfg["generation"]["outline_batch"]
        n = (p.state.get("current") or 0) + 1
        end = min(upto, p.meta["target_chapters"])
        while n <= end and not job.get("stop"):
            job["stage"] = f"第 {n} 章"
            if str(n) not in p._load("chapter_outlines.json", {}):
                nv.step_chapter_outlines(n, batch)
            r = nv.step_chapter(n)
            job.update({"last": r, "done": len(p.state["done"]), "words": p.total_words})
            n += 1
        job["stage"] = "已停止" if job.get("stop") else "完成"
    except Exception as e:
        job["stage"] = "出错"; job["error"] = str(e)
        traceback.print_exc()
    finally:
        job["running"] = False


@app.post("/api/projects/<slug>/auto")
def auto(slug: str):
    """启动/停止后台自动写作."""
    b = request.json or {}
    if b.get("stop"):
        JOBS.setdefault(slug, {})["stop"] = True
        return jsonify({"ok": True, "stopping": True})
    if JOBS.get(slug, {}).get("running"):
        return jsonify({"ok": False, "msg": "已在运行"}), 409
    upto = int(b.get("upto") or 5)
    JOBS[slug] = {"running": True, "stop": False, "stage": "启动中", "upto": upto}
    threading.Thread(target=_auto_worker, args=(slug, upto), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/projects/<slug>/job")
def job_status(slug: str):
    p = Project(slug)
    j = dict(JOBS.get(slug, {}))
    j.update({"done": len(p.state.get("done", [])), "words": p.total_words,
              "target_words": p.meta.get("target_words", 0),
              "target_chapters": p.meta.get("target_chapters", 0),
              "log": p.state.get("log", [])[-15:]})
    return jsonify(j)


@app.post("/api/audit")
def audit_api():
    b = request.json or {}
    return jsonify(audit(b.get("text", ""), b.get("blacklist"), int(b.get("target_words") or 0)))


if __name__ == "__main__":
    port = int(os.environ.get("NOVEL_PORT", 60001))
    print(f"→ http://127.0.0.1:{port}/   网关 {len(registry.gateways)} 个 / "
          f"类型 {len(registry.types)} 种 / 题材 {len(registry.genres)} 个")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
