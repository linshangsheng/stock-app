// 港股（独立的第三个市场）。第 1 步只有「指数估值与分批建仓」：恒生指数、国企指数、恒生科技。
// 个股行情库、指数走势 / 均线、选股、持仓、回测还在做：这些页面先显示「还在建设中」，不报错、不留空白。
import { get } from './api.js';
import { h, clear } from './util.js';
import { valuationBlock, valuationActs } from './marketpanel.js';

const NAMES = { kline: 'K线', watchlist: '自选股', portfolio: '持仓', market: '行情' };

/** 把 /api/market/valuation（港股）整理成 valuationBlock 要的形状。 */
function asMarketView(v) {
  return { status: 'ok', indices: v.items.map(i => ({ symbol: i.symbol, name: i.name, etf: i.etf, valuation: i })),
    valuation: { refreshed_at: v.refreshed_at, stale: v.stale, rule: v.rule, note: v.note } };
}

const TODO = [
  ['已经有', '三个指数的估值（市盈率、近 10 年 / 有数据以来的最低、距最低多少、历史分位、市净率分位、股息率）和分批建仓建议。'],
  ['下一步', '指数的价格走势和均线位置（恒生指数、国企指数、恒生科技）。'],
  ['还没有', '港股个股的行情库、K 线、自选股、选股扫描、持仓与止损止盈、回测、自动更新开关。每一块要单独校准参数（港股有每手股数不同、T+0、无涨跌停、印花税等差别），后面分几步做。'],
];

function roadmap() {
  return h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '港股现在做到哪了')),
    h('div', { class: 'small', style: 'line-height:1.8' }, TODO.map(([k, v]) => h('div', {}, h('b', {}, k + '：'), v))),
    h('p', { class: 'hint mt-s' }, '在左侧上方的「A股｜港股｜美股」按钮可以随时切换。设置页的数据 / 更新项目现在显示的是 A 股的。'));
}

export const hkView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el);
    const gen = ctx.gen, alive = () => ctx.gen === gen;
    const box = h('div', {}, h('div', { class: 'card' }, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载港股指数估值（第一次要联网取历史，几秒钟）…')));
    el.append(h('div', { class: 'pane-body page' },
      h('div', { class: 'row between mb' }, h('h1', {}, '港股 · 市场概览'), h('span', { class: 'hint' }, '恒生指数 · 国企指数 · 恒生科技')),
      box, roadmap()));
    for (let k = 0; k < 12 && alive(); k++) {                          // 第一次在后台取数：每 3 秒再问一次，最多 36 秒
      let v;
      try { v = await get('/market/valuation', { market: 'HK' }, { cache: false, timeout: 60000 }); }
      catch (e) { if (alive()) clear(box).append(h('div', { class: 'alert bad' }, '加载失败：' + e.message)); return; }
      if (!alive()) return;
      if (v.status === 'ok') {
        const d = asMarketView(v), acts = valuationActs(d);
        clear(box).append(
          h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', {}, '今天要做什么')),
            acts.length ? h('div', { class: 'act-b' }, h('b', {}, '进入分批建仓区：'), acts.join('；'), '。每批之间隔两周以上，不要一次买完。')
              : h('div', { class: 'small muted' }, '三个指数都没有进入分批建仓区（市盈率要降到离最低 10% 以内），不用做任何事。')),
          valuationBlock(d, { standalone: true }));
        return;
      }
      if (v.status !== 'loading') { clear(box).append(h('div', { class: 'alert warn' }, v.message || '暂时取不到港股指数估值')); return; }
      await new Promise(r => setTimeout(r, 3000));
    }
    if (alive()) clear(box).append(h('div', { class: 'alert warn' }, '还没取到港股指数估值（网络慢或数据源暂时不可用），稍后再点一下「港股」重试。'));
  },
};

/** 港股还没做的页面（K线 / 自选股 / 持仓 / 行情）：说明一下，给一个回到「选股（港股概览）」的按钮。 */
export function hkPlaceholder(name) {
  return {
    layout: 'page',
    mount(ctx) {
      const el = ctx.pageEl; clear(el);
      el.append(h('div', { class: 'pane-body page' }, h('div', { class: 'card' }, h('div', { class: 'empty' },
        h('div', { class: 'big' }, `港股的「${NAMES[name] || name}」还在建设中`),
        h('div', {}, '现在港股只有指数估值与分批建仓参考（在「选股」页）。个股行情库、K 线、持仓、回测要后面分几步做。'),
        h('div', { class: 'mt' }, h('button', { class: 'btn primary', onclick: () => { location.hash = '#/screener'; } }, '去看港股指数估值'),
          ' ', h('button', { class: 'btn', onclick: () => document.querySelector('[data-market="CN"]').click() }, '切回 A 股')))),
        roadmap()));
    },
  };
}
