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
  el.addEventListener('contextmenu', e => {
    const sel = String(window.getSelection()).trim();
    if (!sel) return;
    e.preventDefault();
    const m = $('#ctx-menu');
    m.innerHTML = items.map((it,i) =>
      `<div class="ctx-item" data-i="${i}">${esc(it.name)}</div>`).join('');
    m.style.display = 'block';
    m.style.left = Math.min(e.clientX, innerWidth - 180) + 'px';
    m.style.top  = Math.min(e.clientY, innerHeight - 40 - items.length*32) + 'px';
    m.onclick = ev => {
      const i = ev.target.dataset.i;
      if (i !== undefined) { $('#ctx-menu').style.display = 'none'; onPick(items[+i], sel); }
    };
  });
}
document.addEventListener('click', e => {
  if (!e.target.closest('#ctx-menu')) $('#ctx-menu').style.display = 'none';
});
$('#modal-mask').addEventListener('click', e => { if (e.target.id === 'modal-mask') closeModal(); });
