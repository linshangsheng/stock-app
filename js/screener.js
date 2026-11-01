// 选股（默认首页，4.5.3）：状态条（数据完整性 / 市场环境 / run_id 与清单有效期 / 持仓告警）+ 今日观察清单（按形态分组）
// + 低风险组合（定期调仓的名单与操作）+ 次日执行清单（可复制到券商端设置条件单）+ 条件筛选面板 + 历史扫描回看。
// 清单来自后端存档，前端不做任何计算。
import { get, post, put, state } from './api.js';
import { h, clear, fmtPrice, fmtPct, fmtNum, fmtMoney, dirClass, code, toast, copyText, esc, shortDate, lotName, evBadge } from './util.js';
import { prefs } from './db.js';
import { showDetail } from './detail.js';
import { openTradeForm } from './portfolio.js';
import { loadMarketView, marketBrief } from './marketpanel.js';
import { loadFactorPlan, factorTab, factorLine, factorEvidenceBlock } from './factor.js';
import { loadKeyMa, keyMaBrief } from './keyma.js';
import { loadVix, vixBrief } from './vix.js';

const REGIME = { NORMAL: ['正常', '正常开仓'], CAUTION: ['谨慎', '仓位上限减半，只做最强候选'], DEFENSIVE: ['防守', '不开新仓'], UNKNOWN: ['未知', '基准数据缺失，按谨慎处理'] };
const SETUP_ORDER = ['oversold', 'vcp', 'breakout', 'pullback'];
const SETUP_LABEL = { breakout: '突破', pullback: '回踩', vcp: '波动收缩突破', oversold: '强势超跌' };

export const screener = {
  layout: 'split',
  async mount(ctx) {
    const S = { tab: 'list', data: null, preview: false, sel: null };
    const el = ctx.listEl;
    const gen = ctx.gen;
    const alive = () => ctx.gen === gen;     // 切换市场 / 页面后，这一份页面的后台请求回来也不再画（否则 A股的数据会盖住美股）
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
      } catch (e) { if (!alive()) return; clear(el); el.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '无法加载'), e.message)); return; }
      if (!alive()) return;
      S.data = d; S.jobs = jobs; S.health = health;
      render();
      loadFp();
    }

    // 低风险组合：单独加载，不阻塞观察清单；回测证据在后台计算时每 20 秒刷新一次（最多 2 分钟多）
    let fpTimer = null, fpTries = 0;
    async function loadFp() {
      clearTimeout(fpTimer);
      if (state.market !== 'CN') { S.fp = null; return; }          // 低风险组合只在 A 股提供
      try { S.fp = await loadFactorPlan(); } catch (e) { S.fp = { status: 'error', message: e.message }; }
      if (!alive()) return;
      if (S.data && document.body.contains(el)) render();
      if (S.tab === 'factor' && !ctx.currentSymbol && document.body.contains(el)) showFpEvidence();
      if (S.fp?.backtest?.status === 'computing' && fpTries++ < 8) fpTimer = setTimeout(() => { if (alive()) loadFp(); }, 20000);
      else fpTries = 0;
    }

    // 市场温度 + 宽基指数规则：单独加载，不阻塞观察清单。
    // 宽屏：放在右侧（没点开股票时那块原本是空白）；窄屏（手机）：放在左列顶部。
    const brief = h('div', {});
    const wide = () => matchMedia('(min-width: 900px)').matches;
    // 长线趋势（关键均线）摘要：单独加载一次
    const kmSlot = h('div', {});
    (async () => {
      try { S.km = await loadKeyMa(); } catch { S.km = null; }
      if (!alive()) return;
      const b = keyMaBrief(S.km);
      if (b) kmSlot.append(b);
      if (state.market === 'US') {                         // 美股：再加一行 VIX
        try { const v = vixBrief(await loadVix()); if (v && alive()) kmSlot.append(v); } catch { /* VIX 失败不影响概览 */ }
      }
    })();
    function showOverview() {
      if (!wide()) return;
      ctx.currentSymbol = null; S.sel = null;
      [...el.querySelectorAll('.list-item.sel')].forEach(x => x.classList.remove('sel'));
      S.rightEv?.destroy(); S.rightEv = null; S.right = 'overview';
      clear(ctx.detailEl);
      ctx.detailEl.append(h('div', { class: 'pane-body overview' },
        h('div', { class: 'row between mb' }, h('h2', {}, '市场概览'), h('a', { href: '#/market', class: 'small' }, '完整版（行情页）→')),
        S.mv ? marketBrief(S.mv) : h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载市场温度…'),
        S.mv?.portfolio?.health ? h('div', { class: 'alert small mt ' + ({ 警告: 'bad', 注意: 'warn' }[S.mv.portfolio.health.status] || 'ok') },
          h('b', {}, `ETF 规则健康度：${S.mv.portfolio.health.status}。`), S.mv.portfolio.health.text,
          `（当前回撤 ${fmtPct(S.mv.portfolio.health.current_dd, 1)}，10 年最大 ${fmtPct(S.mv.portfolio.health.max_dd, 1)}）`, ' ',
          h('a', { href: '#/market' }, '分年度表现 →')) : null,
        kmSlot,
        h('p', { class: 'hint mt' }, '点左侧清单里的股票，这里会换成它的详情和「明天怎么操作」；点详情顶部的「← 市场概览」回到这里。')));
    }
    // 右侧（宽屏、没点开股票时）：「低风险组合」标签下显示组合的回测证据，其余标签显示市场概览
    function showFpEvidence() {
      if (!wide()) return;
      ctx.currentSymbol = null; S.sel = null;
      S.rightEv?.destroy();
      clear(ctx.detailEl);
      const b = factorEvidenceBlock(S.fp);
      ctx.detailEl.append(h('div', { class: 'pane-body overview' },
        h('div', { class: 'row between mb' }, h('h2', {}, '低风险组合 · 回测证据'), h('a', { href: '#', class: 'small', onclick: e => { e.preventDefault(); showOverview(); } }, '市场概览 →')), b.el));
      requestAnimationFrame(() => b.init());
      S.rightEv = b; S.right = 'factor';
    }
    const showRight = () => (S.tab === 'factor' && S.fp?.status === 'ok' ? showFpEvidence() : showOverview());
    ctx.showOverview = showRight;
    (async () => {
      try { S.mv = await loadMarketView(); } catch (e) { S.mv = { status: 'error', message: e.message }; }
      if (!alive()) return;
      clear(brief); brief.append(marketBrief(S.mv));
      if (S.data) render();
      if (!ctx.currentSymbol && S.right !== 'factor') showRight();
    })();

    /** 「明天要做什么」：把宽基 ETF、个股、持仓三件事汇总成一张卡（数据都来自后端，前端只汇总展示）。 */
    function actionCard(d) {
      const mv = S.mv, run = d.run;
      const day = run?.valid_until ? shortDate(run.valid_until) : '下一交易日';
      const etfActs = mv?.status === 'ok' ? mv.indices.filter(i => i.next_open?.actions?.length).map(i => `${i.name}：${i.next_open.actions.join('、')}`) : null;
      const buys = (d.candidates || []).filter(c => c.fit !== false && run?.official && !run?.is_stale);
      const must = S.health?.summary?.levels?.must ?? 0;
      const fl = factorLine(S.fp), al = S.fp?.allocation;
      const swing0 = al && al.swing === 0;
      const line = (icon, title, body, onclick) => h('div', { class: 'act-line' + (onclick ? ' link' : ''), onclick },
        h('span', { class: 'act-ico' }, icon), h('div', {}, h('div', { class: 'act-t' }, title), h('div', { class: 'act-b' }, body)));
      return h('div', { class: 'act-card' },
        h('div', { class: 'row between' }, h('b', {}, `明天（${day}）要做什么`), mv?.status === 'ok' ? h('span', { class: 'zone-pill z' + mv.thermometer.zone }, `温度 ${mv.thermometer.zone_name} ${fmtPct(mv.thermometer.b20, 0, false)}`) : null),
        mv?.portfolio?.health?.status === '警告' ? h('div', { class: 'alert bad small mt-s' }, '⚠ ETF 规则：', mv.portfolio.health.text) : null,
        line('📊', '宽基 ETF', etfActs == null ? '市场温度计算中…' : etfActs.length ? etfActs.join('；') + '（开盘按市价成交，高开低开都一样）' : '5 个指数都没有买卖信号，不操作',
          () => { location.hash = '#/market'; }),
        state.market === 'CN' ? line('🧺', '低风险组合' + (al?.sleeve ? `（${fmtMoney(al.sleeve)}，${S.fp.params.n} 只）` : ''), fl.body, () => { S.tab = 'factor'; render(); if (!ctx.currentSymbol) showRight(); }) : null,
        line('📈', swing0 ? '个股波段（默认不分钱，只观察）' : '个股波段', buys.length ? buys.map(c => `${c.name}${swing0 ? '' : ' 开盘买' + (c.op?.planned_shares ? ' ' + c.op.planned_shares + ' 股' : '')}（止损 ${fmtPrice(c.stop_price)}）`).join('；') + (swing0 ? ' —— 想练手再看，点这里' : ' —— 点这里看高开 / 低开各买多少')
          : (run?.is_stale ? '这份清单已过期，等今天收盘后的新清单' : '今天没有符合条件的股票'),
          buys.length ? () => { S.sel = buys[0].symbol; showDetail(ctx, buys[0].symbol); } : null),
        line('💼', '持仓', must ? `${must} 只需要处理（止损 / 止盈 / 上移止损）—— 点这里看` : '没有需要处理的持仓', () => { location.hash = '#/portfolio'; }),
        d.account?.equity ? null : h('div', { class: 'tiny', style: 'margin-top:6px;color:var(--warn)' }, '还没填账户资金：在下方清单上方填一次，就能算出具体股数'));
    }

    function render() {
      clear(el);
      const d = S.data, run = d.run;
      if (!brief.firstChild) brief.append(S.mv ? marketBrief(S.mv) : h('div', { class: 'mkt-brief hint' }, h('span', { class: 'spinner' }), ' 市场温度…'));
      el.append(actionCard(d), wide() ? null : brief, statusBar(d, S.jobs, S.health, ctx, recompute), tabsBar());
      if (d._stale) el.append(h('div', { class: 'alert warn', style: 'margin:10px 16px' }, '后端未连接：显示的是最近一次缓存快照（数据延迟）'));
      S.fpView?.destroy(); S.fpView = null;
      if (S.tab === 'list') renderList(d);
      else if (S.tab === 'factor') renderFactor();
      else if (S.tab === 'panic') renderPanic(d);
      else if (S.tab === 'exec') renderExec(d);
      else if (S.tab === 'filter') renderFilter();
      else renderHistory();
    }

    function renderFactor() {
      const v = factorTab(S.fp, { onPick: sym => { S.sel = sym; S.rightEv?.destroy(); S.rightEv = null; S.right = 'detail'; showDetail(ctx, sym); }, reload: load, noEvidence: wide() });
      S.fpView = v;
      el.append(h('div', { style: 'padding:4px 12px 16px' }, v.el));
    }

    function tabsBar() {
      // 左栏很窄：标签用短名（鼠标停留显示全名），均分宽度，放得下就不出现横向滚动
      const tabs = [['list', '观察清单', '观察清单'], ...(state.market === 'CN' ? [['factor', '低风险', '低风险组合']] : []), ['panic', '恐慌日', '恐慌日'],
        ['exec', '次日', '次日执行'], ['filter', '筛选', '条件筛选'], ['hist', '历史', '历史回看']];
      return h('div', { class: 'tabs', style: 'padding:0 8px;margin:0;align-items:center' }, ...tabs.map(([k, l, full]) =>
        h('button', { class: S.tab === k ? 'on' : '', title: full, onclick: () => { const was = S.tab; S.tab = k; render(); if (!ctx.currentSymbol && (was === 'factor') !== (k === 'factor')) showRight(); } }, l)),
        null);
    }

    async function recompute(e) {
      const b = e.currentTarget; b.disabled = true; b.textContent = '计算中…';
      try {
        await post('/scan/run', {}, { timeout: 300000 });
        if (!alive()) return;
        try { S.mv = await loadMarketView(); if (!alive()) return; clear(brief); brief.append(marketBrief(S.mv)); } catch { /* 市场温度失败不影响清单 */ }
        toast('已重新计算', 'ok');
        await load();
      } catch (err) { toast(err.message, 'bad'); b.disabled = false; b.textContent = '↻ 重新计算'; }
    }

    function renderList(d) {
      const run = d.run;
      if (!run) { el.append(emptyNoRun(d, ctx, load, S.jobs)); return; }
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
          h('div', {}, `交易池 ${sum.universe_l2 ?? '—'} 只，形态信号 ${sum.signals_today ?? 0} 个，风险剔除后 ${sum.n_after_exclusion ?? 0} 个。`), funnelNote(sum)),
          evidenceNote(sum, S.fp?.allocation));
        return;
      }
      el.append(accountBox(d), h('div', { class: 'small muted', style: 'padding:8px 16px' },
        `交易池 ${sum.universe_l2} 只 → 形态信号 ${sum.signals_today} → 风险剔除后 ${sum.n_after_exclusion} → 入选 ${cands.length}`));
      el.append(evidenceNote(sum, S.fp?.allocation));
      const groups = {};
      for (const c of cands) (groups[c.setup] ||= []).push(c);
      for (const g of SETUP_ORDER.filter(k => groups[k])) {
        el.append(h('div', { class: 'group-h' }, h('span', {}, SETUP_LABEL[g]), h('span', {}, groups[g].length + ' 只')));
        for (const c of groups[g]) el.append(candRow(c));
      }
      el.append(funnelNote(sum));
    }

    // 买法（取自当次扫描的配置）：next_open = 次日开盘直接买（默认，回测按此验证）；stop_entry = 价格 ≥ 触发价才买（条件单）
    const nextOpen = () => (S.data?.run?.entry_mode || 'next_open') === 'next_open';

    function candRow(c) {
      const row = h('button', { class: 'list-item' + (c.fit === false ? ' dim' : '') + (S.sel === c.symbol ? ' sel' : ''), dataset: { symbol: c.symbol },
        onclick: () => { S.sel = c.symbol; [...el.querySelectorAll('.list-item')].forEach(x => x.classList.toggle('sel', x.dataset.symbol === c.symbol)); showDetail(ctx, c.symbol); } },
        h('div', { class: 't1' },
          h('span', { class: 'name' }, c.name), h('span', { class: 'code num' }, code(c.symbol)),
          c.held ? h('span', { class: 'badge accent' }, '持仓中') : null, c.watched ? h('span', { class: 'badge' }, '★') : null,
          h('span', { class: 'grow' }), h('span', { class: 'score' }, fmtNum(c.score, 0))),
        h('div', { class: 'row wrap gap-s', style: 'margin-top:3px' }, (c.reasons || []).slice(1).map(r => h('span', { class: 'tag' }, r))),
        h('div', { class: 't2 num' },
          nextOpen() ? h('span', { title: '默认买法：下一个交易日开盘买入。参考价 = 信号日收盘价，用来算止损距离和股数' }, '次日开盘买 · 参考 ', h('b', {}, fmtPrice(c.entry_ref ?? c.close)))
            : h('span', { title: '条件单：下一个交易日价格 ≥ 触发价才买入，当天没触发就作废' }, '触发 ', h('b', {}, fmtPrice(c.trigger_price))),
          h('span', {}, '止损 ', h('b', {}, fmtPrice(c.stop_price))),
          h('span', {}, '距止损 ', fmtPct(-(c.stop_dist_pct ?? NaN), 1)),
          h('span', {}, c.shares != null ? `建议 ${c.shares} 股 · 风险 ${fmtMoney(c.risk_amount)}` : `每${lotName()}风险 ${fmtMoney(c.risk_per_lot)}（未录入账户）`)),
        c.open_plan?.length ? h('div', { class: 'small', style: 'margin-top:4px;color:var(--accent)' },
          '▸ 点开看明天怎么操作：', c.op?.planned_shares ? `平开买 ${c.op.planned_shares} 股；` : '', '低开 / 平开 / 高开 0.5%~5% / 涨停 各买多少，止损 ', fmtPrice(c.stop_price),
          c.take_profit ? `、止盈 ${fmtPrice(c.take_profit)}` : '') : null,
        c.fit === false ? h('div', { class: 'small', style: 'color:var(--warn);margin-top:2px' }, '未列入执行：' + c.skip_reason) : null,
        c.outcome && c.outcome.filled ? h('div', { class: 't2 num' }, h('span', {}, '后续：'),
          ...[['1d', 'ret_1d'], ['5d', 'ret_5d'], ['20d', 'ret_20d']].map(([l, k]) => c.outcome[k] != null ? h('span', { class: dirClass(c.outcome[k]) }, `${l} ${fmtPct(c.outcome[k])}`) : null)) : null);
      return row;
    }

    /** 没录入账户资金时：清单顶部直接填，填完自动算每只股票的操作。 */
    function accountBox(d) {
      if (d.account?.equity) {
        return h('div', { class: 'small muted', style: 'padding:6px 16px' }, `按账户资金 ${fmtMoney(d.account.equity)}、每笔最多亏 ${fmtPct(d.account.risk_per_trade, 2, false)}（${fmtMoney(d.account.equity * d.account.risk_per_trade)}）计算操作。`,
          h('a', { href: '#/portfolio', style: 'margin-left:6px' }, '修改'));
      }
      const f = { eq: '' };
      return h('div', { class: 'alert info', style: 'margin:8px 16px;font-size:12px' },
        h('div', {}, h('b', {}, '填一下账户资金，就能算出每只股票明天买几股：'), '每笔最多亏 = 资金 × 0.75%（10 万 = 750 元，回测校准；可在「持仓」页改）。'),
        h('div', { class: 'row mt-s', style: 'gap:6px' },
          h('input', { type: 'number', min: 1000, step: 1000, placeholder: '例如 100000', style: 'width:140px', oninput: e => { f.eq = e.target.value; } }), h('span', {}, '元'),
          h('button', { class: 'btn sm primary', onclick: async () => {
            const eq = +f.eq;
            if (!(eq >= 1000)) { toast('请输入资金（元），至少 1000', 'bad'); return; }
            try { await put('/account', { equity: eq, cash: eq, risk_per_trade: d.account?.risk_per_trade ?? 0.0075 }); toast('已保存，正在计算', 'ok'); load(); }
            catch (e) { toast(e.message, 'bad'); }
          } }, '保存并计算')));
    }

    // 恐慌日（ETF 优先）：A 股市场温度 < 15% / 美股 VIX >= 25 的日子。回测里这种日子之后直接买宽基指数 / ETF 明显好于平时；
    // 站上 200 日线且 RSI14<30 的超跌股名单相对直接买指数没有显著的额外好处，所以折叠在下面当「进攻版」，仅供参考（config.yaml panic）
    async function renderPanic(d) {
      const p = d.run?.summary?.panic;
      const box = h('div', { style: 'padding:12px 16px' });
      el.append(box);
      const pct0 = v => (v == null ? '—' : (v * 100).toFixed(0) + '%');
      const sgn = v => (v == null ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(1) + '%');
      if (!p) { box.append(h('div', { class: 'alert warn' }, '最近一次扫描里没有恐慌日数据（这个功能上线之前的扫描）。重新扫描一次即可。')); return; }
      const vix = p.trigger === 'vix';
      const title = p.active
        ? (vix ? `恐慌日：VIX ${p.vix?.toFixed(1)}（不低于 ${p.cut}）` : `恐慌日：市场温度 ${pct0(p.b20)}（低于 ${pct0(p.cut)}）`)
        : (vix ? `今天不是恐慌日（VIX ${p.vix?.toFixed(1) ?? '—'}，触发线 ${p.cut}）` : `今天不是恐慌日（市场温度 ${pct0(p.b20)}，触发线 ${pct0(p.cut)}）`);
      const items = p.etf?.items || [];
      const mv = S.mv?.status === 'ok' ? S.mv : null;
      const ruleOf = it => mv?.indices?.find(i => it.name.startsWith(i.name))?.washout;
      const etfLine = (lab, x) => (x ? h('div', { class: 'pe-row' }, h('span', { class: 'pe-lab' }, lab),
        h('div', {}, h('div', { class: 'num pe-main' }, h('b', { class: dirClass(x.mean) }, sgn(x.mean)), ` · 胜率 ${pct0(x.win)}`),
          h('div', { class: 'tiny muted' }, `平时 ${sgn(x.base_mean)} · ${pct0(x.base_win)}；多出 ${sgn(x.diff_mean)}（${sgn(x.diff_lo)} ~ ${sgn(x.diff_hi)}）`))) : null);
      const etfTable = items.length ? h('div', { class: 'mt-s' }, items.map(it => {
        const w = ruleOf(it);
        return h('div', { class: 'pe-item' },
          h('div', { class: 'row between' }, h('b', {}, it.name), h('span', { class: 'tiny muted' }, it.etf)),
          etfLine('5 天', it.hold?.['5']), etfLine('20 天', it.hold?.['20']),
          h('div', { class: 'tiny muted', style: 'margin-top:3px' }, `最差的一段（按行情段平均）：5 天 ${sgn(it.hold?.['5']?.worst)}，20 天 ${sgn(it.hold?.['20']?.worst)}`),
          it.real ? h('div', { class: 'tiny', style: 'margin-top:3px' }, h('b', {}, `实际可买的 ${it.real.code} ${it.real.name}（真实成交价，${it.real.from} 起）：`),
            `5 天比平时多 ${sgn(it.real.hold?.['5']?.diff)}（${sgn(it.real.hold?.['5']?.lo)} ~ ${sgn(it.real.hold?.['5']?.hi)}），20 天多 ${sgn(it.real.hold?.['20']?.diff)}（${sgn(it.real.hold?.['20']?.lo)} ~ ${sgn(it.real.hold?.['20']?.hi)}）。`
            + (it.real.hold?.['20']?.vs_us ? `QDII 有溢价和汇率：同一批恐慌日里，它比 ${it.real.hold['20'].vs_us.ticker} 平均少涨 ${sgn(-it.real.hold['20'].vs_us.mean_gap)}（20 天），单次相差最多 ${sgn(it.real.hold['20'].vs_us.worst)}，买入价可能明显偏离净值。` : '')) : null,
          w ? h('div', { class: 'tiny', style: 'margin-top:3px' }, h('b', {}, 'ETF 规则里的抄底仓：'), w.text) : null);
      })) : null;
      const stale = p.evidence_age_days != null && p.evidence_age_days > 180;
      const note = h('p', { class: 'hint mt-s' }, `回测 2018~2026，按「行情段」统计（相隔不超过 5 个交易日的恐慌日算一段，每段一个结果；A 股约 34 段、美股约 25 段），次日开盘买入、扣 0.1% 成本。`
        + '「平时」= 非恐慌日；「多」= 恐慌日之后比平时多出的平均涨跌，括号是 90% 可信范围——范围不含 0 才算有把握。样本内（前 60%）和样本外（后 40%）方向一致。'
        + '注意这是「每段平均」：如果只在每轮恐慌的第一个恐慌日一次买完，结果没有这么好，见下面「怎么买更好」。这是历史统计，不是保证，也不是投资建议。',
        h('span', { style: stale ? 'color:var(--warn)' : '' }, ` 统计生成于 ${p.etf?.generated ?? '—'}${stale ? `（已 ${p.evidence_age_days} 天没更新，建议重新运行 tools/panic_etf_baseline.py --write）` : ''}。`));

      // 怎么买：本轮恐慌第几天 + 每天买 1/10 的计划 + 长历史检验
      const run = p.run, plan = p.plan?.items || [];
      const budgetKey = 'panicBudget.' + state.market;
      let budget = prefs.get(budgetKey, '');
      const perShare = h('span', { class: 'num muted' });
      const showShare = () => { perShare.textContent = +budget > 0 ? `每份约 ${fmtMoney(+budget / 10)}` : ''; };
      showShare();
      const planItem = it => {
        const f5 = it.first?.['5'], f20 = it.first?.['20'], d5 = it.dca?.['5'], d20 = it.dca?.['20'];
        return h('div', { class: 'pe-item' }, h('div', {}, h('b', {}, it.name), h('span', { class: 'tiny muted' }, '  ' + it.span)),
          f5 ? h('div', { class: 'small', style: 'margin-top:4px' }, h('b', {}, '第一个恐慌日一次买完：'),
            `5 天平均 ${sgn(f5.first_mean)}（比平时多 ${sgn(f5.first_diff)}，${sgn(f5.first_lo)} ~ ${sgn(f5.first_hi)}）；20 天平均 ${sgn(f20?.first_mean)}（多 ${sgn(f20?.first_diff)}，${sgn(f20?.first_lo)} ~ ${sgn(f20?.first_hi)}）。共 ${f5.segs} 轮，一轮恐慌的中位数 ${f5.median_days} 天；`
            + `最差几次（20 天）：${(f20?.first_worst || []).slice(0, 3).map(w => `${w[0]} ${sgn(w[1])}`).join('、')}。`) : null,
          d5 ? h('div', { class: 'small', style: 'margin-top:4px' }, h('b', {}, '恐慌持续时每天买 1/10、最多买 10 天：'),
            `买到的部分，5 天比平时多 ${sgn(d5.diff)}（${sgn(d5.lo)} ~ ${sgn(d5.hi)}）；20 天多 ${sgn(d20?.diff)}（${sgn(d20?.lo)} ~ ${sgn(d20?.hi)}）。`) : null);
      };
      const planBox = h('div', { class: 'mt-s' },
        p.active && run ? h('div', { class: 'alert info', style: 'font-size:12.5px' }, h('b', {}, `本轮恐慌第 ${run.n} 个恐慌日（${run.start} 起）。`),
          run.n <= 10 ? `按计划：今天买预算的 1/10（第 ${run.n}/10 份）；恐慌结束就停。` : '已经买满 10 份，本轮不再加仓。',
          h('div', { class: 'row mt-s', style: 'gap:6px;flex-wrap:wrap' }, '你打算用多少钱买（元）：',
            h('input', { type: 'number', min: 0, step: 1000, value: budget, style: 'width:120px', oninput: e => { budget = e.target.value; prefs.set(budgetKey, budget); showShare(); } }), perShare)) : null,
        h('details', { class: 'mt-s', open: p.active ? true : null }, h('summary', {}, '怎么买更好？长历史检验（美股 1990 年起、沪深300 2005 年起）'),
          h('div', { class: 'small', style: 'margin:6px 0;line-height:1.7' }, h('b', {}, '结论（保守口径）：'),
            '① 别在第一个恐慌日一次买完——这样买没有优势，而且 2008、2015、2020 年曾在 20 天内亏 17%~31%；'
            + '② 恐慌持续的每个交易日买一小份、最多买 10 天，历史上这样买到的部分，比平时买多赚约 0.8~1.7 个百分点；'
            + '③ 恐慌往往只持续几天（一轮的中位数美股约 9 天、A 股约 3 天），预算常常用不完，所以它更像「把本来就要买的钱，放到恐慌日买」，不是额外的收益来源，剩下的钱照常处理，不要为了抄底借钱或加杠杆；'
            + '④ A 股 2005~2016 的检验里（温度没有这么早的数据，用走势近似）持有 20 天没有优势，所以 A 股只建议当 5 天以内的依据。'),
          ...plan.map(planItem)));
      box.append(h('div', { class: 'card ' + (p.active ? '' : 'soft'), style: p.active ? 'border-left:3px solid var(--warn)' : '' },
        h('div', { class: 'row between' }, h('h3', {}, title),
          evBadge('中', '平均而言有效：约 25~80 段行情、样本内外方向一致、美股拉到 1990 年也成立。但买入时点难选（第一个恐慌日一次买入没有优势）、A 股 2005~2016 的 20 天不成立；做过很多次检验，已按保守口径处理。')),
        h('div', { class: p.active ? 'act-b mt-s' : 'small mt-s' }, p.active ? '宽基 ETF 可以分批加仓（每个恐慌日买一小份）。' : '平时这一栏不用做任何事。恐慌日来了，会提示宽基 ETF 可以分批加仓。'),
        planBox, p.active ? null : h('div', { class: 'hint mt' }, '下面是历史上恐慌日之后，直接买宽基指数 / ETF 的表现：'), etfTable, note));

      // 超跌股名单（进攻版）：折叠，仅供参考
      const rows = (p.rows || []).map(r => h('tr', { style: 'cursor:pointer', onclick: () => showDetail(ctx, r.symbol) }, h('td', {}, r.name || r.symbol),
        h('td', { class: 'small muted' }, code(r.symbol)), h('td', { class: 'num' }, fmtPrice(r.close)), h('td', { class: 'num' }, r.rsi14 == null ? '—' : r.rsi14.toFixed(0)),
        h('td', { class: 'num ' + dirClass(r.ret_5) }, fmtPct(r.ret_5, 1))));
      box.append(h('details', { class: 'card mt' }, h('summary', {}, p.active ? `超跌股名单（进攻版，仅供参考）· ${p.total} 只` : '超跌股名单（进攻版，仅供参考）· 今天没有'),
        h('p', { class: 'hint mt-s' }, p.evidence),
        p.active ? h('div', { class: 'small', style: 'margin:6px 0' }, `站上 200 日线、RSI14<30 的超跌股共 ${p.total} 只（按成交额从大到小列前 ${(p.rows || []).length} 只）。名单平均涨幅可能更高，但波动大得多，相对直接买指数没有显著的额外好处；想参与建议小仓位、分散、自己设止损。`) : null,
        p.active ? h('table', { class: 'tbl small' }, h('thead', {}, h('tr', {}, ['名称', '代码', '收盘', 'RSI14', '5日涨跌'].map(x => h('th', {}, x)))), h('tbody', {}, rows)) : null));

      const hist = h('div', { class: 'mt' }, h('span', { class: 'spinner' }));
      box.append(hist);
      let r;
      try { r = await get('/scan/history'); } catch (e) { clear(hist); hist.append(h('div', { class: 'alert bad' }, e.message)); return; }
      clear(hist);
      const o = r.panic_overall || {}, E = r.panic_etf || {};
      const seg = (x, k) => (x?.[k]?.win != null ? `${pct0(x[k].win)}（${x[k].segs} 段）` : '—');
      const line = (name, x, a5, a20) => h('tr', {}, h('td', {}, h('b', {}, name)), h('td', { class: 'num ' + dirClass(a5) }, sgn(a5)), h('td', { class: 'num' }, seg(x, 's5')),
        h('td', { class: 'num ' + dirClass(a20) }, sgn(a20)), h('td', { class: 'num' }, seg(x, 's20')));
      hist.append(h('div', { class: 'card soft' }, h('h3', {}, '恐慌日之后的真实结果（软件自己存档的）'),
        h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'tbl small' }, h('thead', {}, h('tr', {}, ['买什么', 'T+5 平均', 'T+5 按行情段胜率', 'T+20 平均', 'T+20 按行情段胜率'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
          h('tbody', {}, ...Object.entries(E).map(([n, x]) => line(n, x, x.a5, x.a20)), line('超跌股名单', o, o.a5, o.a20)))),
        h('p', { class: 'hint mt-s' }, `只记录这个功能上线之后真正出现的恐慌日：次日开盘买入，T+5 / T+20 = 第 5 / 20 个交易日收盘相对买入价的涨跌（前复权，未扣交易成本）。超跌股名单共 ${o.n5 ?? 0} 只有 T+5 结果、${o.n20 ?? 0} 只有 T+20 结果。`
          + '同一个恐慌日里的股票同涨同跌，相隔不超过 5 个交易日的恐慌日也是同一段行情，所以看「按行情段」（一段 = 一个结果）。上面的历史统计能否重现要看这里；样本少时请勿下结论。')));
      if (!(r.panic || []).length) { hist.append(h('div', { class: 'empty mt' }, '还没有出现过恐慌日的存档。')); return; }
      hist.append(h('h3', { class: 'mt' }, '每个恐慌日的超跌股名单结果'), h('div', { class: 'tbl-wrap mt-s' }, h('table', {}, h('thead', {}, h('tr', {}, ['恐慌日', state.market === 'US' ? 'VIX' : '温度', '只数', 'T+5 平均', 'T+5 胜率', 'T+20 平均', 'T+20 胜率'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
        h('tbody', {}, r.panic.map(x => h('tr', {}, h('td', { class: 'num' }, x.date), h('td', { class: 'num' }, state.market === 'US' ? (x.vix?.toFixed(1) ?? '—') : pct0(x.b20)), h('td', { class: 'num' }, x.n),
          h('td', { class: 'num ' + dirClass(x.a5) }, fmtPct(x.a5, 1)), h('td', { class: 'num' }, x.w5 != null ? fmtPct(x.w5, 0, false) : '待回填'),
          h('td', { class: 'num ' + dirClass(x.a20) }, fmtPct(x.a20, 1)), h('td', { class: 'num' }, x.w20 != null ? fmtPct(x.w20, 0, false) : '待回填')))))));
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
      const no = nextOpen();
      const buyCol = no ? '参考价（今日收盘）' : '触发价';
      const buyPx = c => (no ? (c.entry_ref ?? c.close) : c.trigger_price);
      const lines = cands.map(c => `${code(c.symbol)}\t${c.name}\t${no ? '次日开盘买，参考价' : '触发价'} ${fmtPrice(buyPx(c))}\t止损价 ${fmtPrice(c.stop_price)}\t${c.shares != null ? c.shares + '股' : '—'}`);
      el.append(h('div', { style: 'padding:12px 16px' },
        h('div', { class: 'row between' }, h('h3', {}, `次日（${d.run.valid_until || '下一交易日'}）执行参数`),
          h('button', { class: 'btn sm', onclick: () => copyText(`代码\t名称\t${buyCol}\t止损价\t股数\n` + lines.join('\n')) }, '一键复制')),
        h('div', { class: 'alert info small', style: 'margin:6px 0 8px' }, no
          ? h('span', {}, h('b', {}, '怎么买：'), '下一个交易日开盘买入（集合竞价或开盘后挂单）。参考价是今天的收盘价，只用来算止损距离和股数。',
            h('br'), h('b', {}, '高开了怎么办：'), '照样买，但要少买——股数 = 「最多亏」÷（开盘价 − 止损价），按整手向下取整，只少不多。这样无论高开多少，打到止损时亏的钱都不超过计划；高开太多、算出来不足一手就放弃。（回测验证：这样做回撤更小、收益基本不变；「高开就不买」没有稳定好处。）',
            h('br'), h('b', {}, '怎么卖：'), '买入后在券商端挂两张条件单：「价格 ≤ 止损价 卖出」和「价格 ≥ 止盈价 卖出」（止盈价 = 你的实际买入价 × 1.10）；之后到「持仓」页看每天更新的移动止损。')
          : h('span', {}, h('b', {}, '怎么买：'), '在券商端设条件单「价格 ≥ 触发价 买入」，只在下一个交易日有效，没触发就作废（追不上就不追）。',
            h('br'), h('b', {}, '怎么卖：'), '成交后设「价格 ≤ 止损价 卖出」的条件单。')),
        h('p', { class: 'hint' }, (state.market === 'US' ? '美股按整股取整。' : 'A 股股数已按整手取整。') + '系统不接券商下单。',
          acct ? '' : ' 尚未录入账户净值，股数为空（到「持仓」页录入账户参数）。'),
        ...cands.map(c => {
          const tp = c.take_profit ?? (c.entry_ref ? c.entry_ref * 1.10 : null);
          return h('div', { class: 'exec-card' },
            h('div', { class: 'row between' }, h('span', {}, h('b', {}, c.name), h('span', { class: 'muted num' }, ' ' + code(c.symbol))),
              h('button', { class: 'btn sm primary', title: '在券商成交后点这里记录（填实际成交价，表单会提示最多该买几股）', onclick: () => openTradeForm({
                symbol: c.symbol, name: c.name, side: 'buy', price: c.entry_ref, initial_stop: c.stop_price, setup: c.setup, signal_run_id: d.run.run_id,
                planned_trigger: c.trigger_price, shares: c.shares, risk_amount: c.op?.risk_budget ?? c.risk_amount, regime: d.run.regime?.state, onDone: load }) }, '成交了')),
            h('div', { class: 'num small' }, `${no ? '参考价' : '触发价'} `, h('b', {}, fmtPrice(buyPx(c))), '　止损 ', h('b', {}, fmtPrice(c.stop_price)),
              '　止盈 ', h('b', { title: '按参考价估算；实际 = 你的买入价 × 1.10' }, fmtPrice(tp))),
            h('div', { class: 'num small muted' }, c.shares != null ? `平开买 ${c.shares} 股 · 碰到止损最多亏 ${fmtMoney(c.risk_amount)}` : '填账户资金后显示股数',
              '　', h('a', { href: '#', onclick: e => { e.preventDefault(); S.tab = 'list'; S.sel = c.symbol; render(); showDetail(ctx, c.symbol); } }, '各种开盘价买多少 →')));
        }),
        h('div', { class: 'row mt' }, h('button', { class: 'btn', onclick: async () => {
          const syms = cands.filter(c => !c.watched).map(c => ({ symbol: c.symbol })); if (!syms.length) return toast('都已在自选');
          await put('/watchlist', { add: syms }); await ctx.reloadWatch(); toast(`已加入 ${syms.length} 只到自选`, 'ok'); load();
        } }, '全部加入自选'))));
    }

    async function renderFilter() {
      const F = S.filter ||= { min_rps20: '', min_vol_ratio: '', max_atr_pct: '', above_ma50: true, ma20_gt_ma50: false, breakout20: false, max_dist_52w_high: '', min_amount_wan: '', sort: 'lowrisk' };
      const num = (k, label, ph) => h('label', { class: 'f' }, label, h('input', { type: 'number', step: 'any', placeholder: ph || '', value: F[k], oninput: e => { F[k] = e.target.value; } }));
      const chk = (k, label) => h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: F[k] ? true : null, onchange: e => { F[k] = e.target.checked; } }), label);
      const out = h('div', {});
      el.append(h('div', { style: 'padding:12px 16px' },
        h('p', { class: 'hint' }, '漏斗之外的手动补充：按第三章指标自定义组合筛选（当前交易池 L2）。'),
        h('div', { class: 'alert warn small mb-s' }, h('b', {}, '先看证据：'), 'A 股横截面检验（约 1000 只 × 6 年，样本内外一致）里，近期涨得多（RPS、20 日涨幅高）、换手高、波动大的股票，之后平均反而更弱；冷门、平稳的股票更强。所以默认按「低换手 + 低波动」排序，不再默认要求 RPS20 ≥ 80。'),
        h('div', { class: 'grid c2' }, num('min_rps20', 'RPS20 ≥'), num('min_vol_ratio', '量比 ≥'), num('max_atr_pct', 'ATR% ≤（百分数）'), num('max_dist_52w_high', '距52周高 ≤（%）'), num('min_amount_wan', '20日均成交额 ≥（万元）'),
          h('label', { class: 'f' }, '排序', h('select', { onchange: e => { F.sort = e.target.value; } }, [['lowrisk', '低换手 + 低波动（推荐）'], ['rps_20', 'RPS20'], ['rps_60', 'RPS60'], ['vol_ratio', '量比'], ['ret_20', '20日涨幅']].map(([v, l]) => h('option', { value: v, selected: F.sort === v ? true : null }, l))))),
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
        h('p', { class: 'hint mt-s' }, '这不是回测：这里是每天盘后真实给出、并存档的观察清单，按「次日开盘价买入」记下之后第 1 / 3 / 5 / 10 / 20 个交易日的实际涨跌——相当于选股器自己的实盘成绩单。'
          + '用的是当时存档的清单，不是拿今天的数据重算。样本少时请勿下结论。')));
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
    if (!ctx.currentSymbol) showRight();
    ctx.onEnter = load;
  },
};

function kvEl(k, v, cls = '') { return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v)); }

function emptyNoRun(d, ctx, reload, jobs) {
  const noData = jobs ? !jobs.has_data : /初始化/.test(d.message || '');
  if (noData) {
    const step = (n, title, body, action) => h('div', { class: 'onb-step' }, h('span', { class: 'onb-n' }, n), h('div', {}, h('b', {}, title), h('div', { class: 'small muted' }, body), action));
    return h('div', { class: 'onboard' }, h('h2', {}, '欢迎使用 👋'), h('p', { class: 'hint' }, '三步开始：'),
      step('1', '初始化数据', 'A 股全市场 10 年日线，约 6~9 小时；想先试用可以只随机拉 100 只（几分钟）。可以随时中断，下次接着下。',
        h('a', { class: 'btn sm primary mt-s', href: '#/settings' }, '去设置页初始化')),
      step('2', '填账户资金', '例如 100000。软件按它算每只股票买多少股、每个指数投多少钱；每笔碰到止损最多亏资金的 0.75%。',
        h('a', { class: 'btn sm mt-s', href: '#/portfolio' }, '去持仓页填写')),
      step('3', '每天收盘后看这里', '软件会自动更新数据、选股、体检持仓。最上方的「明天要做什么」告诉你第二天开盘该做什么。', null),
      h('p', { class: 'hint mt' }, '详细说明见项目目录里的 README.md（使用手册）。'));
  }
  return h('div', { class: 'empty' }, h('div', { class: 'big' }, '还没有观察清单'), h('div', {}, d.message || ''),
    h('div', { class: 'row', style: 'justify-content:center;margin-top:12px' },
      h('button', { class: 'btn primary', onclick: async () => {
        toast('正在扫描…');
        try { await post('/scan/run', {}, { timeout: 300000 }); toast('扫描完成', 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, '立即扫描'), h('a', { class: 'btn', href: '#/settings' }, '去设置页初始化数据')));
}

const RISK_NOTE = { breakout: '「突破」显著差于同日随机买入', pullback: '「回踩」显著差于同日随机买入', oversold: '「强势超跌」在完整成交规则下样本外为负' };
/** 个股形态的回测证据（tools/calibrate.py，A 股 2018~2026 抽样）：让新手知道清单的可信度。 */
function evidenceNote(sum, al) {
  const on = Object.keys(sum.by_setup || {});
  const risky = on.filter(k => k === 'breakout' || k === 'pullback' || k === 'oversold');
  return h('div', { class: 'alert ' + (risky.length ? 'warn' : 'info'), style: 'margin:6px 16px 10px;font-size:12px' },
    h('div', { style: 'margin-bottom:4px' }, evBadge('弱', '观察清单（波动收缩突破）：A 股 2018~2026 抽样回测里样本内外的期望值都为正，但幅度小、未达统计显著；突破 / 回踩比同一天随便买还差。只建议观察，默认资金方案不给它分钱。')),
    risky.length
      ? `注意：这份清单含「${risky.map(k => SETUP_LABEL[k]).join('、')}」。A 股回测（2018~2026 抽样）里，${risky.map(k => RISK_NOTE[k]).join('；')}——只建议观察，不建议照单下单。`
      : '个股清单默认只用「波动收缩突破」：A 股 2018~2026 抽样回测里，样本内外的期望值都为正，但幅度小、未达统计显著。仓位宜小，严格按止损执行。',
    al && al.swing === 0 ? h('div', { style: 'margin-top:4px' }, h('b', {}, '默认资金方案不给个股波段分钱'),
      `（宽基 ETF ${Math.round(al.etf * 100)}% + 低风险组合 ${Math.round(al.factor * 100)}%）：回测里把这部分钱放进「低风险组合」比做波段更好。这份清单只供观察、练手；想做的话到「设置 → 资金方案」给波段留一部分。`) : null);
}

function statusBar(d, jobs, health, ctx, recompute) {
  const run = d.run;
  const gate = run?.gate?.status;
  const reg = run?.regime?.state;
  const must = health?.summary?.levels?.must ?? 0, watch = health?.summary?.levels?.watch ?? 0;
  const cell = (k, ...v) => h('div', { class: 'cell' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, ...v));
  const gateTip = (run?.gate?.checks || []).map(c => `${c.ok ? '✓' : '✗'} ${c.name}: ${c.msg}`).join('\n');
  return h('div', { class: 'status-bar' },
    cell('数据状态', h('span', { class: 'badge ' + (gate === 'PASS' ? 'ok' : gate ? 'bad' : '') , title: gateTip }, gate === 'PASS' ? '完整性 通过' : gate ? '数据不完整' : '—'),
      h('span', { class: 'num small muted' }, jobs?.data_asof || d.data_asof || '—')),
    cell(`个股开仓闸门（${run?.regime?.market_name || (state.market === 'US' ? '美股' : 'A股')}）`,
      reg ? h('span', { class: 'pill-state ' + reg, title: REGIME[reg]?.[1] }, REGIME[reg]?.[0] || reg) : '—',
      run?.regime?.reason ? h('span', { class: 'small muted', title: '正常：基准指数在 50 日线上方且宽度（站上 50 日线的股票比例）≥ 45%；防守：基准跌破 200 日线且宽度 < 25%；其余为谨慎' }, run.regime.reason)
        : run?.regime?.detail?.breadth_ma50 != null ? h('span', { class: 'small muted num', title: '站上 MA50 的股票占比（交易池）' }, '宽度 ' + fmtPct(run.regime.detail.breadth_ma50, 0, false)) : null),
    cell('扫描 run_id', h('span', { class: 'num small', title: run ? `config ${run.config_hash}` : '' }, run ? run.run_id.slice(-18) : '—'),
      run ? h('span', { class: 'small muted' }, `收盘日 ${shortDate(run.scan_date)} → 有效至 ${run.valid_until ? shortDate(run.valid_until) : '—'}`) : null,
      recompute ? h('button', { class: 'btn sm', title: '按当前参数和账户资金，重新计算今天的观察清单与市场温度（约 30~60 秒）', onclick: recompute }, '↻ 重新计算') : null),
    h('a', { class: 'cell', href: '#/portfolio', style: 'color:inherit' }, h('span', { class: 'k' }, '持仓告警'),
      h('span', { class: 'v' }, must ? h('span', { class: 'badge bad' }, `${must} 只必须处理`) : h('span', { class: 'badge ok' }, '无需处理'),
        watch ? h('span', { class: 'badge warn' }, `${watch} 关注`) : null)));
}
