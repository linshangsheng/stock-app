// 选股（默认首页，4.5.3）：状态条（数据完整性 / 市场环境 / run_id 与清单有效期 / 持仓告警）+ 今日观察清单（按形态分组）
// + 次日执行清单（可复制到券商端设置条件单）+ 条件筛选面板 + 历史扫描回看。清单来自后端存档，前端不做任何计算。
import { get, post, put, state } from './api.js';
import { h, clear, fmtPrice, fmtPct, fmtNum, fmtMoney, dirClass, code, toast, copyText, esc, shortDate, lotName } from './util.js';
import { showDetail } from './detail.js';
import { openTradeForm } from './portfolio.js';
import { loadMarketView, marketBrief } from './marketpanel.js';

const REGIME = { NORMAL: ['正常', '正常开仓'], CAUTION: ['谨慎', '仓位上限减半，只做最强候选'], DEFENSIVE: ['防守', '不开新仓'], UNKNOWN: ['未知', '基准数据缺失，按谨慎处理'] };
const SETUP_ORDER = ['oversold', 'vcp', 'breakout', 'pullback'];
const SETUP_LABEL = { breakout: '突破', pullback: '回踩', vcp: '波动收缩突破', oversold: '强势超跌' };

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

    // 市场温度 + 宽基指数规则：单独加载，不阻塞观察清单。
    // 宽屏：放在右侧（没点开股票时那块原本是空白）；窄屏（手机）：放在左列顶部。
    const brief = h('div', {});
    const wide = () => matchMedia('(min-width: 900px)').matches;
    function showOverview() {
      if (!wide()) return;
      ctx.currentSymbol = null; S.sel = null;
      [...el.querySelectorAll('.list-item.sel')].forEach(x => x.classList.remove('sel'));
      clear(ctx.detailEl);
      ctx.detailEl.append(h('div', { class: 'pane-body overview' },
        h('div', { class: 'row between mb' }, h('h2', {}, '市场概览'), h('a', { href: '#/market', class: 'small' }, '完整版（行情页）→')),
        S.mv ? marketBrief(S.mv) : h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载市场温度…'),
        S.mv?.portfolio?.health ? h('div', { class: 'alert small mt ' + ({ 警告: 'bad', 注意: 'warn' }[S.mv.portfolio.health.status] || 'ok') },
          h('b', {}, `ETF 规则健康度：${S.mv.portfolio.health.status}。`), S.mv.portfolio.health.text,
          `（当前回撤 ${fmtPct(S.mv.portfolio.health.current_dd, 1)}，10 年最大 ${fmtPct(S.mv.portfolio.health.max_dd, 1)}）`, ' ',
          h('a', { href: '#/market' }, '分年度表现 →')) : null,
        h('p', { class: 'hint mt' }, '点左侧清单里的股票，这里会换成它的详情和「明天怎么操作」；点详情顶部的「← 市场概览」回到这里。')));
    }
    ctx.showOverview = showOverview;
    (async () => {
      try { S.mv = await loadMarketView(); } catch (e) { S.mv = { status: 'error', message: e.message }; }
      clear(brief); brief.append(marketBrief(S.mv));
      if (S.data) render();
      if (!ctx.currentSymbol) showOverview();
    })();

    /** 「明天要做什么」：把宽基 ETF、个股、持仓三件事汇总成一张卡（数据都来自后端，前端只汇总展示）。 */
    function actionCard(d) {
      const mv = S.mv, run = d.run;
      const day = run?.valid_until ? shortDate(run.valid_until) : '下一交易日';
      const etfActs = mv?.status === 'ok' ? mv.indices.filter(i => i.next_open?.actions?.length).map(i => `${i.name}：${i.next_open.actions.join('、')}`) : null;
      const buys = (d.candidates || []).filter(c => c.fit !== false && run?.official && !run?.is_stale);
      const must = S.health?.summary?.levels?.must ?? 0;
      const line = (icon, title, body, onclick) => h('div', { class: 'act-line' + (onclick ? ' link' : ''), onclick },
        h('span', { class: 'act-ico' }, icon), h('div', {}, h('div', { class: 'act-t' }, title), h('div', { class: 'act-b' }, body)));
      return h('div', { class: 'act-card' },
        h('div', { class: 'row between' }, h('b', {}, `明天（${day}）要做什么`), mv?.status === 'ok' ? h('span', { class: 'zone-pill z' + mv.thermometer.zone }, `温度 ${mv.thermometer.zone_name} ${fmtPct(mv.thermometer.b20, 0, false)}`) : null),
        mv?.portfolio?.health?.status === '警告' ? h('div', { class: 'alert bad small mt-s' }, '⚠ ETF 规则：', mv.portfolio.health.text) : null,
        line('📊', '宽基 ETF', etfActs == null ? '市场温度计算中…' : etfActs.length ? etfActs.join('；') + '（开盘按市价成交，高开低开都一样）' : '5 个指数都没有买卖信号，不操作',
          () => { location.hash = '#/market'; }),
        line('📈', '个股', buys.length ? buys.map(c => `${c.name} 开盘买${c.op?.planned_shares ? ' ' + c.op.planned_shares + ' 股' : ''}（止损 ${fmtPrice(c.stop_price)}）`).join('；') + ' —— 点这里看高开 / 低开各买多少'
          : (run?.is_stale ? '这份清单已过期，等今天收盘后的新清单' : '今天没有符合条件的股票，不买'),
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
      if (S.tab === 'list') renderList(d);
      else if (S.tab === 'exec') renderExec(d);
      else if (S.tab === 'filter') renderFilter();
      else renderHistory();
    }

    function tabsBar() {
      const tabs = [['list', '观察清单'], ['exec', '次日执行'], ['filter', '条件筛选'], ['hist', '历史回看']];
      return h('div', { class: 'tabs', style: 'padding:0 8px;margin:0;align-items:center' }, ...tabs.map(([k, l]) =>
        h('button', { class: S.tab === k ? 'on' : '', onclick: () => { S.tab = k; render(); } }, l)),
        null);
    }

    async function recompute(e) {
      const b = e.currentTarget; b.disabled = true; b.textContent = '计算中…';
      try {
        await post('/scan/run', {}, { timeout: 300000 });
        try { S.mv = await loadMarketView(); clear(brief); brief.append(marketBrief(S.mv)); } catch { /* 市场温度失败不影响清单 */ }
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
          evidenceNote(sum));
        return;
      }
      el.append(accountBox(d), h('div', { class: 'small muted', style: 'padding:8px 16px' },
        `交易池 ${sum.universe_l2} 只 → 形态信号 ${sum.signals_today} → 风险剔除后 ${sum.n_after_exclusion} → 入选 ${cands.length}`));
      el.append(evidenceNote(sum));
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
    if (!ctx.currentSymbol) showOverview();
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
function evidenceNote(sum) {
  const on = Object.keys(sum.by_setup || {});
  const risky = on.filter(k => k === 'breakout' || k === 'pullback' || k === 'oversold');
  return h('div', { class: 'alert ' + (risky.length ? 'warn' : 'info'), style: 'margin:6px 16px 10px;font-size:12px' },
    risky.length
      ? `注意：这份清单含「${risky.map(k => SETUP_LABEL[k]).join('、')}」。A 股回测（2018~2026 抽样）里，${risky.map(k => RISK_NOTE[k]).join('；')}——只建议观察，不建议照单下单。`
      : '个股清单默认只用「波动收缩突破」：A 股 2018~2026 抽样回测里，样本内外的期望值都为正，但幅度小、未达统计显著。仓位宜小，严格按止损执行；新手可以优先参考上方的宽基 ETF 规则。');
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
    cell('个股开仓闸门', reg ? h('span', { class: 'pill-state ' + reg, title: REGIME[reg]?.[1] }, REGIME[reg]?.[0] || reg) : '—',
      run?.regime?.detail?.breadth_ma50 != null ? h('span', { class: 'small muted num', title: '站上 MA50 的股票占比（交易池）' }, '宽度 ' + fmtPct(run.regime.detail.breadth_ma50, 0, false)) : null),
    cell('扫描 run_id', h('span', { class: 'num small', title: run ? `config ${run.config_hash}` : '' }, run ? run.run_id.slice(-18) : '—'),
      run ? h('span', { class: 'small muted' }, `收盘日 ${shortDate(run.scan_date)} → 有效至 ${run.valid_until ? shortDate(run.valid_until) : '—'}`) : null,
      recompute ? h('button', { class: 'btn sm', title: '按当前参数和账户资金，重新计算今天的观察清单与市场温度（约 30~60 秒）', onclick: recompute }, '↻ 重新计算') : null),
    h('a', { class: 'cell', href: '#/portfolio', style: 'color:inherit' }, h('span', { class: 'k' }, '持仓告警'),
      h('span', { class: 'v' }, must ? h('span', { class: 'badge bad' }, `${must} 只必须处理`) : h('span', { class: 'badge ok' }, '无需处理'),
        watch ? h('span', { class: 'badge warn' }, `${watch} 关注`) : null)));
}
