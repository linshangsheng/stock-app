// 行情页（M6，4.2）：全市场行情总览（盘后日线）——指数、市场宽度与环境状态、行业 / 板块涨跌、涨跌幅 / 放量 / 强势排行。
// 行业强弱由库内个股日收益等权合成（3.14）；排行只在当日交易池 L2 内统计。数据全部来自后端，前端不做计算。
import { get, state } from './api.js';
import { h, clear, fmtPrice, fmtPct, fmtNum, dirClass, code } from './util.js';
import { loadMarketView, marketFull } from './marketpanel.js';

const REGIME = { NORMAL: ['正常', '正常开仓'], CAUTION: ['谨慎', '仓位上限减半，只做最强候选'], DEFENSIVE: ['防守', '不开新仓'], UNKNOWN: ['未知', '基准数据缺失'] };

export const marketView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el);
    const mvBox = h('div', {}, h('div', { class: 'card' }, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载市场温度与指数择时…')));
    const body = h('div', {}, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载行情总览（首次打开需构建全市场数据，约 40 秒）…'));
    el.append(h('div', { class: 'pane-body page' }, h('div', { class: 'row between mb' }, h('h1', {}, state.market === 'US' ? '美股行情' : 'A股行情'), h('span', { class: 'hint', id: 'mk-asof' })),
      mvBox, h('h2', { class: 'mt', style: 'margin-top:22px' }, '个股与行业'), body));
    // 市场温度 / 指数择时：轻量，单独加载，不等行情总览
    (async () => {
      let mv = null;
      for (let k = 0; k < 20; k++) {
        try { mv = await loadMarketView(); } catch (e) { mv = { status: 'error', message: e.message }; }
        if (mv.status !== 'computing' || !document.body.contains(mvBox)) break;
        clear(mvBox); mvBox.append(marketFull(mv).el);
        await new Promise(r => setTimeout(r, 8000));
      }
      if (!document.body.contains(mvBox)) return;
      this._mv?.destroy();
      this._mv = marketFull(mv);
      clear(mvBox); mvBox.append(this._mv.el); this._mv.init();
    })();
    let o;
    try { o = await get('/market/overview', {}, { timeout: 180000 }); } catch (e) { clear(body); body.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '暂无行情数据'), e.message, h('div', { class: 'mt' }, h('a', { class: 'btn', href: '#/settings' }, '去设置页初始化数据')))); return; }
    clear(body);
    const g = o.breadth;
    const [rl, rt] = REGIME[g.regime] || ['—', ''];
    const asof = document.getElementById('mk-asof');
    if (asof) asof.textContent = `数据截止 ${o.date}（盘后日线）${o._stale ? ' · 数据延迟' : ''}`;

    // 指数
    body.append(h('div', { class: 'grid c4' }, o.indices.map(i => h('div', { class: 'idx-card' },
      h('div', { class: 'nm' }, i.name), h('div', { class: 'px' }, fmtNum(i.close, i.close > 1000 ? 0 : 2)),
      h('div', { class: 'num small ' + dirClass(i.symbol === '^VIX' ? -i.chg_1d : i.chg_1d), style: 'font-weight:600' }, fmtPct(i.chg_1d)),
      h('div', { class: 'tiny muted num' }, `5日 ${fmtPct(i.ret_5d, 1)} · 20日 ${fmtPct(i.ret_20d, 1)}`),
      i.above_ma50 != null ? h('div', { class: 'tiny' }, h('span', { class: 'badge ' + (i.above_ma50 ? 'ok' : 'bad') }, i.above_ma50 ? '站上 MA50' : '跌破 MA50'), ' ', i.above_ma200 != null ? h('span', { class: 'badge ' + (i.above_ma200 ? 'ok' : 'bad') }, i.above_ma200 ? 'MA200 上' : 'MA200 下') : null) : null))));

    if (o.sector_etfs?.length) {
      const srt = [...o.sector_etfs].sort((a, b) => (b.ret_20d ?? -9) - (a.ret_20d ?? -9));
      body.append(h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '行业 ETF（辅助基准，按 20 日强弱）')),
        ...srt.map(x => h('div', { class: 'heat' }, h('span', {}, x.name), h('span', { class: 'num r ' + dirClass(x.chg_1d) }, fmtPct(x.chg_1d, 1)), h('span', { class: 'num r ' + dirClass(x.ret_5d) }, fmtPct(x.ret_5d, 1)),
          h('span', { class: 'num r ' + dirClass(x.ret_20d) }, fmtPct(x.ret_20d, 1)), h('span', { class: 'hide-m' }, '')))));
    }

    // 市场环境 / 宽度
    const bar = (v, label) => h('div', { class: 'kv' }, h('span', { class: 'k' }, label), h('span', { class: 'v num' }, v == null ? '—' : fmtPct(v, 0, false)),
      h('div', { class: 'bar' }, h('i', { style: `width:${Math.round((v || 0) * 100)}%` })));
    body.append(h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '个股开仓闸门与宽度（当日交易池）'), h('span', { class: 'pill-state ' + g.regime, title: rt }, rl)),
      h('div', { class: 'grid c4' }, h('div', { class: 'kv' }, h('span', { class: 'k' }, '上涨 / 下跌 / 平'), h('span', { class: 'v num' }, `${g.up} / ${g.down} / ${g.flat}`)),
        bar(g.adv_ratio, '涨跌比（涨 /（涨+跌））'), bar(g.above_ma50, '站上 MA50 的比例'), bar(g.above_ma200, '站上 MA200 的比例')),
      h('div', { class: 'grid c4 mt-s' }, bar(g.above_ma20, '站上 MA20 的比例'), h('div', { class: 'kv' }, h('span', { class: 'k' }, '创 52 周新高'), h('span', { class: 'v num' }, g.new_highs ?? '—')),
        h('div', { class: 'kv' }, h('span', { class: 'k' }, '交易池规模'), h('span', { class: 'v num' }, g.pool)), g.vix != null ? h('div', { class: 'kv' }, h('span', { class: 'k' }, 'VIX'), h('span', { class: 'v num' }, fmtNum(g.vix, 1))) : null),
      h('p', { class: 'hint mt-s' }, rt + '。宽度统计口径为当日交易池 L2（3.5），依赖全池数据，须通过数据完整性闸门。')));

    // 行业 / 板块
    const maxAbs = Math.max(0.01, ...o.industries.map(x => Math.abs(x.ret_20d ?? 0)));
    body.append(h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '行业 / 板块涨跌（按 20 日强弱排序；个股等权合成）'), h('span', { class: 'hint' }, `${o.industries.length} 个行业`)),
      h('div', { class: 'heat', style: 'font-size:11px;color:var(--muted)' }, h('span', {}, '行业'), h('span', { class: 'r' }, '1日'), h('span', { class: 'r' }, '5日'), h('span', { class: 'r' }, '20日'), h('span', { class: 'r hide-m' }, '上涨占比')),
      ...o.industries.map(x => h('div', { class: 'heat' }, h('span', {}, x.industry, h('span', { class: 'faint tiny' }, ` ${x.n}只`)),
        h('span', { class: 'num r ' + dirClass(x.ret_1d) }, fmtPct(x.ret_1d, 1)), h('span', { class: 'num r ' + dirClass(x.ret_5d) }, fmtPct(x.ret_5d, 1)),
        h('span', { class: 'num r ' + dirClass(x.ret_20d), style: 'position:relative' }, fmtPct(x.ret_20d, 1)),
        h('div', { class: 'bar-wrap hide-m', title: `上涨占比 ${fmtPct(x.up, 0, false)}` }, h('i', { style: `left:0;width:${Math.round((x.up ?? 0) * 100)}%;background:${(x.up ?? 0) >= 0.5 ? 'var(--up)' : 'var(--down)'};opacity:.55` }))))));

    // 排行
    const TABS = [['gainers', '涨幅榜'], ['losers', '跌幅榜'], ['volume', '放量榜（量比）'], ['strong', '强势榜（RPS20）']];
    let tab = 'gainers';
    const tbl = h('div', {});
    const draw = () => {
      clear(tbl);
      tbl.append(h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['名称', '收盘', '涨跌幅', '量比', '行业'].map((t, i) => h('th', { class: i > 0 && i < 4 ? 'r' : '' }, t)))),
        h('tbody', {}, o.movers[tab].map(m => h('tr', { style: 'cursor:pointer', onclick: () => { location.hash = '#/screener/' + encodeURIComponent(m.symbol); } },
          h('td', {}, m.name, h('span', { class: 'muted small num' }, ' ' + code(m.symbol))), h('td', { class: 'num' }, fmtPrice(m.close)), h('td', { class: 'num ' + dirClass(m.ret_1d) }, fmtPct(m.ret_1d)),
          h('td', { class: 'num' }, m.vol_ratio != null ? fmtNum(m.vol_ratio, 1) : '—'), h('td', { class: 'small muted' }, m.industry || '—')))))));
    };
    const tabs = h('div', { class: 'tabs' }, TABS.map(([k, l]) => h('button', { class: k === tab ? 'on' : '', onclick: e => { tab = k; [...tabs.children].forEach(b => b.classList.toggle('on', b === e.target)); draw(); } }, l)));
    body.append(h('div', { class: 'card mt' }, h('h3', { style: 'margin-bottom:6px' }, '排行（交易池 L2 内）'), tabs, tbl));
    draw();
  },
  cleanup() { this._mv?.destroy(); this._mv = null; },
};
