// 关键均线（行情页卡片 + 选股页右侧概览）：创业板指 / 标普500 / 纳斯达克100 的 20 / 60 / 120 / 200 / 250 日均线，
// 每条线的位置、明天收盘到多少算站上 / 跌破、按回测证据给出的操作建议。数据全部来自 /api/market/key_ma，前端不做计算。
import { get } from './api.js';
import { h, fmtNum, fmtPct, dirClass } from './util.js';
import { lineChart, themeColors } from './chart.js';

export async function loadKeyMa() {
  return get('/market/key_ma', {}, { cache: false, timeout: 120000, market: false });
}

const ROLE = { primary: ['主线', 'accent'], actionable: ['辅助', 'ok'], reference: ['仅参考', ''] };
const MA_COLORS = { 20: '#e6a23c', 60: '#7a5af8', 120: '#14b8a6', 200: '#e11d48', 250: '#64748b' };
const px = v => fmtNum(v, v >= 1000 ? 0 : 2);

/** 选股页右侧概览里的三行摘要。 */
export function keyMaBrief(d) {
  if (!d || d.status !== 'ok') return null;
  return h('div', { class: 'km-brief mt' },
    h('div', { class: 'row between' }, h('b', { class: 'small' }, '长线趋势（关键均线）'), h('a', { href: '#/market', class: 'small' }, '每条均线的操作 →')),
    ...d.indices.map(i => i.status !== 'ok' ? h('div', { class: 'tiny muted' }, `${i.name}：${i.message || '暂无数据'}`)
      : h('div', { class: 'km-row' },
        h('span', { class: 'km-nm' }, i.name), h('span', { class: 'badge ' + (i.holding ? 'ok' : 'warn') }, i.holding ? '持有' : '空仓'),
        h('span', { class: 'tiny muted' }, i.headline.replace(/^(持有|空仓等待)：/, '')))));
}

/** 行情页卡片。返回 {el, init, destroy}：图表在展开某个指数时才画（需要容器宽度）。 */
export function keyMaCard(d) {
  const charts = [];
  const out = h('div', { class: 'card mt km' });
  out.append(h('div', { class: 'card-title' }, h('h3', {}, '关键均线：创业板指 / 标普500 / 纳斯达克100'), h('span', { class: 'hint' }, '点开指数看每条均线怎么操作')));
  if (!d || d.status !== 'ok') { out.append(h('div', { class: 'hint' }, d?.message || '暂不可用')); return { el: out, init() {}, destroy() {} }; }
  out.append(h('div', { class: 'alert small' },
    h('b', {}, '怎么读：'), '每条均线都单独做过长历史回测（创业板 2011~、标普 1951~、纳指 1986~，前 60% 挑、后 40% 验）。',
    h('b', {}, '主线'), ' = 按它买卖最好的一条，照它操作；', h('b', {}, '辅助'), ' = 也通过了检验，用来确认；', h('b', {}, '仅参考'),
    ' = 单独按它买卖没有更好，只看不操作。均线只看收盘价：盘中跌破又收回不算。',
    h('br'), h('span', { class: 'muted' }, d.note)));
  for (const i of d.indices) out.append(indexBlock(i, charts));
  return { el: out, init() {}, destroy() { for (const c of charts.splice(0)) c?.destroy?.(); } };
}

function indexBlock(i, charts) {
  if (i.status !== 'ok') return h('div', { class: 'card soft mt-s' }, h('b', {}, i.name), h('div', { class: 'hint' }, i.message || '暂无数据'));
  const det = h('details', { class: 'card soft mt-s km-idx' });
  const chartEl = h('div', { class: 'mt-s' });
  let drawn = false;
  det.addEventListener('toggle', () => {
    if (!det.open || drawn) return;
    drawn = true;
    requestAnimationFrame(() => {
      const c = themeColors();
      const ser = [{ name: '收盘', color: c.accent || '#2457d6', data: i.history.map(p => ({ time: p.date, value: p.close })) }];
      for (const l of i.lines.filter(x => x.role !== 'reference')) {
        ser.push({ name: `${l.n}日`, color: MA_COLORS[l.n], data: i.history.filter(p => p['ma' + l.n] != null).map(p => ({ time: p.date, value: p['ma' + l.n] })) });
      }
      charts.push(lineChart(chartEl, ser, { height: 240 }));
    });
  });
  const p = i.lines.find(l => l.role === 'primary');
  det.append(h('summary', {},
    h('span', { class: 'km-sum' },
      h('b', {}, i.name), h('span', { class: 'num' }, px(i.close)), h('span', { class: 'num small ' + dirClass(i.chg_1d) }, fmtPct(i.chg_1d, 2)),
      h('span', { class: 'badge ' + (i.holding ? 'ok' : 'warn') }, i.holding ? '持有' : '空仓'),
      h('span', { class: 'small km-head' }, i.headline))));
  const ev = i.evidence || {};
  det.append(h('div', { class: 'tiny muted mt-s' }, `数据截至 ${i.asof}（${i.source || ''}${i.stale ? '，可能不是最新' : ''}）· 对应 ETF：${i.etf || '—'}`),
    h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini km-tbl' },
      h('thead', {}, h('tr', {}, ['均线', '数值', '收盘距它', '角色', '明天收盘触发价', '操作建议'].map((t, k) => h('th', { class: k > 0 && k < 3 ? 'r' : '' }, t)))),
      h('tbody', {}, i.lines.map(l => {
        const [rl, rc] = ROLE[l.role];
        const trig = l.role === 'reference' ? (l.above ? `跌破 ${px(l.trigger_below)}` : `站上 ${px(l.trigger_above)}`)
          : l.holding ? `跌破 ${px(l.trigger_below)}（${fmtPct(l.trigger_below_pct, 1)}）` : `站上 ${px(l.trigger_above)}（${fmtPct(l.trigger_above_pct, 1)}）`;
        return h('tr', { class: l.role === 'primary' ? 'hl' : '' },
          h('td', { class: 'c-ma' }, h('b', {}, `${l.n} 日`), l.band ? h('span', { class: 'tiny muted' }, ' ±2%') : null, h('div', { class: 'tiny muted' }, l.alias || '')),
          h('td', { class: 'num r c-val' }, px(l.value), l.slope20 != null ? h('div', { class: 'tiny ' + dirClass(l.slope20) }, l.slope20 >= 0 ? '向上' : '向下') : null),
          h('td', { class: 'num r c-dist ' + dirClass(l.dist) }, fmtPct(l.dist, 1)),
          h('td', { class: 'c-role' }, h('span', { class: 'badge ' + rc }, rl)),
          h('td', { class: 'num small c-trig', dataset: { label: '明天收盘：' } }, trig),
          h('td', { class: 'small km-adv c-adv' }, l.advice, l.backtest ? h('div', { class: 'tiny muted' }, l.backtest) : null));
      })))),
    i.zone ? h('div', { class: 'alert small mt-s ' + (i.zone.tone === '偏强' ? 'ok' : i.zone.tone === '偏弱' ? 'warn' : '') },
      h('b', {}, i.zone.label + '：'), i.zone.text, i.zone.extra ? h('div', { class: 'mt-s' }, h('b', {}, i.zone.extra)) : null) : null,
    h('div', { class: 'tiny muted mt-s' }, '近两年走势：收盘价与通过检验的均线（主线 / 辅助）'), chartEl,
    ev.primary ? h('p', { class: 'hint mt-s' },
      `按主线 ${p ? p.n + ' 日' + (p.band ? '±2%' : '') : ''} 操作的历史成绩（${ev.from} ~ ${ev.to}）：全程年化 ${fmtPct(ev.primary.all.cagr, 1)}、最大回撤 ${fmtPct(ev.primary.all.mdd, 0)}；`,
      `样本外（${(ev.split || '').slice(0, 4)} 年起）${fmtPct(ev.primary.oos.cagr, 1)} / ${fmtPct(ev.primary.oos.mdd, 0)}。`,
      `一直持有：全程 ${fmtPct(ev.hold?.all?.cagr, 1)} / ${fmtPct(ev.hold?.all?.mdd, 0)}，样本外 ${fmtPct(ev.hold?.oos?.cagr, 1)} / ${fmtPct(ev.hold?.oos?.mdd, 0)}。每年换仓约 ${ev.primary.switches_per_year} 次。`,
      i.market === 'US' ? ' 美股收盘在北京时间早上：看到跌破 / 站上，当天在 A 股买卖对应的 QDII ETF；QDII 常有溢价，买前先看盘中参考净值（IOPV），溢价明显时别追。' : '') : null);
  return det;
}
