// 低风险组合（选股页「低风险组合」标签 + 回测页「低风险组合」回测共用）：今天的名单、调仓操作、回测证据。
// 数据全部来自 /api/factor/plan 与 /api/backtest（kind=factor），前端不做计算。
import { get } from './api.js';
import { h, clear, fmtPrice, fmtPct, fmtNum, fmtMoney, dirClass, code, shortDate } from './util.js';
import { lineChart, themeColors } from './chart.js';
import { openTradeForm } from './portfolio.js';

export async function loadFactorPlan(summary = true) {
  return get('/factor/plan', { summary: summary ? 1 : 0 }, { cache: false, timeout: 180000 });
}

const MODE = { start: ['还没建仓', 'accent'], rebalance: ['明天调仓', 'warn'], hold: ['持有中', 'ok'] };
const money = v => (v == null ? '—' : fmtMoney(v));

/** 「明天要做什么」卡片里的一行。 */
export function factorLine(fp) {
  if (!fp) return { body: '低风险组合计算中…' };
  if (fp.status !== 'ok') return { body: fp.message || '低风险组合暂不可用' };
  const act = fp.mode === 'hold' ? '' : ' —— 点这里看名单';
  return { body: fp.headline + act, urgent: fp.mode === 'rebalance' && (fp.actions.sell.length + fp.actions.buy.length) > 0 };
}

/** 选股页的「低风险组合」标签页。opts: { onPick(symbol), reload(), noEvidence }（宽屏时回测证据放到右侧，这里就不画）。 */
export function factorTab(fp, opts = {}) {
  const box = h('div', { class: 'fp-tab' });
  const ev = { destroy() {} };
  if (!fp) { box.append(h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 计算今天的名单…（首次约 30 秒）')); return { el: box, destroy() {} }; }
  if (fp.status !== 'ok') { box.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '低风险组合暂不可用'), fp.message || '')); return { el: box, destroy() {} }; }
  const al = fp.allocation, sc = fp.schedule, P = fp.params;
  const [mt, mc] = MODE[fp.mode] || ['', ''];

  // ① 一句话 + 资金 + 日历
  box.append(h('div', { class: 'card fp-head' },
    h('div', { class: 'row between' }, h('h3', {}, '低风险组合'), h('span', { class: 'badge ' + mc }, mt)),
    h('div', { class: 'fp-headline' }, fp.headline),
    h('div', { class: 'grid c3 mt-s fp-kv' },
      kv('分给组合的钱', al.sleeve ? money(al.sleeve) : '未填资金', al.equity ? `总资金 ${money(al.equity)} × ${fmtPct(al.factor, 0, false)}` : '在「持仓」页填账户资金'),
      kv('持有只数', `${P.n} 只`, al.per_stock ? `每只约 ${money(al.per_stock)}` : '等金额'),
      kv('下次调仓', sc.is_rebalance_day ? '今天收盘（明天开盘做）' : shortDate(sc.next_rebalance) + ' 收盘', sc.is_rebalance_day ? `每 ${sc.every} 个交易日一次` : `还有 ${sc.days_left} 个交易日${sc.next_estimated ? '（按工作日估算）' : ''}`)),
    !al.equity ? h('div', { class: 'alert warn small mt-s' }, '还没填账户资金：到「持仓」页填一次，就能算出每只买多少股。') : null));

  // ② 操作清单（建仓 / 调仓日）
  if (fp.mode !== 'hold') {
    const sells = fp.holdings.filter(x => !x.in_target), buys = fp.targets.filter(x => !x.held);
    const how = h('div', { class: 'alert info small mt-s' },
      h('b', {}, '怎么买卖：'), '明天开盘直接按市价买卖（集合竞价挂单，或开盘后马上挂单）。',
      h('br'), '· 开盘就涨停、买不进：这只本期放弃，不追，等下次调仓。',
      h('br'), '· 开盘就跌停、卖不出：第二天开盘再卖。',
      h('br'), '· 不设止损、不设止盈；分红送股不用管。买卖完点「成交了 / 卖出了」记一笔，类型自动标成「低风险组合」。');
    box.append(h('div', { class: 'card mt' },
      h('div', { class: 'card-title' }, h('h3', {}, fp.mode === 'start' ? '建仓：明天开盘买入' : '调仓：明天开盘'), h('span', { class: 'hint' }, `名单来自 ${shortDate(fp.as_of)} 收盘`)),
      sells.length ? h('div', {}, h('div', { class: 'small muted mb-s' }, `卖出 ${sells.length} 只（整只卖完）`),
        ...sells.map(x => opRow(x, 'sell', opts))) : null,
      buys.length ? h('div', { class: sells.length ? 'mt-s' : '' }, h('div', { class: 'small muted mb-s' }, `买入 ${buys.length} 只`),
        ...buys.map(x => opRow(x, 'buy', opts))) : h('div', { class: 'hint' }, '名单没有变化，不用买。'),
      how));
  } else if (fp.holdings.length) {
    box.append(h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, `我的组合持仓（${fp.holdings.length} 只）`), h('span', { class: 'hint' }, `市值 ${money(fp.held_value)}`)),
      h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, ['名称', '股数', '现价', '盈亏', '今日排名'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
        h('tbody', {}, fp.holdings.map(x => h('tr', { class: 'link', onclick: () => opts.onPick?.(x.symbol) },
          h('td', {}, x.name, h('span', { class: 'muted num tiny' }, ' ' + code(x.symbol))), h('td', { class: 'num r' }, x.qty), h('td', { class: 'num r' }, fmtPrice(x.close)),
          h('td', { class: 'num r ' + dirClass(x.pnl_pct) }, fmtPct(x.pnl_pct, 1)),
          h('td', { class: 'num r' + (x.in_target ? '' : ' down') }, x.rank ? '#' + x.rank : '—'))))),
      h('p', { class: 'hint mt-s' }, `平时不用管：不设止损止盈，也不用每天看。排名掉出前 ${Math.round(P.n * P.buffer)} 名的，会在下次调仓日提示卖出。`)));
  }

  // ③ 目标名单
  box.append(h('div', { class: 'card mt' },
    h('div', { class: 'card-title' }, h('h3', {}, fp.mode === 'hold' ? '今天的排名（下次调仓才按它操作）' : '目标名单'), h('span', { class: 'hint' }, `交易池 ${fp.universe} 只里综合分最高的 ${P.n} 只`)),
    h('div', { class: 'tbl-wrap' }, h('table', { class: 'mini fp-list' },
      h('thead', {}, h('tr', {}, ['#', '名称 / 行业', '收盘', '20日换手', '日波动', al.per_stock ? '买入（估）' : '状态'].map((t, i) => h('th', { class: i >= 2 ? 'r' : '' }, t)))),
      h('tbody', {}, fp.targets.map(x => h('tr', { class: 'link', onclick: () => opts.onPick?.(x.symbol) },
        h('td', { class: 'num' }, x.rank ?? '—'),
        h('td', {}, h('b', {}, x.name), h('span', { class: 'muted num tiny' }, ' ' + code(x.symbol)), x.held ? h('span', { class: 'badge ok', style: 'margin-left:4px' }, '已持有') : null,
          x.industry ? h('div', { class: 'tiny muted fp-ind' }, x.industry) : null),
        h('td', { class: 'num r' }, fmtPrice(x.close)),
        h('td', { class: 'num r' }, x.turnover_ma20 != null ? fmtNum(x.turnover_ma20, 2) + '%' : '—'),
        h('td', { class: 'num r' }, fmtPct(x.atr_pct, 1, false)),
        h('td', { class: 'num r' }, x.held ? '继续持有' : x.shares ? `${x.shares} 股` : x.shares === 0 ? '一手都买不起' : '—',
          !x.held && x.amount ? h('div', { class: 'tiny muted' }, '≈ ' + money(x.amount)) : null)))))),
    fp.skipped_expensive?.length ? h('p', { class: 'tiny muted mt-s' }, `一手都买不起、已跳过（名额让给下一名，回测也是这样）：${fp.skipped_expensive.map(x => `${x.name} ${fmtPrice(x.close)}`).join('、')}`) : null,
    h('p', { class: 'hint mt-s' }, '综合分 = 「20 日平均换手率越低越好」和「每天平均波动（ATR%）越小越好」两个排名的平均。冷门、平稳的股票之后平均更强——A 股里检验最稳定的规律之一。买入股数 = 每只的钱 ÷ 收盘价，按整手向下取整（明天开盘价不同，按实际价格算即可）。')));

  // ④ 规则说明
  box.append(h('details', { class: 'card mt fp-rules' }, h('summary', {}, h('b', {}, '这个组合是怎么回事？（规则与风险，第一次用请看）')),
    h('ul', { class: 'small', style: 'margin:8px 0 0;padding-left:18px;line-height:1.7' },
      h('li', {}, `每 ${P.rebalance_days} 个交易日（约一个月）调仓一次：收盘后出名单，第二天开盘买卖。日历从 2018-01-02 起算，所有人、实盘和回测用同一套日期。`),
      h('li', {}, `只在交易池里选（成交额够、不是 ST、没停牌、上市够久），等金额买 ${P.n} 只。`),
      h('li', {}, `已经持有的，只要还在前 ${Math.round(P.n * P.buffer)} 名就继续拿着，不为了一两名的变化来回换——平均一只拿半年左右，一年买卖的金额约为本金的 4 倍，3 万资金的手续费约每年 0.7%。`),
      h('li', {}, '不设止损、不设止盈：它赚的是「一篮子冷门稳健股长期跑赢市场」的钱，单只股票的涨跌不重要。止损反而会在恐慌时把便宜货卖掉。'),
      h('li', {}, h('b', {}, '要接受的代价：'), '回测最大回撤约 -22%，而且不是出现在股灾里，而是 2020~2021 年热门股大牛市时——冷门股阴跌了一年多。牛市、小盘股和题材股大涨的年份会明显跑输（2020 年跑输沪深300 三十多个百分点，2025 年十几个百分点），可能连续一两年不如指数。它的好处在长期：熊市少亏、震荡市稳定赚。'),
      h('li', {}, '和宽基 ETF 规则一起用：两者日收益相关性只有 0.35，一起拿比单独拿更稳。默认资金方案：ETF 70%、组合 30%（可在「设置 → 资金方案」改）。'))));

  // ⑤ 回测证据（窄屏放在最下面；宽屏由选股页放到右侧）
  if (!opts.noEvidence) {
    const b = factorEvidenceBlock(fp, { compact: true });
    box.append(h('div', { class: 'mt' }, b.el));
    requestAnimationFrame(() => b.init());
    ev.destroy = () => b.destroy();
  } else box.append(h('p', { class: 'hint mt' }, '回测证据（分年度、和沪深300 / 中证500 对比、健康度）在右侧。'));
  return { el: box, destroy: () => ev.destroy() };
}

/** 回测证据块：缓存好了就画；后台还在算就转圈（有上次结果先显示上次的）。 */
export function factorEvidenceBlock(fp, { compact = false } = {}) {
  const bt = fp?.backtest;
  const rep = bt?.status === 'ok' ? bt : bt?.stale;
  const wrap = h('div', {});
  if (!rep) {
    wrap.append(h('div', { class: 'card' }, h('h3', {}, '回测证据'), h('div', { class: 'hint mt-s' },
      bt?.status === 'error' ? '回测失败：' + (bt.message || '') : h('span', {}, h('span', { class: 'spinner' }), ' 正在后台用全市场 8 年数据回测（约 1~2 分钟，只在数据更新后算一次）…'))));
    return { el: wrap, init() {}, destroy() {} };
  }
  if (bt.status !== 'ok') wrap.append(h('div', { class: 'alert small mb-s' }, h('span', { class: 'spinner' }), ' 数据已更新，正在后台重算（下面是上次的结果）'));
  const e = factorEvidence(rep, { title: '回测证据（按你的资金规模、真实成交规则与费用）', compact });
  wrap.append(e.el);
  return { el: wrap, init: () => e.init(), destroy: () => e.destroy() };
}

function opRow(x, side, opts) {
  const buy = side === 'buy';
  return h('div', { class: 'exec-card fp-op' },
    h('div', { class: 'row between' },
      h('span', { class: 'link', onclick: () => opts.onPick?.(x.symbol) }, h('b', {}, x.name), h('span', { class: 'muted num small' }, ' ' + code(x.symbol)),
        h('span', { class: 'badge ' + (buy ? 'accent' : 'warn'), style: 'margin-left:6px' }, buy ? `买 #${x.rank}` : '卖')),
      h('button', { class: 'btn sm ' + (buy ? 'primary' : ''), title: '在券商成交后点这里记一笔（填实际成交价和股数）', onclick: () => openTradeForm(buy
        ? { symbol: x.symbol, name: x.name, side: 'buy', price: x.close, shares: x.shares || '', setup: 'factor', onDone: opts.reload }
        : { symbol: x.symbol, name: x.name, side: 'sell', price: x.close, qty: x.qty, setup: 'factor', exit_reason: '调仓', onDone: opts.reload }) }, buy ? '成交了' : '卖出了')),
    h('div', { class: 'num small' }, buy
      ? (x.shares ? h('span', {}, `约 ${x.shares} 股（参考价 ${fmtPrice(x.close)}，≈ ${money(x.amount)}）`) : x.shares === 0 ? '一手都买不起：跳过' : `参考价 ${fmtPrice(x.close)}`)
      : h('span', {}, `${x.qty} 股全部卖出 · 现价 ${fmtPrice(x.close)} · 盈亏 `, h('b', { class: dirClass(x.pnl_pct) }, fmtPct(x.pnl_pct, 1)))),
    !buy && x.why ? h('div', { class: 'tiny muted' }, '原因：' + x.why) : null,
    x.suspended ? h('div', { class: 'tiny', style: 'color:var(--warn)' }, '今天停牌：复牌后再操作') : null);
}

/** 回测证据：全程 / 样本内 / 样本外 对比表、分年度、净值曲线、健康度。返回 {el, init, destroy}（图表要在挂到页面后再画）。 */
export function factorEvidence(r, { title = '回测结果', compact = false } = {}) {
  const charts = [], inits = [];
  const out = h('div', { class: 'card fp-ev' + (compact ? ' compact' : '') });
  const hl = r.health, st = r.stats;
  const hc = hl.status === '警告' ? 'bad' : hl.status === '注意' ? 'warn' : 'ok';
  out.append(h('div', { class: 'card-title' }, h('h3', {}, title), h('span', { class: 'hint' }, `${r.from} ~ ${r.to} · 起始 ${money(st.equity0)}`)));
  const seg = (m) => (m && m.cagr != null ? [h('td', { class: 'num r ' + dirClass(m.cagr) }, fmtPct(m.cagr, 1)), h('td', { class: 'num r' }, fmtPct(m.mdd, 0)), h('td', { class: 'num r' }, m.sharpe ?? '—')]
    : [h('td', { class: 'r muted' }, '—'), h('td'), h('td')]);
  const has = k => r.rows.some(x => x[k]);
  if (compact) {                  // 窄屏：只看全程和样本外的年化 / 回撤
    const c2 = m => (m && m.cagr != null ? [h('td', { class: 'num r ' + dirClass(m.cagr) }, fmtPct(m.cagr, 1)), h('td', { class: 'num r' }, fmtPct(m.mdd, 0))] : [h('td', { class: 'r muted' }, '—'), h('td')]);
    out.append(h('table', { class: 'mini fp-cmp' },
      h('thead', {}, h('tr', {}, h('th', {}), h('th', { colspan: 2, class: 'r' }, '全程'), has('oos') ? h('th', { colspan: 2, class: 'r' }, `样本外 ${(r.oos_start || '').slice(0, 4)}~`) : null),
        h('tr', {}, h('th', {}), ...[1, has('oos')].filter(Boolean).flatMap(() => ['年化', '最大回撤'].map(t => h('th', { class: 'r' }, t))))),
      h('tbody', {}, r.rows.map(x => h('tr', { class: x.main ? 'hl' : '' }, h('td', {}, x.main ? h('b', {}, '组合') : x.name), ...c2(x.all), ...(has('oos') ? c2(x.oos) : []))))));
  } else out.append(h('div', { class: 'tbl-wrap' }, h('table', { class: 'mini fp-cmp' },
    h('thead', {},
      h('tr', {}, h('th', {}), h('th', { colspan: 3, class: 'r' }, '全程'), has('is') ? h('th', { colspan: 3, class: 'r' }, `样本内（${(r.oos_start || '').slice(0, 4) - 1} 年及以前）`) : null, has('oos') ? h('th', { colspan: 3, class: 'r' }, `样本外（${(r.oos_start || '').slice(0, 4)}~）`) : null),
      h('tr', {}, h('th', {}), ...[1, has('is'), has('oos')].filter(Boolean).flatMap(() => ['年化', '最大回撤', 'Sharpe'].map(t => h('th', { class: 'r' }, t))))),
    h('tbody', {}, r.rows.map(x => h('tr', { class: x.main ? 'hl' : '' }, h('td', {}, x.main ? h('b', {}, x.name) : x.name), ...seg(x.all), ...(has('is') ? seg(x.is) : []), ...(has('oos') ? seg(x.oos) : [])))))));
  out.append(h('p', { class: 'tiny muted mt-s' }, '「样本内」是用来挑参数的年份，「样本外」是挑完以后才检验的年份——只有样本外的成绩算数。对照里「交易池等权」= 每天等权买入交易池全部股票（不计费用）。'));

  const curve = h('div', { class: 'mt-s' });
  out.append(h('div', { class: 'small muted mt' }, '净值走势（起点 = 1）'), curve);
  inits.push(() => {
    const c = themeColors();
    const cols = [c.flat || '#8a94a6', '#d68a24', '#7a5af8'];
    const ser = [{ name: '组合', color: c.accent || '#2457d6', data: r.curve.map(p => ({ time: p.date, value: p.port })) }];
    (r.bench_names || []).forEach((k, i) => ser.push({ name: k, color: cols[i % cols.length], data: r.curve.filter(p => p[k] != null).map(p => ({ time: p.date, value: p[k] })) }));
    return lineChart(curve, ser, { height: 220 });
  });

  const benchCols = r.bench_names || [];
  out.append(h('div', { class: 'grid c2 mt', style: 'align-items:start' },
    h('div', {}, h('div', { class: 'small muted mb-s' }, '分年度（* = 不满一年）'),
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'mini' },
        h('thead', {}, h('tr', {}, ['年份', '组合', '年内回撤', ...benchCols].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
        h('tbody', {}, r.by_year.map(y => h('tr', {}, h('td', {}, y.year + (y.partial ? '*' : '')),
          h('td', { class: 'num r ' + dirClass(y.ret) }, h('b', {}, fmtPct(y.ret, 1))), h('td', { class: 'num r' }, fmtPct(y.dd, 0)),
          ...benchCols.map(k => h('td', { class: 'num r muted' }, fmtPct(y.bench[k], 1))))))))),
    h('div', {},
      h('div', { class: 'card soft' }, h('div', { class: 'card-title' }, h('h3', {}, '策略健康度'), h('span', { class: 'badge ' + hc }, hl.status)),
        h('div', { class: 'grid c2' }, kv('当前回撤（距最高点）', fmtPct(hl.current_dd, 1)), kv(`回测最大回撤（${hl.max_dd_date}）`, fmtPct(hl.max_dd, 1))),
        h('p', { class: 'small mt-s' }, hl.text)),
      h('div', { class: 'grid c2 mt-s' },
        kv('年换手', `${fmtNum(st.turnover, 1)} 倍`, '买卖金额 ÷ 平均资金'), kv('手续费', `每年约 ${fmtPct(st.fees_per_year_pct, 2, false)}`, `合计 ${money(st.fees)}`),
        kv('平均持有', `${fmtNum(st.avg_hold_days, 0)} 个交易日`, `已卖出 ${st.n_trades} 笔`), kv('卖出时赚钱的比例', fmtPct(st.win_rate, 0, false), `平均每笔 ${fmtPct(st.avg_trade_ret, 1)}`),
        kv('平均仓位', fmtPct(st.invested, 0, false), '其余是买整手剩下的零钱'), kv('期末', money(st.equity1), `起始 ${money(st.equity0)}`)))));
  out.append(h('p', { class: 'hint mt' }, '读法：它不是「每年都赚」的策略——2020、2025 这种小盘 / 题材牛市会大幅跑输，2023~2024 红利、低波动风格当道时大赚。长期看，熊市少亏（2018、2022）+ 震荡市稳定，是它跑赢指数的来源。回测不代表未来，风格可能长期不利。'));
  return { el: out, init() { for (const f of inits.splice(0)) charts.push(f()); }, destroy() { for (const c of charts.splice(0)) c?.destroy?.(); } };
}

function kv(k, v, sub) {
  return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num' }, v), sub ? h('span', { class: 'tiny muted' }, sub) : null);
}
