/* 视图层. 每个 view 返回 HTML 字符串, mount 后绑事件. */
const S = { catalog:null, settings:null, projects:[], cur:null, tab:'overview',
            curChapter:null, pollTimer:null };

/* ─────────────────────────── 工作台 ─────────────────────────── */
const Dashboard = {
  title: () => '工作台',
  actions: () => `<button class="btn btn-primary" id="a-new">＋ 新建项目</button>`,
  async render() {
    S.projects = await API.projects();
    const words = S.projects.reduce((a,p)=>a+(p.words||0),0);
    const chaps = S.projects.reduce((a,p)=>a+(p.done||0),0);
    const c = S.catalog;
    const stats = `<div class="grid grid-3" style="margin-bottom:18px">
      ${stat('项目', S.projects.length, '本')}
      ${stat('已生成章节', chaps, '章')}
      ${stat('累计字数', fmtNum(words), '字')}
      ${stat('可用模型网关', c.gateways.length, '个')}
      ${stat('内容类型', c.types.length, '种')}
      ${stat('题材包', c.genres.length, '个')}
    </div>`;
    if (!S.projects.length) return stats + `<div class="card"><div class="empty">
        <div class="empty-ico">◇</div><div class="empty-title">还没有项目</div>
        <div>新建一个项目，选好内容类型与题材，就能开始自动创作</div>
        <button class="btn btn-primary" id="a-new2" style="margin-top:16px">＋ 新建项目</button>
      </div></div>`;
    return stats + `<div class="grid grid-2">` + S.projects.map(p => {
      const pct = p.target_words ? Math.min(100, p.words/p.target_words*100) : 0;
      const ty = (c.types.find(t=>t.id===p.type_id)||{}).name || p.type_id;
      const ge = (c.genres.find(g=>g.id===p.genre_id)||{}).name || p.genre_id;
      return `<div class="card proj-card" data-slug="${esc(p.slug)}" style="cursor:pointer">
        <div class="card-head"><div class="card-title">${esc(p.title)}</div>
          <div class="card-actions"><span class="badge badge-accent">${esc(ty)}</span></div></div>
        <div class="card-sub">${esc(ge)} · ${esc(p.style_id||'')}</div>
        <div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div>
        <div class="card-sub" style="margin-top:7px;font-family:var(--mono)">
          ${p.done||0}/${p.target_chapters} 章 · ${fmtNum(p.words)}/${fmtNum(p.target_words)} 字
          · ${pct.toFixed(1)}%</div></div>`;
    }).join('') + `</div>`;
  },
  mount() {
    $$('.proj-card').forEach(el => el.onclick = () => openProject(el.dataset.slug));
    ['#a-new','#a-new2'].forEach(s => { const b=$(s); if(b) b.onclick = newProjectModal; });
  }
};
const stat = (label, v, unit='') => `<div class="stat"><div class="stat-label">${label}</div>
  <div class="stat-value">${v}</div><div class="stat-sub">${unit}</div></div>`;

/* ─────────────────────────── 新建项目 ─────────────────────────── */
function newProjectModal() {
  const c = S.catalog;
  modal(`<h2>新建项目</h2>
    <div class="modal-sub">内容类型决定层级结构与导出格式；题材包提供专业写作规范</div>
    <div class="field"><label>作品名</label>
      <input class="input" id="f-title" placeholder="例如：重生之我成了西门庆"></div>
    <div class="field"><label>内容类型</label><div class="pill-group" id="f-type">
      ${c.types.map((t,i)=>`<div class="pill ${i?'':'active'}" data-v="${t.id}">${esc(t.name)}</div>`).join('')}
    </div><div class="hint" id="f-type-hint"></div></div>
    <div class="row">
      <div class="field"><label>题材包</label><select class="select" id="f-genre">
        ${c.genres.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('')}</select></div>
      <div class="field"><label>平台文风</label><select class="select" id="f-style">
        <option value="">（不指定）</option>
        ${c.styles.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('')}</select></div>
    </div>
    <div class="row">
      <div class="field"><label>目标字数</label>
        <input class="input" id="f-words" type="number" value="100000" step="10000"></div>
      <div class="field"><label>章节数（留空自动算）</label>
        <input class="input" id="f-chapters" type="number" placeholder="自动"></div>
    </div>
    <div class="field"><label>一句话故事</label>
      <textarea class="ta" id="f-premise" style="min-height:60px"></textarea></div>
    <div class="field"><label>背景设定</label><textarea class="ta" id="f-bg"></textarea></div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" id="f-ok">创建</button></div>`,
  { onMount(m) {
      const hint = () => { const id = $('.pill.active', $('#f-type')).dataset.v;
        const t = S.catalog.typeDetail[id];
        $('#f-type-hint').textContent = t ? `层级：${t.levels.map(l=>l.name).join(' → ')} ｜ 导出：${(t.exporters||[]).join(' / ')}` : ''; };
      $$('.pill', $('#f-type')).forEach(p => p.onclick = () => {
        $$('.pill', $('#f-type')).forEach(x=>x.classList.remove('active'));
        p.classList.add('active'); hint(); });
      hint();
      $('#f-ok').onclick = async () => {
        const title = $('#f-title').value.trim();
        if (!title) return toast('请填写作品名', 'err');
        $('#f-ok').disabled = true;
        try {
          const p = await API.createProject({
            title, type_id: $('.pill.active', $('#f-type')).dataset.v,
            genre_id: $('#f-genre').value, style_id: $('#f-style').value,
            target_words: +$('#f-words').value || 100000,
            target_chapters: +$('#f-chapters').value || 0,
            fields: { premise: $('#f-premise').value, background: $('#f-bg').value }
          });
          closeModal(); toast('项目已创建', 'ok'); await openProject(p.slug);
        } catch(e) { toast('创建失败：'+e.message, 'err'); $('#f-ok').disabled = false; }
      };
  }});
}

/* ─────────────────────────── 项目详情 ─────────────────────────── */
const TABS = [['overview','概览'],['setup','设定'],['outline','大纲'],
              ['chapters','章节'],['quality','质检'],['memory','记忆'],
              ['teardown','拆书'],['export','导出']];

const ProjectView = {
  title: () => S.cur ? S.cur.meta.title : '项目',
  actions() {
    const running = S.cur && S.cur.job && S.cur.job.running;
    return `<span class="badge badge-neutral" id="model-badge">${esc((S.cur&&S.cur.meta.model)||'')}</span>
      <button class="btn ${running?'btn-danger':'btn-primary'}" id="a-auto">
        ${running?'■ 停止':'▶ 自动创作'}</button>`;
  },
  async render() {
    const p = S.cur; if (!p) return '';
    const tabs = `<div class="tabs">${TABS.map(([k,n])=>
      `<div class="tab ${S.tab===k?'active':''}" data-tab="${k}">${n}</div>`).join('')}</div>`;
    return tabs + `<div id="tab-body">${TabRender[S.tab](p)}</div>`;
  },
  mount() {
    $$('.tab').forEach(t => t.onclick = () => { S.tab = t.dataset.tab; render(); });
    const a = $('#a-auto'); if (a) a.onclick = toggleAuto;
    TabMount[S.tab] && TabMount[S.tab]();
  }
};

const TabRender = {
  overview(p) {
    const pct = p.meta.target_words ? Math.min(100, p.words/p.meta.target_words*100) : 0;
    const j = p.job || {};
    const scores = Object.keys(p.state.summaries||{}).length;
    return `<div class="grid grid-3" style="margin-bottom:16px">
        ${stat('进度', `${(p.state.done||[]).length}/${p.meta.target_chapters}`, '章')}
        ${stat('字数', fmtNum(p.words), `目标 ${fmtNum(p.meta.target_words)}`)}
        ${stat('完成度', pct.toFixed(1)+'%', '')}
        ${stat('记忆条目', Object.entries(p.memory||{}).filter(([k])=>!k.startsWith('fore')).reduce((a,[,v])=>a+v,0), '条')}
        ${stat('未回收伏笔', (p.memory||{}).foreshadow_total - ((p.memory||{}).foreshadow_resolved||0) || 0, '个')}
        ${stat('状态', j.running ? '生成中' : '空闲', esc(j.stage||''))}
      </div>
      <div class="card"><div class="card-head"><div class="card-title">运行日志</div>
        <div class="card-actions"><button class="btn btn-sm btn-ghost" id="a-refresh">刷新</button></div></div>
        <div class="mono-log" id="log">${esc((p.state.log||[]).slice(-20).join('\n'))||'（暂无）'}</div></div>
      <div class="card"><div class="card-head"><div class="card-title">项目看板</div></div>
        <div class="mono-log" style="max-height:320px">${esc(p.board)}</div></div>`;
  },
  setup(p) {
    const f = p.meta.fields || {};
    return `<div class="card"><div class="card-head"><div class="card-title">基础设定</div>
        <div class="card-actions">
          <button class="btn btn-sm" id="s-wb">生成世界观</button>
          <button class="btn btn-sm" id="s-ch">生成角色档案</button></div></div>
        <div class="field"><label>一句话故事</label>
          <textarea class="ta" id="e-premise" style="min-height:56px">${esc(f.premise||'')}</textarea></div>
        <div class="field"><label>背景设定</label>
          <textarea class="ta" id="e-bg">${esc(f.background||'')}</textarea></div></div>
      <div class="card"><div class="card-head"><div class="card-title">世界观圣经</div>
        <div class="card-sub">${(p.world_bible||'').length} 字</div></div>
        <div id="wb-out" class="prose" style="max-height:400px;overflow-y:auto">${esc(p.world_bible)||'<span style="color:var(--text-3)">尚未生成</span>'}</div></div>
      <div class="card"><div class="card-head"><div class="card-title">角色档案</div>
        <div class="card-sub">${(p.characters||'').length} 字</div></div>
        <div id="ch-out" class="prose" style="max-height:400px;overflow-y:auto">${esc(p.characters)||'<span style="color:var(--text-3)">尚未生成</span>'}</div></div>`;
  },
  outline(p) {
    const co = p.chapter_outlines || {};
    const keys = Object.keys(co).sort((a,b)=>a-b);
    return `<div class="card"><div class="card-head"><div class="card-title">总纲</div>
        <div class="card-actions"><button class="btn btn-sm" id="o-gen">生成总纲</button></div></div>
        <div id="ol-out" class="prose" style="max-height:420px;overflow-y:auto">${esc(p.outline)||'<span style="color:var(--text-3)">尚未生成</span>'}</div></div>
      <div class="card"><div class="card-head"><div class="card-title">分章细纲</div>
        <div class="card-sub">${keys.length} 章</div>
        <div class="card-actions"><button class="btn btn-sm" id="o-batch">续生成 10 章</button></div></div>
        <div class="scroll-y">${keys.length ? keys.map(k=>
          `<div style="padding:9px 0;border-bottom:1px solid var(--border)">
            <div class="prose" style="font-size:13.5px">${esc(co[k])}</div></div>`).join('')
          : '<div class="empty">尚未生成细纲</div>'}</div></div>`;
  },
  chapters(p) {
    const done = (p.state.done||[]).slice().sort((a,b)=>a-b);
    const co = p.chapter_outlines || {};
    const nameOf = n => { const m = /第\s*\d+\s*章\s*(.+)/.exec(co[n]||''); return m ? m[1].split('\n')[0].slice(0,24) : ''; };
    return `<div class="split">
      <div class="card"><div class="card-head"><div class="card-title">章节</div>
        <div class="card-sub">${done.length} 章</div></div>
        <div class="scroll-y">${done.length ? done.map(n=>
          `<div class="chapter-row ${S.curChapter===n?'active':''}" data-n="${n}">
            <span class="chapter-num">${String(n).padStart(3,'0')}</span>
            <span class="chapter-name">${esc(nameOf(n))||'—'}</span></div>`).join('')
          : '<div class="empty" style="padding:30px 10px"><div>还没有正文</div></div>'}</div></div>
      <div class="card"><div class="card-head"><div class="card-title" id="c-title">正文</div>
        <div class="card-actions" id="c-actions"></div></div>
        <div id="c-think"></div>
        <textarea class="ta prose" id="c-body" style="min-height:460px;border:1px solid var(--border);
          padding:14px" placeholder="从左侧选择章节，或点右上角「自动创作」。选中文字后右键可局部改写。"></textarea>
        <div class="card-sub" style="margin-top:8px">选中文字 → 右键 → 扩写 / 润色 / 去 AI 味 / 加冲突…（菜单来自内容类型包 + 题材包）</div>
        </div></div>`;
  },
  teardown(p) {
    return `<div class="card"><div class="card-head"><div class="card-title">拆书</div>
        <div class="card-sub">把一本现成的小说拆成可复用素材，并直接写进本项目</div>
        <div class="card-actions"><button class="btn btn-sm btn-primary" id="td-go">开始拆解</button></div></div>
        <div class="field"><label>粘贴整本或部分正文（≥500 字）</label>
          <textarea class="ta" id="td-text" style="min-height:150px"
            placeholder="支持「第N章」「Chapter N」「N、标题」等章节标记；识别不出时按 3000 字自动分段"></textarea></div>
        <div class="row" style="align-items:flex-end">
          <div class="field" style="margin:0"><label>抽样章数（章多时避免整本烧钱）</label>
            <input class="input" id="td-n" type="number" value="8"></div>
          <div class="field" style="margin:0"><label>写入本项目</label>
            <select class="select" id="td-apply"><option value="1">是（世界观/角色/爽点/文风入库）</option>
              <option value="0">否（只看结果）</option></select></div></div>
        <div class="mono-log" id="td-out" style="margin-top:14px;max-height:420px">等待拆解…</div></div>`;
  },
  quality(p) {
    return `<div class="card"><div class="card-head"><div class="card-title">三层质检</div>
        <div class="card-sub">单章合格 ≠ 全书合格：逐章 95 分的稿子，全书体检可能只有 20 分</div>
        <div class="card-actions">
          <button class="btn btn-sm btn-primary" id="q-book">全书体检</button>
          <input class="input" id="q-n" type="number" style="width:78px"
                 value="${(p.state.done||[]).slice(-1)[0]||1}">
          <button class="btn btn-sm" id="q-win">邻章窗口</button></div></div>
        <div id="q-out"><div class="card-sub">点右上角开始体检</div></div></div>
      <div class="card"><div class="card-head"><div class="card-title">自审守则</div>
        <div class="card-sub">每 ${(S.settings&&S.settings.quality&&S.settings.quality.reflect_every)||5} 章自读自批一次，结论注入后续每一章</div>
        <div class="card-actions"><button class="btn btn-sm" id="q-reflect">立即自审</button></div></div>
        <div class="mono-log" id="q-guide" style="max-height:280px">加载中…</div></div>
      <div class="card"><div class="card-head"><div class="card-title">设定治理</div>
        <div class="card-sub">世界观被污染时不必推倒重来</div>
        <div class="card-actions"><button class="btn btn-sm" id="q-repair">设定返修</button></div></div>
        <div id="q-anchor" class="mono-log">加载中…</div></div>`;
  },
  memory(p) {
    const m = p.memory || {};
    return `<div class="card"><div class="card-head"><div class="card-title">多记忆索引</div>
        <div class="card-sub">SQLite FTS5 · 世界观 / 角色 / 往期剧情 / 伏笔</div></div>
        <div class="grid grid-3" style="margin-bottom:14px">
          ${stat('世界观', m.world||0,'条')}${stat('角色', m.role||0,'条')}
          ${stat('剧情', m.plot||0,'条')}${stat('伏笔', m.foreshadow_total||0,'个')}
          ${stat('已回收', m.foreshadow_resolved||0,'个')}</div>
        <div class="row" style="align-items:flex-end">
          <div class="field" style="margin:0"><label>检索（写到 300 章也能找回第 30 章埋的线）</label>
            <input class="input" id="m-q" placeholder="例如：武松 玉佩 药铺"></div>
          <button class="btn btn-primary" id="m-go" style="flex:0 0 auto;margin-bottom:0">检索</button></div>
        <div id="m-out" style="margin-top:14px"></div></div>
      <div class="card"><div class="card-head"><div class="card-title">分层记忆预算</div>
        <div class="card-sub">写某一章时，五层各占多少上下文 —— 配比在「全局设置」里可调</div>
        <div class="card-actions">
          <input class="input" id="lc-n" type="number" style="width:82px" value="${(p.state.done||[]).slice(-1)[0]||1}">
          <button class="btn btn-sm" id="lc-go">查看</button></div></div>
        <div id="lc-out"><div class="card-sub">输入章号后点「查看」</div></div></div>`;
  },
  export(p) {
    const t = S.catalog.typeDetail[p.meta.type_id] || {};
    const all = [['txt','纯文本 TXT'],['md','Markdown'],['outline','仅大纲'],
                 ['docx','Word DOCX'],['epub','电子书 EPUB'],
                 ['fountain','剧本 Fountain'],['srt','字幕 SRT']];
    return `<div class="card"><div class="card-head"><div class="card-title">导出</div>
      <div class="card-sub">类型「${esc(t.name||'')}」声明的格式：${(t.exporters||[]).join(' / ')}</div></div>
      <div class="grid grid-3">${all.map(([k,n])=>
        `<a class="btn" href="${API.exportUrl(p.slug,k)}" download style="justify-content:center">⬇ ${n}</a>`).join('')}</div>
      <div class="card-sub" style="margin-top:14px">
        一份设定可导出多种形态：小说正文、剧本、字幕、纯大纲。这是「一稿多态」的落点。</div></div>`;
  }
};

const TabMount = {
  overview() { const r=$('#a-refresh'); if(r) r.onclick = () => openProject(S.cur.slug); },
  setup() {
    $('#s-wb').onclick = () => runStep('world_bible', '#wb-out', '生成世界观');
    $('#s-ch').onclick = () => runStep('characters', '#ch-out', '生成角色档案');
  },
  outline() {
    $('#o-gen').onclick   = () => runStep('outline', '#ol-out', '生成总纲');
    $('#o-batch').onclick = () => {
      const done = Object.keys(S.cur.chapter_outlines||{}).length;
      runStep('chapter_outlines', null, '生成细纲', {n: done+1, count: 10});
    };
  },
  chapters() {
    $$('.chapter-row').forEach(r => r.onclick = async () => {
      S.curChapter = +r.dataset.n;
      const d = await API.chapter(S.cur.slug, S.curChapter);
      $('#c-title').textContent = `第 ${S.curChapter} 章`;
      $('#c-actions').innerHTML = `<span class="card-sub">${d.audit?.stats?.cn||0} 字</span>
        ${scoreBadge(d.audit?.score)}
        ${d.audit?.rewritten?'<span class="badge badge-warn">已重写</span>':''}
        <button class="btn btn-sm btn-primary" id="c-save">保存</button>`;
      $('#c-save').onclick = saveChapter;
      $('#c-body').value = d.text || '';
      $$('.chapter-row').forEach(x=>x.classList.toggle('active', +x.dataset.n===S.curChapter));
      bindMenus();
    });
    bindMenus();
  },
  teardown() {
    $('#td-go').onclick = async () => {
      const text = $('#td-text').value;
      if (text.length < 500) return toast('文本太短（至少 500 字）', 'err');
      $('#td-out').textContent = '';
      $('#td-go').disabled = true;
      await API.stream('/api/teardown', {
        text, sample: +$('#td-n').value || 8,
        slug: $('#td-apply').value === '1' ? S.cur.slug : null
      }, {
        onText: t => { $('#td-out').textContent += t; $('#td-out').scrollTop = 1e9; },
        onError: e => toast('拆书失败：' + e.message, 'err'),
        onDone: () => { $('#td-go').disabled = false; toast('拆书完成', 'ok'); }
      });
    };
  },
  quality() {
    const slug = encodeURIComponent(S.cur.slug);
    const render = (r, title) => {
      if (r.error) return `<div class="card-sub">${esc(r.error)}</div>`;
      const k = r.score >= 80 ? 'ok' : r.score >= 55 ? 'warn' : 'err';
      return `<div style="margin-bottom:12px"><b>${title}</b>
        <span class="badge badge-${k}" style="margin-left:8px">${r.score} / 100</span>
        ${r.total_words?`<span class="card-sub"> · ${fmtNum(r.total_words)} 字 / ${r.chapters} 章</span>`:''}</div>
        ${(r.issues||[]).length ? `<table class="tbl"><thead><tr><th style="width:70px">级别</th>
          <th style="width:150px">问题</th><th>详情</th></tr></thead><tbody>` +
          r.issues.map(i=>{
            const lv={high:'err',mid:'warn',low:'neutral'}[i.level]||'neutral';
            let d=i.detail; if (typeof d!=='string') d=JSON.stringify(d,null,0);
            return `<tr><td><span class="badge badge-${lv}">${i.level}</span></td>
              <td><b>${esc(i.type)}</b></td>
              <td style="color:var(--text-2);font-size:12.5px">${esc(String(d||'').slice(0,320))}</td></tr>`;
          }).join('') + `</tbody></table>`
          : '<div class="card-sub">未发现问题</div>'}`;
    };
    $('#q-book').onclick = async () => {
      $('#q-out').innerHTML = '<div class="card-sub">体检中…</div>';
      try { $('#q-out').innerHTML = render(await API.get(`/api/projects/${slug}/bookaudit`), '全书体检'); }
      catch(e){ $('#q-out').innerHTML = `<div class="card-sub">${esc(e.message)}</div>`; }
    };
    $('#q-win').onclick = async () => {
      const n = +$('#q-n').value;
      $('#q-out').innerHTML = '<div class="card-sub">体检中…</div>';
      try {
        const r = await API.get(`/api/projects/${slug}/window/${n}`);
        $('#q-out').innerHTML = render(r, `第 ${n} 章 · 邻章窗口 ${JSON.stringify(r.window||[])}`);
      } catch(e){ $('#q-out').innerHTML = `<div class="card-sub">${esc(e.message)}</div>`; }
    };
    $('#q-reflect').onclick = () => runStep('reflect', '#q-guide', '自审');
    $('#q-repair').onclick = () => runStep('repair', null, '设定返修');
    (async () => {
      try {
        const d = await API.project(S.cur.slug);
        $('#q-guide').textContent = d.style_guide || '（还没有自审守则，写满几章后自动生成）';
      } catch { $('#q-guide').textContent = '(读取失败)'; }
      try {
        const a = await API.get(`/api/projects/${slug}/anchor`);
        $('#q-anchor').textContent =
          `历史模式：${a.anchor.mode}\n国号：${a.anchor.dynasty||'（不适用）'}\n` +
          `主场：${a.anchor.main_place||'-'}\n禁用术语：${(a.anchor.forbidden||[]).slice(0,10).join('、')||'无'}\n` +
          `角色花名册（${a.roster.length}）：${a.roster.join('、')}\n\n` +
          `下一章将收到的质检反馈：\n${a.tic_guard||'（暂无）'}`;
      } catch(e){ $('#q-anchor').textContent = '(读取失败) ' + e.message; }
    })();
  },
  memory() {
    const go = async () => {
      const q = $('#m-q').value.trim(); if (!q) return;
      $('#m-out').innerHTML = '<div class="card-sub">检索中…</div>';
      const r = await API.memory(S.cur.slug, q);
      $('#m-out').innerHTML = r.hits.length ? `<table class="tbl"><thead><tr>
          <th>类型</th><th>标题</th><th>内容</th></tr></thead><tbody>` +
        r.hits.map(h=>`<tr><td><span class="badge badge-neutral">${esc(h.kind)}</span></td>
          <td>${esc(h.title)}</td><td style="color:var(--text-2)">${esc(h.text.slice(0,150))}…</td></tr>`).join('')
        + `</tbody></table>` : '<div class="card-sub">没有命中</div>';
    };
    $('#m-go').onclick = go;
    $('#m-q').onkeydown = e => { if (e.key==='Enter') go(); };

    $('#lc-go').onclick = async () => {
      const n = +$('#lc-n').value;
      $('#lc-out').innerHTML = '<div class="card-sub">计算中…</div>';
      try {
        const r = await API.get(`/api/projects/${encodeURIComponent(S.cur.slug)}/context/${n}`);
        if (r.error) { $('#lc-out').innerHTML = `<div class="card-sub">${esc(r.error)}</div>`; return; }
        $('#lc-out').innerHTML = `
          <div class="card-sub" style="margin-bottom:10px">
            预算 <b>${fmtNum(r.total_budget)}</b> tok ｜ 实用 <b>${fmtNum(r.used)}</b> tok
            (${r.usage_pct}%) ${r.overflow.length?`｜ <span class="badge badge-warn">溢出：${r.overflow.join('、')}</span>`:''}
          </div>
          <table class="tbl"><thead><tr><th>层</th><th>用量 / 配额</th><th style="width:44%">占比</th><th>状态</th></tr></thead><tbody>
          ${r.layers.map(l=>{
            const w = r.total_budget ? l.tokens/r.total_budget*100 : 0;
            return `<tr><td><b>${esc(l.label)}</b><div class="card-sub" style="font-family:var(--mono)">${esc(l.key)}</div></td>
              <td style="font-family:var(--mono)">${fmtNum(l.tokens)} / ${fmtNum(l.cap)}</td>
              <td><div class="progress" style="margin:0"><div class="progress-bar" style="width:${w}%"></div></div></td>
              <td>${l.truncated?'<span class="badge badge-warn">已裁剪</span>':'<span class="badge badge-ok">完整</span>'}</td></tr>`;
          }).join('')}</tbody></table>`;
      } catch(e) { $('#lc-out').innerHTML = `<div class="card-sub">${esc(e.message)}</div>`; }
    };
  },
  export() {}
};

async function saveChapter() {
  const n = S.curChapter, text = $('#c-body').value;
  if (!n) return;
  try {
    const r = await API.post(`/api/projects/${encodeURIComponent(S.cur.slug)}/chapter/${n}`, {text});
    toast(`已保存，AI 味评分 ${r.audit.score}`, 'ok');
    const b = $('#c-actions .badge'); if (b) b.outerHTML = scoreBadge(r.audit.score);
  } catch(e) { toast('保存失败：'+e.message, 'err'); }
}

/* 右键菜单来源三处合并, 全部配置驱动, 代码里不写死任何一条:
     ① 内容类型包 packs/type/*.json 的 menus
     ② 题材对应的老版菜单 packs/shortcuts/legacy-genre-menus.json (v5.2 的 130 条资产)
     ③ 通用兜底项
   改写结果可「替换选中」直接写回正文 —— 这是老版的核心交互, 必须保留. */
function menuItems() {
  const t = S.catalog.typeDetail[S.cur.meta.type_id] || {};
  const lvl = (t.levels||[]).slice(-1)[0] || {};
  let items = ((t.menus||{})[lvl.id] || []).slice();
  const legacy = (S.catalog.shortcuts||{})['legacy-genre-menus'];
  const gname = (S.catalog.genres.find(g=>g.id===S.cur.meta.genre_id)||{}).name;
  if (legacy && legacy.menus) {
    const pool = legacy.menus[gname] || [];
    const seen = new Set(items.map(i=>i.name));
    pool.forEach(i => { if (!seen.has(i.name)) { items.push(i); seen.add(i.name); } });
  }
  return items;
}

function bindMenus() {
  const body = $('#c-body'); if (!body) return;
  const items = menuItems();
  if (!items.length) return;
  bindContextMenu(body, items, async (item, sel) => {
    const f = S.cur.meta.fields || {};
    const prompt = item.prompt
      .replace(/\$\{selected_text\}/g, sel)
      .replace(/\$\{background\}/g, f.background||'')
      .replace(/\$\{characters\}/g, S.cur.characters||'')
      .replace(/\$\{relationships\}/g, f.relationships||'')
      .replace(/\$\{plot\}/g, f.premise||'')
      .replace(/\$\{cliche_blacklist\}/g, '');
    modal(`<h2>${esc(item.name)}</h2>
      <div class="modal-sub">原文 ${sel.length} 字 · 生成完成后可直接替换回正文</div>
      <div class="prose" id="rw-out" style="max-height:46vh;overflow-y:auto;
        background:var(--surface);padding:12px;border-radius:8px"></div>
      <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button>
        <button class="btn btn-primary" id="rw-apply" disabled>替换选中并保存</button></div>`);
    let out = '';
    API.stream('/api/gen', {prompt, profile:'polishing'}, {
      onText: t => { out += t; $('#rw-out').textContent = out; },
      onError: e => toast('生成失败：'+e.message, 'err'),
      onDone: () => {
        if (!out.trim()) return;
        const btn = $('#rw-apply'); if (!btn) return;
        btn.disabled = false;
        btn.onclick = async () => {
          const ta = $('#c-body');
          ta.value = ta.value.replace(sel, out.trim());
          closeModal(); await saveChapter();
        };
      }
    });
  });
}

async function runStep(step, outSel, label, extra={}) {
  toast(label + '…');
  if (outSel) $(outSel).textContent = '';
  await API.stream(`/api/projects/${encodeURIComponent(S.cur.slug)}/step`,
    {step, ...extra}, {
      onText: t => { if (outSel) $(outSel).textContent += t; },
      onError: e => toast('失败：' + e.message, 'err'),
      onDone: async () => { toast(label + ' 完成', 'ok'); await openProject(S.cur.slug); }
    });
}

async function toggleAuto() {
  const running = S.cur.job && S.cur.job.running;
  if (running) { await API.auto(S.cur.slug, {stop:true}); toast('已请求停止'); return; }
  const left = S.cur.meta.target_chapters - (S.cur.state.done||[]).length;
  modal(`<h2>自动创作</h2><div class="modal-sub">
      世界观 → 角色 → 总纲 → 分章细纲 → 逐章正文 → AI 味自审 → 不合格自动重写</div>
    <div class="field"><label>本次写到第几章（剩余 ${left} 章）</label>
      <input class="input" id="au-n" type="number" value="${Math.min((S.cur.state.done||[]).length+5, S.cur.meta.target_chapters)}"></div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" id="au-go">开始</button></div>`, { onMount() {
      $('#au-go').onclick = async () => {
        await API.auto(S.cur.slug, {upto: +$('#au-n').value});
        closeModal(); toast('已启动后台创作', 'ok'); startPoll();
      };
  }});
}

function startPoll() {
  clearInterval(S.pollTimer);
  S.pollTimer = setInterval(async () => {
    if (!S.cur) return clearInterval(S.pollTimer);
    const j = await API.job(S.cur.slug);
    const log = $('#log'); if (log) log.textContent = (j.log||[]).join('\n');
    if (!j.running) { clearInterval(S.pollTimer); await openProject(S.cur.slug); toast('创作完成', 'ok'); }
  }, 4000);
}

/* ─────────────────────────── 全局设置 ─────────────────────────── */
const SettingsView = {
  title: () => '全局设置',
  actions: () => `<button class="btn btn-primary" id="st-save">保存</button>`,
  async render() {
    const s = S.settings = await API.settings();
    const g = s.generation, l = s.limits, q = s.quality, m = s.memory, sd = s.style_defaults;
    const num = (id,label,v,hint='') => `<div class="field"><label>${label}</label>
      <input class="input" id="${id}" type="number" value="${v}" step="any">
      ${hint?`<div class="hint">${hint}</div>`:''}</div>`;
    return `<div class="card"><div class="card-head"><div class="card-title">字数与篇幅</div>
        <div class="card-sub">对所有项目生效，单个项目可覆盖</div></div>
        <div class="row">${num('g-min','单章字数下限',g.chapter_words_min)}
          ${num('g-max','单章字数上限',g.chapter_words_max)}</div>
        <div class="row">${num('l-ch','单本章节上限',l.max_chapters)}
          ${num('l-w','单本总字数上限',l.max_total_words)}</div></div>
      <div class="card"><div class="card-head"><div class="card-title">生成参数</div></div>
        <div class="row">${num('g-batch','每批细纲章数',g.outline_batch)}
          ${num('g-ctx','上下文预算 (token)',g.context_budget,'留足输出空间，勿超模型窗口')}</div>
        <div class="row">${num('g-td','正文温度',g.temperature_draft)}
          ${num('g-tp','规划温度',g.temperature_plan)}</div></div>
      <div class="card"><div class="card-head"><div class="card-title">质量闸</div></div>
        <div class="row">${num('q-pass','AI 味合格线 (0-100)',q.audit_pass_score,'低于此分自动重写')}
          ${num('q-rw','每章最多重写次数',q.max_rewrites)}</div></div>
      <div class="card"><div class="card-head"><div class="card-title">记忆索引</div></div>
        <div class="row">${num('m-k','每次召回条数',m.top_k)}
          ${num('m-rec','带入最近章节摘要数',m.recent_chapters)}
          ${num('m-l2','每 N 章压缩一次摘要',m.l2_every)}</div></div>
      <div class="card"><div class="card-head"><div class="card-title">统一写作偏好</div>
        <div class="card-sub">会拼进每一次生成的提示词</div></div>
        <div class="row">
          <div class="field"><label>叙事视角</label><input class="input" id="s-nar" value="${esc(sd.narration||'')}"></div>
          <div class="field"><label>时态</label><input class="input" id="s-tense" value="${esc(sd.tense||'')}"></div></div>
        <div class="field"><label>自定义追加要求</label>
          <textarea class="ta" id="s-extra" placeholder="例如：多用短句，少用比喻，对话要有性格差异">${esc(sd.extra||'')}</textarea></div>
        <div class="field"><label>全局禁用词（每行一个）</label>
          <textarea class="ta" id="s-ban">${esc((s.banned_global||[]).join('\n'))}</textarea></div></div>`;
  },
  mount() {
    $('#st-save').onclick = async () => {
      const s = JSON.parse(JSON.stringify(S.settings));
      const v = id => +$(id).value;
      Object.assign(s.generation, {chapter_words_min:v('#g-min'), chapter_words_max:v('#g-max'),
        outline_batch:v('#g-batch'), context_budget:v('#g-ctx'),
        temperature_draft:v('#g-td'), temperature_plan:v('#g-tp')});
      Object.assign(s.limits, {max_chapters:v('#l-ch'), max_total_words:v('#l-w')});
      Object.assign(s.quality, {audit_pass_score:v('#q-pass'), max_rewrites:v('#q-rw')});
      Object.assign(s.memory, {top_k:v('#m-k'), recent_chapters:v('#m-rec'), l2_every:v('#m-l2')});
      Object.assign(s.style_defaults, {narration:$('#s-nar').value, tense:$('#s-tense').value,
        extra:$('#s-extra').value});
      s.banned_global = $('#s-ban').value.split('\n').map(x=>x.trim()).filter(Boolean);
      await API.saveSettings(s); toast('设置已保存，对所有项目生效', 'ok');
    };
  }
};

/* ─────────────────────────── 插件包 ─────────────────────────── */
const PacksView = {
  title: () => '插件包',
  actions: () => '',
  async render() {
    const c = S.catalog;
    const sec = (title, sub, rows) => `<div class="card"><div class="card-head">
      <div class="card-title">${title}</div><div class="card-sub">${sub}</div></div>${rows}</div>`;
    return sec('内容类型', `${c.types.length} 种 · 决定层级结构与导出格式`,
        `<table class="tbl"><thead><tr><th>类型</th><th>层级链路</th><th>导出</th></tr></thead><tbody>` +
        c.types.map(t=>`<tr><td><b>${esc(t.name)}</b></td>
          <td style="color:var(--text-2)">${t.levels.map(esc).join(' → ')}</td>
          <td>${(t.exporters||[]).join(' / ')}</td></tr>`).join('') + `</tbody></table>`)
      + sec('题材包', `${c.genres.length} 个 · 力量体系 / 节奏表 / 套话黑名单`,
        `<div class="pill-group">${c.genres.map(g=>
          `<div class="pill genre-pill" data-id="${g.id}">${esc(g.name)}</div>`).join('')}</div>
         <div id="g-detail" style="margin-top:14px"></div>`)
      + sec('平台文风', `${c.styles.length} 个`,
        `<div class="pill-group">${c.styles.map(g=>`<div class="pill">${esc(g.name)}</div>`).join('')}</div>`)
      + sec('模型网关', `${c.gateways.length} 个 · 地址与密钥来自 .env，不进仓库`,
        `<table class="tbl"><thead><tr><th>网关</th><th>默认模型</th><th>上下文</th><th>接入自检</th></tr></thead><tbody>` +
        c.gateways.map(g=>`<tr><td>${esc(g.label)}</td><td><code>${esc(g.model||'')}</code></td>
          <td>${fmtNum(g.context_window)}</td>
          <td><button class="btn btn-sm probe-btn" data-gw="${esc(g.id)}">自检</button>
            <span class="probe-out" data-gw="${esc(g.id)}"></span></td></tr>`).join('')
        + `</tbody></table>
        <div class="card-sub" style="margin-top:10px">各厂商「OpenAI 兼容」的字段名并不统一
        （content / reasoning_content / reasoning / result / parts…）。自检会打一发真实请求，
        报告该网关实际用的字段与首字延迟 —— 接新模型出现空白时先跑这个。</div>`);
  },
  mount() {
    $$('.probe-btn').forEach(b => b.onclick = async () => {
      const gw = b.dataset.gw;
      const out = $(`.probe-out[data-gw="${gw}"]`);
      b.disabled = true; out.innerHTML = ' <span class="card-sub">检测中…</span>';
      try {
        const r = await API.post('/api/probe', {gateway: gw});
        out.innerHTML = ` <span class="badge badge-${r.ok?'ok':'err'}">${r.ok?'通':'异常'}</span>
          <span class="card-sub" style="font-family:var(--mono)">
          字段 ${esc((r.fields_seen||[]).join('/'))} · 首字 ${r.first_token_s ?? '-'}s</span>
          <div class="card-sub">${esc(r.diagnosis || r.error || '')}</div>`;
      } catch(e) { out.innerHTML = ` <span class="badge badge-err">失败</span>`; }
      b.disabled = false;
    });
    $$('.genre-pill').forEach(p => p.onclick = async () => {
      $$('.genre-pill').forEach(x=>x.classList.remove('active')); p.classList.add('active');
      const g = await API.get('/api/genre/'+p.dataset.id);
      $('#g-detail').innerHTML = `<div class="kv">
        <dt>核心爽点</dt><dd>${esc((g.corePleasure||[]).slice(0,4).join(' / '))||'—'}</dd>
        <dt>套话黑名单</dt><dd>${esc((g.clicheBlacklist||[]).join('、'))||'—'}</dd>
        <dt>常见坑</dt><dd>${esc((g.pitfalls||[]).slice(0,4).join(' / '))||'—'}</dd>
        <dt>对标</dt><dd>${esc(g.benchmarks||'—')}</dd></div>`;
    });
  }
};
