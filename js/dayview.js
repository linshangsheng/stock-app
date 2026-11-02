// 日线看盘辅助（个股详情里的两张卡）：「价格在哪儿」（现价在一年区间、均线之间的位置）和「相对大盘」（个股 ÷ 大盘的强弱线）。
// 数据来自 /api/kline 的 position / rs（server/daily_view.py）；前端只做展示。两样都是看盘辅助，没做过回测，不参与任何买卖信号。
import { h, fmtPrice, fmtPct, dirClass, evBadge } from './util.js';

const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

/** 一条横轴：左端 = 52 周最低，右端 = 52 周最高；现价、MA20 / MA50 / MA200 按价格落在轴上。标签上下交错，免得挤在一起。 */
export function positionCard(p) {
  if (!p) return null;
  const at = v => clamp((v - p.lo52) / (p.hi52 - p.lo52), 0, 1) * 100;
  const marks = [['MA200', p.ma200, 'ma200'], ['MA50', p.ma50, 'ma50'], ['MA20', p.ma20, 'ma20']].filter(m => m[1] != null);
  const ticks = marks.map(([name, v, cls]) => ({ name, v, cls })).sort((a, b) => a.v - b.v);                    // 均线：标签画在轴下面，分两行交错；现价单独画在轴上面
  const axis = h('div', { class: 'pos-axis' }, h('div', { class: 'pos-bar' }),
    ...[...ticks.map((t, i) => ({ ...t, row: i % 2 ? 'r2' : 'r1' })), { name: '现价', v: p.close, cls: 'now', row: 'top' }].map(t => h('div', { class: 'pos-tick ' + t.cls + ' ' + t.row, style: `left:${at(t.v).toFixed(1)}%`,
      title: `${t.name} ${fmtPrice(t.v)}${t.v < p.lo52 || t.v > p.hi52 ? '（在一年区间之外，画在端点上）' : ''}` }, h('i'), h('span', {}, t.name))));
  const cell = (k, v, tip, cls = '') => h('div', { class: 'kv', title: tip || '' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v));
  const dist = (k, v) => cell('距 ' + k, v == null ? '—' : fmtPct(v, 1), v == null ? '数据不足 ' + k.replace('MA', '') + ' 根' : `现价比 ${k} ${v >= 0 ? '高' : '低'} ${Math.abs(v * 100).toFixed(1)}%`, dirClass(v));
  const pct0 = v => Math.round(v * 100) + '%';
  return h('div', { class: 'card soft mt pos-card' },
    h('div', { class: 'card-title' }, h('h3', {}, '价格在哪儿'),
      evBadge('无', '看盘辅助：只是把现价放进一年的区间和均线之间，方便一眼看出它偏高还是偏低、离均线多远。没有回测过，不是买卖信号。')),
    h('div', { class: 'pos-ends num' }, h('span', {}, '52 周低 ', h('b', {}, fmtPrice(p.lo52))), h('span', {}, '52 周高 ', h('b', {}, fmtPrice(p.hi52)))),
    axis,
    h('div', { class: 'small mt-s' }, `现价处在一年区间的 `, h('b', {}, pct0(p.pos52)), ` 的位置（0% = 最低点，100% = 最高点）；过去一年里有 `, h('b', {}, pct0(p.rank_1y)), ` 的交易日收盘价不高于现在。`),
    h('div', { class: 'ind-grid mt-s' }, dist('MA20', p.dist_ma20), dist('MA50', p.dist_ma50), dist('MA200', p.dist_ma200),
      cell('距 52 周高', fmtPct(p.dist_hi52, 1), `${p.days_since_high} 个交易日前创的高点`, dirClass(p.dist_hi52)),
      cell('距 52 周低', fmtPct(p.dist_lo52, 1), `${p.days_since_low} 个交易日前创的低点`, dirClass(p.dist_lo52)),
      cell('高点是几天前', p.days_since_high === 0 ? '今天' : p.days_since_high + ' 天前', '近一年最高价出现在多少个交易日之前')));
}

const RS_TEXT = {
  strong_up: ['比大盘强，最近还在走强', '近 60 个交易日跑赢大盘，强弱线还在自己的均线上方。'],
  strong_pause: ['比大盘强，但最近在回调', '近 60 个交易日整体跑赢大盘，但最近一小段在回落，强弱线跌到了自己的均线下面。'],
  weak_bounce: ['比大盘弱，最近有反弹', '近 60 个交易日整体跑输大盘，最近有所回升，但还没有扭转。'],
  weak_down: ['持续弱于大盘', '近 60 个交易日跑输大盘，最近还在走弱。'],
};

/** 相对大盘卡片：一句话判断 + 近 20 / 60 日相对大盘变化；曲线在主图下方「相对大盘」标签里画。 */
export function rsCard(rs, benchName, showChart, period = 'D') {
  if (!rs) return null;
  const unit = period === 'W' ? '周' : '个交易日', u2 = period === 'W' ? '周' : '日';
  const [head, body0] = RS_TEXT[rs.state] || ['', '数据还不够判断。'];
  const body = body0.replaceAll('个交易日', unit);
  const cell = (k, v, tip) => h('div', { class: 'kv', title: tip }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + dirClass(v) }, v == null ? '—' : fmtPct(v, 1)));
  return h('div', { class: 'card soft mt' },
    h('div', { class: 'card-title' }, h('h3', {}, `相对大盘（${benchName}）`),
      evBadge('无', `看盘辅助：个股收盘价 ÷ ${benchName}。线往上 = 比大盘强，哪怕股价本身在跌；线往下 = 在跑输大盘。没有回测过，不是买卖信号。美股用的是未调整分红的指数 / ETF 价格，差别很小。`)),
    head ? h('div', { class: 'act-b' }, head) : null,
    h('div', { class: 'small muted' }, body),
    h('div', { class: 'grid c3 mt-s' }, cell(`近 20 ${u2}（强弱线涨跌）`, rs.chg_20, '强弱线 = 个股 ÷ 大盘；大于 0 表示这段时间跑赢大盘'), cell(`近 60 ${u2}`, rs.chg_60, `强弱线近 60 ${unit}的涨跌`),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, '曲线'), h('a', { href: '#', class: 'v', onclick: e => { e.preventDefault(); showChart(); } }, '在主图下方看 →'))));
}
