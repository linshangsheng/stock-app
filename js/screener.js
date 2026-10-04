// 选股（默认首页，4.5.3）：状态条（数据完整性 / 市场环境 / run_id 与清单有效期 / 持仓告警）+ 今日观察清单（按形态分组）
// + 次日执行清单（可复制到券商端设置条件单）+ 条件筛选面板 + 历史扫描回看。清单来自后端存档，前端不做任何计算。
import { get, post, put, state } from './api.js';
import { h, clear, fmtPrice, fmtPct, fmtNum, fmtMoney, dirClass, code, toast, copyText, esc, shortDate, lotName } from './util.js';
import { showDetail } from './detail.js';
import { openTradeForm } from './portfolio.js';

const REGIME = { NORMAL: ['正常', '正常开仓'], CAUTION: ['谨慎', '仓位上限减半，只做最强候选'], DEFENSIVE: ['防守', '不开新仓'], UNKNOWN: ['未知', '基准数据缺失，按谨慎处理'] };
const SETUP_ORDER = ['breakout', 'pullback', 'vcp'];
const SETUP_LABEL = { breakout: '突破', pullback: '回踩', vcp: '波动收缩突破' };

export const screener = {
  layout: 'split',
  async mount(ctx) {
    const S = { tab: 'list', data: null, preview: false, sel: null };
    const el = ctx.listEl;
    ctx.refreshList = () => load();

    async function load(preview = false) {
      S.preview = preview;
      clear(el);
      el.append(h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载观察清单…'));
      let d, jobs, health;
      try {
        [d, jobs, health] = await Promise.all([
          get('/scan', preview ? { preview: 1 } : {}),
          get('/jobs').catch(() => null),
          get('/portfolio/health').catch(() => null),
        ]);
      } catch (e) { clear(el); el.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '无法加载'), e.message)); return; }
      S.data = d; S.jobs = jobs; S.health = health;
      render();
    }

    function render() {
      clear(el);
      const d = S.data, run = d.run;
      el.append(statusBar(d, S.jobs, S.health, ctx), tabsBar());
      if (d._stale) el.append(h('div', { class: 'alert warn', style: 'margin:10px 16px' }, '后端未连接：显示的是最近一次缓存快照（数据延迟）'));
      if (S.tab === 'list') renderList(d);
      else if (S.tab === 'exec') renderExec(d);
      else if (S.tab === 'filter') renderFilter();
      else renderHistory();
    }

    function tabsBar() {
      const tabs = [['list', '观察清单'], ['exec', '次日执行'], ['filter', '条件筛选'], ['hist', '历史回看']];
      return h('div', { class: 'tabs', style: 'padding:0 8px;margin:0' }, tabs.map(([k, l]) =>
        h('button', { class: S.tab === k ? 'on' : '', onclick: () => { S.tab = k; render(); } }, l)));
    }

    function renderList(d) {
      const run = d.run;
      if (!run) { el.append(emptyNoRun(d, ctx, load)); return; }
      if (!run.official && !S.preview) {
        el.append(h('div', { style: 'padding:16px' },
          h('div', { class: 'alert bad' }, h('b', {}, '数据完整性闸门未通过，不生成观察清单。'),
            h('ul', { style: 'margin:6px 0 0 18px;padding:0' }, (run.gate.reasons || []).map(r => h('li', {}, r)))),
          h('div', { class: 'row mt' },
            h('button', { class: 'btn', onclick: () => load(true) }, '仅供参考：查看即时计算结果'),
            h('button', { class: 'btn', onclick: () => runJob('daily') }, '重新运行盘后任务（补拉数据）')),
          h('p', { class: 'hint mt' }, '宁可不出结果：上游数据缺失会让市场宽度与 RPS 排名悄悄失真（需求书 3.25.1）。「仅供参考」的结果不写入正式存档。')));
        return;
      }
      if (S.preview) el.append(h('div', { class: 'alert warn', style: 'margin:10px 16px' }, '仅供参考：数据不完整，此结果不计入正式存档。'));
      if (run.is_stale) el.append(h('div', { class: 'alert warn', style: 'margin:10px 16px' },
        `这是 ${run.scan_date} 收盘的清单，有效期至 ${run.valid_until || '下一交易日'} 开盘前——该交易日已过，仅作记录，不再作为可执行清单。`));
      const cands = d.candidates || [];
      const sum = run.summary || {};
      if (run.regime?.new_positions_allowed === false) {
        el.append(h('div', { class: 'alert bad', style: 'margin:10px 16px' }, h('b', {}, '市场环境：防守。'), ` 不开新仓（本次共有 ${sum.suppressed_by_regime ?? 0} 个形态信号被闸门挡下）。`));
      }
      if (!cands.length) {
        el.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '今日没有符合条件的候选'),
          h('div', {}, `交易池 ${sum.universe_l2 ?? '—'} 只，形态信号 ${sum.signals_today ?? 0} 个，风险剔除后 ${sum.n_after_exclusion ?? 0} 个。`), funnelNote(sum)));
        return;
      }
      el.append(h('div', { class: 'small muted', style: 'padding:8px 16px' },
        `交易池 ${sum.universe_l2} 只 → 形态信号 ${sum.signals_today} → 风险剔除后 ${sum.n_after_exclusion} → 入选 ${cands.length}`));
      const groups = {};
      for (const c of cands) (groups[c.setup] ||= []).push(c);
      for (const g of SETUP_ORDER.filter(k => groups[k])) {
        el.append(h('div', { class: 'group-h' }, h('span', {}, SETUP_LABEL[g]), h('span', {}, groups[g].length + ' 只')));
        for (const c of groups[g]) el.append(candRow(c));
      }
      el.append(funnelNote(sum));
    }

    function candRow(c) {
      const row = h('button', { class: 'list-item' + (c.fit === false ? ' dim' : '') + (S.sel === c.symbol ? ' sel' : ''), dataset: { symbol: c.symbol },
        onclick: () => { S.sel = c.symbol; [...el.querySelectorAll('.list-item')].forEach(x => x.classList.toggle('sel', x.dataset.symbol === c.symbol)); showDetail(ctx, c.symbol); } },
        h('div', { class: 't1' },
          h('span', { class: 'name' }, c.name), h('span', { class: 'code num' }, code(c.symbol)),
          c.held ? h('span', { class: 'badge accent' }, '持仓中') : null, c.watched ? h('span', { class: 'badge' }, '★') : null,
          h('span', { class: 'grow' }), h('span', { class: 'score' }, fmtNum(c.score, 0))),
        h('div', { class: 'row wrap gap-s', style: 'margin-top:3px' }, (c.reasons || []).slice(1).map(r => h('span', { class: 'tag' }, r))),
        h('div', { class: 't2 num' },
          h('span', {}, '触发 ', h('b', {}, fmtPrice(c.trigger_price))), h('span', {}, '止损 ', h('b', {}, fmtPrice(c.stop_price))),
          h('span', {}, '距止损 ', fmtPct(-(c.stop_dist_pct ?? NaN), 1)),
          h('span', {}, c.shares != null ? `建议 ${c.shares} 股 · 风险 ${fmtMoney(c.risk_amount)}` : `每${lotName()}风险 ${fmtMoney(c.risk_per_lot)}（未录入账户）`)),
        c.fit === false ? h('div', { class: 'small', style: 'color:var(--warn);margin-top:2px' }, '未列入执行：' + c.skip_reason) : null,
        c.outcome && c.outcome.filled ? h('div', { class: 't2 num' }, h('span', {}, '后续：'),
          ...[['1d', 'ret_1d'], ['5d', 'ret_5d'], ['20d', 'ret_20d']].map(([l, k]) => c.outcome[k] != null ? h('span', { class: dirClass(c.outcome[k]) }, `${l} ${fmtPct(c.outcome[k])}`) : null)) : null);
      return row;
    }

    function funnelNote(sum) {
      const ex = sum.excluded || {};
      const bits = [];
      if (ex.held) bits.push(`已持有 ${ex.held}`); if (ex.limit_up) bits.push(`涨停/次日难成交 ${ex.limit_up}`);
      if (ex.earnings) bits.push(`财报窗口 ${ex.earnings}`); if (ex.anomaly) bits.push(`数据异常 ${ex.anomaly}`);
      return h('div', { class: 'hint', style: 'padding:10px 16px' },
        bits.length ? '风险剔除：' + bits.join('，') + '。' : '',
        sum.earnings_data === false ? (sum.earnings_failed ? ` ⚠ ${sum.earnings_failed} 只候选的财报日历获取失败（上游不可用），未做财报窗口排除。` : '') : '');
    }

    function renderExec(d) {
      const cands = (d.candidates || []).filter(c => c.fit !== false);
      if (!d.run?.official || !cands.length) { el.append(h('div', { class: 'empty' }, '没有可执行的清单。')); return; }
      const acct = cands.some(c => c.shares != null);
      const lines = cands.map(c => `${code(c.symbol)}\t${c.name}\t触发价 ${fmtPrice(c.trigger_price)}\t止损价 ${fmtPrice(c.stop_price)}\t${c.shares != null ? c.shares + '股' : '—'}`);
      el.append(h('div', { style: 'padding:12px 16px' },
        h('div', { class: 'row between' }, h('h3', {}, `次日（${d.run.valid_until || '下一交易日'}）执行参数`),
          h('button', { class: 'btn sm', onclick: () => copyText('代码\t名称\t触发价\t止损价\t股数\n' + lines.join('\n')) }, '一键复制')),
        h('p', { class: 'hint' }, (state.market === 'US' ? '美股按整股取整。' : 'A 股股数已按整手取整。') + '系统不接券商下单——请在券商端按这些参数设置条件单。',
          acct ? '' : ' 尚未录入账户净值，股数为空（到「持仓」页录入账户参数）。'),
        h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['代码', '名称', '触发价', '止损价', '股数', ''].map((t, i) => h('th', { class: i > 1 && i < 5 ? 'r' : '' }, t)))),
          h('tbody', {}, cands.map(c => h('tr', {}, h('td', { class: 'num' }, code(c.symbol)), h('td', {}, c.name), h('td', { class: 'num' }, fmtPrice(c.trigger_price)),
            h('td', { class: 'num' }, fmtPrice(c.stop_price)), h('td', { class: 'num' }, c.shares ?? '—'),
            h('td', {}, h('button', { class: 'btn sm', onclick: () => openTradeForm({ symbol: c.symbol, name: c.name, side: 'buy', price: c.entry_ref, initial_stop: c.stop_price,
              setup: c.setup, signal_run_id: d.run.run_id, planned_trigger: c.trigger_price, shares: c.shares, regime: d.run.regime?.state, onDone: load }) }, '成交了'))))))),
        h('div', { class: 'row mt' }, h('button', { class: 'btn', onclick: async () => {
          const syms = cands.filter(c => !c.watched).map(c => ({ symbol: c.symbol })); if (!syms.length) return toast('都已在自选');
          await put('/watchlist', { add: syms }); await ctx.reloadWatch(); toast(`已加入 ${syms.length} 只到自选`, 'ok'); load();
        } }, '全部加入自选'))));
    }

    async function renderFilter() {
      const F = S.filter ||= { min_rps20: 80, min_vol_ratio: '', max_atr_pct: '', above_ma50: true, ma20_gt_ma50: true, breakout20: false, max_dist_52w_high: '', min_amount_wan: '', sort: 'rps_20' };
      const num = (k, label, ph) => h('label', { class: 'f' }, label, h('input', { type: 'number', step: 'any', placeholder: ph || '', value: F[k], oninput: e => { F[k] = e.target.value; } }));
      const chk = (k, label) => h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: F[k] ? true : null, onchange: e => { F[k] = e.target.checked; } }), label);
      const out = h('div', {});
      el.append(h('div', { style: 'padding:12px 16px' },
        h('p', { class: 'hint' }, '漏斗之外的手动补充：按第三章指标自定义组合筛选（当前交易池 L2）。'),
        h('div', { class: 'grid c2' }, num('min_rps20', 'RPS20 ≥'), num('min_vol_ratio', '量比 ≥'), num('max_atr_pct', 'ATR% ≤（百分数）'), num('max_dist_52w_high', '距52周高 ≤（%）'), num('min_amount_wan', '20日均成交额 ≥（万元）'),
          h('label', { class: 'f' }, '排序', h('select', { onchange: e => { F.sort = e.target.value; } }, [['rps_20', 'RPS20'], ['rps_60', 'RPS60'], ['vol_ratio', '量比'], ['ret_20', '20日涨幅']].map(([v, l]) => h('option', { value: v, selected: F.sort === v ? true : null }, l))))),
        h('div', { class: 'row wrap mt-s' }, chk('above_ma50', '收盘 > MA50'), chk('ma20_gt_ma50', 'MA20 > MA50'), chk('breakout20', '突破 20 日高点')),
        h('div', { class: 'row mt' }, h('button', { class: 'btn primary', onclick: run }, '筛选')), out));
      async function run() {
        clear(out); out.append(h('div', { class: 'empty' }, h('span', { class: 'spinner' })));
        try {
          const r = await post('/analysis', { conditions: F, sort: F.sort, limit: 120 });
          clear(out);
          out.append(h('p', { class: 'hint mt' }, `交易池 ${r.universe} 只，命中 ${r.total} 只${r.total > r.items.length ? `（显示前 ${r.items.length}）` : ''}　· 数据 ${r.date}`),
            h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['名称', '收盘', '20日', 'RPS20', '量比', 'ATR%'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
              h('tbody', {}, r.items.map(it => h('tr', { style: 'cursor:pointer', onclick: () => showDetail(ctx, it.symbol) },
                h('td', {}, it.name, h('span', { class: 'muted small num' }, ' ' + code(it.symbol))), h('td', { class: 'num' }, fmtPrice(it.close)),
                h('td', { class: 'num ' + dirClass(it.ret_20) }, fmtPct(it.ret_20, 1)), h('td', { class: 'num' }, fmtNum(it.rps_20, 0)),
                h('td', { class: 'num' }, fmtNum(it.vol_ratio, 2)), h('td', { class: 'num' }, fmtPct(it.atr_pct, 1, false))))))));
        } catch (e) { clear(out); out.append(h('div', { class: 'alert bad mt' }, e.message)); }
      }
    }

    async function renderHistory() {
      const box = h('div', { style: 'padding:12px 16px' }, h('span', { class: 'spinner' }));
      el.append(box);
      let r;
      try { r = await get('/scan/history'); } catch (e) { clear(box); box.append(h('div', { class: 'alert bad' }, e.message)); return; }
      clear(box);
      const o = r.overall || {};
      box.append(h('div', { class: 'card soft' }, h('h3', {}, '选股器自身表现（真实前向收益）'),
        h('div', { class: 'grid c4 mt-s' }, ...[['T+1', 'a1'], ['T+3', 'a3'], ['T+5', 'a5'], ['T+10', 'a10']].map(([l, k]) => kvEl('平均 ' + l, fmtPct(o[k]), dirClass(o[k])))),
        h('div', { class: 'grid c4 mt-s' }, kvEl('T+20 平均', fmtPct(o.a20), dirClass(o.a20)), kvEl('T+5 胜率', o.w5 != null ? fmtPct(o.w5, 0, false) : '—'), kvEl('T+20 胜率', o.w20 != null ? fmtPct(o.w20, 0, false) : '—'), kvEl('样本', String(o.n ?? 0))),
        h('p', { class: 'hint mt-s' }, r.note + ' 回看的是当时存档的清单，不是用今天的数据重算。样本少时请勿下结论。')));
      box.append(h('div', { class: 'tbl-wrap mt' }, h('table', {}, h('thead', {}, h('tr', {}, ['日期', '状态', '环境', '候选', 'T+5', 'T+20', ''].map((t, i) => h('th', { class: i > 2 && i < 6 ? 'r' : '' }, t)))),
        h('tbody', {}, r.runs.map(x => h('tr', {}, h('td', { class: 'num' }, x.scan_date), h('td', {}, h('span', { class: 'badge ' + (x.official ? 'ok' : 'bad') }, x.official ? 'PASS' : 'INCOMPLETE')),
          h('td', {}, REGIME[x.regime]?.[0] || x.regime), h('td', { class: 'num' }, x.n_candidates), h('td', { class: 'num ' + dirClass(x.outcome?.a5) }, fmtPct(x.outcome?.a5, 1)),
          h('td', { class: 'num ' + dirClass(x.outcome?.a20) }, fmtPct(x.outcome?.a20, 1)),
          h('td', {}, h('button', { class: 'btn sm ghost', onclick: async () => { S.tab = 'list'; const dd = await get('/scan', { run_id: x.run_id }); S.data = dd; render(); } }, '查看'))))))));
    }

    async function runJob(task) {
      const r = await post('/jobs/' + task, {}); toast(r.message || '已启动', r.ok ? 'ok' : 'bad');
      ctx.pollJobs?.();
    }

    await load();
    ctx.onEnter = load;
  },
};

function kvEl(k, v, cls = '') { return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v)); }

function emptyNoRun(d, ctx, reload) {
  return h('div', { class: 'empty' }, h('div', { class: 'big' }, '还没有观察清单'), h('div', {}, d.message || ''),
    h('div', { class: 'row', style: 'justify-content:center;margin-top:12px' },
      h('button', { class: 'btn primary', onclick: async () => {
        toast('正在扫描…');
        try { await post('/scan/run', {}, { timeout: 300000 }); toast('扫描完成', 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, '立即扫描'), h('a', { class: 'btn', href: '#/settings' }, '去设置页初始化数据')));
}

function statusBar(d, jobs, health, ctx) {
  const run = d.run;
  const gate = run?.gate?.status;
  const reg = run?.regime?.state;
  const must = health?.summary?.levels?.must ?? 0, watch = health?.summary?.levels?.watch ?? 0;
  const cell = (k, ...v) => h('div', { class: 'cell' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, ...v));
  const gateTip = (run?.gate?.checks || []).map(c => `${c.ok ? '✓' : '✗'} ${c.name}: ${c.msg}`).join('\n');
  return h('div', { class: 'status-bar' },
    cell('数据状态', h('span', { class: 'badge ' + (gate === 'PASS' ? 'ok' : gate ? 'bad' : '') , title: gateTip }, gate === 'PASS' ? '完整性 通过' : gate ? '数据不完整' : '—'),
      h('span', { class: 'num small muted' }, jobs?.data_asof || d.data_asof || '—')),
    cell('市场环境', reg ? h('span', { class: 'pill-state ' + reg, title: REGIME[reg]?.[1] }, REGIME[reg]?.[0] || reg) : '—',
      run?.regime?.detail?.breadth_ma50 != null ? h('span', { class: 'small muted num', title: '站上 MA50 的股票占比（交易池）' }, '宽度 ' + fmtPct(run.regime.detail.breadth_ma50, 0, false)) : null),
    cell('扫描 run_id', h('span', { class: 'num small', title: run ? `config ${run.config_hash}` : '' }, run ? run.run_id.slice(-18) : '—'),
      run ? h('span', { class: 'small muted' }, `收盘日 ${shortDate(run.scan_date)} → 有效至 ${run.valid_until ? shortDate(run.valid_until) : '—'}`) : null),
    h('a', { class: 'cell', href: '#/portfolio', style: 'color:inherit' }, h('span', { class: 'k' }, '持仓告警'),
      h('span', { class: 'v' }, must ? h('span', { class: 'badge bad' }, `${must} 只必须处理`) : h('span', { class: 'badge ok' }, '无需处理'),
        watch ? h('span', { class: 'badge warn' }, `${watch} 关注`) : null)));
}
