// 设置（4.2）：数据源与更新状态、股票池过滤阈值与剔除数量、费用与滑点、涨跌配色、访问口令、备份状态。
import { get, post, put, state } from './api.js';
import { h, clear, fmtPct, fmtNum, fmtMoney, toast, confirmBox, modal, dirClass } from './util.js';
import { prefs } from './db.js';
import { loadAbout, aboutBlock } from './about.js';

let pollTimer = null;
const stopPoll = () => { clearInterval(pollTimer); pollTimer = null; };

export const settingsView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el); stopPoll();
    const wrap = h('div', { class: 'pane-body page' });
    el.append(wrap);
    const secData = h('div', { class: 'card' }), secAlloc = h('div', { class: 'card mt' }), secPool = h('div', { class: 'card mt' }), secCost = h('div', { class: 'card mt' }), secLook = h('div', { class: 'card mt' }), secBackup = h('div', { class: 'card mt' }), secStorage = h('div', { class: 'card mt' }), secAbout = h('div', { class: 'card soft mt' });
    wrap.append(h('h1', { class: 'mb' }, '设置'), secData, secAlloc, secPool, secCost, secLook, secBackup, secStorage, secAbout);

    let cfg = {};
    try { cfg = await get('/settings', {}, { cache: false }); } catch (e) { wrap.append(h('div', { class: 'alert bad' }, e.message)); return; }

    // ---------- 数据与更新 ----------
    async function renderData() {
      let j = null;
      try { j = await get('/jobs', {}, { cache: false }); } catch (e) { clear(secData); secData.append(h('div', { class: 'alert bad' }, e.message)); return; }
      clear(secData);
      const run = j.job?.running;
      const pr = j.job?.progress;
      secData.append(h('div', { class: 'card-title' }, h('h2', {}, '数据与更新状态'), h('span', { class: 'badge ' + (j.has_data ? 'ok' : 'warn') }, j.has_data ? '已有数据' : '尚未初始化')),
        h('div', { class: 'grid c4' }, kv('数据截止', j.data_asof || '—'), kv('最近已收盘交易日', j.last_closed_trading_day || '—'), kv('交易日历来源', j.calendar_source === 'fallback' ? '兜底（仅周末，节假日不识别！）' : j.calendar_source === 'source' ? '数据源' : '—', j.calendar_source === 'fallback' ? 'down' : ''),
          kv('初始化范围', j.init_sample ? `随机 ${j.init_sample.count} 只` : '全部'), kv('数据源', (cfg.datasource?.[state.market.toLowerCase()] === 'demo') ? '合成演示数据' : (state.market === 'US' ? 'yfinance + Nasdaq Trader' : 'BaoStock（日线）+ 东方财富（全市场快照）'))),
        j.calendar_source === 'fallback' ? h('div', { class: 'alert warn mt-s' }, '交易日历取自兜底（周一至周五），节假日会被当作交易日——完整性闸门可能误报缺失。请检查网络后重新初始化日历。') : null,
        run ? h('div', { class: 'mt' }, h('div', { class: 'row between small' }, h('span', {}, h('span', { class: 'spinner' }), ` 运行中：${run} ${j.job.message || ''}`), pr ? h('span', { class: 'num' }, `${pr.task} ${pr.done}/${pr.total}`) : null),
          pr ? h('div', { class: 'progress mt-s' }, h('div', { style: `width:${Math.round(100 * pr.done / Math.max(pr.total, 1))}%` })) : null,
          h('div', { class: 'row mt-s' }, h('button', { class: 'btn sm', onclick: async () => { await post('/jobs/stop', {}); toast('已请求停止（当前请求结束后中断，可续跑）'); } }, '停止（可续跑）'))) : null,
        h('div', { class: 'row wrap mt' },
          h('button', { class: 'btn primary', disabled: run ? true : null, onclick: () => initDlg(j) }, j.has_data ? '补全 / 继续初始化' : '初始化数据'),
          h('button', { class: 'btn', disabled: run || !j.has_data ? true : null, onclick: async () => { const r = await post('/jobs/daily', {}); toast(r.message, r.ok ? 'ok' : 'bad'); startPoll(); } }, '运行盘后任务链'),
          h('button', { class: 'btn', disabled: run || !j.has_data ? true : null, onclick: async () => { const r = await post('/jobs/catch_up', {}); toast(r.message, r.ok ? 'ok' : 'bad'); startPoll(); } }, '补跑缺失交易日'),
          h('button', { class: 'btn', disabled: !j.has_data ? true : null, onclick: async () => { toast('扫描中…'); try { await post('/scan/run', {}, { timeout: 300000 }); toast('扫描完成', 'ok'); } catch (e) { toast(e.message, 'bad'); } } }, '仅选股扫描')));
      for (const [t, v] of Object.entries(j.job?.last || {})) {
        secData.append(h('div', { class: 'small mt-s ' + (v.ok ? 'muted' : ''), style: v.ok ? '' : 'color:var(--red)' }, `上次 ${t}：${v.at.replace('T', ' ')} ${v.ok ? '成功' : '失败 ' + v.error}`));
      }
      if (j.gate) secData.append(h('details', { class: 'mt', open: j.gate.status !== 'PASS' }, h('summary', {}, `数据完整性闸门（${j.gate.day}）：${j.gate.status}`),
        h('table', {}, h('tbody', {}, j.gate.checks.map(c => h('tr', {}, h('td', { style: 'width:20px' }, c.ok ? '✓' : c.hard ? '✗' : '!'), h('td', {}, c.name), h('td', { class: 'small muted' }, c.msg), h('td', { class: 'num small' }, String(c.value))))))));
      secData.append(h('details', { class: 'mt' }, h('summary', {}, '上游限速 / 熔断状态（反封 IP，3.26.3）'), h('div', { class: 'row wrap' }, (j.throttle || []).map(t => h('span', { class: 'badge ' + (t.breaker_open ? 'bad' : 'ok') }, `${t.name}：${t.breaker_open ? '熔断 ' + t.breaker_remaining_s + 's' : '正常'} · 请求 ${t.calls}`)))),
        h('details', {}, h('summary', {}, '最近任务日志'), h('div', { class: 'small' }, (j.logs || []).map(l => h('div', { class: 'muted num' }, `${l.ts.replace('T', ' ')}  ${l.job}  ${l.status}  ${l.detail || ''}`)))));
      if (run && !pollTimer) startPoll();
      if (!run && pollTimer) stopPoll();
    }
    function startPoll() {
      if (pollTimer) return;
      let busy = false, tick = 0;              // 串行轮询：上一轮没返回就跳过；股票池（要构建全市场面板，较重）每约 30 秒才刷新一次
      pollTimer = setInterval(async () => {
        if (!document.body.contains(wrap)) return stopPoll();
        if (busy || document.hidden) return;
        busy = true;
        try { await renderData(); if (++tick % 12 === 0) await renderPool(); } finally { busy = false; }
      }, 2500);
      renderData();
    }
    ctx.pollJobs = startPoll;

    function initDlg(j) {
      const demo = cfg.datasource?.[state.market.toLowerCase()] === 'demo';
      const US = state.market === 'US';
      const hadSample = j?.init_sample;
      const f = { mode: (j?.init_sample_default || 0) > 0 ? 'random' : 'all', n: (j?.init_sample_default || 100) || 100, remember: false, resample: false };
      const nBox = h('input', { type: 'number', min: 1, value: f.n, style: 'width:100px', oninput: e => { f.n = +e.target.value; } });
      const radio = (v, label, hint) => h('label', { class: 'chk', style: 'align-items:flex-start;margin-bottom:8px' }, h('input', { type: 'radio', name: 'init-mode', checked: f.mode === v ? true : null, onchange: () => { f.mode = v; sync(); } }),
        h('span', {}, h('b', {}, label), h('div', { class: 'hint' }, hint)));
      const rsBox = h('label', { class: 'chk small', style: 'margin-left:24px' }, h('input', { type: 'checkbox', onchange: e => { f.resample = e.target.checked; } }), '重新抽一批（默认沿用上次抽到的那批，续跑不重复下载）');
      const sync = () => { nBox.disabled = f.mode !== 'random'; rsBox.style.display = f.mode === 'random' && hadSample ? '' : 'none'; };
      const allHint = demo ? '演示数据源：几秒钟生成，范围选项不影响。' : US ? '名单约 5000 只 → 批量预筛 → 约 2850 只拉 10 年 → 逐股补行业。耗时约 2 小时。' : '全量约半天以上（BaoStock 单只 10 年约 8~10 秒），先按价格 / 成交额预筛。';
      const body = h('div', {}, h('p', { class: 'hint' }, '默认全部拉取；只想试用或省流量，可以只随机拉一部分。'),
        radio('all', '全部', allHint),
        radio('random', '随机拉 N 只', '随机抽样模式不做全市场快照 / 预筛 / 已退市名单并集，流量与耗时都只有全部的零头（约 N × 每只一次请求）。之后想补全，选「全部」再点一次即可，已拉过的不会重复下载。'),
        h('div', { class: 'row', style: 'margin:-2px 0 8px 24px' }, '只数：', nBox, hadSample ? h('span', { class: 'hint' }, `当前已抽样 ${j.init_sample.count} 只`) : null), rsBox,
        h('label', { class: 'chk small' }, h('input', { type: 'checkbox', onchange: e => { f.remember = e.target.checked; } }), '记住这个选择（下次默认沿用）'));
      sync();
      modal('初始化数据', body, [{ label: '取消' }, { label: '开始', primary: true, onclick: async () => {
        if (f.mode === 'random' && !(f.n >= 1)) { toast('请输入大于 0 的只数', 'bad'); return false; }
        const r = await post('/jobs/init', { sample: f.mode === 'random' ? f.n : 0, resample: f.resample, remember: f.remember });
        toast(r.message, r.ok ? 'ok' : 'bad'); startPoll();
      } }]);
    }

    // ---------- 股票池 ----------
    async function renderPool() {
      let u = null;
      try { u = await get('/universe', {}, { cache: false }); } catch { return; }
      clear(secPool);
      const th = u.thresholds;
      secPool.append(h('div', { class: 'card-title' }, h('h2', {}, '股票池（L1 入库池 / L2 交易池）'), h('span', { class: 'hint' }, '只增不删：被剔除只标记 inactive，历史永不删除')),
        h('div', { class: 'grid c4' }, kv('证券总数', u.securities), kv('L1 入库池', u.l1), kv('其中已退市（保留）', u.delisted_kept), kv('今日 L2 交易池', u.l2 ?? '—')),
        u.l1_stats?.asof ? h('p', { class: 'hint mt-s' }, `L1 更新于 ${u.l1_stats.asof}：板块剔除 ${u.l1_stats.board}，价格 ${u.l1_stats.price}，市值 ${u.l1_stats.mktcap}，成交额 ${u.l1_stats.amount}${u.l1_stats.snapshot_used === false ? '（未使用全市场快照：AkShare 不可用，L1 不做价格/市值/成交额粗筛，由 L2 逐日兜底）' : ''}`) : null,
        u.l2_exclusions ? h('div', { class: 'mt' }, h('h3', {}, '今日 L2 各条件剔除数量（逐项独立统计，用于校准阈值）'), h('div', { class: 'row wrap mt-s' }, Object.entries(u.l2_exclusions).map(([k, v]) => h('span', { class: 'tag gray' }, `${({ tradable: '当日可交易', list_age: '上市天数', price: '股价', liquidity: '成交额', suspend: '停牌天数', oneword: '一字板', not_st: '非 ST' })[k] || k}：剔除 ${v}`)))) : null,
        h('details', { class: 'mt' }, h('summary', {}, '阈值（config.yaml，需编辑文件后重启；L2 不使用市值——免费源无历史市值序列）'), h('pre', { class: 'code' }, JSON.stringify(th, null, 2))));
    }

    // ---------- 费用与组合参数 ----------
    function renderCost() {
      clear(secCost);
      const US = state.market === 'US';
      const c = US ? cfg.costs.us : cfg.costs.cn, ex = cfg.execution, po = cfg.portfolio, xs = cfg.exits;
      const F = US
        ? { commission_per_share: c.commission_per_share, sec_fee_sell: c.sec_fee_sell, finra_taf_per_share: c.finra_taf_per_share }
        : { commission_rate: c.commission_rate, commission_min: c.commission_min, stamp_duty_sell: c.stamp_duty_sell, transfer_fee: c.transfer_fee };
      Object.assign(F, { slippage: ex.slippage, max_adv_pct: ex.max_adv_pct, max_positions: po.max_positions, max_per_industry: po.max_per_industry,
        risk_per_trade: po.risk_per_trade, max_total_risk: po.max_total_risk, stop_atr_k: xs.stop_atr_k, trail_atr_k: xs.trail_atr_k, max_hold_days: xs.max_hold_days, hard_stop_pct: xs.hard_stop_pct });
      const inp = (k, label, hint) => h('label', { class: 'f' }, label, h('input', { type: 'number', step: 'any', value: F[k], oninput: e => { F[k] = +e.target.value; } }), hint ? h('span', { class: 'tiny faint' }, hint) : null);
      secCost.append(h('div', { class: 'card-title' }, h('h2', {}, `费用、滑点与组合参数（${US ? '美股' : 'A 股'}，两个市场参数各自独立）`)),
        h('div', { class: 'alert info mb' }, US
          ? '美股默认零佣金；卖出另收 SEC 规费（按成交额，费率每年调整）与 FINRA TAF（按股数、单笔封顶 8.3 美元）。美股参数需独立验证，不沿用 A 股参数（2.1 / 6.6）。修改保存到 data/user_config.yaml 的 markets.US 下。'
          : '默认值已按现行规则核对（2026-10）：印花税 0.05% 仅卖出、过户费 0.001% 双向，经手费与证管费含在佣金内；佣金按市场常见的万 2.5（最低 5 元）保守设定，你的券商更低请下调。修改保存到 data/user_config.yaml，对新的扫描与回测生效。'),
        h('div', { class: 'grid c4' },
          ...(US ? [inp('commission_per_share', '佣金（美元 / 股）'), inp('sec_fee_sell', 'SEC 规费（卖出，按成交额）'), inp('finra_taf_per_share', 'FINRA TAF（美元 / 股）')]
                 : [inp('commission_rate', '佣金率（双向）', '如 0.00025 = 万 2.5'), inp('commission_min', '佣金最低（元）'), inp('stamp_duty_sell', '印花税（卖出）'), inp('transfer_fee', '过户费（双向）')]),
          inp('slippage', '滑点', '成交价比理想价差多少。0.001 = 0.1%，小盘股可调大'),
          inp('max_adv_pct', '单笔容量（占 20 日均额）', '一笔买入不超过该股日均成交额的这个比例，防止买不进 / 卖不出'),
          inp('stop_atr_k', '初始止损 k×ATR', 'ATR = 一只股票平均每天波动多少。止损放在买入价下方 k 个 ATR：k 越小止损越紧、越容易被洗出'),
          inp('trail_atr_k', '移动止损 k×ATR', '赚钱后止损跟着上移：最高价下方 k 个 ATR。k 越大拿得越久、回吐也越多'),
          inp('max_positions', '最大持仓数', '同时最多持有几只。新手建议 3~6 只'),
          inp('max_per_industry', '单行业上限（只）', '同一行业最多几只，避免一个行业跌就全军覆没'),
          inp('risk_per_trade', '单笔风险（净值比例）', '每笔交易「打到止损」最多亏总资金的多少。默认 0.0075 = 0.75%（10 万资金回测校准：收益风险比最好、回撤可承受）'),
          inp('max_total_risk', '组合总风险上限', '所有持仓同时打到止损时，合计最多亏多少。0.06 = 6%'),
          inp('max_hold_days', '最长持有（交易日）', '买入后最多拿几天还没走出来就卖（时间止损），避免资金长期被套'),
          inp('hard_stop_pct', '硬止损（相对成本）', '不管 ATR 怎么算，亏到这个比例一定卖。0.08 = 跌 8% 必走')),
        h('p', { class: 'hint mt-s' }, '不懂就别改：默认值都经过回测校准（tools/calibrate.py，样本内选、样本外验）。改动后先到「回测」页跑一遍再用，试验次数会被记录。'),
        h('div', { class: 'row mt' }, h('button', { class: 'btn primary', onclick: async () => {
          try {
            const costs = US ? { us: { commission_per_share: F.commission_per_share, sec_fee_sell: F.sec_fee_sell, finra_taf_per_share: F.finra_taf_per_share } }
              : { cn: { commission_rate: F.commission_rate, commission_min: F.commission_min, stamp_duty_sell: F.stamp_duty_sell, transfer_fee: F.transfer_fee } };
            const rest = { execution: { slippage: F.slippage, max_adv_pct: F.max_adv_pct },
              portfolio: { max_positions: F.max_positions, max_per_industry: F.max_per_industry, risk_per_trade: F.risk_per_trade, max_total_risk: F.max_total_risk },
              exits: { stop_atr_k: F.stop_atr_k, trail_atr_k: F.trail_atr_k, max_hold_days: F.max_hold_days, hard_stop_pct: F.hard_stop_pct } };
            // 美股：除费用外都写到 markets.US 覆盖层，保证与 A 股参数各自独立
            cfg = await put('/settings', US ? { costs, markets: { US: rest } } : { costs, ...rest });
            toast('已保存', 'ok');
          } catch (e) { toast(e.message, 'bad'); }
        } }, '保存')));
    }

    // ---------- 资金方案（A 股）：宽基 ETF / 低风险组合 / 个股波段 ----------
    function renderAlloc() {
      clear(secAlloc);
      if (state.market !== 'CN') { secAlloc.hidden = true; return; }
      secAlloc.hidden = false;
      const al = cfg.allocation || {}, fpc = cfg.factor_portfolio || {};
      const F = { etf: Math.round((al.etf ?? 0.7) * 100), factor: Math.round((al.factor ?? 0.3) * 100), n: fpc.n ?? 10 };
      const swingEl = h('span', { class: 'v num' });
      const upd = () => { const sw = 100 - F.etf - F.factor; swingEl.textContent = sw >= 0 ? sw + '%' : '超过 100%'; swingEl.style.color = sw < 0 ? 'var(--red)' : ''; };
      const inp = (k, label, hint, step = 5) => h('label', { class: 'f' }, label, h('input', { type: 'number', min: 0, max: k === 'n' ? 50 : 100, step, value: F[k], oninput: e => { F[k] = +e.target.value; upd(); } }), hint ? h('span', { class: 'tiny faint' }, hint) : null);
      secAlloc.append(h('div', { class: 'card-title' }, h('h2', {}, '资金方案'), h('span', { class: 'hint' }, '总资金在「持仓」页填写')),
        h('div', { class: 'alert info mb small' }, h('b', {}, '默认：宽基 ETF 70% + 低风险组合 30%，个股波段 0。'),
          '10 万资金按 2018~2026 回测（整手、最低 5 元佣金都算上）：全程年化约 5.1%、最大回撤约 -12.5%，几个方案里 Sharpe 最高、回撤最小。',
          'ETF 比例在 30%~70% 之间 Sharpe 差不多——能忍更大回撤、想多赚，可以多给组合（全部给组合：年化约 6.6%、回撤约 -23%）。',
          '把个股波段加进来（1 只或 2 只）在各时段都没有更好，所以默认不分钱；想练手就把组合调小一点，剩下的就是波段的。'),
        h('div', { class: 'grid c4' }, inp('etf', '宽基 ETF（%）', '择时规则，熊市自动空仓'), inp('factor', '低风险组合（%）', '每月调仓一次，不设止损'),
          h('div', { class: 'kv' }, h('span', { class: 'k' }, '个股波段（剩下的）'), swingEl, h('span', { class: 'tiny faint' }, 'VCP 信号，按止损 / 止盈做')),
          inp('n', '组合持有只数', '默认 10；组合资金 20 万以上可改 20', 1)),
        h('div', { class: 'row mt' }, h('button', { class: 'btn primary', onclick: async () => {
          if (F.etf < 0 || F.factor < 0 || F.etf + F.factor > 100) { toast('ETF 和组合合计不能超过 100%', 'bad'); return; }
          if (!(F.n >= 3 && F.n <= 50)) { toast('组合只数请填 3~50', 'bad'); return; }
          try {
            cfg = await put('/settings', { allocation: { etf: F.etf / 100, factor: F.factor / 100 }, factor_portfolio: { n: Math.round(F.n) } });
            toast('已保存：回到「选股」页即按新方案计算', 'ok'); renderAlloc();
          } catch (e) { toast(e.message, 'bad'); }
        } }, '保存'), h('span', { class: 'hint' }, '改只数后，组合的回测证据会在后台重算（约 1~2 分钟）。')));
      upd();
    }

    // ---------- 外观 / 口令 ----------
    function renderLook() {
      clear(secLook);
      const mode = prefs.get('colorMode', 'auto'), theme = prefs.get('theme', 'auto');
      const seg = (items, cur, on) => { const w = h('div', { class: 'seg' }); items.forEach(([v, l]) => { const b = h('button', { class: v === cur ? 'on' : '', onclick: () => { [...w.children].forEach(c => c.classList.remove('on')); b.classList.add('on'); on(v); } }, l); w.append(b); }); return w; };
      secLook.append(h('h2', { class: 'mb' }, '外观与访问'),
        h('div', { class: 'row between wrap mb' }, h('div', {}, h('div', {}, '涨跌配色'), h('div', { class: 'hint' }, '默认随市场自适应：A股 红涨绿跌、美股 绿涨红跌。只改颜色映射，不影响数据与符号。')),
          seg([['auto', '随市场自适应'], ['red', '全局红涨绿跌'], ['green', '全局绿涨红跌']], mode, v => { prefs.set('colorMode', v); ctx.applyColors(); })),
        h('div', { class: 'row between wrap mb' }, h('div', {}, '主题'), seg([['auto', '跟随系统'], ['light', '浅色'], ['dark', '深色']], theme, v => { prefs.set('theme', v); ctx.applyTheme(); })),
        h('div', { class: 'row between wrap mb' }, h('div', {}, h('div', {}, '启动后自动全屏'), h('div', { class: 'hint' }, '浏览器只允许在你点击 / 按键之后进入全屏：打开后第一次点击即全屏，按 Esc 退出（本次不再自动进入）；左下角 ⛶ 随时切换。')),
          seg([['on', '开'], ['off', '关']], prefs.get('autoFullscreen', true) ? 'on' : 'off', v => { prefs.set('autoFullscreen', v === 'on'); })),
        h('div', { class: 'row between wrap' }, h('div', {}, h('div', {}, '访问口令'), h('div', { class: 'hint' }, `后端 ${cfg.server.host}:${cfg.server.port}${cfg.server.token_set ? ' · 已设置口令' : ' · 仅本机访问，未设口令'}。开放局域网（host=0.0.0.0）时必须在 config.yaml 设置 server.token（系统含持仓数据）。`)),
          h('button', { class: 'btn sm', onclick: () => { const f = { t: prefs.get('token', '') }; modal('访问口令', h('label', { class: 'f' }, '与后端 server.token 一致', h('input', { type: 'password', value: f.t, oninput: e => { f.t = e.target.value; } })), [{ label: '取消' }, { label: '保存', primary: true, onclick: () => { prefs.set('token', f.t); toast('已保存', 'ok'); } }]); } }, '设置本机口令')));
    }

    // ---------- 存储空间：每部分多大、有没有用；可清理的项由用户点按钮确认后才执行 ----------
    async function renderStorage() {
      clear(secStorage);
      let r = null; try { r = await get('/storage', {}, { market: false, cache: false }); } catch (e) { secStorage.append(h('div', { class: 'hint' }, e.message)); return; }
      const mb = b => (b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(b >= 1e7 ? 0 : 1) + ' MB');
      const saving = r.items.filter(i => i.action).reduce((s, i) => s + (i.action === 'compress' ? i.bytes * 0.69 : i.bytes), 0);
      secStorage.append(h('div', { class: 'card-title' }, h('h2', {}, '存储空间'), h('span', { class: 'hint' }, `合计约 ${mb(r.total_bytes)}`)),
        h('p', { class: 'hint' }, `数据目录：${r.data_dir}。行情库每个交易日只增加约 1 MB，正常不会变大很多；占地方的主要是备份。`
          + (saving > 5e7 ? ` 下面可清理 / 压缩的项合计约可省 ${mb(saving)}。` : '')),
        h('table', { class: 'mini mt-s' }, h('thead', {}, h('tr', {}, h('th', {}, '内容'), h('th', { class: 'r' }, '大小'), h('th', {}, '说明'), h('th', {}, ''))),
          h('tbody', {}, r.items.map(i => h('tr', {},
            h('td', {}, h('b', {}, i.title)), h('td', { class: 'num r' }, mb(i.bytes)), h('td', { class: 'small muted' }, i.desc),
            h('td', {}, i.action ? h('button', { class: 'btn sm' + (i.action === 'delete' ? '' : ' primary'), onclick: async e => {
              const verb = i.action === 'compress' ? '压缩' : '删除';
              if (!await confirmBox(`${verb}「${i.title}」（${mb(i.bytes)}）？${i.action === 'delete' ? '删除后不能恢复。' : '压缩约需 30 秒，压缩后仍可用于恢复。'}`)) return;
              const b = e.currentTarget; b.disabled = true; b.textContent = verb + '中…';
              try { const x = await post('/storage/clean', { key: i.key }, { market: false, timeout: 600000 }); toast(`已${verb}，腾出 ${mb(x.freed_bytes)}`, 'ok'); }
              catch (err) { toast(err.message, 'bad'); }
              renderStorage();
            } }, i.action === 'compress' ? '压缩' : '删除') : null))))));
    }

    // ---------- 备份 ----------
    async function renderBackup() {
      clear(secBackup);
      let b = null; try { b = await get('/backups', {}, { market: false, cache: false }); } catch (e) { secBackup.append(h('div', { class: 'hint' }, e.message)); return; }
      secBackup.append(h('div', { class: 'card-title' }, h('h2', {}, '备份与恢复'), h('span', { class: 'badge ' + (b.stale ? 'warn' : 'ok') }, b.last ? `最近备份 ${b.last}` : '尚无备份')),
        b.stale ? h('div', { class: 'alert warn mb' }, '备份已超过 1 天。个人数据（持仓 / 交易日志 / 扫描存档）不可重建，请保持每日备份。') : null,
        h('p', { class: 'small muted' }, `备份目录：${b.dir}（建议在 config.yaml 的 backup.dir 指向网盘同步目录或外接盘——只放同一块硬盘防不了硬盘故障）。保留策略：近 ${cfg.backup.keep_daily} 天日备 + 近 ${cfg.backup.keep_monthly} 个月月备。`),
        h('div', { class: 'row mt-s' }, h('button', { class: 'btn', onclick: async () => { const r = await post('/jobs/backup', {}); toast(r.message, r.ok ? 'ok' : 'bad'); setTimeout(renderBackup, 2500); } }, '立即备份')),
        b.items.length ? h('table', { class: 'mt-s' }, h('tbody', {}, b.items.slice(0, 6).map(x => h('tr', {}, h('td', { class: 'num' }, x.date), h('td', { class: 'num r' }, x.size_mb + ' MB'), h('td', { class: 'small muted' }, Object.keys(x.files).join(' · ')))))) : null,
        h('p', { class: 'small muted' }, `行情库全量备份（每周一份、只留 3 份）：${b.full_dir}`),
        h('p', { class: 'hint mt-s' }, '恢复请在停止后端后用命令行：python -m server.cli restore --date YYYY-MM-DD --what portfolio（恢复前会自动另存现有文件，并校验记录数）。'));
    }

    secAbout.id = 'about';
    (async () => {
      try { const a = await loadAbout(); secAbout.prepend(h('h2', { class: 'mb' }, '关于'), aboutBlock(a), h('hr', { class: 'about-hr' })); }
      catch (e) { secAbout.prepend(h('div', { class: 'hint' }, '关于：' + e.message)); }
    })();
    secAbout.append(h('details', { class: 'mb' }, h('summary', {}, '检验通过标准（config.yaml 的 validation，默认值）'), h('pre', { class: 'code' }, JSON.stringify(cfg.validation, null, 2))),
      h('h3', {}, '本版本的范围'), h('ul', { class: 'small muted', style: 'margin:6px 0 0;padding-left:18px' },
      h('li', {}, '范围：需求书 v0.3 的 M1~M6（A 股 + 美股）。美股回测存在幸存者偏差（免费源无已退市股票），结果只作上界参考。'),
      h('li', {}, '所有策略规则都是待验证的假设；没有通过「回测 → 样本外」检验的规则一律视为无效（5.1）。'),
      h('li', {}, '系统不接券商下单；持仓由你手动录入，系统只给出次日执行参数。'),
      h('li', {}, '本工具仅作个人研究，不构成任何投资建议。')));

    // ---------- L1 偏差对比实验 ----------
    async function renderBias() {
      let r = null; try { r = await get('/l1_bias', {}, { cache: false }); } catch { return; }
      const rep = r.report;
      secPool.append(h('details', { class: 'mt', open: !!rep }, h('summary', {}, 'L1 快照偏差对比实验（3.26.1-6）' + (rep ? `：${rep.material ? '差异显著' : '差异不显著'}` : '：尚未运行')),
        rep ? h('div', {}, h('p', { class: 'small' }, rep.verdict),
          h('div', { class: 'grid c4' }, kv('抽样 / 被剔除总数', `${rep.sampled} / ${rep.excluded_total}`), kv('曾进入 L2 的抽样股', rep.sampled_ever_in_l2), kv('期望 R 变化', (rep.delta_expectancy_r > 0 ? '+' : '') + rep.delta_expectancy_r), kv('最大回撤变化', fmtPct(rep.delta_max_drawdown))),
          h('p', { class: 'hint mt-s' }, `不含抽样股：${rep.base.n} 笔，期望 ${rep.base.expectancy_r}R；含抽样股：${rep.with_extra.n} 笔，期望 ${rep.with_extra.expectancy_r}R。抽样股自身 ${rep.trades_in_sampled_stocks.n} 笔，期望 ${rep.trades_in_sampled_stocks.expectancy_r ?? '—'}R。运行于 ${rep.run_at.replace('T', ' ')}。${rep.note}`))
          : h('p', { class: 'hint' }, 'L1 入库粗筛基于入库当天的快照，可能把「过去正常、如今已跌成低价」的股票筛掉，使回测偏乐观。全量初始化完成后运行：python -m server.cli l1-bias --sample 100（在数据库副本上抽样补拉被剔除股票的历史，比较含 / 不含的回测差异）。')));
    }

    renderData(); renderAlloc(); renderPool().then(renderBias); renderCost(); renderLook(); renderBackup(); renderStorage();
  },
  cleanup() { stopPoll(); },
};

function kv(k, v, cls = '') { return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v)); }
