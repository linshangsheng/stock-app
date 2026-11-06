// 设置（4.2）：左右两栏，左边是分类、右边是选中的那一类。
//   通用设置：各市场的自动更新开关、外观与访问（涨跌配色 / 主题 / 全屏 / 口令）、邮件提醒
//   交易参数：资金方案（A 股）、费用 / 滑点 / 组合与止损参数
//   数据更新：数据健康、数据与更新状态（初始化 / 盘后任务链 / 补跑）、股票池
//   备份与存储：备份与恢复、存储空间
//   软件升级：从 git 仓库检查 / 一键升级（进度环），升级后自动重启
//   关于：版本、作者、数据来源、第三方组件与许可
import { get, post, put, state } from './api.js';
import { h, clear, fmtPct, fmtNum, fmtMoney, toast, confirmBox, modal, dirClass, switchEl, mountIcons } from './util.js';
import { prefs } from './db.js';
import { loadAbout, aboutBlock } from './about.js';
import { healthPanel } from './health.js';
import { upgradePanel } from './upgrade.js';

let pollTimer = null;
const stopPoll = () => { clearInterval(pollTimer); pollTimer = null; };
let healthView = null, upgradeView = null;

const SECTIONS = [
  ['general', '通用设置', '自动更新开关、外观、邮件提醒', 'gear'],
  ['trade', '交易参数', '资金方案、费用与风控参数', 'sliders'],
  ['data', '数据更新', '数据健康、更新与补全、股票池', 'refresh'],
  ['backup', '备份与存储', '备份与恢复、占用空间', 'archive'],
  ['upgrade', '软件升级', '检查并一键升级到新版本', 'upload'],
  ['about', '关于', '版本、作者、数据来源与许可', 'info'],
];

export const settingsView = {
  layout: 'split',
  ownDeepLink: true,                       // #/settings/data：直接打开「数据更新」
  async mount(ctx) {
    stopPoll();
    const list = ctx.listEl, pane = ctx.detailEl, wrap = pane;
    const gen = ctx.gen, alive = () => ctx.gen === gen;
    healthView?.destroy(); const health = healthView = healthPanel();
    upgradeView?.destroy(); const upgrade = upgradeView = upgradePanel();
    const secAuto = h('div', { class: 'card' }), secData = h('div', { class: 'card mt' }), secAlloc = h('div', { class: 'card' }), secPool = h('div', { class: 'card mt' }), secCost = h('div', { class: 'card mt' }), secLook = h('div', { class: 'card mt' }), secNotify = h('div', { class: 'card mt' }), secBackup = h('div', { class: 'card' }), secStorage = h('div', { class: 'card mt' }), secAbout = h('div', { class: 'card soft' });

    // ---------- 左栏：分类；右栏：选中那一类的内容 ----------
    const PAGES = { general: [secAuto, secLook, secNotify], trade: [secAlloc, secCost], data: [health.el, secData, secPool], backup: [secBackup, secStorage], upgrade: [upgrade.el], about: [secAbout] };
    let cur = null, upgradeLoaded = false;
    function open(key, byUser) {
      const sec = SECTIONS.find(x => x[0] === key) || SECTIONS[0];
      cur = sec[0]; prefs.set('settingsTab', cur);
      if (byUser) ctx.setMobileDetail(true);
      list.querySelectorAll('.list-item').forEach(x => x.classList.toggle('sel', x.dataset.symbol === cur));
      pane.replaceChildren(h('div', { class: 'mobile-top' }, h('button', { class: 'btn sm', onclick: () => ctx.setMobileDetail(false) }, '← 返回')),
        h('div', { class: 'pane-body page' }, h('div', { class: 'row between mb' }, h('h1', {}, sec[1]), h('span', { class: 'hint' }, sec[2])), ...PAGES[cur]));
      pane.scrollTop = 0;
      if (cur === 'upgrade' && !upgradeLoaded) { upgradeLoaded = true; upgrade.mount(); }          // 第一次点开才联网检查更新
    }
    const nav = h('div', { class: 'set-nav' }, SECTIONS.map(([k, t, d, ico]) => h('button', { class: 'list-item set-item', dataset: { symbol: k }, onclick: () => open(k, true) },
      h('span', { class: 'set-ico' }, h('i', { 'data-ico': ico })), h('span', { class: 'set-txt' }, h('span', { class: 'name' }, t), h('span', { class: 'set-desc' }, d)))));
    list.append(h('div', { class: 'pane-head' }, h('div', { class: 'row between' }, h('h1', {}, '设置'))), nav);
    mountIcons(nav);
    const want = SECTIONS.some(x => x[0] === ctx.deep) ? ctx.deep : prefs.get('settingsTab', 'general');
    const narrow = window.innerWidth <= 860;
    if (!narrow || ctx.deep) open(want, !!ctx.deep && narrow);         // 窄屏：先显示分类列表，点了再进去（带 #/settings/data 这类链接时直接进）
    else open(want, false), ctx.setMobileDetail(false);

    let cfg = {};
    try { cfg = await get('/settings', {}, { cache: false }); } catch (e) { if (alive()) pane.replaceChildren(h('div', { class: 'alert bad', style: 'margin:16px' }, e.message)); return; }
    if (!alive()) return;
    health.mount();                                  // 数据健康：单独加载，不拖慢下面的各块

    // ---------- 数据与更新 ----------
    async function renderData() {
      let j = null;
      try { j = await get('/jobs', {}, { cache: false }); } catch (e) { clear(secData); secData.append(h('div', { class: 'alert bad' }, e.message)); return; }
      clear(secData);
      const run = j.job?.running;
      const pr = j.job?.progress;
      secData.append(h('div', { class: 'card-title' }, h('h2', {}, '数据与更新状态'), h('span', { class: 'badge ' + (j.has_data ? 'ok' : 'warn') }, j.has_data ? '已有数据' : '尚未初始化')),
        h('div', { class: 'grid c4' }, kv('数据截止', j.data_asof || '—'), kv('最近已收盘交易日', j.last_closed_trading_day || '—'), kv('交易日历来源', j.calendar_source === 'fallback' ? '兜底（仅周末，节假日不识别！）' : j.calendar_source === 'xshg' ? '上交所日历（离线，BaoStock 连不上时）' : j.calendar_source === 'source' ? '数据源' : '—', j.calendar_source === 'fallback' ? 'down' : ''),
          kv('初始化范围', j.init_sample ? `随机 ${j.init_sample.count} 只` : '全部'), kv('数据源', (cfg.datasource?.[state.market.toLowerCase()] === 'demo') ? '合成演示数据' : (state.market === 'US' ? 'yfinance + Nasdaq Trader' : 'BaoStock（历史，连不上时自动换腾讯 / 东方财富）+ 腾讯批量报价（每日收盘）'))),
        j.index_members ? h('p', { class: 'hint mt-s' + (j.index_members.stale ? ' down' : '') },
          `美股选股范围（标普500 ∪ 纳斯达克100）：${j.index_members.total} 只 · 名单更新于 ${j.index_members.updated || '未知（软件自带快照）'}`
          + (j.index_members.stale ? '，已超过 5 周没更新（可能一直联网失败），名单可能过时' : '，每月自动更新一次')) : null,
        j.calendar_source === 'fallback' ? h('div', { class: 'alert warn mt-s' }, '交易日历取自兜底（周一至周五），节假日会被当作交易日——完整性闸门可能误报缺失。请检查网络后重新初始化日历。') : null,
        run ? h('div', { class: 'mt' }, h('div', { class: 'row between small' }, h('span', {}, h('span', { class: 'spinner' }), ` 运行中：${run} ${j.job.message || ''}`), pr ? h('span', { class: 'num' }, `${pr.task} ${pr.done}/${pr.total}`) : null),
          pr ? h('div', { class: 'progress mt-s' }, h('div', { style: `width:${Math.round(100 * pr.done / Math.max(pr.total, 1))}%` })) : null,
          h('div', { class: 'row mt-s' }, h('button', { class: 'btn sm', onclick: async () => { await post('/jobs/stop', {}); toast('已请求停止（当前请求结束后中断，可续跑）'); } }, '停止（可续跑）'))) : null,
        h('div', { class: 'row wrap mt' },
          h('button', { class: 'btn primary', disabled: run ? true : null, onclick: () => initDlg(j) }, j.has_data ? '补全 / 继续初始化' : '初始化数据'),
          h('button', { class: 'btn', disabled: run || !j.has_data ? true : null, onclick: async () => { const r = await post('/jobs/daily', {}); toast(r.message, r.ok ? 'ok' : 'bad'); startPoll(); } }, '运行盘后任务链'),
          h('button', { class: 'btn', disabled: run || !j.has_data ? true : null, onclick: async () => { const r = await post('/jobs/catch_up', {}); toast(r.message, r.ok ? 'ok' : 'bad'); startPoll(); } }, '补跑缺失交易日'),
          h('button', { class: 'btn', disabled: !j.has_data ? true : null, onclick: async () => { toast('扫描中…'); try { await post('/scan/run', {}, { timeout: 300000 }); toast('扫描完成', 'ok'); } catch (e) { toast(e.message, 'bad'); } } }, '仅选股扫描')),
        cfg.jobs?.auto_update?.[state.market] === false ? h('div', { class: 'alert info mt-s' }, h('b', {}, `${state.market === 'US' ? '美股' : 'A 股'}的「自动更新」是关着的：`), '行情不会自己更新。需要时点下面的「运行盘后任务链」手动更新；想让它自动更新，到「通用设置」打开开关。') : null,
        j.has_data ? h('p', { class: 'hint mt-s' }, state.market === 'US'
          ? '每天的更新是自动的：软件开着时，美股收盘后（北京时间早上）会自动更新并出清单，不用点按钮。「补全 / 继续初始化」只在第一次安装、很久没打开或想刷新股票名单时用；点了之后也会接着更新到最新交易日。'
          : '每天的更新是自动的：软件开着时，A 股收盘约 35 分钟后（15:35 起）自动用批量报价更新（约 1~2 分钟）并出清单，不用点按钮。错过当晚、直到第二天 9:15 以后才打开时，缺的那天只能逐只从 BaoStock 补（约 50 分钟）；周末打开不受影响。「补全 / 继续初始化」只在第一次安装、很久没打开或想刷新股票名单时用；点了之后也会接着更新到最新交易日。') : null);
      const TASK = { init: '补全 / 初始化', daily: '盘后任务链', catch_up: '自动更新（补跑）', backup: '备份' };
      const secs = s => (s >= 60 ? `${Math.floor(s / 60)} 分 ${String(Math.round(s % 60)).padStart(2, '0')} 秒` : `${Math.round(s)} 秒`);
      for (const [k, v] of Object.entries(j.job?.last || {})) {
        const [t, m] = k.split(':');
        secData.append(h('div', { class: 'small mt-s ' + (v.ok ? 'muted' : ''), style: v.ok ? '' : 'color:var(--danger)' },
          `上次${TASK[t] || t}（${m === 'US' ? '美股' : 'A股'}）：${v.at.replace('T', ' ')} ${v.ok ? '成功' : '失败 ' + v.error}${v.secs != null ? ' · 用时 ' + secs(v.secs) : ''}`,
          v.timing?.length ? h('div', { class: 'tiny muted' }, v.timing.map(([n, x]) => `${n} ${secs(x)}`).join(' · ')) : null));
      }
      if (j.gate) secData.append(h('details', { class: 'mt', open: j.gate.status !== 'PASS' }, h('summary', {}, `数据完整性闸门（${j.gate.day}）：${j.gate.status}`),
        h('table', {}, h('tbody', {}, j.gate.checks.map(c => h('tr', {}, h('td', { style: 'width:20px' }, c.ok ? '✓' : c.hard ? '✗' : '!'), h('td', {}, c.name), h('td', { class: 'small muted' }, c.msg), h('td', { class: 'num small' }, String(c.value))))))));
      if (state.market === 'CN') {                       // A 股数据源体检：初始化失败（如「CircuitOpen: baostock」）时看哪个源能连上
        const box = h('div', { class: 'small mt-s' });
        secData.append(h('div', { class: 'row wrap mt' }, h('button', { class: 'btn sm', onclick: async e => {
          e.target.disabled = true; box.replaceChildren(h('span', { class: 'spinner' }), ' 正在检测（每个源取几天数据，约 5~60 秒）…');
          try {
            const r = await get('/datasources/probe', {}, { cache: false, timeout: 180000 });
            box.replaceChildren(...r.results.map(x => h('div', {}, h('span', { class: 'badge ' + (x.ok ? 'ok' : 'bad') }, x.ok ? '可用' : '不可用'), ` ${x.label} · ${x.seconds} 秒`,
              x.ok ? '' : h('span', { class: 'muted' }, ` · ${x.error || ''}`))),
              h('div', { class: 'hint mt-s' }, r.usable.length ? `初始化 / 补全会按 BaoStock → 腾讯 → 东方财富 的顺序自动选用能连上的：本次会用「${r.usable[0]}」。` : '三个数据源都连不上：请检查网络（公司网络 / 代理可能拦截），换个网络或稍后再试。'));
          } catch (err) { box.replaceChildren(h('span', { class: 'down' }, err.message)); }
          e.target.disabled = false;
        } }, '检测 A 股数据源'), h('span', { class: 'hint' }, 'BaoStock 常在北京时间下午到晚上连不上；连不上时软件会自动改用腾讯 / 东方财富。')), box);
      }
      secData.append(h('details', { class: 'mt' }, h('summary', {}, '上游限速 / 熔断状态（反封 IP，3.26.3）'), h('div', { class: 'row wrap' }, (j.throttle || []).map(t => h('span', { class: 'badge ' + (t.breaker_open ? 'bad' : 'ok') }, `${t.name}：${t.breaker_open ? '熔断 ' + t.breaker_remaining_s + 's' : '正常'} · 请求 ${t.calls}`)))),
        h('details', {}, h('summary', {}, '最近任务日志'), h('div', { class: 'small' }, (j.logs || []).map(l => h('div', { class: 'muted num' }, `${l.ts.replace('T', ' ')}  ${l.job}  ${l.status}  ${l.detail || ''}`)))));
      if (run && !pollTimer) startPoll();
      if (!run && pollTimer) stopPoll();
    }
    function startPoll() {
      if (pollTimer) return;
      let busy = false, tick = 0;              // 串行轮询：上一轮没返回就跳过；股票池（要构建全市场面板，较重）每约 30 秒才刷新一次
      pollTimer = setInterval(async () => {
        if (!document.body.contains(wrap) || !alive()) return stopPoll();
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
        u.l2_exclusions ? h('div', { class: 'mt' }, h('h3', {}, '今日 L2 各条件剔除数量（逐项独立统计，用于校准阈值）'), h('div', { class: 'row wrap mt-s' }, Object.entries(u.l2_exclusions).map(([k, v]) => h('span', { class: 'tag gray' }, `${({ tradable: '当日可交易', list_age: '上市天数', price: '股价', liquidity: '成交额', suspend: '停牌天数', oneword: '一字板', not_st: '非 ST', index_member: '不在标普500 / 纳指100' })[k] || k}：剔除 ${v}`)))) : null,
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
      const upd = () => { const sw = 100 - F.etf - F.factor; swingEl.textContent = sw >= 0 ? sw + '%' : '超过 100%'; swingEl.style.color = sw < 0 ? 'var(--danger)' : ''; };
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

    // ---------- 自动更新开关：各市场分开；默认只开 A 股 ----------
    function renderAuto() {
      clear(secAuto);
      const au = { CN: cfg.jobs?.auto_update?.CN ?? true, US: cfg.jobs?.auto_update?.US ?? false };
      const MK = [['CN', 'A 股', '收盘约 35 分钟后（15:35 起）自动用批量报价更新，约 1~2 分钟，并生成选股清单。'], ['US', '美股', '美股收盘后（北京时间早上）自动更新并生成选股清单。']];
      const rows = MK.map(([m, name, when]) => {
        const status = h('div', { class: 'au-status' });
        const note = h('div', { class: 'au-note' });
        const paint = () => {
          status.textContent = au[m] ? '已开启：软件开着时会自动更新' : '已关闭：这个市场不会自动更新，需要时到「数据更新」手动更新';
          status.className = 'au-status ' + (au[m] ? 'on' : 'off');
        };
        const sw = switchEl(au[m], async v => {
          au[m] = v; paint();
          try { cfg = await put('/settings', { jobs: { auto_update: { [m]: v } } }); toast(`${name}自动更新已${v ? '开启' : '关闭'}`, 'ok'); }
          catch (e) { au[m] = !v; sw.set(!v); paint(); toast(e.message, 'bad'); }
        }, `${name}自动更新`);
        paint();
        (async () => {                                   // 还没有这个市场的数据：开着开关也不会更新，提醒先初始化
          try { const j = await get('/jobs', { market: m }, { market: false, cache: false }); if (!j.has_data) note.textContent = `${name}还没有数据：先到「数据更新」点「初始化数据」，之后才会自动更新。`; } catch { /* 忽略 */ }
        })();
        return h('div', { class: 'au-row' }, h('div', { class: 'au-main' }, h('div', { class: 'au-name' }, h('span', { class: 'badge accent' }, name), h('b', {}, `${name}自动更新`)), h('div', { class: 'hint' }, when), status, note), sw);
      });
      secAuto.append(h('div', { class: 'card-title' }, h('h2', {}, '自动更新股票数据')),
        h('p', { class: 'small muted' }, '软件开着时，每个市场收盘后自动补拉行情、生成当天的选股清单；错过的交易日下次打开时会补上。关掉的市场不会自己更新，随时可以到「数据更新」手动更新。默认只开 A 股。'),
        ...rows);
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
        h('div', { class: 'row between mb', style: 'gap:16px' }, h('div', { style: 'min-width:0' }, h('div', {}, '启动后自动全屏'), h('div', { class: 'hint' }, '桌面快捷方式打开的窗口本身就是全屏（按 F11 退出），但鼠标移到屏幕最上沿时 Edge 会滑出一条标题栏。打开后第一次点击 / 按键，这里会把它升级成「网页全屏」——不再有那条标题栏，按 Esc 退出（本次不再自动进入）；左下角 ⛶ 随时切换。关掉这个开关就不升级。')),
          switchEl(prefs.get('autoFullscreen', true), v => { prefs.set('autoFullscreen', v); }, '启动后自动全屏')),
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


    // ---------- 邮件提醒：默认不发送；填好发件邮箱 + 授权码 + 收件邮箱并打开开关才会发 ----------
    async function renderNotify() {
      let n = null;
      try { n = await get('/notify', {}, { market: false, cache: false }); } catch (e) { clear(secNotify); secNotify.append(h('div', { class: 'alert bad' }, e.message)); return; }
      clear(secNotify);
      const f = { enabled: n.enabled, sender: n.sender, password: '', recipients: n.recipients, host: n.host, port: n.port, security: n.security, events: { ...n.events } };
      const tip = h('div', { class: 'hint mt-s' }, n.preset?.tip || '');
      const host = h('input', { type: 'text', value: f.host, placeholder: '例如 smtp.qq.com', style: 'width:200px', oninput: e => { f.host = e.target.value; } });
      const port = h('input', { type: 'number', value: f.port, style: 'width:90px', oninput: e => { f.port = e.target.value; } });
      const sec = h('select', { onchange: e => { f.security = e.target.value; } }, [['ssl', 'SSL（465）'], ['starttls', 'STARTTLS（587）'], ['none', '不加密（不推荐）']].map(([v, l]) => h('option', { value: v, selected: v === f.security ? true : null }, l)));
      const applyPreset = async () => {
        if (!/@/.test(f.sender)) return;
        let p = {}; try { p = await get('/notify/preset', { email: f.sender }, { market: false, cache: false }); } catch { /* 忽略 */ }
        tip.textContent = p.tip || '';
        if (p.host && !f.host) { f.host = p.host; f.port = p.port; f.security = p.security; host.value = p.host; port.value = p.port; sec.value = p.security; }
      };
      const save = async () => {
        const body = { enabled: f.enabled, sender: f.sender, recipients: f.recipients, host: f.host, port: f.port, security: f.security, events: f.events };
        if (f.password) body.password = f.password;
        try { await put('/notify', body, { market: false }); toast('已保存', 'ok'); renderNotify(); return true; }
        catch (e) { toast(e.message, 'bad'); return false; }
      };
      const chk = (label, key) => h('label', { class: 'chk' }, h('input', { type: 'checkbox', checked: f.events[key] ? true : null, onchange: e => { f.events[key] = e.target.checked; } }), label);
      secNotify.append(h('div', { class: 'card-title' }, h('h2', {}, '邮件提醒'), h('span', { class: 'badge ' + (n.ready ? 'ok' : '') }, n.status)),
        h('p', { class: 'small muted' }, '默认不发送任何邮件。填好下面三项并打开开关后，每天盘后任务链跑完会检查一次，有这些事才发：恐慌日（本轮第几个恐慌日、今天买预算的 1/10）、宽基 ETF 规则明天开盘的买卖信号、需要处理的持仓。同一件事同一个交易日只发一封。'),
        h('div', { class: 'form-grid mt' },
          h('div', { class: 'f full' }, h('span', {}, '邮件提醒开关'), h('div', { class: 'row', style: 'gap:10px' }, switchEl(f.enabled, v => { f.enabled = v; }, '邮件提醒'), h('span', { class: 'hint' }, '改完点下面的「保存」才生效'))),
          h('label', { class: 'f' }, h('span', {}, '发件邮箱（用它来发信）'), h('input', { type: 'text', value: f.sender, placeholder: 'you@qq.com', onchange: e => { f.sender = e.target.value.trim(); applyPreset(); } })),
          h('label', { class: 'f' }, h('span', {}, n.password_set ? '授权码（已保存；留空 = 保持不变）' : '授权码'), h('input', { type: 'password', placeholder: n.password_set ? '••••••••' : '邮箱网页里生成的授权码', autocomplete: 'new-password', oninput: e => { f.password = e.target.value; } })),
          h('label', { class: 'f full' }, h('span', {}, '收件邮箱（多个用逗号分隔；留空 = 发给自己）'), h('input', { type: 'text', value: f.recipients, placeholder: '留空 = 发给发件邮箱', oninput: e => { f.recipients = e.target.value; } }))),
        tip,
        h('div', { class: 'row wrap mt', style: 'gap:16px' }, h('b', { class: 'small' }, '发送哪些：'), chk('恐慌日', 'panic'), chk('ETF 规则信号', 'etf'), chk('需要处理的持仓', 'positions')),
        h('details', { class: 'mt' }, h('summary', {}, 'SMTP 服务器（常见邮箱会自动填好，一般不用改）'),
          h('div', { class: 'row wrap mt-s', style: 'gap:8px' }, host, port, sec)),
        h('div', { class: 'row wrap mt' },
          h('button', { class: 'btn primary', onclick: save }, '保存'),
          h('button', { class: 'btn', onclick: async e => {
            e.target.disabled = true; e.target.textContent = '发送中…';
            try { if (await save()) { const r = await post('/notify/test', {}, { market: false, timeout: 60000 }); toast(r.message, r.ok ? 'ok' : 'bad'); } }
            catch (err) { toast(err.message, 'bad'); }
            e.target.disabled = false; e.target.textContent = '保存并发送测试邮件';
          } }, '保存并发送测试邮件')),
        h('p', { class: 'hint mt-s' }, '授权码不是邮箱的登录密码，要在邮箱网页的设置里单独生成（QQ / 163 的「授权码」、Gmail 的「应用专用密码」）。它只保存在本机的 data/user_config.yaml，不会出现在备份和页面里。'
          + '邮件里只有提醒内容，不含你的资金数字。关闭开关或清空授权码，就不会再发。'));
    }

    secAbout.id = 'about';
    (async () => {
      try { const a = await loadAbout(); secAbout.prepend(aboutBlock(a), h('hr', { class: 'about-hr' })); }
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

    renderAuto(); renderData(); renderAlloc(); renderPool().then(renderBias); renderCost(); renderLook(); renderNotify(); renderBackup(); renderStorage();
  },
  cleanup() { stopPoll(); healthView?.destroy(); upgradeView?.destroy(); },
};

function kv(k, v, cls = '') { return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v)); }
