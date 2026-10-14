// VIX 恐慌指数（美股行情页卡片 + 选股页右侧概览一行）。数据全部来自 /api/market/vix，前端不做计算。
import { get } from './api.js';
import { h, fmtNum, fmtPct, dirClass } from './util.js';
import { lineChart, themeColors } from './chart.js';

export async function loadVix() {
  return get('/market/vix', {}, { cache: false, timeout: 120000 });
}

const ZONE_COLORS = ['#2f7ed8', '#8a94a6', '#e6a23c', '#f0803c', '#e24b4a', '#a4161a'];

/** 选股页右侧概览里的一行摘要（美股）。 */
export function vixBrief(d) {
  if (!d || d.status !== 'ok') return null;
  return h('div', { class: 'km-brief mt' },
    h('div', { class: 'row between' }, h('b', { class: 'small' }, 'VIX 恐慌指数'), h('a', { href: '#/market', class: 'small' }, '详情 →')),
    h('div', { class: 'km-row' }, h('span', { class: 'km-nm num' }, fmtNum(d.value, 2)),
      h('span', { class: 'zone-pill', style: `background:${ZONE_COLORS[d.zone]}` }, d.zone_name),
      h('span', { class: 'tiny muted' }, `较前日 ${d.chg_1d >= 0 ? '+' : ''}${fmtNum(d.chg_1d, 2)} · 10 年分位 ${fmtPct(d.pct_10y, 0, false)}` + (d.term ? ` · ${d.term.name}` : ''))));
}

/** 行情页卡片。返回 {el, init, destroy}。 */
export function vixCard(d) {
  const charts = [];
  const out = h('div', { class: 'card mt vix' });
  out.append(h('div', { class: 'card-title' }, h('h3', {}, 'VIX 恐慌指数'), h('span', { class: 'hint' }, d?.asof ? `数据 ${d.asof} · ${d.source || ''}${d.stale ? '（可能不是最新）' : ''}` : '')));
  if (!d || d.status !== 'ok') { out.append(h('div', { class: 'hint' }, d?.message || '暂不可用')); return { el: out, init() {}, destroy() {} }; }
  const kv = (k, v, sub) => h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num' }, v), sub ? h('span', { class: 'tiny muted' }, sub) : null);
  out.append(h('div', { class: 'grid c4' },
    h('div', { class: 'kv' }, h('span', { class: 'k' }, '当前'), h('span', { class: 'big-num num' }, fmtNum(d.value, 2)),
      h('span', { class: 'zone-pill', style: `background:${ZONE_COLORS[d.zone]};align-self:flex-start` }, d.zone_name)),
    kv('较前一日', h('span', { class: dirClass(-d.chg_1d) }, `${d.chg_1d >= 0 ? '+' : ''}${fmtNum(d.chg_1d, 2)}`), `${fmtPct(d.chg_1d_pct, 1)} · 5 日 ${d.chg_5d >= 0 ? '+' : ''}${fmtNum(d.chg_5d, 2)}`),
    kv('10 年历史分位', fmtPct(d.pct_10y, 0, false), `中位数 ${fmtNum(d.median_10y, 1)}`),
    kv('近一年区间', `${fmtNum(d.lo_1y, 1)} ~ ${fmtNum(d.hi_1y, 1)}`, d.term ? `VIX÷VIX3M = ${fmtNum(d.term.ratio, 2)}` : '')));
  // 分区色带
  out.append(h('div', { class: 'vix-zones mt-s' }, ...d.zones.map((z, i) => h('div', { class: 'vz' + (i === d.zone ? ' on' : ''), style: `background:${ZONE_COLORS[i]}` },
    h('b', {}, z.name), h('span', {}, z.hi == null ? `≥ ${z.lo}` : z.lo === 0 ? `< ${z.hi}` : `${z.lo}~${z.hi}`)))));
  out.append(h('div', { class: 'alert small mt-s ' + (d.zone >= 4 ? 'warn' : '') }, h('b', {}, '怎么读：'), d.read));
  if (d.term) out.append(h('div', { class: 'alert small mt-s ' + (d.term.zone === 2 ? 'warn' : '') }, h('b', {}, `期限结构（VIX ÷ VIX3M = ${fmtNum(d.term.ratio, 2)}）：`), `${d.term.name}。${d.term.text}`,
    d.term.evidence ? h('div', { class: 'tiny muted mt-s' }, d.term.evidence) : null));
  const chart = h('div', { class: 'mt-s' });
  out.append(h('div', { class: 'tiny muted mt' }, '近一年走势（虚线：20 = 略紧张、30 = 恐慌；灰线 = 3 个月期 VIX3M）'), chart);
  const el = { c: null };
  const init = () => {
    const c = themeColors();
    const data = d.history.map(p => ({ time: p.date, value: p.vix }));
    const ser = [{ name: 'VIX', color: '#e24b4a', data }];
    const v3 = d.history.filter(p => p.vix3m != null).map(p => ({ time: p.date, value: p.vix3m }));
    if (v3.length) ser.push({ name: 'VIX3M', color: c.flat || '#8a94a6', data: v3 });
    const ch = lineChart(chart, ser, { height: 200 });
    const s = ch.chart.addLineSeries({ color: 'rgba(0,0,0,0)', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    s.setData(data);
    for (const [v, col] of [[20, '#e6a23c'], [30, '#e24b4a']]) s.createPriceLine({ price: v, color: col, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: String(v) });
    charts.push(ch);
  };
  if (d.evidence) {
    const un = d.unconditional || {};
    const tbl = h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' },
      h('thead', {}, h('tr', {}, ['VIX 区间', '20 日平均', '20 日上涨', '60 日平均', '60 日上涨', '60 日最差', '独立次数'].map((t, k) => h('th', { class: k ? 'r' : '' }, t)))),
      h('tbody', {}, ...(d.evidence_all || []).map((z, i) => h('tr', { class: i === d.zone ? 'hl' : '' },
        h('td', {}, h('span', { class: 'zone-pill', style: `background:${ZONE_COLORS[i]}` }, d.zones[i]?.name), ' ', z.hi >= 900 ? `≥ ${z.lo}` : z.lo === 0 ? `< ${z.hi}` : `${z.lo}~${z.hi}`),
        h('td', { class: 'num r ' + dirClass(z.fwd20.mean) }, fmtPct(z.fwd20.mean, 1)), h('td', { class: 'num r' }, fmtPct(z.fwd20.win, 0, false)),
        h('td', { class: 'num r ' + dirClass(z.fwd60.mean) }, fmtPct(z.fwd60.mean, 1)), h('td', { class: 'num r' }, fmtPct(z.fwd60.win, 0, false)),
        h('td', { class: 'num r down' }, fmtPct(z.fwd60.worst, 0)), h('td', { class: 'num r muted' }, z.episodes))),
        h('tr', {}, h('td', { class: 'muted' }, '所有日子'), h('td', { class: 'num r muted' }, fmtPct(un.fwd20?.mean, 1)), h('td', { class: 'num r muted' }, fmtPct(un.fwd20?.win, 0, false)),
          h('td', { class: 'num r muted' }, fmtPct(un.fwd60?.mean, 1)), h('td', { class: 'num r muted' }, fmtPct(un.fwd60?.win, 0, false)), h('td'), h('td')))));
    out.append(h('div', { class: 'card soft mt' }, h('div', { class: 'card-title' }, h('h3', {}, '历史统计：处在这个区间之后'), h('span', { class: 'hint' }, `${d.evidence_range?.from || ''} ~ ${d.evidence_range?.to || ''}`)),
      h('p', { class: 'small' }, d.evidence_line), tbl,
      h('p', { class: 'hint mt-s' }, '读法：VIX 高（市场恐慌）之后，标普500 的平均表现好于平时，这是历史上「别人恐慌时反而相对安全」的统计倾向；但最差的一次仍是大跌，所以只是倾向，不是保证。VIX 低（平静）不是卖出信号。样本是标普500 价格指数，不含股息。')));
  }
  out.append(h('p', { class: 'hint mt-s' }, d.note));
  return { el: out, init, destroy() { for (const c of charts.splice(0)) c?.destroy?.(); } };
}
