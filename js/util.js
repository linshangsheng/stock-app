// 工具函数：DOM 构造、格式化、提示。涨跌数字强制带 +/- 符号，颜色只作辅助（4.4.1 / 4.7）。
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** h('div', {class:'x', onclick:fn}, child, ...) —— 不经 innerHTML，天然避免注入 */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// 约定：条件渲染写成 `cond ? node : null`。原生 append 会把 null / false 渲染成文本「null」，这里统一过滤（并展平数组）。
const _append = Element.prototype.append;
Element.prototype.append = function (...nodes) {
  return _append.apply(this, nodes.flat(Infinity).filter(n => n != null && n !== false));
};

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export const isNum = v => typeof v === 'number' && Number.isFinite(v);

export function fmtNum(v, d = 2) {
  return isNum(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
}
export function fmtPrice(v) { return isNum(v) ? v.toFixed(2) : '—'; }
/** 涨跌幅 / 比例：强制带符号。v 为小数（0.0123 -> +1.23%） */
export function fmtPct(v, d = 2, signed = true) {
  if (!isNum(v)) return '—';
  const s = (v * 100).toFixed(d);
  return (signed && v > 0 ? '+' : '') + s + '%';
}
export function fmtSigned(v, d = 2) {
  if (!isNum(v)) return '—';
  return (v > 0 ? '+' : '') + v.toFixed(d);
}
/** 金额：亿 / 万 */
export function fmtAmount(v) {
  if (!isNum(v)) return '—';
  const a = Math.abs(v);
  if (_market === 'US') {                       // 美元：B / M / K
    if (a >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
    return '$' + (v / 1e3).toFixed(0) + 'K';
  }
  if (a >= 1e8) return (v / 1e8).toFixed(2) + '亿';
  if (a >= 1e4) return (v / 1e4).toFixed(1) + '万';
  return v.toFixed(0);
}
let _market = 'CN';
export function setMarketFmt(m) { _market = m; }
export const currencySymbol = () => (_market === 'US' ? '$' : '¥');
export function fmtMoney(v) { return isNum(v) ? currencySymbol() + v.toLocaleString(_market === 'US' ? 'en-US' : 'zh-CN', { maximumFractionDigits: 0 }) : '—'; }
export const lotName = () => (_market === 'US' ? '股' : '手');
export function fmtVol(v) {
  if (!isNum(v)) return '—';
  return v >= 1e8 ? (v / 1e8).toFixed(2) + '亿股' : v >= 1e4 ? (v / 1e4).toFixed(0) + '万股' : v.toFixed(0);
}
/** 涨跌色 class */
export function dirClass(v) { return !isNum(v) || v === 0 ? 'flat' : v > 0 ? 'up' : 'down'; }
export function code(symbol) { return (symbol || '').split('.').pop(); }

export function debounce(fn, ms = 250) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function toast(msg, kind = '') {
  const root = $('#toast-root');
  const el = h('div', { class: 'toast ' + kind }, msg);
  root.append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 6000 : 3000);
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('已复制到剪贴板', 'ok'); }
  catch {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' }, text);
    document.body.append(ta); ta.select();
    try { document.execCommand('copy'); toast('已复制到剪贴板', 'ok'); } catch { toast('复制失败，请手动选择复制', 'bad'); }
    ta.remove();
  }
}

export function download(filename, text, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h('a', { href: url, download: filename }); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 模态框；返回 {close}。content 为 Node，actions 为 [{label, primary, onclick}] */
export function modal(title, content, actions = []) {
  const root = $('#modal-root');
  const onKey = e => { if (e.key === 'Escape' && root.lastElementChild === ov) { e.stopPropagation(); close(); } };
  function close() { ov.remove(); document.removeEventListener('keydown', onKey, true); window.removeEventListener('hashchange', close); }
  document.addEventListener('keydown', onKey, true);          // Esc 关掉最上面的弹窗
  window.addEventListener('hashchange', close);                // 换页面时弹窗跟着关掉
  const box = h('div', { class: 'modal', role: 'dialog', 'aria-label': title },
    h('h2', {}, title), content,
    h('div', { class: 'actions' }, actions.map(a => h('button', {
      class: 'btn' + (a.primary ? ' primary' : ''), onclick: async () => { if ((await a.onclick?.()) !== false) close(); }
    }, a.label))));
  const ov = h('div', { class: 'overlay', onclick: e => { if (e.target === ov) close(); } }, box);
  root.append(ov);
  ov.querySelector('input,select,textarea')?.focus();
  return { close };
}

export function confirmBox(msg) {
  return new Promise(res => {
    modal('请确认', h('p', {}, msg), [{ label: '取消', onclick: () => res(false) }, { label: '确定', primary: true, onclick: () => res(true) }]);
  });
}

/** 轻量图标（内联 SVG） */
const ICONS = {
  radar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12l6-6"/></svg>',
  star: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 16.9 6.8 19.7l1-5.9L3.5 9.7l5.9-.8z"/></svg>',
  wallet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M3 10h18M16 14.5h2"/></svg>',
  flask: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"><path d="M9 3h6M10 3v6l-5.5 9.5A1.5 1.5 0 005.8 21h12.4a1.5 1.5 0 001.3-2.5L14 9V3"/><path d="M7.5 15h9"/></svg>',
  chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19V5M4 19h16"/><path d="M8 15l3-4 3 2 4-6"/></svg>',
  news: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h8M8 17h5"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/></svg>',
};
export function mountIcons(root = document) {
  $$('i[data-ico]', root).forEach(i => { i.innerHTML = ICONS[i.dataset.ico] || ''; });
}

/** 把 ISO 日期显示为 MM-DD（同年）或 YYYY-MM-DD */
export function shortDate(d) { return d ? d.slice(5) : '—'; }
export const sleep = ms => new Promise(r => setTimeout(r, ms));
