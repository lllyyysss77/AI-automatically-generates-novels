/* 无框架 UI 原语. */
const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
const esc = s => String(s??'').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = n => (n||0).toLocaleString('zh-CN');

function toast(msg, kind='') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, 3200);
}

function modal(html, {onMount} = {}) {
  $('#modal').innerHTML = html;
  $('#modal-mask').classList.add('show');
  onMount && onMount($('#modal'));
}
function closeModal() { $('#modal-mask').classList.remove('show'); }

function scoreBadge(s) {
  if (s === undefined || s === null) return '<span class="badge badge-neutral">–</span>';
  const k = s >= 85 ? 'ok' : s >= 70 ? 'warn' : 'err';
  return `<span class="badge badge-${k}">${s}</span>`;
}

/* 选中文本右键菜单 —— 局部改写能力, 老版的核心交互, 保留并做成配置驱动 */
function bindContextMenu(el, items, onPick) {
  const draw = (m, list, q='') => {
    const show = q ? list.filter(x => x.name.includes(q)) : list;
    let html = '', last = null;
    show.forEach(it => {
      if (it.group && it.group !== last) {
        html += `<div class="ctx-group">${esc(it.group)}</div>`;
        last = it.group;
      }
      html += `<div class="ctx-item" data-n="${esc(it.name)}">${esc(it.name)}</div>`;
    });
    m.querySelector('.ctx-body').innerHTML = html ||
      '<div class="ctx-item" style="color:var(--text-3)">无匹配</div>';
  };
  el.addEventListener('contextmenu', e => {
    const ta = el.tagName === 'TEXTAREA';
    const sel = ta ? el.value.slice(el.selectionStart, el.selectionEnd).trim()
                   : String(window.getSelection()).trim();
    if (!sel) return;
    e.preventDefault();
    const m = $('#ctx-menu');
    m.innerHTML = `<input class="ctx-search" placeholder="筛选 ${items.length} 条指令…">
                   <div class="ctx-body"></div>`;
    draw(m, items);
    m.style.display = 'block';
    m.style.left = Math.min(e.clientX, innerWidth - 240) + 'px';
    m.style.top  = Math.min(e.clientY, innerHeight - 380) + 'px';
    const inp = m.querySelector('.ctx-search');
    inp.oninput = () => draw(m, items, inp.value.trim());
    setTimeout(() => inp.focus(), 30);
    m.onclick = ev => {
      const n = ev.target.dataset.n;
      if (n) {
        m.style.display = 'none';
        onPick(items.find(x => x.name === n), sel);
      }
    };
  });
}
document.addEventListener('click', e => {
  if (!e.target.closest('#ctx-menu')) $('#ctx-menu').style.display = 'none';
});
$('#modal-mask').addEventListener('click', e => { if (e.target.id === 'modal-mask') closeModal(); });
