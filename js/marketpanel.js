// 市场温度 + 宽基指数 / ETF 择时（选股页摘要、行情页完整版共用）。数据全部来自 /api/market/view，前端不做计算。
// 「温度」= 全A站上 20 日线的股票比例：A 股市场级信号是逆向的——冰点之后历史上涨概率最高，过热之后最低（见后端 market_view.py）。
import { get } from './api.js';
import { h, clear, fmtPct, fmtNum, dirClass } from './util.js';
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
    `个股 ${money(al.stock_amount)}（最多同时 ${al.stock_max_positions} 只，每笔最多亏 ${money(al.equity * al.stock_risk_per_trade)}）`) : null;
  return h('div', { class: 'mkt-brief' },
    h('div', { class: 'row between' },
      h('div', { class: 'row', style: 'gap:8px' }, h('span', { class: 'k' }, '市场温度'), zonePill(t.zone, t.zone_name),
        h('span', { class: 'num small', title: '全A站上 20 日线的股票比例' }, pct0(t.b20))),
      h('a', { href: '#/market', class: 'small' }, '指数买入位 / 止损位 →')),
    gauge(t.b20, t.cuts),
    h('div', { class: 'tiny muted mt-s' }, t.hint),
    allocLine,
    h('table', { class: 'mini mt-s' }, h('thead', {}, h('tr', {}, h('th', { title: '#名次 = 近 20 日涨幅强弱；趋势仓只做前 2 名。点指数名展开明天的情景' }, '宽基 / ETF（点开）'), h('th', { class: 'r' }, '今日'), h('th', { class: 'r' }, '规则仓位'), h('th', {}, '趋势仓下一步'),
      h('th', { title: '指数规则只看收盘：明天高开还是低开都不改变信号' }, '明天开盘'))),
      h('tbody', {}, rows)));
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
      d.allocation ? h('div', { class: 'mt-s' }, h('b', {}, '按你的资金：'), `宽基 ETF 一共 ${money(d.allocation.etf_amount)}，每个指数 ${money(d.allocation.per_index)}（趋势仓 ${money(d.allocation.trend_amount)}、抄底仓 ${money(d.allocation.washout_amount)}）；个股 ${money(d.allocation.stock_amount)}，最多同时 ${d.allocation.stock_max_positions} 只。`) : null),
    d.portfolio?.oos ? h('div', { class: 'alert ok mt-s small' }, h('b', {}, `${d.portfolio.n} 个宽基等分资金的组合（规则要整体看）：`),
      `样本外 ${d.portfolio.oos.rule.from} ~ ${d.portfolio.oos.rule.to}：规则 年化 ${fmtPct(d.portfolio.oos.rule.cagr, 1)}、最大回撤 ${fmtPct(d.portfolio.oos.rule.mdd, 1)}、Sharpe ${d.portfolio.oos.rule.sharpe}；`,
      `一直持有 ${fmtPct(d.portfolio.oos.hold.cagr, 1)}、${fmtPct(d.portfolio.oos.hold.mdd, 1)}、${d.portfolio.oos.hold.sharpe}。单个指数的成绩受轮动影响，参考意义有限。`) : null,
    d.portfolio?.health ? healthBox(d.portfolio) : null,
    h('div', { class: 'tbl-wrap mt' }, h('table', {},
      h('thead', {}, h('tr', {}, ['指数 / 对应 ETF', '收盘', '20 日', '规则仓位', '趋势仓', '抄底仓', '明天开盘（高开低开都一样）', '样本外：规则 vs 持有'].map((x, k) => h('th', { class: k > 0 && k < 4 ? 'r' : '' }, x)))),
      h('tbody', {}, d.indices.map(i => indexRow(i)))))));

  // ③ 每个指数：权益曲线、样本内外、最近交易
  for (const i of d.indices) out.append(indexDetail(i, charts));

  // ④ 历史证据
  const ev = d.evidence;
  const evT = hz => h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, ['温度区间', '天数', `${idxName(d, ev.index)} 平均`, '上涨概率', '全A等权 平均', '上涨概率'].map((x, k) => h('th', { class: k ? 'r' : '' }, x)))),
    h('tbody', {}, ev.horizons[hz].map(r => h('tr', { class: r.zone === t.zone ? 'hl' : '' }, h('td', {}, zonePill(r.zone, r.name)), h('td', { class: 'num r' }, r.days),
      h('td', { class: 'num r ' + dirClass(r.idx_mean) }, fmtPct(r.idx_mean, 1)), h('td', { class: 'num r' }, pct0(r.idx_win)),
      h('td', { class: 'num r ' + dirClass(r.ew_mean) }, fmtPct(r.ew_mean, 1)), h('td', { class: 'num r' }, pct0(r.ew_win))))));
  out.append(h('div', { class: 'card mt' },
    h('div', { class: 'card-title' }, h('h3', {}, '历史证据：处在各温度区间之后的涨跌'), h('span', { class: 'hint' }, `${ev.from} ~ ${ev.to}`)),
    h('div', { class: 'grid c2' }, h('div', {}, h('div', { class: 'small muted' }, '之后 20 个交易日'), evT('20')), h('div', {}, h('div', { class: 'small muted' }, '之后 60 个交易日'), evT('60'))),
    h('p', { class: 'hint mt-s' }, '读法：A 股的市场情绪是「逆向」的——大家都在跌（冰点）之后反而更容易涨，普涨（过热）之后更容易跌。这是统计倾向，不是保证；单次结果可能相反。'),
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
