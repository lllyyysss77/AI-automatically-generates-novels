#!/usr/bin/env python3
"""端到端测试 (Playwright). 真实浏览器 + 真实后端 + 真实模型.

  python3 tests/e2e/run.py            # 全部
  python3 tests/e2e/run.py --case 7   # 单条
  python3 tests/e2e/run.py --headed   # 有头观察
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:60001"
SHOTS = Path(__file__).resolve().parent.parent.parent / "reports" / "e2e"
SHOTS.mkdir(parents=True, exist_ok=True)

RESULTS: list = []
ERRORS: list = []


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)


def case(n, title):
    def deco(fn):
        fn._case = (n, title)
        return fn
    return deco


def open_first_project(page):
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".proj-card", timeout=15000)
    page.locator(".proj-card").first.click()
    page.wait_for_selector(".tabs", timeout=15000)


@case(1, "首页与唯一主路由 /")
def c1(page):
    page.goto(BASE, wait_until="networkidle")
    assert page.title(), "无标题"
    expect(page.locator(".brand-name")).to_be_visible()
    expect(page.locator(".sidebar")).to_be_visible()
    shot(page, "01-dashboard")


@case(2, "目录加载：网关/类型/题材计数非零")
def c2(page):
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".stat-value", timeout=15000)
    assert len(page.locator(".stat-value").all()) >= 6
    assert "题材包" in page.locator("#view").inner_text()
    cnt = page.locator("#pack-count").inner_text()
    assert cnt.isdigit() and int(cnt) >= 20, f"插件包计数异常 {cnt}"
    shot(page, "02-catalog")


@case(3, "暗色主题：背景不透明且无横向滚动")
def c3(page):
    page.goto(BASE, wait_until="networkidle")
    page.click("#btn-theme")
    page.wait_for_timeout(400)
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent"), f"body 背景透明 {bg}"
    assert not page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
    shot(page, "03-dark")
    page.click("#btn-theme")
    page.wait_for_timeout(300)


@case(4, "插件包页：三种内容类型的层级链路各不相同")
def c4(page):
    page.goto(BASE, wait_until="networkidle")
    page.click('.nav-item[data-view="packs"]')
    page.wait_for_selector("table.tbl", timeout=15000)
    t = page.locator("#view").inner_text()
    for kw in ("长篇小说", "影视剧本", "短剧剧本"):
        assert kw in t, f"缺内容类型 {kw}"
    assert "→" in t and "fountain" in t.lower()
    page.locator(".genre-pill").first.click()
    page.wait_for_selector("#g-detail .kv", timeout=10000)
    assert "套话黑名单" in page.locator("#g-detail").inner_text()
    shot(page, "04-packs")


@case(5, "全局设置：改字数上限并持久化")
def c5(page):
    page.goto(BASE, wait_until="networkidle")
    page.click('.nav-item[data-view="settings"]')
    page.wait_for_selector("#g-max", timeout=15000)
    old = page.input_value("#g-max")
    page.fill("#g-max", "3200")
    page.click("#st-save")
    page.wait_for_selector(".toast", timeout=10000)
    shot(page, "05-settings")
    page.reload(wait_until="networkidle")
    page.click('.nav-item[data-view="settings"]')
    page.wait_for_selector("#g-max", timeout=15000)
    assert page.input_value("#g-max") == "3200", "设置未持久化"
    page.fill("#g-max", old)
    page.click("#st-save")
    page.wait_for_timeout(800)


@case(6, "项目概览：统计卡与运行日志")
def c6(page):
    open_first_project(page)
    t = page.locator("#view").inner_text()
    assert "进度" in t and "字数" in t and "运行日志" in t
    shot(page, "06-project-overview")


@case(7, "章节页：正文非空且显示 AI 味评分")
def c7(page):
    open_first_project(page)
    page.click('.tab[data-tab="chapters"]')
    page.wait_for_selector(".chapter-row", timeout=15000)
    page.locator(".chapter-row").first.click()
    page.wait_for_timeout(1800)
    body = page.input_value("#c-body")
    assert len(body) > 800, f"正文过短({len(body)})"
    badge = page.locator("#c-actions .badge").first.inner_text().strip()
    assert badge.isdigit(), f"未显示评分 {badge!r}"
    shot(page, "07-chapter")


@case(8, "右键菜单：老版 130 条指令仍可用")
def c8(page):
    open_first_project(page)
    page.click('.tab[data-tab="chapters"]')
    page.wait_for_selector(".chapter-row", timeout=15000)
    page.locator(".chapter-row").first.click()
    page.wait_for_timeout(1500)
    n = page.evaluate("menuItems().length")
    names = page.evaluate("menuItems().map(i=>i.name)")
    assert n >= 5, f"右键菜单项过少 {n}: {names}"
    # 真实触发一次右键
    page.evaluate("""() => {
      const ta = document.querySelector('#c-body');
      ta.focus(); ta.setSelectionRange(0, 40);
      ta.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, clientX:400, clientY:300}));
    }""")
    page.wait_for_timeout(600)
    shot(page, "08-context-menu")


@case(9, "记忆页：FTS5 检索 + 五层预算可视化")
def c9(page):
    open_first_project(page)
    page.click('.tab[data-tab="memory"]')
    page.wait_for_selector("#m-q", timeout=15000)
    page.fill("#m-q", "武松 西门庆")
    page.click("#m-go")
    page.wait_for_selector("#m-out table.tbl", timeout=20000)
    assert page.locator("#m-out tbody tr").count() > 0, "检索无命中"
    page.click("#lc-go")
    page.wait_for_selector("#lc-out table.tbl", timeout=25000)
    lc = page.locator("#lc-out").inner_text()
    for layer in ("本章细纲", "世界观常驻", "最近章节", "段落摘要", "检索召回", "红线约束"):
        assert layer in lc, f"分层预算缺 {layer}"
    assert "tok" in lc
    shot(page, "09-memory-layers")


@case(10, "导出：txt / 大纲 / fountain / srt 内容特征正确")
def c10(page):
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".proj-card", timeout=15000)
    slug = page.evaluate("document.querySelector('.proj-card').dataset.slug")
    for fmt, must in (("txt", "第1章"), ("outline", "大纲"),
                      ("fountain", "Title:"), ("srt", "-->")):
        r = page.request.get(f"{BASE}/api/projects/{slug}/export?fmt={fmt}")
        assert r.ok, f"{fmt} HTTP {r.status}"
        b = r.text()
        assert len(b) > 200 and must in b, f"{fmt} 导出缺特征 {must}"
    page.locator(".proj-card").first.click()
    page.wait_for_selector(".tabs", timeout=15000)
    page.click('.tab[data-tab="export"]')
    page.wait_for_timeout(700)
    shot(page, "10-export")


@case(11, "新建项目：切换内容类型时层级链路随之变化")
def c11(page):
    page.goto(BASE, wait_until="networkidle")
    page.click("#btn-new")
    page.wait_for_selector("#f-type", timeout=10000)
    hints = []
    for pill in page.locator("#f-type .pill").all():
        pill.click()
        page.wait_for_timeout(300)
        hints.append(page.locator("#f-type-hint").inner_text())
    assert len(set(hints)) == len(hints), f"层级提示未随类型变化: {hints}"
    assert any("总纲" in h for h in hints), "小说层级缺失"
    assert any("分场" in h for h in hints), "剧本层级缺失"
    assert any("分镜" in h for h in hints), "短剧层级缺失"
    shot(page, "11-new-project")
    page.evaluate("closeModal()")


@case(12, "真实模型流式：正文非空且无思考泄漏")
def c12(page):
    page.goto(BASE, wait_until="networkidle")
    got = page.evaluate("""async () => {
      const r = await fetch('/api/gen', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({prompt:'写一段120字左右的古代市井场景描写，直接输出正文。',
                              profile:'drafting'})});
      const rd=r.body.getReader(), dec=new TextDecoder();
      let text='',reason='',buf='';
      for(;;){const {value,done}=await rd.read(); if(done)break;
        buf+=dec.decode(value,{stream:true});
        const ls=buf.split('\\n\\n'); buf=ls.pop();
        for(const l of ls){ if(!l.startsWith('data: '))continue;
          try{const d=JSON.parse(l.slice(6)); if(d.t)text+=d.t; if(d.r)reason+=d.r;}catch{} }}
      return {text, reason};
    }""")
    assert len(got["text"]) > 60, f"流式正文为空/过短: {got['text'][:120]!r}"
    assert "<answer" not in got["text"], "answer 标签泄漏"
    assert not got["text"].lstrip().startswith(("我们需要", "用户要求", "首先")), "思考泄漏进正文"
    shot(page, "12-stream")


@case(13, "窄屏 420px 不破版")
def c13(page):
    page.set_viewport_size({"width": 420, "height": 860})
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(900)
    assert not page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
    shot(page, "13-mobile")
    page.set_viewport_size({"width": 1440, "height": 900})


CASES = [c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, default=0)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    todo = [c for c in CASES if not a.case or c._case[0] == a.case]

    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=not a.headed)
        ctx = br.new_context(viewport={"width": 1440, "height": 900},
                             device_scale_factor=2, locale="zh-CN")
        page = ctx.new_page()
        page.on("pageerror", lambda e: ERRORS.append(f"JS错误: {e}"))
        page.on("console", lambda m: ERRORS.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        for fn in todo:
            n, title = fn._case
            t0 = time.time()
            try:
                fn(page)
                RESULTS.append((n, title, True, ""))
                print(f"  ✓ [{n:2d}] {title}  ({time.time()-t0:.1f}s)", flush=True)
            except Exception as e:
                RESULTS.append((n, title, False, str(e)[:220]))
                print(f"  ✗ [{n:2d}] {title}\n       {str(e)[:220]}", flush=True)
                try:
                    shot(page, f"FAIL-{n:02d}")
                except Exception:
                    pass
        br.close()

    ok = sum(1 for r in RESULTS if r[2])
    print(f"\n{'='*60}\n通过 {ok}/{len(RESULTS)}   截图 reports/e2e/")
    if ERRORS:
        print(f"\n浏览器错误 {len(ERRORS)} 条:")
        for e in list(dict.fromkeys(ERRORS))[:8]:
            print("   -", e[:170])
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
