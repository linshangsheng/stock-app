// 港股（独立的第三个市场）。已有：三个指数（恒生指数、国企指数、恒生科技）的估值与分批建仓（选股页）、价格走势与均线位置（行情页）。
// 个股行情库、K 线、个股观察清单（选股页最上面）已经有；自选股、持仓、回测还在做：这些页面先显示「还在建设中」，不报错、不留空白。
import { get } from './api.js';
import { h, clear, fmtPct, fmtNum, dirClass, debounce } from './util.js';
import { valuationBlock, valuationActs } from './marketpanel.js';
import { lineChart, themeColors, StockChart, onThemeChange } from './chart.js';

const NAMES = { kline: 'K线', watchlist: '自选股', portfolio: '持仓', market: '行情' };
const fmtHKD = v => (v == null ? '—' : 'HK$' + (v >= 1e8 ? (v / 1e8).toFixed(1) + ' 亿' : v >= 1e4 ? (v / 1e4).toFixed(0) + ' 万' : v.toFixed(0)));
const TAG_CLASS = { 强势: 'ok', 偏强: 'ok', 震荡: '', 偏弱: 'warn', 弱势: 'bad', 数据不足: '' };
const fmtIdx = v => (v == null ? '—' : v.toLocaleString('zh-CN', { maximumFractionDigits: 0 }));

/** 「站上 / 跌破哪几条均线」的小标签：20 / 60 / 120 / 200（绿色 = 站上，灰色 = 跌破）。 */
function maChips(i) {
  return h('span', { class: 'row', style: 'gap:4px;display:inline-flex' }, (i.mas || []).filter(m => m.n <= 200).map(m =>
    h('span', { class: 'badge ' + (m.above ? 'ok' : ''), title: `${m.name}（${m.n} 日）${fmtNum(m.value, 0)}，现价${m.above ? '在上方' : '在下方'} ${fmtPct(Math.abs(m.dist), 1, false)}` }, m.n)));
}

/** 把 /api/market/valuation（港股）整理成 valuationBlock 要的形状。 */
function asMarketView(v) {
  return { status: 'ok', indices: v.items.map(i => ({ symbol: i.symbol, name: i.name, etf: i.etf, valuation: i })),
    valuation: { refreshed_at: v.refreshed_at, stale: v.stale, rule: v.rule, note: v.note, bond: v.bond } };
}

const TODO = [
  ['已经有', '三个指数的估值与分批建仓建议、价格走势与均线位置（行情页）、港股个股行情库（成交额前 800 只的 10 年日线，设置 → 数据更新里初始化）和 K 线页（搜索代码 / 名称 / 拼音）、个股观察清单（选股页最上面）、设置里的港股自动更新开关与数据状态。'],
  ['下一步', '自选股，然后是持仓与止损止盈、回测。港股的参数（T+0、无涨跌停、每手股数因股票而异、双边印花税）要单独校准，不会照搬 A 股 / 美股。'],
  ['还没有', '自选股、持仓与止损止盈、回测（所以观察清单现在没有历史证据，见清单上方的提示）。后面分几步做。'],
];

function roadmap() {
  return h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '港股现在做到哪了')),
    h('div', { class: 'small', style: 'line-height:1.8' }, TODO.map(([k, v]) => h('div', {}, h('b', {}, k + '：'), v))),
    h('p', { class: 'hint mt-s' }, '「A股｜港股｜美股」切换按钮（电脑在左上角，手机在顶部）可以随时切换。'));
}

/** 选股页里的「指数走势一览」：每个指数一行（收盘、今日、站上哪几条均线），点一行去「行情」页看图。 */
function trendCard(o) {
  const rows = o.indices.map(i => h('tr', { style: 'cursor:pointer', title: '点这里看走势图', onclick: () => { location.hash = '#/market'; } },
    h('td', {}, h('b', {}, i.name), h('div', { class: 'tiny faint' }, (i.etf || '').split(' ')[0])),
    h('td', { class: 'num r' }, fmtIdx(i.close)), h('td', { class: 'num r ' + dirClass(i.chg_1d) }, fmtPct(i.chg_1d, 2)),
    h('td', { class: 'num r ' + dirClass(i.ret_20d) }, fmtPct(i.ret_20d, 1)),
    h('td', { style: 'white-space:nowrap' }, h('span', { class: 'badge ' + (TAG_CLASS[i.trend] || ''), title: i.trend_text }, i.trend), ' ', maChips(i))));
  return h('div', { class: 'card mt-s' }, h('div', { class: 'row between' }, h('b', {}, '指数走势一览'), h('a', { href: '#/market', class: 'small' }, '走势图与均线 →')),
    h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, h('th', {}, '指数'), h('th', { class: 'r' }, '收盘'), h('th', { class: 'r' }, '今日'), h('th', { class: 'r' }, '20 日'),
      h('th', { title: '站上哪几条均线（亮 = 站上）：20 / 60 / 120 / 200 日' }, '均线'))), h('tbody', {}, rows))),
    h('div', { class: 'tiny faint mt-s' }, `截至 ${o.asof}。均线位置只是描述现状，不是买卖信号（A 股 / 美股的均线结论没有在港股上验证过）。`));
}

/** 港股在选股页（和 A 股同一个页面，js/screener.js）里用的「市场概览」：指数走势一览 + 指数估值与分批建仓。loadHkOverview 第一次要联网取，轮询到取完（最多约 30 秒）。 */
export async function loadHkOverview(alive = () => true) {
  let v = null, o = null;
  for (let k = 0; k < 10 && alive(); k++) {
    [v, o] = await Promise.all([get('/market/valuation', { market: 'HK' }, { cache: false, timeout: 60000 }),
      get('/hk/overview', { market: 'HK' }, { cache: false, timeout: 60000 }).catch(() => null)]);
    if (v.status !== 'loading' && o?.status !== 'loading') break;
    await new Promise(r => setTimeout(r, 3000));
  }
  return { v, o };
}

/** 「已经进入分批建仓区」的指数（给「明天要做什么」卡用）。 */
export const hkActs = x => (x?.v?.status === 'ok' ? valuationActs(asMarketView(x.v)) : []);

export function hkBrief(x) {
  if (!x) return h('div', { class: 'mkt-brief hint' }, h('span', { class: 'spinner' }), ' 加载港股指数…（第一次要联网取历史，几秒钟）');
  const { v, o } = x;
  return h('div', {},
    o?.status === 'ok' ? trendCard(o) : null,
    v?.status === 'ok' ? valuationBlock(asMarketView(v), { compact: true }) : h('div', { class: 'alert warn small mt-s' }, v?.message || '指数估值暂时取不到（网络慢或数据源不可用），稍后点一下「港股」重试。'),
    roadmap());
}

/** 港股还没做的页面（K线 / 自选股 / 持仓 / 行情）：说明一下，给一个回到「选股（港股概览）」的按钮。 */
export function hkPlaceholder(name) {
  return {
    layout: 'page',
    mount(ctx) {
      const el = ctx.pageEl; clear(el);
      el.append(h('div', { class: 'pane-body page' }, h('div', { class: 'card' }, h('div', { class: 'empty' },
        h('div', { class: 'big' }, `港股的「${NAMES[name] || name}」还在建设中`),
        h('div', {}, '港股现在有选股（个股观察清单 + 指数估值）、K 线、行情；自选股、持仓、回测要后面分几步做。'),
        h('div', { class: 'mt' }, h('button', { class: 'btn primary', onclick: () => { location.hash = '#/screener'; } }, '去看港股选股'),
          ' ', h('button', { class: 'btn', onclick: () => document.querySelector('[data-market="CN"]').click() }, '切回 A 股')))),
        roadmap()));
    },
  };
}


/** 港股「行情」页：三个指数的价格走势、均线位置（点指数名切换图）。 */
export const hkMarketView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el);
    const gen = ctx.gen, alive = () => ctx.gen === gen;
    const box = h('div', {}, h('div', { class: 'card' }, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载港股指数走势（第一次要联网取日线，几秒钟）…')));
    el.append(h('div', { class: 'pane-body page' }, h('div', { class: 'row between mb' }, h('h1', {}, '港股 · 指数走势'), h('span', { class: 'hint', id: 'hk-asof' })), box));
    let o = null;
    for (let k = 0; k < 12 && alive(); k++) {
      try { o = await get('/hk/overview', { market: 'HK' }, { cache: false, timeout: 60000 }); }
      catch (e) { if (alive()) clear(box).append(h('div', { class: 'alert bad' }, '加载失败：' + e.message)); return; }
      if (!alive()) return;
      if (o.status !== 'loading') break;
      await new Promise(r => setTimeout(r, 3000));
    }
    if (!alive()) return;
    if (!o || o.status !== 'ok') { clear(box).append(h('div', { class: 'alert warn' }, o?.message || '还没取到港股指数日线（网络慢或数据源暂时不可用），稍后再点一下「行情」重试。')); return; }
    el.querySelector('#hk-asof').textContent = `截至 ${o.asof}${o.stale ? '（后台正在刷新）' : ''}`;
    let cur = o.indices[0], chart = null;
    const chartEl = h('div', { style: 'height:300px' });
    const pills = h('div', { class: 'row', style: 'gap:6px;flex-wrap:wrap' });
    const legend = h('div', { class: 'tiny muted mt-s' });
    const maBox = h('div', { class: 'tbl-wrap mt-s' });
    const draw = () => {
      chart?.destroy();
      const c = themeColors();
      const s = (key, name, color) => ({ name, color, data: cur.history.filter(p => p[key] != null).map(p => ({ time: p.date, value: p[key] })) });
      chart = lineChart(chartEl, [s('close', cur.name, c.accent || '#2457d6'), s('ma20', '月线', '#e8a317'), s('ma60', '季线', '#9b59b6'), s('ma200', '200 日线', '#8a8f98')], { height: 300, fmt: v => v.toFixed(0) });
      clear(pills).append(...o.indices.map(i => h('button', { class: 'btn sm' + (i === cur ? ' primary' : ''), onclick: () => { cur = i; draw(); } }, i.name)));
      legend.textContent = `${cur.name}：收盘线（蓝）· 月线 20 日（橙）· 季线 60 日（紫）· 200 日线（灰）；近 300 个交易日，数据 ${cur.since} 起。`;
      clear(maBox).append(h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, ['均线', '数值', '现价离均线', '位置'].map((x, k) => h('th', { class: k === 1 || k === 2 ? 'r' : '' }, x)))),
        h('tbody', {}, cur.mas.map(m => h('tr', {}, h('td', {}, `${m.name}（${m.n} 日）`), h('td', { class: 'num r' }, fmtNum(m.value, 0)),
          h('td', { class: 'num r ' + dirClass(m.dist) }, fmtPct(m.dist, 1)), h('td', {}, h('span', { class: 'badge ' + (m.above ? 'ok' : '') }, m.above ? '站上' : '跌破')))))));
    };
    const rows = o.indices.map(i => h('tr', { style: 'cursor:pointer', onclick: () => { cur = i; draw(); chartEl.scrollIntoView({ block: 'nearest' }); } },
      h('td', {}, h('b', {}, i.name), h('div', { class: 'tiny faint' }, i.etf)),
      h('td', { class: 'num r' }, fmtIdx(i.close)), h('td', { class: 'num r ' + dirClass(i.chg_1d) }, fmtPct(i.chg_1d, 2)),
      h('td', { class: 'num r ' + dirClass(i.ret_20d) }, fmtPct(i.ret_20d, 1)), h('td', { class: 'num r ' + dirClass(i.ret_60d) }, fmtPct(i.ret_60d, 1)),
      h('td', { class: 'num r ' + dirClass(i.dd_52w) }, fmtPct(i.dd_52w, 1)),
      h('td', { style: 'white-space:nowrap' }, h('span', { class: 'badge ' + (TAG_CLASS[i.trend] || ''), title: i.trend_text }, i.trend), ' ', maChips(i))));
    clear(box).append(
      h('div', { class: 'card' }, h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['指数 / 对应 ETF', '收盘', '今日', '20 日', '60 日', '距 52 周高', '趋势（站上哪几条均线）'].map((x, k) => h('th', { class: k > 0 && k < 6 ? 'r' : '' }, x)))), h('tbody', {}, rows))),
        h('div', { class: 'tiny muted mt-s' }, '趋势 = 站上 20 / 60 / 120 / 200 日这 4 条均线里的几条（4 = 强势，0 = 弱势）；右边的小圆点亮 = 站上。点一行切换下面的图。')),
      h('div', { class: 'card mt' }, pills, h('div', { class: 'mt-s' }, chartEl), legend, maBox),
      h('p', { class: 'hint mt-s' }, o.note, ` 数据：${o.source}${o.refreshed_at ? '，' + o.refreshed_at.replace('T', ' ').slice(5, 16) + ' 更新' : ''}。`));
    requestAnimationFrame(draw);
    setTimeout(() => { if (!chart && alive()) draw(); }, 200);            // 后台标签里 requestAnimationFrame 不触发：兜底
    ctx.cleanup = () => { chart?.destroy(); chart = null; };
  },
};


/** 港股「K线」页：左边搜索（代码 / 名称 / 拼音首字母，不输入时是成交额最高的几只），右边前复权 K 线 + 均线 + 成交量，以及每手股数。 */
export const hkKlineView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el);
    const gen = ctx.gen, alive = () => ctx.gen === gen;
    const body = h('div', {}, h('div', { class: 'card' }, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 读取港股行情库状态…')));
    el.append(h('div', { class: 'pane-body page' }, h('div', { class: 'row between mb' }, h('h1', {}, '港股 · K线'), h('span', { class: 'hint', id: 'hkk-asof' })), body));
    let st;
    try { st = await get('/hk/stocks/status', { market: 'HK' }, { cache: false }); }
    catch (e) { if (alive()) clear(body).append(h('div', { class: 'alert bad' }, '加载失败：' + e.message)); return; }
    if (!alive()) return;
    if (!st.ready) {
      clear(body).append(h('div', { class: 'card' }, h('div', { class: 'empty' }, h('div', { class: 'big' }, '港股个股行情库还没初始化'),
        h('div', {}, '到「设置 → 数据更新」点「初始化港股个股行情库」：先取成交额前 800 只的名单，再下载 10 年日线，约几分钟（可以随时中断，下次接着下）。'),
        h('div', { class: 'mt' }, h('button', { class: 'btn primary', onclick: () => { location.hash = '#/settings/data'; } }, '去初始化')))));
      return;
    }
    el.querySelector('#hkk-asof').textContent = `日线截至 ${st.bars_asof}（${st.with_bars} 只）`;
    const input = h('input', { type: 'search', placeholder: '代码 / 名称 / 拼音首字母，如 00700 或 txkg', style: 'width:100%' });
    const listEl = h('div', { class: 'hk-list' });
    const head = h('div', { class: 'row between', style: 'align-items:baseline' });
    const stats = h('div', { class: 'grid c4 mt-s' });
    const mainEl = h('div', { style: 'height:420px' });
    const lotEl = h('div', { class: 'small muted mt-s' });
    const note = h('div', { class: 'tiny faint mt-s' });
    const right = h('div', { style: 'flex:1 1 440px;min-width:0' }, h('div', { class: 'card' }, head, stats, h('div', { class: 'mt-s', style: 'position:relative' }, mainEl), lotEl, note));
    clear(body).append(h('div', { style: 'display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start' },
      h('div', { class: 'card', style: 'flex:0 1 300px;min-width:240px' }, input, h('div', { class: 'tiny muted mt-s', id: 'hk-list-title' }), h('div', { class: 'mt-s' }, listEl)), right));
    let sc = null, offTheme = null, cur = null, seq = 0;
    ctx.cleanup = () => { sc?.destroy(); offTheme?.(); sc = null; };
    const kv = (k, v, cls = '') => h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v));
    async function show(symbol) {
      cur = symbol; const my = ++seq;
      [...listEl.children].forEach(x => x.classList.toggle('sel', x.dataset.symbol === symbol));
      let k;
      try { k = await get('/hk/stocks/kline', { market: 'HK', symbol }, { cache: false }); }
      catch (e) { clear(head).append(h('div', { class: 'alert bad' }, '加载失败：' + e.message)); return; }
      if (!alive() || my !== seq) return;
      if (k.status !== 'ok') { clear(head).append(h('div', { class: 'alert warn' }, k.message || '没有数据')); clear(stats); lotEl.textContent = ''; return; }
      const L = k.last;
      clear(head).append(h('div', {}, h('b', { style: 'font-size:16px' }, k.name), h('span', { class: 'muted small' }, '  ' + symbol.split('.').pop() + '.HK')),
        h('div', { class: 'num', style: 'font-size:18px' }, fmtNum(L.close, L.close < 10 ? 3 : 2), h('span', { class: 'small ' + dirClass(L.chg_1d) }, '  ' + fmtPct(L.chg_1d, 2))));
      clear(stats).append(kv('今日成交额', fmtHKD(L.amount)), kv('20 日平均成交额', fmtHKD(L.amount_20d)), kv('20 日涨跌', fmtPct(L.ret_20d, 1), dirClass(L.ret_20d)), kv('距 52 周高', fmtPct(L.dd_52w, 1), dirClass(L.dd_52w)));
      sc?.destroy(); offTheme?.();
      sc = new StockChart(mainEl, null);
      sc.build(); sc.setData({ bars: k.bars, ma: k.ma });
      offTheme = onThemeChange(() => { if (sc?.payload && sc.main) sc.setData(sc.payload); });
      note.textContent = `${k.adjusted}。日线 ${k.since} 起，截至 ${k.asof}，来源：雅虎财经。均线 5 / 10 / 20 / 50 / 200 日。`;
      lotEl.textContent = '每手股数：查询中…（第一次要联网，约几秒）';
      try {
        const lot = await get('/hk/stocks/lot', { symbol }, { cache: false, timeout: 60000 });
        if (!alive() || my !== seq) return;
        lotEl.textContent = lot.lot ? `每手 ${lot.lot} 股，买一手约 ${fmtHKD(lot.lot * L.close)}（不含佣金和印花税）。` : '这次没查到每手股数（数据源暂时不可用），买之前请在券商里确认。';
      } catch { if (alive() && my === seq) lotEl.textContent = '这次没查到每手股数，买之前请在券商里确认。'; }
    }
    async function search(q) {
      let r;
      try { r = await get('/hk/stocks/search', { market: 'HK', q }, { cache: false }); } catch { return; }
      if (!alive()) return;
      el.querySelector('#hk-list-title').textContent = q.trim() ? `搜索结果 ${r.items.length} 只` : '成交额最高的几只（点一只看 K 线）';
      clear(listEl).append(...(r.items.length ? r.items.map(i => h('div', { class: 'list-item' + (i.symbol === cur ? ' sel' : ''), dataset: { symbol: i.symbol }, style: 'cursor:pointer;padding:6px 8px', onclick: () => show(i.symbol) },
        h('div', { class: 'row between' }, h('b', {}, i.name), h('span', { class: 'num small ' + dirClass(i.pct) }, i.price != null ? fmtNum(i.price, i.price < 10 ? 3 : 2) : '')),
        h('div', { class: 'row between tiny muted' }, h('span', {}, i.symbol.split('.').pop()), h('span', {}, i.amount ? '成交额 ' + fmtHKD(i.amount) : ''))))
        : [h('div', { class: 'muted small' }, '没有找到。只有成交额前 800 只在行情库里。')]));
    }
    input.addEventListener('input', debounce(() => search(input.value), 250));
    await search('');
    const first = listEl.firstElementChild?.dataset.symbol;
    if (first && alive()) show(first);
  },
};
