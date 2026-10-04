// 回测页（4.5.6）：策略回测 / 单因子检验 / 形态事件研究 / 漏斗消融 / 参数网格。前端不做任何计算，只展示 POST /api/backtest 的结果。
// 每次结果显示 run_id、配置快照哈希与「试验次数」（6.6），避免只看最好看的一次。
import { get, runBacktest, state } from './api.js';
import { h, clear, fmtPct, fmtNum, fmtSigned, fmtMoney, dirClass, code, toast } from './util.js';
import { lineChart, themeColors } from './chart.js';

const KINDS = [['strategy', '策略回测'], ['single_factor', '单因子检验'], ['event_study', '形态事件研究'], ['ablation', '漏斗消融'], ['param_grid', '参数网格']];
const SETUP_LABEL = { breakout: '突破', pullback: '回踩', vcp: '波动收缩突破' };
const GRID_PRESETS = { breakout: [['vol_ratio_min', [1.2, 1.5, 1.8, 2.2]], ['close_pos_min', [0.6, 0.7, 0.8]], ['n', [20, 50, 252]]],
  pullback: [['rps60_min', [70, 80, 90]], ['vol_ratio_max', [0.6, 0.8, 1.0]], ['depth_atr_min', [-1, -0.5, 0]]],
  vcp: [['atr_ratio_max', [0.6, 0.7, 0.8, 0.9]], ['bb_pct_max', [0.1, 0.2, 0.3]], ['vol_ratio_min', [1.1, 1.3, 1.6, 2.0]]] };

let charts = [];
const killCharts = () => { charts.forEach(c => c.destroy()); charts = []; };

export const backtestView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el); killCharts();
    const S = { kind: 'strategy', start: '', end: '', oos: '', entry_mode: 'next_open', slip: 0.1, cost_mult: 1, max_pos: 8, risk: 0.5,
      setups: { breakout: true, pullback: true, vcp: true }, f: { regime_gate: true, risk_exclusion: true, industry_score: true, rps_score: true },
      n_random: 200, grid_setup: 'breakout', grid_idx: 0, trail: 'atr', stop_k: 2, hold: 20 };
    const out = h('div', { id: 'bt-out' });
    const form = h('div', { class: 'card' });

    const num = (k, label, step = 'any', w = '') => h('label', { class: 'f' }, label, h('input', { type: 'number', step, value: S[k], style: w, oninput: e => { S[k] = e.target.value; } }));
    const chk = (obj, k, label) => h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: obj[k] ? true : null, onchange: e => { obj[k] = e.target.checked; } }), label);

    function renderForm() {
      clear(form);
      form.append(h('div', { class: 'tabs' }, KINDS.map(([k, l]) => h('button', { class: S.kind === k ? 'on' : '', onclick: () => { S.kind = k; renderForm(); } }, l))),
        h('div', { class: 'grid c4' },
          h('label', { class: 'f' }, '开始日期（留空=最早）', h('input', { type: 'date', value: S.start, oninput: e => { S.start = e.target.value; } })),
          h('label', { class: 'f' }, '结束日期', h('input', { type: 'date', value: S.end, oninput: e => { S.end = e.target.value; } })),
          S.kind === 'strategy' ? h('label', { class: 'f' }, '样本外起点（可选）', h('input', { type: 'date', value: S.oos, oninput: e => { S.oos = e.target.value; } })) : null,
          S.kind !== 'single_factor' ? h('label', { class: 'f' }, '入场模式', h('select', { onchange: e => { S.entry_mode = e.target.value; } },
            [['next_open', '次日开盘 next_open'], ['stop_entry', '触发价 stop_entry']].map(([v, l]) => h('option', { value: v, selected: S.entry_mode === v ? true : null }, l)))) : null),
        S.kind !== 'single_factor' ? h('div', { class: 'grid c4 mt-s' }, num('slip', '滑点 %', '0.01'), num('cost_mult', '费用倍数（成本敏感性）', '0.5'), num('max_pos', '最大持仓数', '1'), num('risk', '单笔风险 %', '0.05'),
          num('stop_k', '初始止损 k×ATR', '0.5'), num('hold', '最长持有（交易日）', '1'),
          h('label', { class: 'f' }, '移动止损', h('select', { onchange: e => { S.trail = e.target.value; } }, [['atr', 'ATR 跟踪'], ['ma10', '跟踪 MA10'], ['none', '不移动']].map(([v, l]) => h('option', { value: v, selected: S.trail === v ? true : null }, l))))) : null,
        ['strategy', 'event_study', 'ablation'].includes(S.kind) ? h('div', { class: 'row wrap mt-s' }, h('span', { class: 'hint' }, '形态：'), ...Object.keys(SETUP_LABEL).map(k => chk(S.setups, k, SETUP_LABEL[k]))) : null,
        S.kind === 'strategy' ? h('div', { class: 'row wrap mt-s' }, h('span', { class: 'hint' }, '漏斗：'), chk(S.f, 'risk_exclusion', '⑤ 风险剔除'), chk(S.f, 'regime_gate', '② 市场环境闸门'), chk(S.f, 'industry_score', '② 行业强弱(打分)'), chk(S.f, 'rps_score', '③ RPS(打分)')) : null,
        S.kind === 'event_study' ? h('div', { class: 'mt-s' }, num('n_random', '随机基线抽样次数（规范建议 ≥1000，越大越慢）', '50', 'width:260px')) : null,
        S.kind === 'param_grid' ? h('div', { class: 'row wrap mt-s' }, h('label', { class: 'f' }, '形态', h('select', { onchange: e => { S.grid_setup = e.target.value; S.grid_idx = 0; renderForm(); } }, Object.entries(SETUP_LABEL).map(([v, l]) => h('option', { value: v, selected: S.grid_setup === v ? true : null }, l)))),
          h('label', { class: 'f' }, '参数', h('select', { onchange: e => { S.grid_idx = +e.target.value; } }, GRID_PRESETS[S.grid_setup].map(([p, vs], i) => h('option', { value: i, selected: S.grid_idx === i ? true : null }, `${p}：${vs.join(' / ')}`)))),
          h('span', { class: 'hint' }, '每个形态最多 3 个自由参数参与网格搜索（5.4.1）；每个网格点都计入试验次数。')) : null,
        h('div', { class: 'row mt' }, h('button', { class: 'btn primary', id: 'bt-run', onclick: run }, '运行'),
          h('span', { class: 'hint' }, '结果不是收益预测，是对历史规律的统计检验；参数请只用样本外结果确认。')));
    }

    function strategyOverride() {
      const f = { ...S.f };
      return {
        id: 'ui-' + S.kind, start: S.start || null, end: S.end || null, oos_start: S.oos || null, entry_mode: S.entry_mode, slippage: S.slip / 100, cost_mult: +S.cost_mult,
        setups: Object.keys(S.setups).filter(k => S.setups[k]),
        exits: { stop_atr_k: +S.stop_k, max_hold_days: +S.hold, trail: S.trail },
        portfolio: { max_positions: +S.max_pos, risk_per_trade: S.risk / 100 }, funnel: f,
      };
    }

    async function run() {
      const btn = document.getElementById('bt-run'); btn.disabled = true; btn.textContent = '运行中…';
      clear(out); killCharts();
      const prog = h('div', { class: 'alert info' }, h('span', { class: 'spinner' }), ' 回测运行中（后台线程，完成后自动显示）…');
      out.append(prog);
      try {
        const st = strategyOverride();
        let params = { strategy: st };
        if (S.kind === 'single_factor') params = { strategy: { start: S.start || null, end: S.end || null } };
        if (S.kind === 'event_study') params = { strategy: st, n_random: +S.n_random, setups: st.setups };
        if (S.kind === 'param_grid') { const [p, vs] = GRID_PRESETS[S.grid_setup][S.grid_idx]; params = { strategy: st, setup: S.grid_setup, param: p, values: vs }; }
        const r = await runBacktest(S.kind, params);
        clear(out); renderResult(r);
        loadRuns();
      } catch (e) { clear(out); out.append(h('div', { class: 'alert bad' }, e.message)); }
      btn.disabled = false; btn.textContent = '运行';
    }

    function header(r) {
      return h('div', { class: 'row wrap small muted', style: 'margin-bottom:8px' }, h('span', { class: 'badge accent' }, 'run_id ' + (r.run_id || '').slice(-22)),
        r.config_hash ? h('span', { class: 'badge' }, 'config ' + r.config_hash) : null, h('span', { class: 'badge warn', title: '参数 / 规则的尝试次数，用于评估多重检验带来的虚假显著' }, '试验次数 ' + (r.trial_count ?? '—')),
        h('span', { class: 'badge' }, '数据截止 ' + (r.data_asof || '—')), r.code_version ? h('span', { class: 'badge' }, 'code ' + r.code_version) : null);
    }

    function renderResult(r) {
      killCharts();
      out.append(header(r));
      if (r.error) { out.append(h('div', { class: 'alert bad' }, r.message || r.error)); return; }
      ({ strategy: renderStrategy, single_factor: renderFactor, event_study: renderEvents, ablation: renderAblation, param_grid: renderGrid }[r.kind] || (() => {}))(r);
      if (r.notes) out.append(h('details', { class: 'mt', open: true }, h('summary', {}, '已知偏差与局限（请务必阅读）'), h('ul', { class: 'small muted', style: 'margin:0;padding-left:18px' }, r.notes.map(n => h('li', {}, n)))));
      if (r.note || r.caveat) out.append(h('p', { class: 'hint mt-s' }, [r.note, r.caveat].filter(Boolean).join(' ')));
    }

    const kv = (k, v, cls = '', tip = '') => h('div', { class: 'kv', title: tip }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v));

    function renderStrategy(r) {
      const m = r.metrics;
      if (m.error) { out.append(h('div', { class: 'alert bad' }, m.error)); return; }
      out.append(h('div', { class: 'card' }, h('div', { class: 'grid c4' },
        kv('总收益', fmtPct(m.total_return), dirClass(m.total_return)), kv('年化', fmtPct(m.cagr), dirClass(m.cagr)), kv('最大回撤', fmtPct(m.max_drawdown), 'down', `回撤持续最长 ${m.max_dd_days} 交易日`), kv('夏普 / Sortino / Calmar', `${m.sharpe ?? '—'} / ${m.sortino ?? '—'} / ${m.calmar ?? '—'}`)),
        h('div', { class: 'grid c4 mt-s' }, kv('交易笔数', m.n ?? 0), kv('胜率', fmtPct(m.win_rate, 0, false)), kv('期望值 (R)', m.expectancy_r != null ? fmtSigned(m.expectancy_r, 2) : '—', dirClass(m.expectancy_r)), kv('盈亏比 / PF', `${m.payoff ?? '—'} / ${m.profit_factor ?? '—'}`)),
        h('div', { class: 'grid c4 mt-s' }, kv('平均持有', (m.avg_hold_days ?? '—') + ' 日'), kv('MAE / MFE', `${fmtPct(m.avg_mae, 1)} / ${fmtPct(m.avg_mfe, 1)}`), kv('资金利用率', fmtPct(m.exposure, 0, false)), kv('最大连亏', (m.max_consec_losses ?? '—') + ' 笔')),
        h('div', { class: 'grid c4 mt-s' }, kv('相对基准超额', fmtPct(m.excess_return), dirClass(m.excess_return)), kv('alpha / beta', `${fmtPct(m.alpha, 1)} / ${m.beta ?? '—'}`), kv('年换手', (m.turnover_per_year ?? '—') + ' 倍'), kv('亏损>1.5R', (m.loss_over_1_5r ?? 0) + ' 次', '', '止损可执行性检验：跳空 / 跌停导致实际亏损超过计划风险'))));
      const c = themeColors();
      const eq = h('div', {}), dd = h('div', {});
      out.append(h('div', { class: 'card mt' }, h('h3', {}, '权益曲线（与基准对比）'), eq, h('h3', { style: 'margin-top:8px' }, '回撤'), dd));
      const ser = [{ name: '策略', color: c.accent, data: r.equity_curve.map(x => ({ time: x.date, value: x.equity })) }];
      if (r.benchmark_curve) ser.push({ name: '基准', color: c.flat, data: r.benchmark_curve.map(x => ({ time: x.date, value: x.value })) });
      charts.push(lineChart(eq, ser, { height: 260 }));
      charts.push(lineChart(dd, [{ name: '回撤', color: '#e11d48', area: true, data: r.drawdown_curve.map(x => ({ time: x.date, value: x.dd })) }], { height: 120, percent: true }));
      const sample = m.sample || {};
      out.append(h('div', { class: 'card mt' }, h('h3', {}, '样本量'), h('p', { class: 'small num' }, `形态信号 ${sample.signals} → 风险剔除后 ${sample.after_exclusion} → 下单 ${sample.ordered} → 成交 ${sample.filled}；闸门挡下 ${sample.regime_blocked}；放弃 ${sample.abandoned_total}`),
        h('div', { class: 'row wrap' }, Object.entries(sample.abandoned || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => h('span', { class: 'tag gray' }, `${k} ${v}`)))));
      const segTable = (title, obj) => h('div', { class: 'card' }, h('h3', {}, title), h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['', '笔数', '胜率', '期望R', '盈亏比', 'PF', '合计盈亏'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
        h('tbody', {}, Object.entries(obj).map(([k, s]) => h('tr', {}, h('td', {}, k), h('td', { class: 'num' }, s.n), h('td', { class: 'num' }, fmtPct(s.win_rate, 0, false)), h('td', { class: 'num ' + dirClass(s.expectancy_r) }, s.expectancy_r != null ? fmtSigned(s.expectancy_r, 2) : '—'),
          h('td', { class: 'num' }, s.payoff ?? '—'), h('td', { class: 'num' }, s.profit_factor ?? '—'), h('td', { class: 'num ' + dirClass(s.pnl) }, fmtMoney(s.pnl))))))));
      out.append(h('div', { class: 'grid c2 mt' }, segTable('按年度（6.6.4）', r.segments.by_year), segTable('按市场环境状态（6.6.4）', r.segments.by_regime)));
      if (r.oos) out.append(h('div', { class: 'card mt' }, h('h3', {}, `样本内 / 样本外（起点 ${r.oos.oos_start}）`), h('div', { class: 'grid c2 mt-s' },
        h('div', {}, h('div', { class: 'small muted' }, '样本内'), h('div', { class: 'num' }, `${r.oos.in_sample.n} 笔 · 期望 ${r.oos.in_sample.expectancy_r ?? '—'}R · 胜率 ${fmtPct(r.oos.in_sample.win_rate, 0, false)}`)),
        h('div', {}, h('div', { class: 'small muted' }, '样本外（以此为准）'), h('div', { class: 'num' }, `${r.oos.out_of_sample.n} 笔 · 期望 ${r.oos.out_of_sample.expectancy_r ?? '—'}R · 胜率 ${fmtPct(r.oos.out_of_sample.win_rate, 0, false)}`)))));
      const tr = (r.trades || []).slice(-150).reverse();
      out.append(h('details', { class: 'mt' }, h('summary', {}, `交易明细（最近 ${tr.length} 笔，共 ${(r.trades || []).length}）`), h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['股票', '形态', '入场', '出场', '入场价', '出场价', '股数', '盈亏', 'R', '持有', '原因'].map((t, i) => h('th', { class: i > 3 && i < 10 ? 'r' : '' }, t)))),
        h('tbody', {}, tr.map(t => h('tr', {}, h('td', {}, t.name || code(t.symbol)), h('td', {}, SETUP_LABEL[t.setup] || t.setup), h('td', { class: 'num' }, t.entry_date), h('td', { class: 'num' }, t.exit_date), h('td', { class: 'num' }, fmtNum(t.entry_px)), h('td', { class: 'num' }, fmtNum(t.exit_px)),
          h('td', { class: 'num' }, t.shares), h('td', { class: 'num ' + dirClass(t.pnl) }, fmtMoney(t.pnl)), h('td', { class: 'num ' + dirClass(t.r) }, t.r != null ? fmtSigned(t.r, 2) : '—'), h('td', { class: 'num' }, t.hold_days), h('td', {}, t.exit_reason))))))));
    }

    function renderFactor(r) {
      const hs = r.horizons;
      out.append(h('div', { class: 'card' }, h('h3', {}, 'Rank IC（因子截面排名 vs 未来 h 日收益）· 当日交易池 L2'),
        h('p', { class: 'hint' }, '未来收益按 5.5.1 成交假设：T 日收盘出信号，T+1 开盘入场，持有 h 日后收盘。t 值已按前瞻窗口重叠折算有效样本数。方向不预设（如 5 日动量是延续还是反转，以检验为准）。'),
        h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, [h('th', {}, '因子'), ...hs.flatMap(x => [h('th', { class: 'r' }, `IC(${x}d)`), h('th', { class: 'r' }, `IR`), h('th', { class: 'r' }, `t`), h('th', { class: 'r' }, '单调')]), h('th', {}, '判定（通过标准见设置）')])),
          h('tbody', {}, r.factors.map(f => h('tr', {}, h('td', {}, f.factor), ...hs.flatMap(x => { const v = f.horizons[x] || {}; return [h('td', { class: 'num ' + dirClass(v.ic_mean) }, v.ic_mean != null ? fmtSigned(v.ic_mean, 3) : '—'), h('td', { class: 'num' }, v.ic_ir ?? '—'), h('td', { class: 'num' }, v.t_stat_eff ?? '—'), h('td', { class: 'num' }, v.monotonic ?? '—')]; }),
            h('td', { class: 'small' }, f.verdict?.pass ? h('span', { class: 'badge ok' }, '通过 · ' + f.verdict.direction) : h('span', { class: 'badge', title: f.verdict?.reason }, '未通过')))))))));
      out.append(h('div', { class: 'card mt' }, h('h3', {}, '分层收益（5 组，第 5 组因子最大；单位：持有期平均收益）'), h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['因子', '周期', 'G1', 'G2', 'G3', 'G4', 'G5', '多空'].map((t, i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
        h('tbody', {}, r.factors.flatMap(f => { const x = hs[Math.floor(hs.length / 2)], v = f.horizons[x]; if (!v?.group_ret) return []; return [h('tr', {}, h('td', {}, f.factor), h('td', {}, x + 'd'), ...v.group_ret.map(g => h('td', { class: 'num ' + dirClass(g) }, fmtPct(g, 2))), h('td', { class: 'num ' + dirClass(v.long_short) }, fmtPct(v.long_short, 2)))]; }))))));
    }

    function renderEvents(r) {
      for (const s of r.setups) {
        const rb = s.random_baseline;
        out.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h2', {}, s.label), h('span', { class: 'badge ' + (s.pass ? 'ok' : 'warn'), style: 'white-space:normal;text-align:right;max-width:70%' }, s.verdict)),
          s.mean_r == null ? h('div', { class: 'hint' }, s.verdict) : h('div', {},
            h('div', { class: 'grid c4' }, kv('信号 / 成交', `${s.n_signals} / ${s.n_filled}`), kv('独立信号日', s.independent_signal_days, s.independent_signal_days < 50 ? 'down' : ''), kv('期望 R（按日聚合）', fmtSigned(s.mean_r_by_day, 2), dirClass(s.mean_r_by_day)), kv('胜率', fmtPct(s.win_rate, 0, false))),
            h('div', { class: 'grid c4 mt-s' }, kv('95% 置信区间（block bootstrap）', `[${s.mean_r_ci95?.[0] ?? '—'}, ${s.mean_r_ci95?.[1] ?? '—'}]`), kv('随机基线均值 R', rb ? fmtSigned(rb.mean_r, 2) : '—'), kv('p 值（vs 随机入场）', rb ? String(rb.p_value) : '—', rb && rb.p_value < 0.05 ? 'up' : ''), kv('盈亏比 / PF', `${s.payoff ?? '—'} / ${s.profit_factor ?? '—'}`)),
            h('div', { class: 'tbl-wrap mt-s' }, h('table', {}, h('thead', {}, h('tr', {}, ['触发后', '平均', '中位', '相对基准超额'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
              h('tbody', {}, Object.entries(s.post_returns || {}).map(([k, v]) => h('tr', {}, h('td', {}, k + ' 日'), h('td', { class: 'num ' + dirClass(v.mean) }, fmtPct(v.mean)), h('td', { class: 'num' }, fmtPct(v.median)), h('td', { class: 'num ' + dirClass(v.excess_vs_bench) }, fmtPct(v.excess_vs_bench))))))),
            h('div', { class: 'row wrap mt-s small' }, h('span', { class: 'muted' }, '分年度：'), ...Object.entries(s.by_year || {}).map(([y, v]) => h('span', { class: 'tag gray' }, `${y} ${fmtSigned(v.mean_r, 2)}R (${v.n})`))),
            h('div', { class: 'row wrap mt-s small' }, h('span', { class: 'muted' }, '分环境：'), ...Object.entries(s.by_regime || {}).map(([y, v]) => h('span', { class: 'tag gray' }, `${y} ${fmtSigned(v.mean_r, 2)}R (${v.n})`))))));
      }
    }

    function renderAblation(r) {
      out.append(h('div', { class: 'card' }, h('h3', {}, '漏斗消融（逐层加入，全部走同一套成交规则与成本）'), h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['步骤', '形态信号', '成交笔数', '期望R', '胜率', '最大回撤', '总收益', '夏普', '年换手'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
        h('tbody', {}, r.steps.map(s => h('tr', {}, h('td', {}, s.step), h('td', { class: 'num' }, s.signals ?? '—'), h('td', { class: 'num' }, s.n_trades), h('td', { class: 'num ' + dirClass(s.expectancy_r) }, s.expectancy_r != null ? fmtSigned(s.expectancy_r, 2) : '—'),
          h('td', { class: 'num' }, fmtPct(s.win_rate, 0, false)), h('td', { class: 'num down' }, fmtPct(s.max_drawdown)), h('td', { class: 'num ' + dirClass(s.total_return) }, fmtPct(s.total_return)), h('td', { class: 'num' }, s.sharpe ?? '—'), h('td', { class: 'num' }, s.turnover ?? '—'))))))));
      const g = r.gate_value;
      out.append(h('div', { class: 'card mt' }, h('div', { class: 'card-title' }, h('h3', {}, '闸门价值检验（6.6.4）'), h('span', { class: 'badge ' + (g.keep_gate ? 'ok' : 'warn') }, g.keep_gate == null ? '无结论' : g.keep_gate ? '建议保留闸门' : '闸门未显示价值')),
        h('div', { class: 'grid c2' }, h('div', {}, h('div', { class: 'small muted' }, '带闸门'), h('div', { class: 'num' }, `回撤 ${fmtPct(g.with_gate.max_drawdown)} · 期望 ${g.with_gate.expectancy_r ?? '—'}R · 收益 ${fmtPct(g.with_gate.total_return)} · ${g.with_gate.n} 笔`)),
          h('div', {}, h('div', { class: 'small muted' }, '不带闸门'), h('div', { class: 'num' }, `回撤 ${fmtPct(g.without_gate.max_drawdown)} · 期望 ${g.without_gate.expectancy_r ?? '—'}R · 收益 ${fmtPct(g.without_gate.total_return)} · ${g.without_gate.n} 笔`))),
        h('p', { class: 'hint mt-s' }, g.rule)));
    }

    function renderGrid(r) {
      out.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', {}, `${SETUP_LABEL[r.setup]} · ${r.param}`), h('span', { class: 'badge ' + (r.stable ? 'ok' : 'warn') }, r.stable ? '参数相对稳健' : '结果对参数敏感（过拟合风险）')),
        h('table', {}, h('thead', {}, h('tr', {}, ['取值', '笔数', '期望R', '胜率', '最大回撤', '总收益'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
          h('tbody', {}, r.rows.map(x => h('tr', {}, h('td', { class: 'num' }, x.value), h('td', { class: 'num' }, x.n), h('td', { class: 'num ' + dirClass(x.expectancy_r) }, x.expectancy_r != null ? fmtSigned(x.expectancy_r, 2) : '—'), h('td', { class: 'num' }, fmtPct(x.win_rate, 0, false)), h('td', { class: 'num down' }, fmtPct(x.max_drawdown)), h('td', { class: 'num ' + dirClass(x.total_return) }, fmtPct(x.total_return))))))));
    }

    const runsBox = h('div', { class: 'card mt' });
    async function loadRuns() {
      clear(runsBox);
      try {
        const r = await get('/backtest/runs');
        runsBox.append(h('h3', {}, '历史记录（可复现：同一 run_id 对应同一配置快照与数据截止日）'),
          r.items.length ? h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['时间', '类型', 'run_id', '试验次数', '期望R', '总收益', ''].map(t => h('th', {}, t)))),
            h('tbody', {}, r.items.map(x => h('tr', {}, h('td', { class: 'num small' }, x.created_at.replace('T', ' ')), h('td', {}, (KINDS.find(k => k[0] === x.kind) || [, x.kind])[1]), h('td', { class: 'num small' }, x.run_id.slice(-22)), h('td', { class: 'num' }, x.trial_count),
              h('td', { class: 'num' }, x.metrics?.expectancy_r ?? '—'), h('td', { class: 'num' }, x.metrics?.total_return != null ? fmtPct(x.metrics.total_return) : '—'),
              h('td', {}, h('button', { class: 'btn sm ghost', onclick: async () => { clear(out); const rr = await get('/backtest/run/' + x.run_id, {}, { market: false }); renderResult(rr); window.scrollTo(0, 0); } }, '查看'))))))) : h('div', { class: 'hint' }, '还没有回测记录'));
      } catch (e) { runsBox.append(h('div', { class: 'hint' }, e.message)); }
    }

    el.append(h('div', { class: 'pane-body page' }, h('div', { class: 'row between mb' }, h('h1', {}, '回测与验证'), h('span', { class: 'hint' }, '回测只在后端运行；前端只展示')), form, h('div', { class: 'mt' }, out), runsBox));
    renderForm(); loadRuns();
    out.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '选择一种检验并点「运行」'),
      h('div', {}, '建议顺序：单因子检验 → 形态事件研究 → 策略回测 → 漏斗消融。先确认因子 / 形态本身有预测力，再组合成完整策略。')));
  },
  cleanup() { killCharts(); },
};
