// 实盘成绩单（回测页第二个标签）：软件每天存档的信号，之后真实发生了什么，和不用信号时比。不是回测——存档当天写下、事后改不了。
// 前端不做计算，只展示 GET /api/scoreboard；样本不够时只记录、不下结论（后端给出 verdict.level）。
import { get } from './api.js';
import { h, clear, fmtPct, dirClass, isNum, evBadge, shortDate } from './util.js';

const LEVEL = { none: ['还不能下结论', 'none'], few: ['只作参考', 'few'], ok: ['可以参考', 'ok'] };
const pp = v => (isNum(v) ? (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v * 100).toFixed(1) + ' 个百分点' : '—');

export function scoreboardPanel() {
  const el = h('div', { class: 'sb' });
  let alive = true;

  const verdict = v => h('div', { class: 'sb-verdict ' + LEVEL[v.level][1] }, h('i', {}), h('span', {}, h('b', {}, LEVEL[v.level][0] + '　'), v.text));

  /** 一行：持有 N 天，实际平均 / 对比线 / 差 / 跑赢次数 / 胜率；差用一根细条表示方向和大小（±10 个百分点封顶）。 */
  function horizon(r, benchLabel, what) {
    if (!r.n) return h('div', { class: 'sb-h nil' }, h('b', {}, `持有 ${r.h} 天`), h('span', { class: 'muted small' }, r.pending ? `${r.pending} 个${what}还没到期` : '还没有记录'));
    const w = Math.min(50, Math.abs(r.excess || 0) / 0.10 * 50);
    return h('div', { class: 'sb-h' },
      h('b', {}, `持有 ${r.h} 天`),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, '实际平均'), h('span', { class: 'v num ' + dirClass(r.avg) }, fmtPct(r.avg, 1))),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, benchLabel), h('span', { class: 'v num ' + dirClass(r.bench) }, fmtPct(r.bench, 1))),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, '比对比线'), h('span', { class: 'v num ' + dirClass(r.excess) }, pp(r.excess))),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, `跑赢 / 事件`), h('span', { class: 'v num' }, `${r.beat} / ${r.n}`)),
      h('div', { class: 'kv' }, h('span', { class: 'k' }, '赚钱的事件'), h('span', { class: 'v num' }, fmtPct(r.win, 0, false))),
      h('div', { class: 'sb-diff', title: '比对比线多（右）/ 少（左），±10 个百分点封顶' }, h('span', { class: 'mid' }), h('i', { class: r.excess >= 0 ? 'pos' : 'neg', style: `width:${w}%;${r.excess >= 0 ? 'left:50%' : `right:50%`}` })),
      r.pending ? h('span', { class: 'muted tiny' }, `另有 ${r.pending} 个没到期`) : null);
  }

  function recent(s) {
    if (!s.recent.length) return null;
    const only20 = (s.horizons || []).length === 1;                       // 只有 20 天一列（低风险组合：一期 = 一个调仓周期）
    const cols = only20 ? [['20 天', 'r20'], ['对比', 'b20']] : [['5 天', 'r5'], ['对比', 'b5'], ['20 天', 'r20'], ['对比', 'b20']];
    return h('details', { class: 'mt-s' }, h('summary', {}, `最近 ${s.recent.length} 个事件`),
      h('table', { class: 'tbl small mt-s' }, h('thead', {}, h('tr', {}, [['日期'], ['说明'], ...cols].map(([t], i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
        h('tbody', {}, s.recent.map(e => h('tr', {}, h('td', { class: 'num' }, e.date), h('td', { class: 'muted' }, e.note || ''),
          ...cols.map(([, k], i) => h('td', { class: 'num r ' + (i % 2 ? 'muted' : dirClass(e[k])) }, isNum(e[k]) ? fmtPct(e[k], 1) : '待定')))))));
  }

  /** 低风险组合：当前这一期的影子名单，以及到最新收盘为止的估算（还没结束，不算进上面的成绩）。 */
  function currentList(c, bench) {
    const prog = isNum(c.ret)
      ? `到最新收盘（第 ${c.days} 个交易日）：名单等权 ${fmtPct(c.ret, 1)}，${bench} ${isNum(c.bench) ? fmtPct(c.bench, 1) : '—'}（进行中，不计入上面的成绩）`
      : `${c.entry} 开盘买入，还没有可用的收盘数据。`;
    return h('div', { class: 'sb-cur mt-s' }, h('div', { class: 'small' }, h('b', {}, `现在这一期（${c.date} 收盘出的名单，${c.n} 只）`), '　', prog),
      h('div', { class: 'small muted' }, c.names.join('、')));
  }

  function signalCard(s, bench) {
    return h('div', { class: 'card sb-card' },
      h('div', { class: 'row between wrap' }, h('h3', {}, s.name), h('div', { class: 'row', style: 'gap:8px' }, s.since ? h('span', { class: 'small muted' }, '始于 ' + s.since) : null, evBadge(s.evidence, '这个信号在回测里的证据强度；成绩单看的是它之后的真实表现。'))),
      h('p', { class: 'small muted', style: 'margin:4px 0 10px' }, s.desc),
      verdict(s.verdict),
      h('div', { class: 'sb-rows' + (s.verdict.level === 'none' ? ' dim' : '') }, s.rows.map(r => horizon(r, s.bench_label || bench, s.what))),
      s.id === 'watchlist' ? h('p', { class: 'small muted mt-s' }, `共 ${s.runs} 次正式扫描，其中 ${s.empty_runs} 次没有候选（空仓日，不算事件）；清单里累计 ${s.stocks5} 只股票已有 5 天结果，${isNum(s.stock_win5) ? '赚钱的占 ' + fmtPct(s.stock_win5, 0, false) : '暂无胜率'}。`) : null,
      s.current ? currentList(s.current, bench) : null,
      s.expect ? h('p', { class: 'small muted mt-s' }, h('b', {}, '回测当时的预期：'), s.expect) : null,
      s.caveat ? h('p', { class: 'small muted' }, s.caveat) : null,
      recent(s));
  }

  function ruleCard(r) {
    const p = r.portfolio;
    return h('div', { class: 'card sb-card' },
      h('div', { class: 'row between wrap' }, h('h3', {}, r.name), h('div', { class: 'row', style: 'gap:8px' }, r.since ? h('span', { class: 'small muted' }, `始于 ${r.since}，已存档 ${r.recorded} 天`) : null, evBadge(r.evidence, '规则在回测里的证据强度；成绩单看的是存档之后的真实表现。'))),
      h('p', { class: 'small muted', style: 'margin:4px 0 10px' }, r.desc),
      verdict(r.verdict),
      p ? h('div', { class: 'sb-rows' + (r.verdict.level === 'none' ? ' dim' : '') }, h('div', { class: 'sb-h' }, h('b', {}, `全部宽基等权（${p.days} 个交易日）`),
        h('div', { class: 'kv' }, h('span', { class: 'k' }, '照着做'), h('span', { class: 'v num ' + dirClass(p.rule) }, fmtPct(p.rule, 1))),
        h('div', { class: 'kv' }, h('span', { class: 'k' }, '一直持有'), h('span', { class: 'v num ' + dirClass(p.hold) }, fmtPct(p.hold, 1))),
        h('div', { class: 'kv' }, h('span', { class: 'k' }, '差'), h('span', { class: 'v num ' + dirClass(p.excess) }, pp(p.excess))))) : null,
      r.rows.length ? h('table', { class: 'tbl small mt-s' }, h('thead', {}, h('tr', {}, ['指数', '记录起', '计入天数', '照着做', '一直持有', '差', '现在仓位'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
        h('tbody', {}, r.rows.map(x => h('tr', {}, h('td', {}, x.name), h('td', { class: 'num r muted' }, shortDate(x.since)), h('td', { class: 'num r' }, x.days),
          h('td', { class: 'num r ' + dirClass(x.rule) }, isNum(x.rule) ? fmtPct(x.rule, 1) : '—'), h('td', { class: 'num r ' + dirClass(x.hold) }, isNum(x.hold) ? fmtPct(x.hold, 1) : '—'),
          h('td', { class: 'num r ' + dirClass(x.excess) }, isNum(x.excess) ? pp(x.excess) : '—'), h('td', { class: 'num r' }, fmtPct(x.pos, 0, false)))))) : null,
      r.expect ? h('p', { class: 'small muted mt-s' }, h('b', {}, '回测当时的预期：'), r.expect) : null,
      h('p', { class: 'small muted' }, '「计入天数」只算前两天都有存档的交易日；规则按收盘定、次日开盘成交，已扣单边费用。仓位 = 趋势仓 + 抄底仓的合计。'));
  }

  function render(d) {
    clear(el);
    if (!d || d.status !== 'ok') { el.append(h('div', { class: 'card' }, h('div', { class: 'empty' }, h('div', { class: 'big' }, '暂无成绩单'), d?.message || '请先初始化数据。'))); return; }
    const enough = [...d.signals, d.rule].filter(s => s.verdict.level !== 'none').length;
    el.append(h('div', { class: 'card sb-hero' },
      h('div', { class: 'small muted' }, '实盘成绩单'),
      h('div', { class: 'sb-title' }, '软件说过的话，后来兑现了多少'),
      h('p', { class: 'sb-sum' }, '每天收盘后，软件把信号存档；等 5 天、20 天之后，再回头核对真实发生了什么，并和「不用这个信号」比。这不是回测：存档当天就写下了，事后不能改参数、不能挑好看的。'),
      h('div', { class: 'fl-mini' },
        h('span', {}, '存档始于 ', h('b', { class: 'num' }, d.archive_start || '—')),
        h('span', {}, '信号 ', h('b', { class: 'num' }, d.signals.length + 1), ' 类'),
        h('span', {}, '样本够看的 ', h('b', { class: 'num' }, `${enough} / ${d.signals.length + 1}`)),
        h('span', {}, '个股类对比 ', h('b', {}, d.bench)))),
      ...d.signals.map(s => signalCard(s, `同期买${d.bench}`)), ruleCard(d.rule),
      h('p', { class: 'hint mt-s' }, d.note));
  }

  async function load() {
    el.append(h('div', { class: 'card' }, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 正在核对存档…')));
    let d;
    try { d = await get('/scoreboard', {}, { cache: false, timeout: 120000 }); } catch (e) { d = { status: 'error', message: e.message }; }
    if (alive) render(d);
  }
  return { el, mount() { load(); }, destroy() { alive = false; } };
}
