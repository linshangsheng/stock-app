// 持仓风险总览（持仓页右侧）：把所有持仓放在一起看——行业集中度、哪几只同涨同跌、大盘跌 10% 大概亏多少、按现在的持仓重放过去一年。
// 数据来自 /api/portfolio/risk（server/risk_view.py）。这是估算，不是预测；提醒线是常识性的经验值，不是回测出来的。
import { get } from './api.js';
import { h, fmtMoney, fmtPct, dirClass, evBadge } from './util.js';

export function riskCard() {
  const box = h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '风险总览（持仓放在一起看）')), h('div', { class: 'hint' }, h('span', { class: 'spinner' }), ' 计算中…'));
  load();
  return box;

  async function load() {
    let d;
    try { d = await get('/portfolio/risk'); } catch (e) { fill(h('div', { class: 'hint' }, '暂时算不出来：' + e.message)); return; }
    if (d.status !== 'ok') { box.remove(); return; }
    fill(...render(d));
  }

  function fill(...kids) {
    box.replaceChildren(h('div', { class: 'card-title' }, h('h3', {}, '风险总览（持仓放在一起看）'),
      evBadge('无', '看盘辅助：用最近约一年的日线收盘价算的估算，不是预测，不参与任何买卖信号。提醒线（行业 40%、单只 30%、相关 0.8）是常识性的经验值，不是回测出来的。')), ...kids);
  }
}

function render(d) {
  const kv = (k, v, tip, cls = '') => h('div', { class: 'kv', title: tip || '' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v));
  const flags = d.flags || [];
  const out = [];
  out.push(h('div', { class: 'small muted' }, `${d.n} 只持仓 · 市值 ${fmtMoney(d.value)}`, d.exposure != null ? ` · 占账户净值 ${fmtPct(d.exposure, 0, false)}` : '', ` · 数据截至 ${d.date}`));
  const real = flags.filter(f => f.level !== 'info'), info = flags.filter(f => f.level === 'info');
  out.push(real.length ? h('div', { class: 'alert warn small mt-s' }, real.map(f => h('div', {}, '⚠ ' + f.text)))
    : h('div', { class: 'alert ok small mt-s' }, d.n < 2 ? '只有一只持仓，谈不上分散；多买几只后这里会告诉你它们是不是同涨同跌。' : '没有发现明显的集中风险（行业、单只占比、同涨同跌）。这不等于没有风险——大盘下跌时它们仍会一起跌，见下面的估算。'));
  info.forEach(f => out.push(h('div', { class: 'small muted mt-s' }, f.text)));

  const sh = d.shock, rp = d.replay;
  out.push(h('div', { class: 'grid c4 mt-s' },
    kv('相当于几只独立持仓', d.eff_n == null ? '—' : `${d.eff_n.toFixed(1)} / ${d.n - (d.short || []).length}`, '把相关性考虑进去：互不相关的持仓才算一只。数字越接近持仓只数，分散得越好；接近 1 就是几只其实在同涨同跌'),
    kv('对大盘的敏感度（beta）', d.beta == null ? '—' : d.beta.toFixed(2), `大盘涨跌 1%，这些持仓平均大约涨跌 ${d.beta == null ? '—' : d.beta.toFixed(2)}%（按最近约半年算）；大于 1 比大盘波动大`),
    kv(`${d.bench_name || '大盘'}跌 10%`, sh ? `约 ${fmtPct(-Math.abs(sh.loss_pct), 1)}` : '—', sh ? `按 beta 估算，持仓约亏 ${fmtMoney(Math.abs(sh.loss_amount))}${sh.loss_of_equity != null ? `，占账户净值 ${(Math.abs(sh.loss_of_equity) * 100).toFixed(1)}%` : ''}。这是估算，真实下跌时个股常常比估算更差` : '数据不足', sh ? 'down' : ''),
    kv('过去一年最差一天', rp ? fmtPct(rp.worst_day, 1) : '—', rp ? `把现在的持仓（按现在的比例）放回过去一年，最差的一天是 ${rp.worst_day_date}` : '数据不足', rp ? dirClass(rp.worst_day) : '')));
  if (sh && sh.loss_of_equity != null) out.push(h('div', { class: 'small mt-s' }, `大盘跌 10% 时，按估算账户净值约少 `, h('b', {}, fmtMoney(Math.abs(sh.loss_amount))), `（${(Math.abs(sh.loss_of_equity) * 100).toFixed(1)}%）。`));
  if (rp) out.push(h('div', { class: 'small muted mt-s' }, `按现在的持仓重放过去 ${rp.days} 个交易日：最大回撤 ${fmtPct(rp.max_dd, 1)}`,
    rp.bench_max_dd != null ? `（同期 ${d.bench_name || '大盘'} ${fmtPct(rp.bench_max_dd, 1)}）` : '', rp.covered < 0.99 ? `；只覆盖了 ${(rp.covered * 100).toFixed(0)}% 的市值（其余历史太短）` : '', '。只是回看，不代表以后会这样。'));

  out.push(h('div', { class: 'small mt', style: 'font-weight:600' }, '行业分布'));
  out.push(h('div', { class: 'rk-bars mt-s' }, d.industry.slice(0, 8).map(r => h('div', { class: 'rk-row' }, h('span', { class: 'rk-name' }, r.name),
    h('span', { class: 'rk-bar' }, h('i', { style: `width:${Math.max(2, r.share * 100)}%`, class: r.share >= 0.4 && r.name !== '其他' ? 'hot' : '' })), h('span', { class: 'num rk-pct' }, fmtPct(r.share, 0, false))))));
  if ((d.pairs || []).length) {
    out.push(h('div', { class: 'small mt', style: 'font-weight:600' }, '过去一年走势最像的几组'),
      h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'tbl small' }, h('tbody', {}, d.pairs.map(p => h('tr', {}, h('td', {}, p.a_name), h('td', {}, p.b_name),
        h('td', { class: 'num r', title: '相关系数：1 = 完全同涨同跌，0 = 互不相关' }, p.corr.toFixed(2))))))));
  }
  return out;
}
