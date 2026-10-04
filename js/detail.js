// 个股详情（4.5.2）：不是一级导航，从选股 / 自选 / 持仓点进来。
// 顶部报价区 + 日 / 周 K 线（lightweight-charts）+ 成交量 + 均线 + 触发价 / 止损线 / 成本线 + 信号与买卖点 + 关键指标卡。
import { get, put, post, del, state } from './api.js';
import { StockChart } from './chart.js';
import { h, clear, fmtPrice, fmtPct, fmtSigned, fmtAmount, fmtNum, dirClass, code, toast, esc, modal, copyText, download } from './util.js';
import { openTradeForm } from './portfolio.js';

let chart = null;
let seq = 0;

export function destroyDetail() { chart?.destroy(); chart = null; }

const IND = [
  ['rsi14', 'RSI(14)', v => fmtNum(v, 1)], ['macd_hist', 'MACD 柱', v => fmtSigned(v, 3)], ['vol_ratio', '量比', v => fmtNum(v, 2)],
  ['atr_pct', 'ATR%', v => fmtPct(v, 2, false)], ['ret_5', '5 日涨幅', v => fmtPct(v)], ['ret_20', '20 日涨幅', v => fmtPct(v)],
  ['ret_60', '60 日涨幅', v => fmtPct(v)], ['close_ma20', '收盘/MA20', v => fmtNum(v, 3)], ['close_ma50', '收盘/MA50', v => fmtNum(v, 3)],
  ['dist_52w_high', '距 52 周高', v => fmtPct(v, 1, false)], ['close_pos', '收盘位置', v => fmtNum(v, 2)], ['rs_20', '相对大盘 20 日', v => fmtPct(v)],
  ['hv20', '20 日波动率', v => fmtPct(v, 1, false)], ['trend_r2', '趋势 R²', v => fmtNum(v, 2)],
];

export async function showDetail(ctx, symbol, meta = {}) {
  const el = ctx.detailEl;
  const my = ++seq;
  clear(el);
  ctx.setMobileDetail(true);
  el.append(h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载 ', symbol, ' …'));
  let k;
  try {
    k = await get('/kline', { symbol, period: ctx.detailPeriod || 'D' });
  } catch (e) {
    if (my !== seq) return;
    clear(el);
    el.append(topBar(ctx), h('div', { class: 'empty' }, h('div', { class: 'big' }, '无法加载 ' + symbol), e.message));
    return;
  }
  if (my !== seq) return;
  clear(el);
  const bars = k.bars, last = bars[bars.length - 1], prev = bars[bars.length - 2];
  const chg = prev ? last.close - prev.close : null, pct = prev ? last.close / prev.close - 1 : null;
  const watched = ctx.watchSet.has(symbol);
  const lv = k.levels || {};

  const head = h('div', { class: 'quote-head' },
    h('div', {},
      h('div', { class: 'row' }, h('h1', {}, k.name), h('span', { class: 'muted num' }, code(symbol)),
        k.industry ? h('span', { class: 'badge' }, k.industry) : null,
        k.board === 'star' ? h('span', { class: 'badge' }, '科创板') : k.board === 'chinext' ? h('span', { class: 'badge' }, '创业板') : null),
      h('div', { class: 'row', style: 'align-items:baseline;gap:12px;margin-top:4px' },
        h('span', { class: 'price ' + dirClass(chg) }, fmtPrice(last.close)),
        h('span', { class: 'num ' + dirClass(chg), style: 'font-size:15px;font-weight:600' }, `${fmtSigned(chg)}  ${fmtPct(pct)}`)),
      h('div', { class: 'small muted num mt-s' },
        `${k.asof_bar} 收盘　今开 ${fmtPrice(last.open)}　最高 ${fmtPrice(last.high)}　最低 ${fmtPrice(last.low)}　成交额 ${fmtAmount(last.amount)}`,
        k._stale ? h('span', { class: 'badge warn', style: 'margin-left:8px' }, '数据延迟（离线快照）') : null)),
    h('div', { class: 'row wrap' },
      h('button', { class: 'btn', onclick: async () => {
        await put('/watchlist', watched ? { remove: [symbol] } : { add: [{ symbol }] });
        await ctx.reloadWatch(); toast(watched ? '已移出自选' : '已加入自选', 'ok'); showDetail(ctx, symbol, meta); ctx.refreshList?.();
      } }, watched ? '★ 已自选' : '☆ 加自选'),
      h('button', { class: 'btn', title: '价格 / 涨跌幅预警（仅页面打开时生效）', onclick: () => alertDlg(symbol, k.name, last.close) }, '⏰ 预警'),
      h('button', { class: 'btn primary', onclick: () => openTradeForm({
        symbol, name: k.name, side: 'buy', price: lv.scan?.entry_ref ?? last.close, initial_stop: lv.scan?.stop_price, setup: lv.scan?.setup,
        signal_run_id: lv.scan?.run_id, planned_trigger: lv.scan?.trigger_price, shares: lv.scan?.shares, onDone: () => ctx.refreshList?.(),
      }) }, '记录买入')));

  // 清单信息卡
  const info = [];
  if (lv.scan) {
    info.push(h('div', { class: 'card soft mt' },
      h('div', { class: 'card-title' }, h('h3', {}, `今日观察清单（${lv.scan.scan_date}）`), h('span', { class: 'badge accent' }, lv.scan.setup)),
      h('div', { class: 'row wrap' }, (lv.scan.reasons || []).map(r => h('span', { class: 'tag' }, r))),
      h('div', { class: 'grid c4 mt-s' },
        kv('综合分', fmtNum(lv.scan.score, 0)), kv('触发价', fmtPrice(lv.scan.trigger_price)), kv('止损位', fmtPrice(lv.scan.stop_price)),
        kv('建议股数', lv.scan.shares != null ? String(lv.scan.shares) : '未录入账户'))));
  }
  if (lv.position) {
    const p = lv.position, pnl = last.close / p.avg_cost - 1;
    info.push(h('div', { class: 'card soft mt' },
      h('div', { class: 'card-title' }, h('h3', {}, '当前持仓'), h('span', { class: 'badge' }, `${p.open_date} 起`)),
      h('div', { class: 'grid c4' }, kv('持仓', p.qty + ' 股'), kv('成本', fmtPrice(p.avg_cost)),
        kv('浮盈', fmtPct(pnl), dirClass(pnl)), kv('移动止损', fmtPrice(p.current_stop)))));
  }

  const tools = h('div', { class: 'chart-tools' },
    seg([['D', '日K'], ['W', '周K']], ctx.detailPeriod || 'D', v => { ctx.detailPeriod = v; showDetail(ctx, symbol, meta); }),
    seg([['macd', 'MACD'], ['rsi', 'RSI']], chart?.subKind || 'macd', v => chart?.setSub(v)),
    h('button', { class: 'btn sm', title: '把当前图表导出为 PNG（分享图片）', onclick: () => exportImage(k.name, symbol) }, '导出图片'),
    h('button', { class: 'btn sm', title: '复制行情摘要文本', onclick: () => copyText(summaryText(k, last, chg, pct, lv)) }, '复制摘要'),
    h('span', { class: 'hint' }, adjLabel(k)));
  const mainEl = h('div', { class: 'chart-main' });
  const subEl = h('div', { class: 'chart-sub' });
  const box = h('div', { class: 'chart-box' }, mainEl, subEl);
  const ind = h('div', { class: 'ind-grid' }, IND.map(([key, label, f]) => {
    const v = k.indicators?.[key];
    return kv(label, v == null ? '—' : f(v), key.startsWith('ret') || key === 'rs_20' ? dirClass(v) : '');
  }));

  const finBox = h('div', { class: 'mt' });
  const flowBox = h('div', { class: 'mt' });
  el.append(topBar(ctx), head, ...info, tools, box, h('div', { class: 'pane-body' }, h('h3', { style: 'margin-bottom:8px' }, '关键指标'), ind, finBox, flowBox,
    h('p', { class: 'hint mt' }, '指标全部由后端 features.py 统一计算（口径见需求书 3.6.1），前端只做展示。价格为前复权。')));
  renderFin(finBox, symbol);
  renderFlow(flowBox, symbol);
  destroyDetail();
  chart = new StockChart(mainEl, subEl);
  chart.subKind = chart.subKind || 'macd';
  chart.build();
  chart.setData(k);
  ctx.currentSymbol = symbol;
}

function adjLabel(k) { return `前复权 · ${k.period === 'W' ? '周线' : '日线'} · 数据截止 ${k.data_asof || k.asof_bar}`; }

function topBar(ctx) {
  return h('div', { class: 'mobile-top' }, h('button', { class: 'btn sm', onclick: () => ctx.setMobileDetail(false) }, '← 返回'));
}

function kv(k, v, cls = '') { return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v)); }

function seg(items, cur, onPick) {
  const wrap = h('div', { class: 'seg' });
  for (const [v, label] of items) {
    const b = h('button', { class: v === cur ? 'on' : '', onclick: () => { [...wrap.children].forEach(c => c.classList.remove('on')); b.classList.add('on'); onPick(v); } }, label);
    wrap.append(b);
  }
  return wrap;
}

export function themeChanged() { if (chart && chart.payload) { const p = chart.payload; chart.build(); chart.setData(p); } }

/** 价格 / 涨跌幅预警：页面内提醒，仅在页面打开时生效（4.6）；基于盘后日线，不是盘中实时价。 */
async function alertDlg(symbol, name, lastClose) {
  const box = h('div', {});
  const f = { kind: 'price_above', value: '' };
  const kinds = { price_above: '收盘价 ≥', price_below: '收盘价 ≤', pct_above: '涨幅 ≥ (%)', pct_below: '跌幅 ≤ (%)' };
  async function refresh() {
    clear(box);
    let r; try { r = await get('/alerts', { symbol }, { cache: false }); } catch (e) { box.append(h('div', { class: 'alert bad' }, e.message)); return; }
    box.append(h('p', { class: 'hint' }, `${name}　最新收盘 ${fmtPrice(lastClose)}。${r.note}`),
      ...r.items.map(a => h('div', { class: 'row between', style: 'padding:4px 0' },
        h('span', {}, kinds[a.rule.split(':')[0]] + ' ' + a.rule.split(':')[1], a.triggered ? h('span', { class: 'badge bad', style: 'margin-left:6px' }, '已触发') : null),
        h('button', { class: 'btn sm ghost', onclick: async () => { await del('/alerts/' + a.id); refresh(); } }, '删除'))),
      h('div', { class: 'row mt-s' }, h('select', { onchange: e => { f.kind = e.target.value; } }, Object.entries(kinds).map(([v, l]) => h('option', { value: v }, l))),
        h('input', { type: 'number', step: 'any', placeholder: '数值', style: 'width:110px', oninput: e => { f.value = e.target.value; } }),
        h('button', { class: 'btn sm primary', onclick: async () => { try { await post('/alerts', { symbol, kind: f.kind, value: f.value }); refresh(); } catch (e) { toast(e.message, 'bad'); } } }, '添加')));
  }
  await refresh();
  modal('预警：' + name, box, [{ label: '关闭' }]);
}

/** 导出图表图片：lightweight-charts 自带 takeScreenshot()；离屏 Canvas 合成标题（分享图片，1.3.2 / 1.3.4）。 */
function exportImage(name, symbol) {
  if (!chart?.main) return;
  const src = chart.main.takeScreenshot();
  const pad = 36, cv = document.createElement('canvas');
  cv.width = src.width; cv.height = src.height + pad;
  const g = cv.getContext('2d');
  g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#fff'; g.fillRect(0, 0, cv.width, cv.height);
  g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text').trim() || '#000';
  g.font = '600 16px sans-serif'; g.fillText(`${name}  ${code(symbol)}`, 12, 24);
  g.drawImage(src, 0, pad);
  cv.toBlob(b => { const url = URL.createObjectURL(b); const a = document.createElement('a'); a.href = url; a.download = `${code(symbol)}-${new Date().toISOString().slice(0, 10)}.png`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
}

function summaryText(k, last, chg, pct, lv) {
  const i = k.indicators || {};
  const lines = [`${k.name}（${code(k.symbol)}）${k.industry ? ' · ' + k.industry : ''}`,
    `${k.asof_bar} 收盘 ${fmtPrice(last.close)}  ${fmtSigned(chg)} (${fmtPct(pct)})  成交额 ${fmtAmount(last.amount)}`,
    `RSI14 ${fmtNum(i.rsi14, 1)} · 量比 ${fmtNum(i.vol_ratio, 2)} · ATR% ${fmtPct(i.atr_pct, 2, false)} · 20日 ${fmtPct(i.ret_20)} · 距52周高 ${fmtPct(i.dist_52w_high, 1, false)}`];
  if (lv.scan) lines.push(`观察清单：${lv.scan.setup}，触发价 ${fmtPrice(lv.scan.trigger_price)}，止损位 ${fmtPrice(lv.scan.stop_price)}${lv.scan.shares != null ? '，建议 ' + lv.scan.shares + ' 股' : ''}`);
  if (lv.position) lines.push(`持仓：${lv.position.qty} 股，成本 ${fmtPrice(lv.position.avg_cost)}，移动止损 ${fmtPrice(lv.position.current_stop)}`);
  return lines.join('\n');
}

/** 基本面摘要（3.15）：按需加载。A 股带公告日（点时可用）；美股无披露时间，仅展示。 */
function renderFin(box, symbol) {
  const btn = h('button', { class: 'btn sm', onclick: load }, '加载基本面');
  box.append(h('div', { class: 'row between' }, h('h3', {}, '基本面'), btn));
  async function load() {
    btn.disabled = true; btn.textContent = '加载中…';
    let d;
    try { d = await get('/financials', { symbol }, { timeout: 120000 }); } catch (e) { btn.disabled = false; btn.textContent = '重试'; box.append(h('div', { class: 'alert bad mt-s' }, e.message)); return; }
    clear(box);
    const big = v => v == null ? '—' : Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(2) + (d.currency === 'USD' ? ' 亿美元' : ' 亿') : Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + ' 万' : v.toFixed(2);
    const pct = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
    box.append(h('div', { class: 'row between' }, h('h3', {}, '基本面'), h('span', { class: 'hint' }, `更新于 ${(d.fetched_at || '').replace('T', ' ')}`)));
    if (d.snapshot) {
      const sn = d.snapshot;
      box.append(h('div', { class: 'ind-grid mt-s' }, [['市值', big(sn.marketCap)], ['PE(TTM)', sn.trailingPE?.toFixed(1) ?? '—'], ['PB', sn.priceToBook?.toFixed(2) ?? '—'], ['净利率', pct(sn.profitMargins)],
        ['ROE', pct(sn.returnOnEquity)], ['营收增速', pct(sn.revenueGrowth)], ['空头占流通盘', pct(sn.shortPercentOfFloat)], ['Beta', sn.beta?.toFixed(2) ?? '—']].map(([k, v]) =>
        h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num' }, String(v))))));
    }
    if (d.periods?.length) {
      box.append(h('div', { class: 'tbl-wrap mt-s' }, h('table', {}, h('thead', {}, h('tr', {}, ['报告期', d.pit ? '公告日' : '', '营收', '净利润', d.pit ? 'ROE' : 'EPS', d.pit ? '净利率' : '', d.pit ? '资产负债率' : ''].filter((x, i) => x || i < 2).map((t, i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
        h('tbody', {}, d.periods.map(p => h('tr', {}, h('td', { class: 'num' }, p.stat_date), d.pit ? h('td', { class: 'num' }, p.pub_date || '—') : h('td', {}), h('td', { class: 'num' }, big(p.revenue)), h('td', { class: 'num' }, big(p.net_profit)),
          d.pit ? h('td', { class: 'num' }, pct(p.roe)) : h('td', { class: 'num' }, p.eps_basic?.toFixed(2) ?? '—'), d.pit ? h('td', { class: 'num' }, pct(p.net_margin)) : null, d.pit ? h('td', { class: 'num' }, pct(p.debt_ratio)) : null))))));
    }
    box.append(h('p', { class: 'hint mt-s' }, d.note));
  }
}

/** 资金与情绪（3.20）：原始披露数据优先于第三方加工的「主力资金」。东财接口较慢（约 20 秒），按需加载。 */
function renderFlow(box, symbol) {
  const btn = h('button', { class: 'btn sm', onclick: load }, '加载资金数据');
  box.append(h('div', { class: 'row between' }, h('h3', {}, state.market === 'US' ? '做空与期权' : '资金（融资融券 / 龙虎榜 / 北向）'), btn));
  async function load() {
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> 加载中（约 20 秒）…';
    let d;
    try { d = await get('/flows', { symbol }, { timeout: 180000 }); } catch (e) { btn.disabled = false; btn.textContent = '重试'; box.append(h('div', { class: 'alert bad mt-s' }, e.message)); return; }
    clear(box);
    const money = v => v == null ? '—' : Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(2) + '亿' : Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + '万' : String(Math.round(v));
    const pct = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
    const kv = (k, v) => h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v num' }, String(v)));
    box.append(h('div', { class: 'row between' }, h('h3', {}, state.market === 'US' ? '做空与期权' : '资金（融资融券 / 龙虎榜 / 北向）'), h('span', { class: 'hint' }, `更新于 ${(d.fetched_at || '').replace('T', ' ')}`)));
    if (d.market === 'US') {
      const s = d.short || {}, o = d.options;
      box.append(h('div', { class: 'ind-grid mt-s' }, kv('空头股数', s.shares_short != null ? money(s.shares_short) : '—'), kv('占流通盘', pct(s.short_pct_float)), kv('回补天数', s.short_ratio_days ?? '—'),
        kv('机构持股', pct(s.held_pct_institutions)), kv('内部人持股', pct(s.held_pct_insiders)), kv('Put/Call 成交量比', o?.pc_volume_ratio != null ? o.pc_volume_ratio.toFixed(2) : '—'), kv('Put/Call 持仓量比', o?.pc_oi_ratio != null ? o.pc_oi_ratio.toFixed(2) : '—')));
      if (s.date_short_interest) box.append(h('p', { class: 'hint mt-s' }, `Short Interest 截至 ${s.date_short_interest}`));
    } else {
      if (d.failed?.length) box.append(h('div', { class: 'alert warn mt-s' }, `部分接口失败：${d.failed.join('、')}（东财数据中心繁忙，稍后重试）`));
      if (d.margin?.length) box.append(h('h3', { class: 'mt-s' }, '融资融券（最近 20 个交易日）'), h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['日期', '融资余额', '融资买入', '融资净买入', '融券余量', '融资占流通'].map((t, i) => h('th', { class: i ? 'r' : '' }, t)))),
        h('tbody', {}, d.margin.slice(0, 8).map(m => h('tr', {}, h('td', { class: 'num' }, m.date), h('td', { class: 'num' }, money(m.rz_balance)), h('td', { class: 'num' }, money(m.rz_buy)), h('td', { class: 'num' }, money(m.rz_net_buy)), h('td', { class: 'num' }, m.rq_volume ?? '—'), h('td', { class: 'num' }, m.rz_pct_float != null ? m.rz_pct_float.toFixed(2) + '%' : '—')))))));
      box.append(h('h3', { class: 'mt-s' }, '龙虎榜（近 120 天）'), d.billboard?.length ? h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['日期', '上榜原因', '净买入', '买入', '卖出'].map((t, i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
        h('tbody', {}, d.billboard.map(b => h('tr', {}, h('td', { class: 'num' }, b.date), h('td', { class: 'small' }, b.reason || '—'), h('td', { class: 'num ' + dirClass(b.net_buy) }, money(b.net_buy)), h('td', { class: 'num' }, money(b.buy)), h('td', { class: 'num' }, money(b.sell))))))) : h('div', { class: 'hint' }, '近 120 天未上榜'));
      if (d.north?.length) box.append(h('h3', { class: 'mt-s' }, '北向资金持股（季度披露）'), h('div', { class: 'grid c4' }, kv('最近披露', d.north[0].date), kv('持股市值', money(d.north[0].hold_value)), kv('占 A 股比例', d.north[0].ratio_a != null ? d.north[0].ratio_a + '%' : '—'), kv('持股数', money(d.north[0].hold_shares))));
    }
    box.append(h('p', { class: 'hint mt-s' }, (d.notes || []).join(' ')));
  }
}
