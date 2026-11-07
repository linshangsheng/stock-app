// 市场温度 + 宽基指数 / ETF 择时（选股页摘要、行情页完整版共用）。数据全部来自 /api/market/view，前端不做计算。
// 「温度」= 全A站上 20 日线的股票比例：A 股市场级信号是逆向的——冰点之后历史上涨概率最高，过热之后最低（见后端 market_view.py）。
import { get, state } from './api.js';
import { h, clear, fmtPct, fmtNum, dirClass, evBadge } from './util.js';
import { prefs } from './db.js';
import { lineChart, themeColors } from './chart.js';

export const ZONE_NAMES = ['冰点', '偏冷', '中性', '偏热', '过热'];

export async function loadMarketView() {
  return get('/market/view', {}, { cache: false, timeout: 120000 });
}

const pct0 = v => (v == null ? '—' : fmtPct(v, 0, false));
const money = v => (v == null ? '—' : '¥' + Math.round(v).toLocaleString('zh-CN'));

/** 温度计：5 段色带 + 当前位置标记。 */
export function gauge(b20, cuts) {
  const edges = [0, ...cuts, 1];
  return h('div', { class: 'gauge', title: `全A站上 20 日线比例 ${pct0(b20)}` },
    ...edges.slice(0, -1).map((e, i) => h('i', { class: 'z' + i, style: `width:${(edges[i + 1] - e) * 100}%` })),
    b20 != null ? h('b', { style: `left:${Math.min(99.5, Math.max(0.5, b20 * 100))}%` }) : null);
}

export function zonePill(z, name) {
  return h('span', { class: 'zone-pill z' + z }, name);
}

/** 某个指数「明天」的情景：开盘跳空不改变信号；明天收盘到多少会触发买 / 卖；盘中跌破先不卖。 */
export function tomorrowTable(i) {
  const t = i.next_open?.tomorrow || [];
  if (!t.length) return null;
  return h('div', { class: 'tmr-box' },
    h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, h('th', {}, '时间'), h('th', {}, '如果'), h('th', {}, '那么'))),
      h('tbody', {}, t.map(r => h('tr', {}, h('td', { style: 'white-space:nowrap' }, r.when), h('td', { class: 'num' }, r.level), h('td', {}, r.then))))),
    h('div', { class: 'tiny muted' }, `点位都是指数点位；对应 ETF 按「较今收」的百分比换算。近两年开盘跳空中位数 ${fmtPct(i.next_open.gap_median, 2, false)}。`));
}

/** 已经进入「分批建仓区」的指数（市盈率距近 10 年最低不超过 10%）：['创业板指 累计 2/5 批', ...]。给「明天要做什么」卡用。 */
export function valuationActs(d) {
  if (!d || d.status !== 'ok') return [];
  return valRows(d).filter(i => i.valuation.in_zone).map(i => `${i.name} 累计 ${i.valuation.batches_now}/${i.valuation.batches_total} 批（市盈率 ${fmtNum(i.valuation.pe, 1)}，距${i.valuation.window_text || '近 10 年'}最低 ${fmtPct(i.valuation.gap, 1)}）`);
}

/** 估值表的行：每个有估值的宽基指数 + 只有估值的指数（A 股页面里的港股：恒生指数 / 国企指数 / 恒生科技）。 */
function valRows(d) {
  return [...(d?.indices || []).filter(i => i.valuation).map(i => ({ name: i.name, etf: i.etf, valuation: i.valuation })),
    ...(d?.valuation?.extra || []).map(x => ({ name: x.name, etf: x.etf, valuation: x, hk: true }))];
}

/** 指数估值与分批建仓参考（只在 A 股、数据来自 /api/market/view 里每个指数的 valuation）。compact = 选股页窄栏用的精简版。 */
export function valuationBlock(d, { compact = false, standalone = false } = {}) {
  const items = valRows(d);
  if (!items.length) return null;
  const rule = d.valuation?.rule || {}, total = rule.batches_total || 5, enter = rule.enter_gap ?? 0.1;
  const bond = d.valuation?.bond || null;
  const BK = 'valBudget';
  let budget = prefs.get(BK, '');
  const hot = items.filter(i => i.valuation.in_zone);
  const summary = hot.length
    ? h('div', { class: 'act-b' }, h('b', {}, `${hot.map(i => i.name).join('、')} 进入分批建仓区`), `（市盈率离近 10 年最低不超过 ${fmtPct(enter, 0, false)}）。不要一次买完：分 ${total} 批，每批 ${fmtPct(1 / total, 0, false)}，现在累计买到下表「建议」那一栏写的批数，批与批之间至少隔 ${rule.min_days || 10} 个交易日。`)
    : h('div', { class: 'small muted' }, `现在没有指数进入分批建仓区（要市盈率降到离近 10 年最低 ${fmtPct(enter, 0, false)} 以内）。平时这一栏不用做任何事。`);
  const sgn = v => (v == null ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(1) + '%');
  const detail = (i) => {
    const v = i.valuation, box = h('div', { class: 'small', style: 'line-height:1.7' });
    const paint = () => {
      const each = +budget > 0 ? +budget / v.batches_total : 0;
      clear(box).append(h('div', {}, v.advice),
        h('div', { class: 'tiny muted mt-s' }, `${v.window_text || '近 10 年'}：最低 ${fmtNum(v.low, 2)}（${v.low_date}）· 中位 ${fmtNum(v.median, 2)} · 最高 ${fmtNum(v.high, 2)}（${v.high_date}）；建仓区上沿 ${fmtNum(v.enter_price, 2)}。`),
        v.spread != null ? h('div', { class: 'tiny mt-s' }, h('b', {}, '股债利差：'), `盈利收益率 ${fmtNum(v.ey, 2)}%（= 100 ÷ 市盈率）− ${bond?.name || '10 年期国债'} ${fmtNum(v.bond_yield, 2)}% = `, h('b', {}, `${fmtNum(v.spread, 2)}%`),
          v.spread_pct != null ? `。近 10 年里这个利差的分位 ${pct0(v.spread_pct)}（中位 ${fmtNum(v.spread_median, 2)}%，区间 ${fmtNum(v.spread_min, 2)}% ~ ${fmtNum(v.spread_max, 2)}%）：分位越高 = 股票相对国债越便宜，现在算「${v.spread_label}」。` : '。（历史不足一年，没有分位）') : null,
        h('div', { class: 'tiny muted' }, `市盈率历史分位 ${pct0(v.pct)}${v.pb_pct != null ? ` · 市净率分位 ${pct0(v.pb_pct)}` : ''}${v.dividend_yield != null ? ` · 股息率 ${fmtPct(v.dividend_yield, 2, false)}` : ''}；数据截至 ${v.asof}。`),
        each ? h('div', { class: 'tiny mt-s' }, `按你打算投入的 ${money(+budget)}：每批约 ${money(each)}；现在累计应买到 ${v.batches_now} 批 = ${money(each * v.batches_now)}（已经买过的部分请自己对照）。`) : null);
    };
    paint();
    return { el: box, paint };
  };
  const painters = [];
  const rows = items.flatMap(i => {
    const v = i.valuation, dt = detail(i); painters.push(dt.paint);
    const sub = h('tr', { class: 'tmr', hidden: true }, h('td', { colspan: compact ? 6 : 7 }, dt.el));
    const tag = v.in_zone ? h('span', { class: 'badge ok' }, `分批建仓 ${v.batches_now}/${v.batches_total} 批`) : h('span', { class: 'muted small' }, '还没到');
    return [h('tr', { style: 'cursor:pointer', title: '点开看这个指数的估值细节和怎么分批', onclick: () => { sub.hidden = !sub.hidden; } },
      h('td', {}, h('span', { class: 'muted' }, '▸ '), h('b', {}, i.name), h('div', { class: 'tiny faint' }, (i.hk ? '港股 · ' : '') + (i.etf || '').split(' ')[0])),
      h('td', { class: 'num r' }, fmtNum(v.pe, 1)),
      h('td', { class: 'num r', title: `${v.window_text || '近 10 年'}最低，出现在 ${v.low_date}` }, fmtNum(v.low, 1), v.span_years < 9.4 ? h('span', { class: 'tiny', style: 'color:var(--warn)', title: `这个指数只有约 ${v.span_years} 年的数据（${v.since} 起），不是 10 年` }, '*') : null),
      h('td', { class: 'num r', style: v.in_zone ? 'font-weight:700' : '' }, sgn(v.gap)),
      h('td', { class: 'num r', style: 'white-space:nowrap', title: v.spread != null ? `盈利收益率 ${fmtNum(v.ey, 2)}% − ${bond?.name || '国债'} ${fmtNum(v.bond_yield, 2)}%；${v.spread_label ? '近 10 年分位 ' + pct0(v.spread_pct) + '，' + v.spread_label : '历史不足一年，没有分位'}` : '没有国债数据（正在后台取，第一次要几分钟）' },
        v.spread != null ? [fmtNum(v.spread, 1) + '%', v.spread_label ? h('div', { class: 'tiny', style: 'white-space:nowrap;color:' + (v.spread_label === '股票相对便宜' ? 'var(--ok, #1a9b5b)' : v.spread_label === '股票相对偏贵' ? 'var(--warn)' : 'var(--muted)') }, v.spread_label.replace('股票相对', '')) : null] : '—'),
      ...(compact ? [] : [h('td', { class: 'num r', title: '现在的市盈率在近 10 年里排第几位（0% = 最便宜）' }, pct0(v.pct))]),
      h('td', { style: 'white-space:nowrap' }, tag)), sub];
  });
  const budgetIn = h('input', { type: 'number', min: 0, step: 1000, value: budget, style: 'width:110px', oninput: e => { budget = e.target.value; prefs.set(BK, budget); painters.forEach(f => f()); } });
  const tiers = (rule.tiers || []).map(t => `距最低 ≤ ${fmtPct(t.gap, 1, false)} → 累计 ${t.batches} 批`);
  const how = h('details', { class: 'mt-s' }, h('summary', {}, '怎么分批？为什么这样分'),
    h('div', { class: 'small', style: 'margin:6px 0;line-height:1.7' },
      h('b', {}, '规则：'), `「距最低」= 现在的市盈率 ÷ 近 10 年最低市盈率 − 1。例如近 10 年最低是 20，市盈率降到 22 以内（≤ ${fmtPct(enter, 0, false)}）就进入建仓区。`,
      h('br'), h('b', {}, `分 ${total} 批，每批 ${fmtPct(1 / total, 0, false)}：`), tiers.join('；'), `；第 ${total} 批留作机动，只在市盈率创出新低后还继续明显下探时再用。`,
      h('br'), h('b', {}, '股债利差（表里新增的一列）：'), '盈利收益率（100 ÷ 市盈率）减去 10 年期国债收益率。国债利率下降时，同样的市盈率相对国债更划算；利率上升时相反。分位是和这个指数自己近 10 年的利差比，分位越高 = 股票相对国债越便宜。它只是参考，不影响上面分几批；而且利率为什么变很重要（经济变差导致的降息，股市不一定好），A 股样本里这个关系并不强。',
      h('br'), h('b', {}, '节奏：'), `每批之间至少隔 ${rule.min_days || 10} 个交易日（约两周），同一档里不要一天买完两批。低估值常常持续一两年（2012~2014、2018、2022~2024 年的低位都拖了很久），所以才要拉开买。`,
      standalone ? null : h('br'), standalone ? null : h('b', {}, '和上面的规则是两套思路：'), standalone ? null : '趋势仓看「走势」、抄底仓看「恐慌」，这里看「便宜程度」。默认不并进规则仓位；想用就从你准备长期持有的那部分钱里划出来分批买，并且仍然要接受买完后可能继续跌。',
      h('br'), h('b', {}, '提醒：'), d.valuation?.note),
    h('div', { class: 'row mt-s', style: 'gap:6px;flex-wrap:wrap' }, '这个指数你打算一共投入多少钱（元，可不填）：', budgetIn));
  return h('div', { class: 'card mt-s', style: hot.length ? 'border-left:3px solid var(--warn)' : '' },
    h('div', { class: 'row between' }, h('b', {}, '指数估值 · 分批建仓参考'),
      evBadge('弱', '「起点估值越低、之后几年平均回报越好」是一般经验，但这套 5 批的分法没有用本软件的数据回测过；指数历史只有近 10 年，真正的低位只有几轮。只作参考。')),
    h('div', { class: 'mt-s' }, summary),
    bond ? h('div', { class: 'tiny muted mt-s' }, `${bond.name} ${fmtNum(bond.yield, 2)}%（${bond.asof}）`, bond.chg_1y != null ? `，比一年前${bond.chg_1y >= 0 ? '高' : '低'} ${fmtNum(Math.abs(bond.chg_1y), 2)} 个百分点` : '', bond.note ? '。' + bond.note : '') : null,
    h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' },
      h('thead', {}, h('tr', {}, h('th', {}, '指数（点开）'), h('th', { class: 'r', title: '成分股整体滚动市盈率' }, '市盈率'),
        h('th', { class: 'r', title: '近 10 年最低（每周一个点）' }, '10年最低'),
        h('th', { class: 'r', title: '现在比近 10 年最低高出多少；≤ 10% 进入建仓区' }, '距最低'),
        h('th', { class: 'r', title: '股债利差 = 指数盈利收益率（1 ÷ 市盈率）− 10 年期国债收益率。越大 = 股票相对国债越便宜。只是参考，不影响分批建仓的批数' }, '股债利差'),
        ...(compact ? [] : [h('th', { class: 'r', title: '市盈率在近 10 年里的分位，0% = 最便宜' }, '分位')]),
        h('th', {}, '建议'))), h('tbody', {}, rows))),
    how,
    h('div', { class: 'tiny faint mt-s' }, `数据：蛋卷基金指数估值（第三方）${d.valuation?.refreshed_at ? '，' + d.valuation.refreshed_at.replace('T', ' ').slice(5, 16) + ' 更新' : ''}${d.valuation?.stale ? '（已超过半天没更新，后台正在刷新）' : ''}`));
}

/** 选股页顶部摘要：温度 + 每个宽基的规则仓位与下一步价位。 */
export function marketBrief(d) {
  if (!d || d.status !== 'ok') {
    return h('div', { class: 'mkt-brief' }, h('span', { class: 'hint' }, d?.message || '市场温度暂不可用'));
  }
  const t = d.thermometer;
  const rows = d.indices.flatMap(i => {
    const next = i.trend.holding ? `跌破 ${fmtNum(i.trend.exit_level, 0)} 卖` : (i.trend.rank_ok === false ? `进前 ${i.trend.top_k} 且站上 ${fmtNum(i.trend.buy_level, 0)}` : `站上 ${fmtNum(i.trend.buy_level, 0)} 买`);
    const dist = i.trend.holding ? i.trend.dist_exit : i.trend.dist_buy;
    const sub = h('tr', { class: 'tmr', hidden: true }, h('td', { colspan: 5 }, tomorrowTable(i)));
    return [h('tr', { style: 'cursor:pointer', title: '点开看明天的情景（高开 / 低开 / 收盘到多少会触发买卖）', onclick: () => { sub.hidden = !sub.hidden; } },
      h('td', {}, h('span', { class: 'muted' }, '▸ '), h('b', {}, i.name),
        i.trend.rank ? h('span', { class: 'rank-badge' + (i.trend.rank_ok ? ' ok' : ''), title: `近 20 日强弱第 ${i.trend.rank} 名；趋势仓只做前 ${i.trend.top_k} 名` }, '#' + i.trend.rank) : null,
        h('div', { class: 'tiny faint' }, (i.etf || '').split(' ')[0])),
      h('td', { class: 'num r ' + dirClass(i.chg_1d) }, fmtPct(i.chg_1d, 1)),
      h('td', { class: 'r' }, h('span', { class: 'badge ' + (i.position >= 1 ? 'ok' : i.position > 0 ? 'accent' : '') }, fmtPct(i.position, 0, false)),
        i.hold_amount != null ? h('div', { class: 'tiny muted num' }, money(i.hold_amount)) : null),
      h('td', { class: 'small' }, next, h('span', { class: 'tiny muted num' }, ` (${fmtPct(dist, 1)})`)),
      h('td', { class: 'small' + (i.next_open?.actions?.length ? '' : ' muted') }, i.next_open?.actions?.length ? i.next_open.actions.join('；') : '不操作')), sub];
  });
  const al = d.allocation;
  const allocLine = al ? h('div', { class: 'tiny mt-s' }, h('b', {}, '资金方案：'),
    `总资金 ${money(al.equity)} → 宽基 ETF ${money(al.etf_amount)}（${d.indices.length} 个指数各 ${money(al.per_index)}：趋势仓 ${money(al.trend_amount)} + 抄底仓 ${money(al.washout_amount)}）；`,
    al.factor_amount ? `低风险组合 ${money(al.factor_amount)}（${al.factor_n} 只）；` : '',
    al.stock_amount ? `个股波段 ${money(al.stock_amount)}（最多同时 ${al.stock_max_positions} 只，每笔最多亏 ${money(al.equity * al.stock_risk_per_trade)}）` : '个股波段不分钱（只观察）') : null;
  return h('div', { class: 'mkt-brief' },
    h('div', { class: 'row between' },
      h('div', { class: 'row', style: 'gap:8px' }, h('span', { class: 'k' }, '市场温度'), zonePill(t.zone, t.zone_name),
        h('span', { class: 'num small', title: '全A站上 20 日线的股票比例' }, pct0(t.b20))),
      h('a', { href: '#/market', class: 'small' }, '指数买入位 / 止损位 →')),
    gauge(t.b20, t.cuts),
    h('div', { class: 'tiny muted mt-s' }, t.hint),
    allocLine,
    h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, h('th', { title: '#名次 = 近 20 日涨幅强弱；趋势仓只做前 2 名。点指数名展开明天的情景' }, '宽基 / ETF（点开）'), h('th', { class: 'r' }, '今日'), h('th', { class: 'r' }, '规则仓位'), h('th', {}, '趋势仓下一步'),
      h('th', { title: '指数规则只看收盘：明天高开还是低开都不改变信号' }, '明天开盘'))),
      h('tbody', {}, rows))),
    valuationBlock(d, { compact: true }));
}

/** 行情页完整版。返回 { el, init, destroy }：el 挂进页面后再调用 init() 画图（图表需要容器宽度）。 */
export function marketFull(d) {
  const charts = [], inits = [];
  const api = { el: null, init() { for (const f of inits.splice(0)) charts.push(f()); }, destroy() { for (const c of charts.splice(0)) c?.destroy?.(); } };
  if (!d || d.status !== 'ok') {
    api.el = h('div', { class: 'card' }, h('div', { class: 'empty' }, d?.status === 'computing' ? h('span', { class: 'spinner' }) : null, ' ', d?.message || '市场温度暂不可用'));
    return api;
  }
  const t = d.thermometer, rule = d.rule;
  const out = h('div', {});

  // ① 温度
  const kv = (k, v, tip, cls = '') => h('div', { class: 'kv', title: tip || '' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v));
  const hist = h('div', { class: 'mt', style: 'height:150px' });
  out.append(h('div', { class: 'card' },
    h('div', { class: 'card-title' }, h('h3', {}, '市场温度（全A情绪）'), h('span', { class: 'hint' }, `截至 ${t.date} · 统计 ${t.pool} 只`)),
    h('div', { class: 'row', style: 'gap:14px;align-items:flex-end' },
      h('div', {}, h('div', { class: 'tiny muted' }, '全A站上 20 日线的比例'), h('div', { class: 'big-num' }, pct0(t.b20))),
      h('div', { style: 'flex:1;min-width:200px' }, h('div', { class: 'row between tiny muted' }, ...ZONE_NAMES.map(n => h('span', {}, n))), gauge(t.b20, t.cuts)),
      zonePill(t.zone, t.zone_name)),
    h('div', { class: 'alert info mt' }, h('b', {}, t.hint), h('div', { class: 'small mt-s' }, t.evidence_line)),
    h('div', { class: 'grid c4 mt' },
      kv('站上 60 日线', pct0(t.b60), '中期宽度'), kv('站上 200 日线', pct0(t.b200), '长期宽度'),
      kv('近 10 日上涨家数占比', pct0(t.adv_ratio_10d)), kv('新高 − 新低（250 日）', `${t.new_highs} − ${t.new_lows}`, '创一年新高的股票数减去创一年新低的股票数'),
      kv('全A等权 20 日', fmtPct(t.ew_ret_20d, 1), '所有股票等权平均的涨跌', dirClass(t.ew_ret_20d)), kv('距一年高点', fmtPct(t.ew_dd_250d, 1), '全A等权指数的回撤'),
      kv('成交额（5 日 / 60 日均）', t.amount_ratio == null ? '—' : fmtNum(t.amount_ratio, 2) + ' 倍', '大于 1 = 放量，小于 1 = 缩量'), kv('今日涨 / 跌家数', `${t.up} / ${t.down}`)),
    h('div', { class: 'tiny muted mt' }, '近一年温度走势（虚线为冰点 / 过热分界）'), hist));
  inits.push(() => {
    const c = themeColors();
    const data = t.history.filter(x => x.b20 != null).map(x => ({ time: x.date, value: Math.round(x.b20 * 1000) / 10 }));
    const ch = lineChart(hist, [{ name: '站上20日线 %', color: c.accent || '#2457d6', data }], { height: 150 });
    const s = ch.chart.addLineSeries({ color: 'rgba(0,0,0,0)', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    s.setData(data);
    for (const [v, col, tl] of [[t.cuts[0], '#2f7ed8', '冰点'], [t.cuts[3], '#E24B4A', '过热']]) s.createPriceLine({ price: v * 100, color: col, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: tl });
    return ch;
  });

  // ② 宽基指数 / ETF：规则仓位、买入位、止损位
  const tw = rule.trend, ww = rule.washout;
  out.append(h('div', { class: 'card mt' },
    h('div', { class: 'card-title' }, h('h3', {}, '宽基指数 / ETF：规则买入位与止损位'), h('span', { class: 'hint' }, '点位为指数点位；ETF 价格按百分比换算')),
    h('div', { class: 'alert mt-s small' },
      h('b', {}, '规则怎么用：'), `每个指数分两半仓位。`,
      h('br'), `① 趋势仓（${fmtPct(tw.weight, 0, false)}）：收盘站上 MA${tw.ma}×${(1 + tw.band).toFixed(2)} 第二天开盘买；收盘跌破 MA${tw.ma}×${(1 - tw.band).toFixed(2)} 第二天开盘卖。`,
      tw.top_k ? h('span', {}, `（并且只做近 ${tw.mom_days || 20} 日涨幅排前 ${tw.top_k} 的指数，掉出前 ${tw.top_k} 也卖——轮动让回撤更小）`) : null,
      h('br'), `② 抄底仓（${fmtPct(ww.weight, 0, false)}）：全A站上 20 日线比例跌破 ${pct0(ww.enter_below)}（恐慌）第二天开盘买；收盘跌破「信号日收盘 − ${ww.stop_atr_k}×ATR${ww.atr_n}」止损；比例回到 ${pct0(ww.exit_above)} 以上止盈，最多持有 ${ww.max_hold_days} 个交易日。`,
      h('br'), `③ 短期止盈：趋势仓的离场位（MA${tw.ma}×${(1 - tw.band).toFixed(2)}）会随均线上移，本身就是移动止盈；抄底仓表格里给出 +5% / +10% 的「参考止盈位」——回测中给抄底仓加固定止盈反而少赚（恐慌后的反弹常走得更远），所以规则不采用，想落袋为安时再参考。`,
      h('br'), h('b', {}, '④ 高开 / 低开怎么办：'), '指数规则所有判断都在收盘时做（站上 / 跌破均线、宽度、止损都看收盘价）。所以：今天收盘出了买入 / 卖出信号，明天就在开盘时买 / 卖，不论高开还是低开（回测就是按次日开盘价执行的）；没有信号，明天不论怎么开盘都不用操作；盘中跌破止损价也先不卖，等收盘确认，收盘仍在止损价下方才在下一个交易日开盘卖。宽基指数开盘跳空通常很小（近两年中位数约 0.2%~0.4%）。',
      h('br'), h('span', { class: 'muted' }, '规则仓位是「计划投入这个指数的资金」的比例，不是总资产比例。规则输出，不构成投资建议。'),
      d.allocation ? h('div', { class: 'mt-s' }, h('b', {}, '按你的资金：'), `宽基 ETF 一共 ${money(d.allocation.etf_amount)}，每个指数 ${money(d.allocation.per_index)}（趋势仓 ${money(d.allocation.trend_amount)}、抄底仓 ${money(d.allocation.washout_amount)}）；`,
        d.allocation.factor_amount ? `低风险组合 ${money(d.allocation.factor_amount)}（${d.allocation.factor_n} 只，见「选股 → 低风险组合」）；` : '',
        d.allocation.stock_amount ? `个股波段 ${money(d.allocation.stock_amount)}，最多同时 ${d.allocation.stock_max_positions} 只。` : '个股波段默认不分钱（只观察）。') : null),
    d.portfolio?.oos ? h('div', { class: 'alert ok mt-s small' }, h('b', {}, `${d.portfolio.n} 个宽基等分资金的组合（规则要整体看）：`),
      `样本外 ${d.portfolio.oos.rule.from} ~ ${d.portfolio.oos.rule.to}：规则 年化 ${fmtPct(d.portfolio.oos.rule.cagr, 1)}、最大回撤 ${fmtPct(d.portfolio.oos.rule.mdd, 1)}、Sharpe ${d.portfolio.oos.rule.sharpe}；`,
      `一直持有 ${fmtPct(d.portfolio.oos.hold.cagr, 1)}、${fmtPct(d.portfolio.oos.hold.mdd, 1)}、${d.portfolio.oos.hold.sharpe}。单个指数的成绩受轮动影响，参考意义有限。`) : null,
    d.portfolio?.health ? healthBox(d.portfolio) : null,
    h('div', { class: 'tbl-wrap mt' }, h('table', {},
      h('thead', {}, h('tr', {}, ['指数 / 对应 ETF', '收盘', '20 日', '规则仓位', '趋势仓', '抄底仓', '明天开盘（高开低开都一样）', '样本外：规则 vs 持有'].map((x, k) => h('th', { class: k > 0 && k < 4 ? 'r' : '' }, x)))),
      h('tbody', {}, d.indices.map(i => indexRow(i)))))));

  const vb = valuationBlock(d);                                            // 指数估值与分批建仓参考（A 股）
  if (vb) out.append(vb);

  // ③ 每个指数：权益曲线、样本内外、最近交易
  for (const i of d.indices) out.append(indexDetail(i, charts));

  // ④ 历史证据
  const ev = d.evidence;
  const xName = ev.extra_index ? idxName(d, ev.extra_index) : null;       // 再多列一个指数（A股 = 创业板指，美股 = 纳斯达克100）
  const evT = hz => h('div', { class: 'tbl-wrap' }, h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, ['温度区间', '天数', `${idxName(d, ev.index)} 平均`, '上涨概率', ...(xName ? [`${xName} 平均`, '上涨概率'] : []), '全A等权 平均', '上涨概率'].map((x, k) => h('th', { class: k ? 'r' : '' }, x)))),
    h('tbody', {}, ev.horizons[hz].map(r => h('tr', { class: r.zone === t.zone ? 'hl' : '' }, h('td', {}, zonePill(r.zone, r.name)), h('td', { class: 'num r' }, r.days),
      h('td', { class: 'num r ' + dirClass(r.idx_mean) }, fmtPct(r.idx_mean, 1)), h('td', { class: 'num r' }, pct0(r.idx_win)),
      ...(xName ? [h('td', { class: 'num r ' + dirClass(r.x2_mean) }, fmtPct(r.x2_mean, 1)), h('td', { class: 'num r' }, pct0(r.x2_win))] : []),
      h('td', { class: 'num r ' + dirClass(r.ew_mean) }, fmtPct(r.ew_mean, 1)), h('td', { class: 'num r' }, pct0(r.ew_win)))))));
  out.append(h('div', { class: 'card mt' },
    h('div', { class: 'card-title' }, h('h3', {}, '历史证据：处在各温度区间之后的涨跌'), h('span', { class: 'hint' }, `${ev.from} ~ ${ev.to}`)),
    h('div', { class: 'small muted' }, '之后 20 个交易日'), evT('20'), h('div', { class: 'small muted mt' }, '之后 60 个交易日'), evT('60'),
    h('p', { class: 'hint mt-s' }, state.market === 'US'
      ? '读法（美股）：近 10 年美股的「逆向」没有 A 股明显——冰点（普跌）之后确实更容易涨（60 天平均约 +7%），但过热（普涨）之后并没有变差，强势行情常常延续。所以美股的温度只用来判断「恐慌时可以分批」，不要用它判断「过热要卖」。样本只有约 10 年，是统计倾向，不是保证。'
      : '读法（A 股）：市场情绪是「逆向」的——大家都在跌（冰点）之后反而更容易涨，普涨（过热）之后更容易跌。这是统计倾向，不是保证；单次结果可能相反。'),
    h('p', { class: 'hint' }, d.note)));

  // ⑤ 风格轮动
  out.append(h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '风格强弱（按 20 日涨跌）')),
    h('div', { class: 'row wrap' }, d.rotation.map((r, k) => h('span', { class: 'tag ' + (k === 0 ? '' : 'gray') }, `${k + 1}. ${r.name}（${r.style}）20日 ${fmtPct(r.ret_20d, 1)} · 60日 ${fmtPct(r.ret_60d, 1)}`))),
    h('p', { class: 'hint mt-s' }, '排在前面的风格近期更强。只作参考：轮动规则没有单独回测验证。')));
  api.el = out;
  return api;
}

/** 策略健康度 + 分年度（压力测试）：让人提前知道「熊市少亏很多、牛市会明显跑输」。 */
function healthBox(pf) {
  const hl = pf.health;
  const cls = hl.status === '警告' ? 'bad' : hl.status === '注意' ? 'warn' : 'ok';
  return h('div', { class: 'grid c2 mt-s', style: 'align-items:start' },
    h('div', { class: 'card soft' }, h('div', { class: 'card-title' }, h('h3', {}, '策略健康度'), h('span', { class: 'badge ' + cls }, hl.status)),
      h('div', { class: 'grid c2' },
        h('div', { class: 'kv' }, h('span', { class: 'k' }, '当前回撤（距最高点）'), h('span', { class: 'v num' }, fmtPct(hl.current_dd, 1))),
        h('div', { class: 'kv' }, h('span', { class: 'k' }, `10 年最大回撤（${hl.max_dd_date}）`), h('span', { class: 'v num' }, fmtPct(hl.max_dd, 1)))),
      h('p', { class: 'small mt-s' }, hl.text),
      h('p', { class: 'tiny muted' }, '超过历史最大回撤 = 出现了回测里没见过的情况，是规则可能失效的信号。')),
    h('div', { class: 'card soft' }, h('div', { class: 'card-title' }, h('h3', {}, '分年度：规则 vs 一直持有'), h('span', { class: 'hint' }, `${pf.n} 个宽基等分`)),
      h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, ['年份', '规则', '规则回撤', '持有', '持有回撤'].map((x, k) => h('th', { class: k ? 'r' : '' }, x)))),
        h('tbody', {}, pf.by_year.map(y => h('tr', {}, h('td', {}, y.year + (y.partial ? '*' : '')),
          h('td', { class: 'num r ' + dirClass(y.rule) }, fmtPct(y.rule, 1)), h('td', { class: 'num r' }, fmtPct(y.rule_dd, 1)),
          h('td', { class: 'num r muted' }, fmtPct(y.hold, 1)), h('td', { class: 'num r muted' }, fmtPct(y.hold_dd, 1)))))),
      h('p', { class: 'tiny muted mt-s' }, '规则的特点：熊市（2018、2022）少亏很多，牛市（2019、2020、2025）明显跑输。这是用收益换平稳，不是失效。* = 不满一年。')));
}

function idxName(d, sym) { return (d.indices.find(i => i.symbol === sym) || {}).name || sym; }

function indexRow(i) {
  const s = i.stats.oos;
  const tr = i.trend, wo = i.washout;
  return h('tr', {},
    h('td', {}, h('b', {}, i.name), h('div', { class: 'tiny muted' }, i.etf, ' · ', i.style)),
    h('td', { class: 'num r' }, fmtNum(i.close, i.close > 1000 ? 0 : 2), h('div', { class: 'tiny ' + dirClass(i.chg_1d) }, fmtPct(i.chg_1d, 2))),
    h('td', { class: 'num r ' + dirClass(i.ret_20d) }, fmtPct(i.ret_20d, 1)),
    h('td', { class: 'r' }, h('span', { class: 'badge ' + (i.position >= 1 ? 'ok' : i.position > 0 ? 'accent' : '') }, fmtPct(i.position, 0, false))),
    h('td', { class: 'small' }, h('span', { class: 'badge ' + (tr.holding ? 'ok' : '') }, tr.holding ? '持有' : '空仓'), ' ',
      tr.rank ? h('span', { class: 'rank-badge' + (tr.rank_ok ? ' ok' : ''), title: `近 20 日强弱第 ${tr.rank} 名；只做前 ${tr.top_k} 名` }, '#' + tr.rank) : null, ' ',
      tr.holding ? h('span', {}, '离场位 ', h('b', { class: 'num' }, fmtNum(tr.exit_level, 0)), h('span', { class: 'tiny muted num' }, ` (${fmtPct(tr.dist_exit, 1)})`))
        : h('span', {}, '买入位 ', h('b', { class: 'num' }, fmtNum(tr.buy_level, 0)), h('span', { class: 'tiny muted num' }, ` (${fmtPct(tr.dist_buy, 1)})`))),
    h('td', { class: 'small' }, h('span', { class: 'badge ' + (wo.holding ? 'ok' : '') }, wo.holding ? '持有' : '等待'), ' ',
      wo.holding ? h('span', {}, '止损 ', h('b', { class: 'num' }, fmtNum(wo.stop, 0)), h('span', { class: 'tiny muted num' }, ` (${fmtPct(wo.dist_stop, 1)}) · 剩 ${wo.days_left} 天`),
        (wo.ref_take_profit || []).length ? h('div', { class: 'tiny muted' }, '参考止盈 ' + wo.ref_take_profit.map(x => `+${Math.round(x.pct * 100)}% ${fmtNum(x.price, 0)}`).join(' · ')) : null)
        : h('span', { class: 'muted' }, `宽度 ${pct0(wo.b20)} → 需 < ${pct0(wo.enter_below)}；触发时止损约 ${fmtNum(wo.stop_if_today, 0)} (${fmtPct(wo.stop_pct_if_today, 1)})`,
          (wo.ref_take_profit || []).length ? `；参考止盈 ${wo.ref_take_profit.map(x => `+${Math.round(x.pct * 100)}% ≈ ${fmtNum(x.price, 0)}`).join(' / ')}` : '')),
    h('td', { class: 'small' }, h('b', {}, i.next_open?.actions?.length ? i.next_open.actions.join('；') : '不操作'),
      i.next_open?.gap_median != null ? h('div', { class: 'tiny muted' }, `近两年开盘跳空中位数 ${fmtPct(i.next_open.gap_median, 2, false)}，超过 1% 的日子 ${fmtPct(i.next_open.gap_gt1, 0, false)}`) : null),
    h('td', { class: 'small num' }, s?.rule?.cagr != null
      ? h('span', {}, `年化 ${fmtPct(s.rule.cagr, 1)} / 回撤 ${fmtPct(s.rule.mdd, 0)}`, h('div', { class: 'tiny muted' }, `持有 ${fmtPct(s.hold.cagr, 1)} / ${fmtPct(s.hold.mdd, 0)}`)) : '—'));
}

function indexDetail(i, charts) {
  const el = h('div', { style: 'height:180px' });
  const st = i.stats;
  const line = (lab, x) => h('tr', {}, h('td', {}, lab, h('div', { class: 'tiny faint' }, x.rule.from ? `${x.rule.from} ~ ${x.rule.to}` : '')),
    h('td', { class: 'num r' }, fmtPct(x.rule.cagr, 1)), h('td', { class: 'num r' }, fmtPct(x.rule.mdd, 1)), h('td', { class: 'num r' }, x.rule.sharpe ?? '—'), h('td', { class: 'num r' }, pct0(x.rule.exposure)),
    h('td', { class: 'num r muted' }, fmtPct(x.hold.cagr, 1)), h('td', { class: 'num r muted' }, fmtPct(x.hold.mdd, 1)), h('td', { class: 'num r muted' }, x.hold.sharpe ?? '—'));
  const REASON = { trend: '趋势仓', washout: '抄底仓' };
  const det = h('details', { class: 'card mt' },
    h('summary', {}, h('b', {}, i.name), ` · ${i.position_text} · `, h('span', { class: 'small muted' }, i.trend.text)),
    h('div', { class: 'small mt-s' }, '抄底仓：', i.washout.text),
    h('div', { class: 'mt-s' }, h('div', { class: 'small', style: 'font-weight:600' }, '明天的情景'), tomorrowTable(i)),
    h('div', { class: 'grid c4 mt-s' },
      h('div', { class: 'kv' }, h('span', { class: 'k' }, 'ATR20（日均波动）'), h('span', { class: 'v num' }, `${fmtNum(i.atr, 0)} 点 · ${fmtPct(i.atr_pct, 2, false)}`)),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, '距 52 周高点'), h('span', { class: 'v num' }, fmtPct(i.dd_52w, 1))),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, '60 日涨跌'), h('span', { class: 'v num ' + dirClass(i.ret_60d) }, fmtPct(i.ret_60d, 1))),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, '年线（250 日）'), h('span', { class: 'v' }, i.above_ma250 == null ? '—' : i.above_ma250 ? '在年线上方' : '在年线下方'))),
    h('div', { class: 'tiny muted mt' }, '权益曲线：规则（蓝） vs 一直持有（灰），已扣成本，信号次日开盘执行'), el,
    h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' },
      h('thead', {}, h('tr', {}, ['区间', '规则 年化', '最大回撤', 'Sharpe', '平均仓位', '持有 年化', '最大回撤', 'Sharpe'].map((x, k) => h('th', { class: k ? 'r' : '' }, x)))),
      h('tbody', {}, line('全部', st.all), line('样本内（选参）', st.is), line('样本外（检验）', st.oos)))),
    h('div', { class: 'small mt-s' }, `抄底仓历史 ${st.washout_trades.n} 笔，胜率 ${pct0(st.washout_trades.win_rate)}，平均每笔 ${fmtPct(st.washout_trades.avg_ret, 1)}`),
    i.recent_trades.length ? h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' },
      h('thead', {}, h('tr', {}, ['仓位', '买入（次日开盘）', '卖出（次日开盘）', '原因', '收益'].map((x, k) => h('th', { class: k === 4 ? 'r' : '' }, x)))),
      h('tbody', {}, i.recent_trades.map(x => h('tr', {}, h('td', {}, REASON[x.sleeve]), h('td', { class: 'num' }, `${x.entry_date || '—'} ${fmtNum(x.entry, 0)}`),
        h('td', { class: 'num' }, x.exit_date ? `${x.exit_date} ${fmtNum(x.exit, 0)}` : '—'), h('td', { class: 'small' }, x.reason), h('td', { class: 'num r ' + dirClass(x.ret) }, fmtPct(x.ret, 1))))))) : null);
  let drawn = false;
  det.addEventListener('toggle', () => {
    if (!det.open || drawn) return;
    drawn = true;
    const c = themeColors();
    const pts = i.curve.map(p => ({ time: p.date, rule: p.rule, hold: p.hold }));
    charts.push(lineChart(el, [{ name: '规则', color: c.accent || '#2457d6', data: pts.map(p => ({ time: p.time, value: p.rule })) },
      { name: '持有', color: '#9a9da3', data: pts.map(p => ({ time: p.time, value: p.hold })) }], { height: 180 }));
  });
  return det;
}
