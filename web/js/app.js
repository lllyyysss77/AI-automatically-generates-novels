/* 路由与启动. */
const VIEWS = { dashboard: Dashboard, settings: SettingsView, packs: PacksView, project: ProjectView };
let curView = 'dashboard';

async function render() {
  const v = VIEWS[curView];
  $('#page-title').textContent = v.title();
  $('#top-actions').innerHTML = v.actions ? v.actions() : '';
  $('#view').innerHTML = '<div class="card-sub">加载中…</div>';
  try {
    $('#view').innerHTML = await v.render();
    v.mount && v.mount();
    if (curView === 'dashboard') { const b=$('#a-new'); if(b) b.onclick = newProjectModal; }
  } catch (e) {
    $('#view').innerHTML = `<div class="card"><div class="empty">
      <div class="empty-ico">⚠</div><div class="empty-title">加载失败</div>
      <div style="font-family:var(--mono);font-size:12px">${esc(e.message)}</div></div></div>`;
  }
}

function go(view) {
  curView = view;
  if (view !== 'project') S.cur = null;
  $$('.nav-item[data-view]').forEach(n => n.classList.toggle('active', n.dataset.view === view));
  $$('.proj-item').forEach(n => n.classList.toggle('active',
    S.cur && n.dataset.slug === S.cur.slug));
  render();
}

async function openProject(slug) {
  S.cur = await API.project(slug);
  S.cur.slug = slug;
  curView = 'project';
  $$('.nav-item[data-view]').forEach(n => n.classList.remove('active'));
  await refreshSidebar();
  await render();
  if (S.cur.job && S.cur.job.running) startPoll();
}

async function refreshSidebar() {
  const list = await API.projects();
  $('#proj-list').innerHTML = list.length ? list.map(p =>
    `<div class="proj-item ${S.cur && S.cur.slug === p.slug ? 'active' : ''}" data-slug="${esc(p.slug)}">
      <div class="proj-title">${esc(p.title)}</div>
      <div class="proj-meta">${p.done || 0}/${p.target_chapters} 章 · ${fmtNum(p.words)} 字</div>
    </div>`).join('') : '<div class="card-sub" style="padding:8px 10px">暂无项目</div>';
  $$('.proj-item').forEach(el => el.onclick = () => openProject(el.dataset.slug));
}

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('theme', t);
}

(async function boot() {
  applyTheme(localStorage.getItem('theme') || 'light');
  $('#btn-theme').onclick = () =>
    applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  $('#btn-new').onclick = newProjectModal;
  $$('.nav-item[data-view]').forEach(n => n.onclick = () => go(n.dataset.view));

  try {
    S.catalog = await API.catalog();
    $('#pack-count').textContent =
      S.catalog.types.length + S.catalog.genres.length + S.catalog.styles.length;
    const h = await API.health();
    if (!h.gateways) toast('没有可用模型网关，请配置 .env', 'err');
  } catch (e) {
    toast('后端连接失败：' + e.message, 'err');
  }
  await refreshSidebar();
  await render();
})();
