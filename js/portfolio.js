// 持仓（4.5.4）：持仓列表 + 盘后体检（必须处理 / 关注 / 正常，5.10）+ 交易日志与复盘统计（5.11）+ 账户参数。
// 系统不接券商下单：持仓与交易由你手动录入（或 CSV 导入），系统只给出次日执行参数。数据全部存后端 portfolio.db。
import { get, post, put, del, state } from './api.js';
import { h, clear, fmtPrice, fmtPct, fmtNum, fmtMoney, fmtSigned, dirClass, code, toast, modal, confirmBox, download, copyText } from './util.js';
import { showDetail } from './detail.js';
import { parseTrades, fileToText } from './parse.js';
import { toCSV } from './parse.js';

const SETUPS = [['', '未标注'], ['breakout', '突破'], ['pullback', '回踩'], ['vcp', '波动收缩突破'], ['oversold', '强势超跌']];
const EXIT_REASONS = ['止损', '移动止盈', '时间退出', '信号反转', '事件', '主观'];
const LEVEL = { must: ['必须处理', 'bad'], watch: ['关注', 'warn'], ok: ['正常', 'ok'] };
const REGIME = { NORMAL: '正常', CAUTION: '谨慎', DEFENSIVE: '防守', UNKNOWN: '未知' };

/** 录入一笔交易的表单（买入 / 卖出）。可从观察清单一键带出形态与理由（5.11）。 */
export function openTradeForm(pre = {}) {
  const today = new Date().toISOString().slice(0, 10);
  const f = { side: pre.side || 'buy', symbol: pre.symbol || '', date: pre.date || today, price: pre.price ?? '', qty: pre.qty ?? pre.shares ?? '',
    fee: '', initial_stop: pre.initial_stop ?? '', setup: pre.setup || '', exit_reason: pre.exit_reason || '', note: '' };
  const err = h('div', { class: 'alert bad', hidden: true });
  const sizeHint = h('div', { class: 'small', style: 'grid-column:1/-1' });
  // 风险预算（来自观察清单）：高开时按实际成交价重算股数，只少不多，单笔最多亏的钱不变
  const lotOf = sym => (state.market === 'US' ? 1 : /^sh\.688|^688/.test(sym || '') ? 200 : 100);
  function riskHint() {
    const risk = pre.risk_amount, px = +f.price, sp = +f.initial_stop, q = +f.qty;
    if (f.side !== 'buy' || !risk || !px || !sp || px <= sp) { sizeHint.textContent = ''; return; }
    const lot = lotOf(f.symbol);
    const maxQ = lot === 1 ? Math.floor(risk / (px - sp)) : Math.floor(risk / (px - sp) / (lot === 200 ? 1 : 100)) * (lot === 200 ? 1 : 100);
    const ok = maxQ >= lot;
    sizeHint.className = 'alert small ' + (!ok ? 'bad' : q > maxQ ? 'warn' : 'ok');
    sizeHint.textContent = !ok ? `按这个成交价，止损距离太大：最多亏 ${risk.toFixed(0)} 元的预算连一手都买不了——建议放弃这笔。`
      : `按这个成交价，最多买 ${maxQ} 股（最多亏 ${risk.toFixed(0)} 元 ÷ 每股风险 ${(px - sp).toFixed(2)}）` + (q > maxQ ? `；你填的 ${q} 股超出预算，打到止损会多亏 ${((q - maxQ) * (px - sp)).toFixed(0)} 元。` : '。');
  }
  const inp = (k, label, type = 'text', extra = {}) => h('label', { class: 'f ' + (extra.full ? 'full' : '') }, label,
    h('input', { type, step: 'any', value: f[k], placeholder: extra.ph || '', oninput: e => { f[k] = e.target.value; riskHint(); }, ...(extra.attrs || {}) }));
  const stopLabel = h('label', { class: 'f' }, '初始止损价（必填，用于 R 倍数）', h('input', { type: 'number', step: 'any', value: f.initial_stop, oninput: e => { f.initial_stop = e.target.value; riskHint(); } }));
  const exitLabel = h('label', { class: 'f' }, '出场原因（必选）', h('select', { onchange: e => { f.exit_reason = e.target.value; } },
    [['', '请选择'], ...EXIT_REASONS.map(x => [x, x])].map(([v, l]) => h('option', { value: v, selected: f.exit_reason === v ? true : null }, l))));
  const sideSel = h('select', { onchange: e => { f.side = e.target.value; sync(); } }, [['buy', '买入'], ['sell', '卖出']].map(([v, l]) => h('option', { value: v, selected: f.side === v ? true : null }, l)));
  function sync() { stopLabel.style.display = f.side === 'buy' ? '' : 'none'; exitLabel.style.display = f.side === 'sell' ? '' : 'none'; }
  const body = h('div', {}, err,
    pre.name ? h('p', { class: 'hint' }, `${pre.name}（${code(pre.symbol)}）` + (pre.signal_run_id ? ` · 来自观察清单 ${pre.signal_run_id.slice(-10)}` : '')) : null,
    h('div', { class: 'form-grid' },
      h('label', { class: 'f' }, '方向', sideSel), inp('date', '成交日期', 'date'),
      inp('symbol', '股票代码', 'text', { ph: state.market === 'US' ? 'AAPL' : '600519 / sh.600519', attrs: pre.symbol ? { readonly: true } : {} }), inp('price', '成交价', 'number'),
      inp('qty', state.market === 'US' ? '数量（股，整股）' : '数量（股）', 'number'), inp('fee', '费用（留空按配置费率估算）', 'number'),
      stopLabel, exitLabel,
      h('label', { class: 'f' }, '形态标签', h('select', { onchange: e => { f.setup = e.target.value; } }, SETUPS.map(([v, l]) => h('option', { value: v, selected: f.setup === v ? true : null }, l)))),
      inp('note', '备注（出场原因选「主观」须写）', 'text'), sizeHint));
  sync(); riskHint();
  const m = modal(f.side === 'buy' ? '记录买入' : '记录卖出', body, [{ label: '取消' }, { label: '保存', primary: true, onclick: async () => {
    err.hidden = true;
    try {
      const { normSymbol } = await import('./parse.js');
      const sym = normSymbol(f.symbol, state.market);
      if (!sym) throw new Error('股票代码无法识别');
      const payload = { symbol: sym, date: f.date, side: f.side, price: +f.price, qty: +f.qty, setup: f.setup || null, note: f.note || null };
      if (f.fee !== '') payload.fee = +f.fee;
      if (f.side === 'buy') { if (f.initial_stop !== '') payload.initial_stop = +f.initial_stop; payload.signal_run_id = pre.signal_run_id; payload.planned_trigger = pre.planned_trigger; payload.regime = pre.regime; }
      else payload.exit_reason = f.exit_reason;
      await post('/trades', payload);
      toast('已记录', 'ok'); pre.onDone?.(); return true;
    } catch (e) { err.hidden = false; err.textContent = e.message; return false; }
  } }]);
  return m;
}

export const portfolio = {
  layout: 'split',
  async mount(ctx) {
    const el = ctx.listEl;
    const S = { tab: 'pos', health: null, trades: null, account: null };
    ctx.refreshList = load;

    async function load() {
      try {
        const [h_, a, t] = await Promise.all([get('/portfolio/health'), get('/account'), get('/trades')]);
        S.health = h_; S.account = a.account; S.trades = t;
      } catch (e) {
        S.health = null; S.err = e.message;
        try { const [a, t] = await Promise.all([get('/account'), get('/trades')]); S.account = a.account; S.trades = t; } catch { /* 保持空 */ }
      }
      render(); overview();
    }

    function render() {
      clear(el);
      const a = S.account || {};
      el.append(h('div', { class: 'pane-head' },
        h('div', { class: 'row between' }, h('h1', {}, '持仓'), h('div', { class: 'row' },
          h('button', { class: 'btn sm', onclick: overview }, '概览'), h('button', { class: 'btn sm primary', onclick: () => openTradeForm({ onDone: load }) }, '录入交易'))),
        h('div', { class: 'row wrap mt-s small muted num' },
          h('span', {}, '净值 ', h('b', {}, a.equity ? fmtMoney(a.equity) : '未录入')), h('span', {}, '单笔风险 ', h('b', {}, fmtPct(a.risk_per_trade, 2, false))),
          h('button', { class: 'btn sm ghost', onclick: accountDlg }, '账户参数')),
        h('div', { class: 'tabs mt-s', style: 'margin-bottom:0' }, [['pos', '持仓体检'], ['trades', '交易日志']].map(([k, l]) =>
          h('button', { class: S.tab === k ? 'on' : '', onclick: () => { S.tab = k; render(); } }, l)))));
      if (S.tab === 'pos') renderPositions(); else renderTrades();
    }

    function renderPositions() {
      if (S.err && !S.health) { el.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '无法获取体检'), S.err, h('div', { class: 'hint' }, '尚无行情数据时无法体检；请先初始化数据。'))); return; }
      const ps = S.health?.positions || [];
      if (!ps.length) { el.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '当前没有持仓'), h('div', {}, '在「选股」页点「成交了」或点上方「录入交易」登记买入；也可 CSV 批量导入历史交易。'))); return; }
      for (const p of ps) {
        const [lt, lc] = LEVEL[p.level];
        el.append(h('button', { class: 'list-item level-' + p.level, onclick: () => { [...el.querySelectorAll('.list-item')].forEach(x => x.classList.remove('sel')); showDetail(ctx, p.symbol); } },
          h('div', { class: 't1' }, h('span', { class: 'name' }, p.name), h('span', { class: 'code num' }, code(p.symbol)), h('span', { class: 'badge ' + lc }, lt), h('span', { class: 'grow' }),
            h('span', { class: 'num', style: 'font-weight:600' }, fmtPrice(p.price)), h('span', { class: 'num ' + dirClass(p.pnl_pct), style: 'min-width:62px;text-align:right;font-weight:600' }, fmtPct(p.pnl_pct))),
          h('div', { class: 't2 num' }, h('span', {}, `${p.qty} 股 · 成本 ${fmtPrice(p.avg_cost)}`), h('span', { class: dirClass(p.r_multiple) }, p.r_multiple != null ? `${fmtSigned(p.r_multiple, 2)}R` : ''),
            h('span', {}, `持有 ${p.hold_days ?? '—'} 日`), h('span', {}, `止损 ${fmtPrice(p.current_stop)}${p.stop_dist_atr != null ? `（${fmtNum(p.stop_dist_atr, 1)} ATR）` : ''}`),
            p.take_profit ? h('span', { title: '短期止盈：买入价 × 1.10，盘中涨到即全部卖出（挂条件单）' }, `止盈 ${fmtPrice(p.take_profit)}${p.take_profit_dist != null ? `（还差 ${fmtPct(p.take_profit_dist, 1)}）` : ''}`) : null),
          (p.reasons || []).length ? h('div', { class: 'small', style: 'margin-top:3px;color:' + (p.level === 'must' ? 'var(--red)' : 'var(--warn)') }, p.reasons.join('；')) : null,
          p.level !== 'ok' || p.new_stop ? h('div', { class: 'small', style: 'margin-top:2px' }, '▸ ' + p.action) : null,
          (p.flags || []).length ? h('div', { class: 'small muted' }, p.flags.join('；')) : null,
          h('div', { class: 'row', style: 'margin-top:4px' }, h('span', { class: 'btn sm', onclick: e => { e.stopPropagation(); openTradeForm({ symbol: p.symbol, name: p.name, side: 'sell', price: p.price, qty: p.qty, onDone: load }); } }, '记录卖出'))));
      }
    }

    function renderTrades() {
      const items = S.trades?.items || [];
      el.append(h('div', { class: 'row', style: 'padding:10px 16px' }, h('button', { class: 'btn sm', onclick: importDlg }, '导入 CSV'),
        h('button', { class: 'btn sm', onclick: exportAll }, '导出 CSV / JSON')));
      if (!items.length) { el.append(h('div', { class: 'empty' }, '还没有交易记录')); return; }
      for (const t of items) {
        el.append(h('div', { class: 'list-item', style: 'cursor:default' },
          h('div', { class: 't1' }, h('span', { class: 'badge ' + (t.side === 'buy' ? 'accent' : 'warn') }, t.side === 'buy' ? '买' : '卖'), h('span', { class: 'name' }, code(t.symbol)), h('span', { class: 'code num' }, t.date),
            h('span', { class: 'grow' }), h('span', { class: 'num' }, `${fmtPrice(t.price)} × ${t.qty}`)),
          h('div', { class: 't2' }, h('span', {}, '费用 ' + fmtNum(t.fee, 2)), t.setup ? h('span', { class: 'tag' }, t.setup) : null, t.exit_reason ? h('span', { class: 'tag gray' }, t.exit_reason) : null, t.note ? h('span', {}, t.note) : null,
            h('span', { class: 'grow' }), h('span', { class: 'btn sm ghost danger', onclick: async () => { if (await confirmBox('删除这条交易记录？（不会回滚持仓）')) { await del('/trades/' + t.trade_id); load(); } } }, '删除'))));
      }
    }

    // ---- 右侧概览：组合汇总 + 次日参数 + 复盘统计 ----
    async function overview() {
      const box = ctx.detailEl; clear(box); ctx.setMobileDetail(true);
      const sm = S.health?.summary;
      box.append(h('div', { class: 'mobile-top' }, h('button', { class: 'btn sm', onclick: () => ctx.setMobileDetail(false) }, '← 返回')));
      const body = h('div', { class: 'pane-body' });
      box.append(body);
      if (sm) {
        body.append(h('h2', { style: 'margin-bottom:8px' }, '组合概览'), h('div', { class: 'grid c4' },
          kv('总市值', fmtMoney(sm.market_value)), kv('组合风险占用', sm.risk_cap ? `${fmtMoney(sm.risk_to_stop)} / ${fmtMoney(sm.risk_cap)}` : fmtMoney(sm.risk_to_stop)),
          kv('剩余空位', `${sm.slots_free} / ${sm.max_positions}`), kv('市场环境', REGIME[sm.regime] || sm.regime)),
          h('p', { class: 'hint mt-s' }, sm.regime_note + (sm.risk_used_pct != null ? `　· 风险占用 ${fmtPct(sm.risk_used_pct, 0, false)}` : '')),
          Object.keys(sm.industry_share || {}).length ? h('div', { class: 'row wrap mt-s' }, Object.entries(sm.industry_share).map(([k, v]) => h('span', { class: 'tag gray' }, `${k} ${fmtPct(v, 0, false)}`))) : null);
        const np = S.health.next_day_params || [];
        body.append(h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '次日执行参数（在券商端设置条件单）'),
          np.length ? h('button', { class: 'btn sm', onclick: () => copyText(np.map(x => `${code(x.symbol)}\t${x.name}\t止损价 ${fmtPrice(x.stop_price)}\t${x.qty}股\t${x.action}`).join('\n')) }, '复制') : null),
          np.length ? h('table', {}, h('tbody', {}, np.map(x => h('tr', {}, h('td', {}, x.name, h('span', { class: 'muted small num' }, ' ' + code(x.symbol))), h('td', { class: 'num' }, '止损 ' + fmtPrice(x.stop_price)), h('td', { class: 'num' }, x.qty + ' 股'),
            h('td', {}, h('span', { class: 'badge ' + LEVEL[x.level][1] }, LEVEL[x.level][0]), ' ', x.action))))) : h('div', { class: 'hint' }, '今日无需调整。'),
          h('p', { class: 'hint mt-s' }, '体检只用日线数据：盘中触发由券商端条件单承担，系统给出的是次日参数，不是盘中盯盘。')));
      } else body.append(h('h2', {}, '组合概览'), h('p', { class: 'hint' }, '暂无体检数据。'));
      // 复盘统计
      const stBox = h('div', { class: 'mt' }, h('span', { class: 'spinner' }));
      body.append(h('h2', { class: 'mt', style: 'margin-bottom:6px' }, '交易复盘统计'), stBox);
      try {
        const js = await get('/journal/stats');
        clear(stBox);
        if (!js.overall) { stBox.append(h('div', { class: 'hint' }, '尚无已平仓交易。平仓后，这里按「形态 × 市场环境」统计胜率、盈亏比、期望值（R）、MAE / MFE，并对比实盘与回测。')); return; }
        const o = js.overall;
        stBox.append(h('div', { class: 'grid c4' }, kv('已平仓', o.n + ' 笔'), kv('胜率', fmtPct(o.win_rate, 0, false)), kv('期望值', o.expectancy_r != null ? fmtSigned(o.expectancy_r, 2) + 'R' : '—', dirClass(o.expectancy_r)),
          kv('盈亏比', o.payoff != null ? fmtNum(o.payoff, 2) : '—')), o.small_sample ? h('p', { class: 'hint mt-s' }, '⚠ 样本不足（< 20 笔），仅供参考，不下结论。') : null);
        stBox.append(h('div', { class: 'tbl-wrap mt' }, h('table', {}, h('thead', {}, h('tr', {}, ['形态', '环境', '笔数', '胜率', '盈亏比', '期望R', 'PF', 'MAE', 'MFE', ''].map((t, i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
          h('tbody', {}, js.groups.map(g => h('tr', {}, h('td', {}, g.key.setup || '—'), h('td', {}, REGIME[g.key.regime] || g.key.regime || '—'), h('td', { class: 'num' }, g.n), h('td', { class: 'num' }, fmtPct(g.win_rate, 0, false)),
            h('td', { class: 'num' }, g.payoff ?? '—'), h('td', { class: 'num ' + dirClass(g.expectancy_r) }, g.expectancy_r != null ? fmtSigned(g.expectancy_r, 2) : '—'), h('td', { class: 'num' }, g.profit_factor ?? '—'),
            h('td', { class: 'num' }, fmtPct(g.avg_mae, 1)), h('td', { class: 'num' }, fmtPct(g.avg_mfe, 1)), h('td', {}, g.small_sample ? h('span', { class: 'badge warn' }, '样本不足') : null)))))));
        const dev = js.execution_deviation || {};
        if (dev.subjective_exit || dev.chased_entry) stBox.append(h('div', { class: 'card soft mt' }, h('h3', {}, '执行偏差影响'),
          dev.subjective_exit ? h('p', { class: 'small' }, `主观离场 ${dev.subjective_exit.n} 笔：期望 ${dev.subjective_exit.expectancy_r ?? '—'}R；按计划离场：${dev.planned_exit?.expectancy_r ?? '—'}R`) : null,
          dev.chased_entry ? h('p', { class: 'small' }, `追高入场（高于计划触发价 >1%）${dev.chased_entry.n} 笔：期望 ${dev.chased_entry.expectancy_r ?? '—'}R`) : null));
        if (js.live_vs_backtest?.some(x => x.backtest_expectancy_r != null)) stBox.append(h('p', { class: 'hint mt-s' }, '实盘 vs 回测（期望 R）：' + js.live_vs_backtest.map(x => `${x.setup} 实盘 ${x.live_expectancy_r ?? '—'} / 回测 ${x.backtest_expectancy_r ?? '—'}`).join('；')));
      } catch (e) { clear(stBox); stBox.append(h('div', { class: 'hint' }, e.message)); }
    }

    function accountDlg() {
      const a = S.account || {};
      const f = { equity: a.equity ?? '', cash: a.cash ?? '', rpt: a.risk_per_trade != null ? (a.risk_per_trade * 100).toFixed(2) : '0.5' };
      modal('账户参数', h('div', {}, h('p', { class: 'hint' }, '用于风险预算仓位：股数 = 单笔风险金额 ÷（入场价 − 止损价）。由你手动维护；未录入时清单只给「每手风险金额」。'),
        h('div', { class: 'form-grid' }, h('label', { class: 'f' }, '账户净值（元）', h('input', { type: 'number', value: f.equity, oninput: e => { f.equity = e.target.value; } })),
          h('label', { class: 'f' }, '可用资金（元）', h('input', { type: 'number', value: f.cash, oninput: e => { f.cash = e.target.value; } })),
          h('label', { class: 'f' }, '单笔风险（净值的 %）', h('input', { type: 'number', step: '0.05', value: f.rpt, oninput: e => { f.rpt = e.target.value; } })))),
        [{ label: '取消' }, { label: '保存', primary: true, onclick: async () => {
          await put('/account', { equity: f.equity === '' ? null : +f.equity, cash: f.cash === '' ? null : +f.cash, risk_per_trade: (+f.rpt) / 100 }); toast('已保存', 'ok'); load();
        } }]);
    }

    function importDlg() {
      const ta = h('textarea', { rows: 9, style: 'width:100%;font:12px var(--mono)', placeholder: '代码,日期,方向,价格,数量,止损价,形态,出场原因,备注\n600519,2026-09-01,买入,1250,100,1180,breakout,,\n600519,2026-09-15,卖出,1310,100,,,移动止盈,' });
      const file = h('input', { type: 'file', accept: '.csv,.txt,.tsv,.xlsx', onchange: async e => { const f = e.target.files[0]; if (!f) return; try { ta.value = await fileToText(f); } catch (err) { toast(err.message, 'bad'); } } });
      modal('导入交易记录 (CSV / Excel)', h('div', {}, file, ta, h('p', { class: 'hint' }, '必需列：代码 / 日期 / 方向 / 价格 / 数量。买入须有止损价，卖出须有出场原因。按日期顺序入账。')), [{ label: '取消' }, { label: '导入', primary: true, onclick: async () => {
        const { rows, errors } = parseTrades(ta.value, state.market);
        if (!rows.length) { toast(errors[0]?.error || '没有可导入的行', 'bad'); return false; }
        try {
          const r = await post('/trades', { rows });
          const all = [...errors, ...(r.errors || [])];
          toast(`已导入 ${r.items.length} 笔` + (all.length ? `，${all.length} 行失败：${all[0].error}` : ''), all.length ? 'bad' : 'ok'); load();
        } catch (e) { toast(e.message, 'bad'); return false; }
      } }]);
    }

    async function exportAll() {
      const d = await get('/export', {}, { cache: false });
      download(`portfolio-${state.market}-${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(d, null, 1));
      download('trades.csv', toCSV(d.trades, [['date', '日期'], ['symbol', '代码'], ['side', '方向'], ['price', '价格'], ['qty', '数量'], ['fee', '费用'], ['setup', '形态'], ['exit_reason', '出场原因'], ['note', '备注']].map(([key, label]) => ({ key, label }))), 'text/csv');
    }

    await load();
  },
};

function kv(k, v, cls = '') { return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v)); }
