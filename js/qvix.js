// A 股恐慌指数（期权隐含波动率 QVIX：沪深300ETF、创业板ETF 期权）——A 股行情页卡片。数据来自 /api/market/qvix，前端不做计算。
import { get } from './api.js';
import { h, fmtNum, fmtPct, dirClass } from './util.js';
import { lineChart } from './chart.js';

export async function loadQvix() {
  return get('/market/qvix', {}, { cache: false, timeout: 120000 });
}

const ZONE_COLORS = ['#2f7ed8', '#8a94a6', '#e6a23c', '#f0803c', '#e24b4a', '#a4161a'];
const LINE_COLORS = ['#e24b4a', '#f0803c'];
const rng = z => (z.hi == null ? `≥ ${z.lo}` : z.lo === 0 ? `< ${z.hi}` : `${z.lo}~${z.hi}`);

/** 行情页卡片。onRetry：还在下载历史时，过一会儿重新加载。返回 {el, init, destroy}。 */
export function qvixCard(d, onRetry) {
  const charts = [];
  const out = h('div', { class: 'card mt vix' });
  out.append(h('div', { class: 'card-title' }, h('h3', {}, 'A 股恐慌指数（期权隐含波动率 QVIX）'),
    h('span', { class: 'hint' }, d?.status === 'ok' ? `数据 ${d.series[0]?.asof || ''} · ${d.source}${d.stale ? '（可能不是最新）' : ''}` : '')));
  if (!d || d.status !== 'ok') {
    out.append(h('div', { class: 'hint' }, d?.status === 'loading' ? h('span', {}, h('span', { class: 'spinner' }), ' ', d.message) : (d?.message || '暂不可用')));
    let t = null;
    if (d?.status === 'loading' && onRetry) t = setTimeout(onRetry, 30000);
    return { el: out, init() {}, destroy() { clearTimeout(t); } };
  }
  const kv = (k, v, sub) => h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num' }, v), sub ? h('span', { class: 'tiny muted' }, sub) : null);
  for (const s of d.series) {
    out.append(h('div', { class: 'qv-row mt-s' },
      h('div', { class: 'grid c4' },
        h('div', { class: 'kv' }, h('span', { class: 'k' }, `${s.name}恐慌指数`), h('span', { class: 'big-num num' }, fmtNum(s.value, 2)),
          s.zone_name ? h('span', { class: 'zone-pill', style: `background:${ZONE_COLORS[s.zone]};align-self:flex-start` }, s.zone_name) : null),
        kv('较前一日', h('span', { class: dirClass(-s.chg_1d) }, `${s.chg_1d >= 0 ? '+' : ''}${fmtNum(s.chg_1d, 2)}`), `${fmtPct(s.chg_1d_pct, 1)} · 5 日 ${s.chg_5d >= 0 ? '+' : ''}${fmtNum(s.chg_5d, 2)}`),
        kv('自身历史分位', fmtPct(s.pct_all, 0, false), `${s.since} 起 · 中位数 ${fmtNum(s.median, 1)}`),
        kv('近一年区间', `${fmtNum(s.lo_1y, 1)} ~ ${fmtNum(s.hi_1y, 1)}`)),
      s.zones ? h('div', { class: 'vix-zones mt-s' }, ...s.zones.map((z, i) => h('div', { class: 'vz' + (i === s.zone ? ' on' : ''), style: `background:${ZONE_COLORS[i]}` },
        h('b', {}, z.name), h('span', {}, rng(z))))) : null,
      s.read ? h('div', { class: 'alert small mt-s ' + (s.zone >= 4 ? 'warn' : '') }, h('b', {}, `${s.name}：`), s.read) : null));
  }
  const chart = h('div', { class: 'mt-s' });
  out.append(h('div', { class: 'tiny muted mt' }, '近一年走势（红 = 沪深300、橙 = 创业板）'), chart);
  const init = () => {
    const ser = d.series.map((s, i) => ({ name: s.name, color: LINE_COLORS[i % 2], data: s.history.map(p => ({ time: p.date, value: p.v })) }));
    charts.push(lineChart(chart, ser, { height: 200 }));
  };
  const tables = d.series.filter(s => s.evidence_all?.length).map(s => {
    const un = s.unconditional || {};
    return h('div', {}, h('div', { class: 'small', style: 'font-weight:600' }, `${s.name}恐慌指数 → 之后${s.symbol === 'QVIXCYB' ? '创业板指' : '沪深300'}的涨跌`),
      s.evidence_line ? h('p', { class: 'tiny muted' }, s.evidence_line) : null,
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'mini' },
        h('thead', {}, h('tr', {}, ['区间', '20 日平均', '20 日上涨', '60 日平均', '60 日上涨', '次数'].map((t, k) => h('th', { class: k ? 'r' : '' }, t)))),
        h('tbody', {}, ...s.evidence_all.map((z, i) => h('tr', { class: i === s.zone ? 'hl' : '' },
          h('td', {}, h('span', { class: 'zone-pill', style: `background:${ZONE_COLORS[i]}` }, s.zones[i].name), ' ', rng(s.zones[i])),
          h('td', { class: 'num r ' + dirClass(z.fwd20.mean) }, z.fwd20.n ? fmtPct(z.fwd20.mean, 1) : '—'), h('td', { class: 'num r' }, z.fwd20.n ? fmtPct(z.fwd20.win, 0, false) : '—'),
          h('td', { class: 'num r ' + dirClass(z.fwd60.mean) }, z.fwd60.n ? fmtPct(z.fwd60.mean, 1) : '—'), h('td', { class: 'num r' }, z.fwd60.n ? fmtPct(z.fwd60.win, 0, false) : '—'),
          h('td', { class: 'num r muted' }, z.episodes))),
          h('tr', {}, h('td', { class: 'muted' }, '所有日子'), h('td', { class: 'num r muted' }, fmtPct(un.fwd20?.mean, 1)), h('td', { class: 'num r muted' }, fmtPct(un.fwd20?.win, 0, false)),
            h('td', { class: 'num r muted' }, fmtPct(un.fwd60?.mean, 1)), h('td', { class: 'num r muted' }, fmtPct(un.fwd60?.win, 0, false)), h('td'))))));
  });
  if (tables.length) out.append(h('div', { class: 'card soft mt' }, h('div', { class: 'card-title' }, h('h3', {}, '历史统计：处在这个区间之后')),
    h('div', { class: 'grid c2' }, ...tables),
    h('p', { class: 'hint mt-s' }, '读法：样本只有沪深300ETF 期权 2019-12、创业板ETF 期权 2022-09 以来的几年，进入「恐慌」「极度恐慌」的次数很少，平均数受一两次行情影响很大——只作参考，不要当成规律。')));
  out.append(h('p', { class: 'hint mt-s' }, d.note));
  return { el: out, init, destroy() { for (const c of charts.splice(0)) c?.destroy?.(); } };
}
