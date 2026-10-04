// 主逻辑：路由（hash）、三栏布局、市场切换、涨跌配色 / 主题、键盘导航、离线提示、PWA 注册。
// 前端只做界面与交互；所有数据来自本地 Python 后端（api.js）。
import { get, api, state, onStatus } from './api.js';
import { prefs } from './db.js';
import { $, $$, h, clear, mountIcons, toast, modal, setMarketFmt } from './util.js';
import { destroyDetail, themeChanged } from './detail.js';
import { screener } from './screener.js';
import { watchlist } from './watchlist.js';
import { portfolio } from './portfolio.js';
import { backtestView } from './backtest.js';
import { settingsView } from './settings.js';
import { marketView } from './market.js';
import { newsView } from './news.js';
import { showDetail } from './detail.js';

const VIEWS = { screener, watchlist, portfolio, backtest: backtestView, settings: settingsView, market: marketView, news: newsView };
const appEl = $('#app');

export const ctx = {
  listEl: $('#col-list'), detailEl: $('#col-detail'), pageEl: $('#col-page'),
  watchSet: new Set(), detailPeriod: 'D', currentSymbol: null, refreshList: null, pollJobs: null,
  setMobileDetail(on) { appEl.dataset.mobile = on ? 'detail' : ''; },
  async reloadWatch() {
    try { const d = await get('/watchlist', {}, { cache: false }); ctx.watchSet = new Set(d.items.map(i => i.symbol)); } catch { /* 未初始化 / US */ }
  },
  applyColors, applyTheme,
};

// ---------- 涨跌配色（4.4.1）：随市场自适应，可改全局统一 ----------
function applyColors() {
  const mode = prefs.get('colorMode', 'auto');
  const up = mode === 'red' ? 'red' : mode === 'green' ? 'green' : (state.market === 'CN' ? 'red' : 'green');
  document.documentElement.dataset.up = up;
  themeChanged();
}

function applyTheme() {
  const t = prefs.get('theme', 'auto');
  const dark = t === 'dark' || (t === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.querySelector('meta[name=theme-color]')?.setAttribute('content', dark ? '#0f1115' : '#ffffff');
  themeChanged();
}
matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => { if (prefs.get('theme', 'auto') === 'auto') applyTheme(); });

// 演示数据徽标：按当前市场的数据源显示
let _demo = {};
function refreshDemoBadge() {
  const on = !!_demo[state.market];
  $('#demo-badge').hidden = !on;
  document.title = '股票 · 趋势波段' + (on ? '（演示数据）' : '');
}

// ---------- 路由 ----------
let current = null;
async function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  const name = parts[0] || 'screener';
  const deepSymbol = parts[1] ? decodeURIComponent(parts[1]) : null;      // #/screener/sh.600519：从行情 / 资讯页点进个股详情
  const view = VIEWS[name] || VIEWS.screener;
  current?.cleanup?.();
  ctx.cleanup?.(); ctx.cleanup = null;
  destroyDetail();
  for (const k of ['listEl', 'detailEl', 'pageEl']) clear(ctx[k]);
  ctx.refreshList = null; ctx.currentSymbol = null; ctx.pollJobs = null;
  appEl.dataset.view = name; appEl.dataset.layout = view.layout; ctx.setMobileDetail(false);
  $$('.nav a').forEach(a => a.classList.toggle('on', a.dataset.nav === name));
  if (view.layout === 'split') {
    ctx.detailEl.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '选择一只股票查看详情'), '日 / 周 K 线、均线、触发价与止损线、关键指标。'));
  }
  current = view;
  try { await view.mount(ctx); if (deepSymbol && view.layout === 'split') showDetail(ctx, deepSymbol); } catch (e) { console.error(e); (view.layout === 'page' ? ctx.pageEl : ctx.listEl).append(h('div', { class: 'alert bad', style: 'margin:16px' }, '页面加载失败：' + e.message)); }
}
window.addEventListener('hashchange', route);

// ---------- 市场切换 ----------
$$('.market-switch button').forEach(b => b.addEventListener('click', async () => {
  state.market = b.dataset.market;
  prefs.set('market', state.market);
  setMarketFmt(state.market);
  refreshDemoBadge();
  $$('.market-switch button').forEach(x => x.classList.toggle('on', x === b));
  applyColors();
  await ctx.reloadWatch();
  route();                                   // 自选 / 选股 / 持仓 / 回测整体切换到该市场（2.1 A股/美股分开原则）
}));

// ---------- 键盘：方向键选择、回车看详情、Esc 返回（4.6）----------
document.addEventListener('keydown', e => {
  if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
  if (appEl.dataset.layout !== 'split') return;
  const items = $$('.list-item', ctx.listEl);
  if (e.key === 'Escape') { ctx.setMobileDetail(false); return; }
  if (!items.length || !['ArrowDown', 'ArrowUp', 'Enter'].includes(e.key)) return;
  let i = items.findIndex(x => x.classList.contains('sel'));
  if (e.key === 'Enter') { if (i >= 0) items[i].click(); return; }
  e.preventDefault();
  i = e.key === 'ArrowDown' ? Math.min(items.length - 1, i + 1) : Math.max(0, i - 1);
  items[i].click(); items[i].scrollIntoView({ block: 'nearest' });
});
ctx.listEl.addEventListener('click', e => {                 // 统一维护选中态
  const it = e.target.closest('.list-item');
  if (!it || !it.dataset.symbol) return;
  $$('.list-item', ctx.listEl).forEach(x => x.classList.toggle('sel', x === it));
}, true);

// ---------- 离线 / 数据延迟提示（4.6）----------
const offBar = h('div', { class: 'alert warn', id: 'offline-bar', hidden: true, style: 'position:fixed;left:50%;top:8px;transform:translateX(-50%);z-index:70;box-shadow:var(--shadow)' });
document.body.append(offBar);
onStatus(s => { offBar.hidden = !s.offline; offBar.textContent = s.offline ? `${s.lastError || '离线'}：显示最近一次缓存快照（数据延迟）` : ''; });

window.addEventListener('need-token', () => {
  if (document.getElementById('token-modal')) return;
  const f = { t: '' };
  const m = modal('需要访问口令', h('div', { id: 'token-modal' }, h('p', { class: 'hint' }, '后端设置了访问口令（server.token）。'), h('input', { type: 'password', style: 'width:100%', oninput: e => { f.t = e.target.value; } })),
    [{ label: '确定', primary: true, onclick: () => { prefs.set('token', f.t); location.reload(); } }]);
});

// ---------- 预警（4.6）：页面内提醒，仅在页面打开时生效 ----------
const alerted = new Set(prefs.get('alerted', []));
async function checkAlerts() {
  if (state.market !== 'CN' || state.offline) return;
  try {
    const r = await get('/alerts', {}, { cache: false });
    for (const a of r.items) {
      if (!a.triggered) continue;
      const key = `${a.id}@${r.data_asof}`;
      if (alerted.has(key)) continue;
      alerted.add(key); prefs.set('alerted', [...alerted].slice(-200));
      const [kind, val] = a.rule.split(':');
      toast(`预警：${a.name} ${({ price_above: '收盘价 ≥', price_below: '收盘价 ≤', pct_above: '涨幅 ≥', pct_below: '跌幅 ≤' })[kind]} ${val} 已触发（最新 ${a.quote?.close}）`, 'ok');
    }
  } catch { /* 未初始化 / 无预警 */ }
}
setInterval(checkAlerts, 60000);

// ---------- 启动 ----------
(async function boot() {
  mountIcons();
  const saved = prefs.get('market', 'CN');
  if (saved === 'US' || saved === 'CN') {
    state.market = saved; setMarketFmt(saved);
    $$('.market-switch button').forEach(x => x.classList.toggle('on', x.dataset.market === saved));
  }
  applyTheme(); applyColors();
  $('#theme-btn').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme;
    prefs.set('theme', cur === 'dark' ? 'light' : 'dark'); applyTheme();
  });
  try {
    const p = await api('/ping', { market: false, cache: false });
    _demo = p.demo || {};
    refreshDemoBadge();
  } catch { /* 后端未启动：api.js 已触发离线提示 */ }
  await ctx.reloadWatch();
  checkAlerts();
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
    navigator.serviceWorker.register('sw.js').catch(() => { /* 非 HTTPS 的局域网访问不允许注册，仅影响离线 / 安装 */ });
  }
  route();
})();
