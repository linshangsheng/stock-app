// 美股关键统计（Yahoo Key Statistics）：个股详情里的「关键统计」卡片 + 持仓 / 自选的「基本面」对比表。
// 数据来自 /api/keystats 与 /api/keystats/batch；解读文字由后端按数据生成（只描述数据，不给买卖建议）。
import { get } from './api.js';
import { h, clear, fmtNum, fmtPct, fmtPrice, dirClass, code } from './util.js';
import { lineChart, themeColors } from './chart.js';

const pc = (v, d = 1) => (v == null ? '—' : (v * 100).toFixed(d) + '%');
const pcs = (v, d = 1) => (v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(d) + '%');
const big = v => (v == null ? '—' : Math.abs(v) >= 1e12 ? (v / 1e12).toFixed(2) + ' 万亿' : Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(1) + ' 亿' : Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + ' 万' : v.toFixed(0));
const x2 = v => (v == null ? '—' : v.toFixed(2));
const x1 = v => (v == null ? '—' : v.toFixed(1));
const LV = { good: ['ok', '偏好'], warn: ['warn', '留意'], neutral: ['', '中性'], na: ['', '无数据'] };

/** 个股详情里的「关键统计」卡片：自动加载。返回 {el, destroy}。 */
export function keystatsCard(symbol) {
  const box = h('div', { class: 'card ks' });
  const charts = [];
  box.append(h('div', { class: 'row between' }, h('h3', {}, '关键统计（Yahoo Key Statistics）'), h('span', { class: 'hint' }, h('span', { class: 'spinner' }), ' 加载中（每只股票首次约 15~25 秒，之后缓存 24 小时）…')));
  (async () => {
    let d;
    try { d = await get('/keystats', { symbol }, { timeout: 120000 }); } catch (e) {
      clear(box); box.append(h('h3', {}, '关键统计'), h('div', { class: 'alert bad small mt-s' }, e.message),
        h('button', { class: 'btn sm mt-s', onclick: () => { const n = keystatsCard(symbol); box.replaceWith(n.el); } }, '重试'));
      return;
    }
    if (!box.isConnected) return;
    clear(box);
    render(box, d, charts, symbol);
  })();
  return { el: box, destroy() { for (const c of charts.splice(0)) c?.destroy?.(); } };
}

function render(box, d, charts, symbol) {
  const s = d.stats, a = d.analysis || {};
  box.append(h('div', { class: 'row between' }, h('h3', {}, '关键统计'),
    h('span', { class: 'hint' }, `更新于 ${(d.fetched_at || '').replace('T', ' ')}${d.stale ? '（上游失败，用的是旧数据）' : ''} · `,
      h('a', { href: '#', onclick: e => { e.preventDefault(); const n = keystatsCard(symbol); box.replaceWith(n.el); } }, '强制刷新'))));
  if (a.flags?.length) box.append(h('div', { class: 'alert warn small mt-s' }, h('b', {}, '需要留意：'), a.flags.join('；') + '。'));

  // 四个重点
  const grid = h('div', { class: 'ks-focus mt-s' });
  for (const k of ['valuation', 'margins', 'growth', 'cashflow']) {
    const f = a[k]; if (!f) continue;
    const [cls, lab] = LV[f.level] || LV.na;
    grid.append(h('div', { class: 'ks-block' },
      h('div', { class: 'row between' }, h('b', { class: 'small' }, f.title), h('span', { class: 'badge ' + cls }, lab)),
      h('div', { class: 'ks-head' }, f.headline),
      f.bullets?.length ? h('ul', { class: 'ks-ul' }, ...f.bullets.map(b => h('li', {}, b))) : null));
  }
  box.append(grid);

  // Forward P/E 历史
  const hist = d.fwd_pe_history || [];
  if (hist.length >= 2) {
    const chart = h('div', { class: 'mt-s' });
    box.append(h('div', { class: 'tiny muted mt' }, `Forward P/E 历史（${hist[0].date} ~ ${hist[hist.length - 1].date}，${hist.length} 个记录点）`), chart);
    requestAnimationFrame(() => {
      const c = themeColors();
      const data = hist.map(p => ({ time: p.date, value: p.value }));
      if (s.forward_pe) data.push({ time: new Date().toISOString().slice(0, 10), value: s.forward_pe });
      const seen = new Set();
      const uniq = data.filter(p => !seen.has(p.time) && seen.add(p.time));
      if (box.isConnected) charts.push(lineChart(chart, [{ name: 'Forward P/E', color: c.accent || '#2457d6', data: uniq }], { height: 150 }));
    });
    const ch = a.valuation?.changes || {};
    if (Object.keys(ch).length) box.append(h('div', { class: 'row wrap small mt-s' }, h('span', { class: 'muted' }, '现在相对：'),
      ...Object.entries(ch).map(([lab, v]) => h('span', { class: 'tag gray' }, `${lab}（${v.date}）${x1(v.value)} → ${pcs(v.chg, 0)}`))));
  }
  // 估值季度表
  const vt = (d.valuation_table || []).filter(r => r.forward_pe != null || r.trailing_pe != null);
  if (vt.length) box.append(h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' },
    h('thead', {}, h('tr', {}, ['估值（季度末）', ...vt.map(r => (r.period === 'current' ? '当前' : r.period))].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
    h('tbody', {}, [['Forward P/E', 'forward_pe', x1], ['过去 12 个月 P/E', 'trailing_pe', x1], ['PEG', 'peg', x2], ['市净率 P/B', 'price_to_book', x2]].map(([lab, k, f]) =>
      h('tr', {}, h('td', {}, lab), ...vt.map(r => h('td', { class: 'num r' }, f(r[k])))))))));

  // 季度表
  const qs = d.quarters || [];
  if (qs.length) box.append(h('div', { class: 'tbl-wrap mt' }, h('table', { class: 'mini' },
    h('thead', {}, h('tr', {}, ['季度', '营收', '营业利润率', '净利率', '自由现金流'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
    h('tbody', {}, [...qs].reverse().map(q => h('tr', {}, h('td', { class: 'num' }, q.date), h('td', { class: 'num r' }, big(q.revenue)), h('td', { class: 'num r' }, pc(q.operating_margin)),
      h('td', { class: 'num r' }, pc(q.net_margin)), h('td', { class: 'num r ' + dirClass(q.free_cashflow) }, big(q.free_cashflow))))))));

  // 全部指标（按你要的清单）
  const range = s.low_52w != null && s.high_52w != null ? `${fmtPrice(s.low_52w)} ~ ${fmtPrice(s.high_52w)}` + (a.range_pos != null ? `（现价在区间的 ${(a.range_pos * 100).toFixed(0)}% 位置）` : '') : '—';
  const ma = (v, rel) => (v == null ? '—' : `${fmtPrice(v)}${rel != null ? `（现价${rel >= 0 ? '高于' : '低于'} ${Math.abs(rel * 100).toFixed(1)}%）` : ''}`);
  const groups = [
    ['估值与价格', [['现价', fmtPrice(s.price)], ['Forward P/E', x1(s.forward_pe)], ['PEG', x2(s.peg)], ['市净率 P/B', x2(s.price_to_book)], ['52 周区间', range],
      ['50 日均线', ma(s.ma50, a.vs_ma50)], ['200 日均线', ma(s.ma200, a.vs_ma200)]]],
    ['盈利能力', [['营业利润率', pc(s.operating_margin)], ['净利率', pc(s.profit_margin)], ['ROE', pc(s.roe)]]],
    ['增长（最近一季同比）', [['季度营收同比', pcs(s.revenue_growth_q)], ['季度盈利同比', pcs(s.earnings_growth_q)]]],
    ['现金流（近 12 个月）', [['经营现金流', big(s.operating_cashflow)], ['自由现金流', big(s.free_cashflow)]]],
    ['资产负债', [['总现金', big(s.total_cash)], ['总负债', big(s.total_debt)], ['流动比率', x2(s.current_ratio)]]],
    ['市场', [['做空比例（占流通股）', pc(s.short_pct_float)], ['股息率', pc(s.dividend_yield, 2)]]],
  ];
  box.append(h('details', { class: 'mt' }, h('summary', {}, '全部指标'),
    h('div', { class: 'ks-all mt-s' }, ...groups.map(([t, rows]) => h('div', { class: 'ks-grp' }, h('div', { class: 'tiny muted' }, t),
      ...rows.map(([k, v]) => h('div', { class: 'ks-kv' }, h('span', {}, k), h('b', { class: 'num' }, v))))))));
  box.append(h('p', { class: 'hint mt-s' }, '数据来自 Yahoo Finance，可能有延迟或口径差异；Forward P/E 用的是分析师盈利预期，预期会被上调或下调。解读只描述数据（与自己的历史、与上一季度比较），不是买卖建议，也不进入选股。金额单位：美元。'));
}

/** 持仓 / 自选的「基本面」列表：每只股票一张紧凑卡片（窄列放得下）。symbols 为美股代码，names 为 {symbol: 名称}，onPick(symbol) 点卡片打开详情。返回 {el, destroy}。 */
export function keystatsTable(symbols, names, onPick) {
  const box = h('div', { class: 'ks-table' });
  let alive = true, timer = null, tries = 0;
  if (!symbols.length) { box.append(h('div', { class: 'empty' }, '还没有美股股票。')); return { el: box, destroy() {} }; }
  const list = h('div', {});
  const status = h('div', { class: 'hint', style: 'padding:8px 16px' });
  box.append(status, list);
  const cell = (k, v, cls = '', sub = null) => h('div', { class: 'ks-c' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v), sub);
  async function load() {
    let d;
    try { d = await get('/keystats/batch', { symbols: symbols.join(',') }, { cache: false, timeout: 60000 }); } catch (e) { if (alive) status.textContent = e.message; return; }
    if (!alive) return;
    clear(list);
    for (const s of symbols) {
      const r = d.rows[s];
      const nm = names[s] || r?.name || code(s);
      if (!r) {
        list.append(h('button', { class: 'list-item', onclick: () => onPick?.(s) }, h('div', { class: 't1' }, h('span', { class: 'name' }, nm), h('span', { class: 'code num' }, code(s))),
          h('div', { class: 't2 small muted' }, d.failed?.[s] ? '取数失败：' + d.failed[s] : d.pending.includes(s) ? h('span', {}, h('span', { class: 'spinner' }), ' 正在从 Yahoo 加载…') : '—')));
        continue;
      }
      const chg = r.fwd_pe_chg;
      list.append(h('button', { class: 'list-item ks-card', onclick: () => onPick?.(s) },
        h('div', { class: 't1' }, h('span', { class: 'name' }, nm), h('span', { class: 'code num' }, code(s)), r.stale ? h('span', { class: 'badge', title: '缓存已超过 24 小时，正在后台刷新' }, '旧') : null,
          h('span', { class: 'grow' }), h('span', { class: 'num', style: 'font-weight:600' }, fmtPrice(r.price)),
          r.flags.length ? h('span', { class: 'badge warn', title: r.flags.join('\n') }, `留意 ${r.flags.length}`) : null),
        h('div', { class: 'ks-grid' },
          cell('Forward P/E', x1(r.forward_pe), '', chg != null ? h('span', { class: 'tiny muted' }, `${pcs(chg, 0)} 比${r.fwd_pe_chg_label || ''}`) : null),
          cell('营业 / 净利率', `${pc(r.operating_margin, 0)} / ${pc(r.profit_margin, 0)}`, '', r.margin_trend != null ? h('span', { class: 'tiny ' + dirClass(r.margin_trend) }, r.margin_trend > 0.01 ? '↑ 在扩张' : r.margin_trend < -0.01 ? '↓ 在收缩' : '→ 稳定') : null),
          cell('营收 / 盈利同比', `${pcs(r.revenue_growth_q, 0)} / ${pcs(r.earnings_growth_q, 0)}`, dirClass(r.revenue_growth_q)),
          cell('自由现金流', big(r.free_cashflow), dirClass(r.free_cashflow), r.fcf_yield != null ? h('span', { class: 'tiny muted' }, `回报率 ${pc(r.fcf_yield)}`) : null)),
        h('div', { class: 'row small muted', style: 'gap:10px;margin-top:5px' },
          h('span', {}, '52 周位置'), r.range_pos != null ? h('span', { class: 'ks-pos', title: `52 周区间的 ${(r.range_pos * 100).toFixed(0)}% 位置` }, h('i', { style: `left:${Math.round(r.range_pos * 100)}%` })) : '—',
          r.vs_ma200 != null ? h('span', { class: dirClass(r.vs_ma200) }, `200 日线${r.vs_ma200 >= 0 ? '上方' : '下方'} ${Math.abs(r.vs_ma200 * 100).toFixed(0)}%`) : null)));
    }
    const left = d.pending.length;
    status.textContent = left ? `正在从 Yahoo 补齐 ${left} 只（逐只取，受限速保护，约每只 15~25 秒）…` : '数据来自 Yahoo Finance，缓存 24 小时；点一张卡片看详情（Forward P/E 历史、全部指标）。金额单位：美元；涨跌颜色按你设置的配色。';
    if (left && tries++ < 80) timer = setTimeout(load, 5000);
  }
  load();
  return { el: box, destroy() { alive = false; clearTimeout(timer); } };
}
