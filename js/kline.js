// 「K线」页：指数日 K 线（均线 + 成交额）；A 股指数（上证50 / 沪深300 / 创业板指）右侧带筹码分布（估算，见 server/chips.py）。
// 美股（标普500 / 纳斯达克100）只有 K 线。鼠标指到哪天，顶部行情和右侧筹码分布就显示那天的。展示用，不进入选股与回测。
import { get, state } from './api.js';
import { prefs } from './db.js';
import { h, clear, fmtNum, fmtPct, fmtAmount, isNum } from './util.js';
import { themeColors, makeChart, disposeChart, onThemeChange } from './chart.js';

const RANGES = [['3 个月', 63], ['半年', 126], ['1 年', 250], ['2 年', 500], ['全部', 0]];
const MA_COLORS = { 5: '#f59e0b', 20: '#2563eb', 60: '#8b5cf6', 200: '#0ea5a4', 250: '#e11d48' };      // MA250 = 关键线：红色、画粗
const wide = () => window.innerWidth >= 900;
const maColor = k => MA_COLORS[k] || '#64748b';
/** 标签文字用黑还是白：按底色亮度挑，保证青色 / 橙色这类浅色底上也看得清 */
function ink(hex) {
  const [r, g, b] = [1, 3, 5].map(i => { const v = parseInt(hex.slice(i, i + 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.25 ? '#0b1220' : '#ffffff';
}

export const klineView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el);
    const key = prefs.get('klineIdx.' + state.market, null);
    const page = h('div', { class: 'pane-body page' });
    el.append(page);
    this._page = page;
    await this.load(key);
  },

  async load(key) {
    const page = this._page; this._dispose();
    const mk = state.market;
    clear(page).append(h('div', { class: 'row between mb' }, h('h1', {}, 'K线'), h('span', { class: 'hint', id: 'kl-asof' })),
      h('div', { class: 'card' }, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 加载 K 线…（第一次打开筹码分布要算几秒）')));
    let d;
    try { d = await get('/index/kline', { index: key || undefined }, { cache: false, timeout: 120000 }); } catch (e) { d = { status: 'error', message: e.message, indices: [] }; }
    if (!document.body.contains(page) || mk !== state.market) return;
    this._build(d);
  },

  _dispose() { this._off?.(); this._off = null; this._ro?.disconnect(); this._ro = null; if (this._chart) { disposeChart(this._chart); this._chart = null; } },
  cleanup() { this._dispose(); },

  _build(d) {
    const page = this._page;
    clear(page);
    const asof = h('span', { class: 'hint' }, d.data_asof ? `数据截止 ${d.data_asof}（盘后日线）` : '');
    page.append(h('div', { class: 'row between mb' }, h('h1', {}, 'K线'), asof));
    const idxSeg = h('div', { class: 'seg' }, (d.indices || []).map(i => h('button', {
      class: i.key === d.index?.key ? 'on' : '', disabled: i.ok ? null : true, title: i.ok ? '' : '这个指数还没有日线数据',
      onclick: () => { prefs.set('klineIdx.' + state.market, i.key); this.load(i.key); } }, i.name)));
    const card = h('div', { class: 'card kl' });
    page.append(card);
    if (d.status !== 'ok') {
      card.append(h('div', { class: 'row between mb' }, idxSeg), h('div', { class: 'empty' }, h('div', { class: 'big' }, '暂无 K 线数据'), d.message || '请先到设置页初始化数据。'));
      return;
    }
    const chips = d.chips;
    const n = d.dates.length;
    const showChips = !!chips && prefs.get('klineChips', wide());
    const rangeSeg = h('div', { class: 'seg' }, RANGES.map(([lab, m]) => h('button', { class: m === 126 ? 'on' : '', onclick: e => { [...rangeSeg.children].forEach(b => b.classList.toggle('on', b === e.target)); setRange(m); } }, lab)));
    const chipBtn = chips ? h('button', { class: 'btn sm', onclick: () => { prefs.set('klineChips', !showChips); this._build(d); } }, showChips ? '隐藏筹码分布' : '显示筹码分布') : null;
    const quote = h('div', { class: 'kl-quote' });
    const maLine = h('div', { class: 'kl-ma' });
    const mainEl = h('div', { class: 'kl-main' });
    const chipEl = showChips ? h('div', { class: 'kl-chips' }, h('canvas')) : null;
    const stats = chips ? h('div', { class: 'kl-stats' }) : null;
    card.append(h('div', { class: 'row between wrap kl-tools' }, idxSeg, h('div', { class: 'row wrap' }, rangeSeg, chipBtn)),
      quote, maLine, h('div', { class: 'kl-grid' }, mainEl, chipEl), stats,
      h('p', { class: 'hint mt-s' }, chips ? chips.note : (state.market === 'US' ? '美股日线没有换手率，所以没有筹码分布。' : '')),
      h('p', { class: 'hint' }, `均线：MA5 / MA20 / MA60 / MA200 / MA250（线的右端有标注，红色粗线 MA250 是关键线）；底部柱子是成交额。指数用日线，数据来自本地库，盘后更新。`));

    const c = themeColors();
    const chart = makeChart(mainEl, {
      height: mainEl.clientHeight || 430, width: mainEl.clientWidth,
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.text, fontSize: 11, fontFamily: 'ui-sans-serif, system-ui, "Microsoft YaHei"' },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.grid, minimumWidth: 64 }, timeScale: { borderColor: c.grid, rightOffset: 3 },
      crosshair: { mode: 0 }, localization: { locale: 'zh-CN' },
    });
    this._chart = chart;
    const dt = d.dates;
    const candle = chart.addCandlestickSeries({ upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down, priceLineVisible: false });
    candle.setData(dt.map((t, i) => ({ time: t, open: d.candles[i][0], high: d.candles[i][1], low: d.candles[i][2], close: d.candles[i][3] })));
    candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.24 } });
    const vol = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', priceLineVisible: false, lastValueVisible: false });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    const volData = (t = themeColors()) => dt.map((x, i) => ({ time: x, value: d.amount[i] ?? 0, color: (d.candles[i][3] >= d.candles[i][0] ? t.up : t.down) + '66' }));
    vol.setData(volData());
    const maSeries = {};
    const tagsEl = h('div', { class: 'kl-tags' });
    mainEl.append(tagsEl);
    for (const [k, vals] of Object.entries(d.ma)) {
      const s = chart.addLineSeries({ color: maColor(k), lineWidth: k === '250' ? 2 : 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      s.setData(dt.map((x, i) => (vals[i] == null ? { time: x } : { time: x, value: vals[i] })));
      maSeries[k] = s;
    }
    this._off = onThemeChange(t => {
      candle.applyOptions({ upColor: t.up, downColor: t.down, borderUpColor: t.up, borderDownColor: t.down, wickUpColor: t.up, wickDownColor: t.down });
      vol.setData(volData(t)); draw();
    });

    // ---------- 筹码分布（画在 K 线右侧的 canvas 上，纵轴和 K 线的价格刻度对齐） ----------
    const idxOf = new Map(dt.map((t, i) => [t, i]));
    const chipIdx = chips ? new Map(chips.dates.map((t, i) => [t, i])) : null;
    let cur = n - 1, raf = 0;
    // 均线右端的标注（MA5 / MA20 …）：贴在最新一根 K 线的右边，上下靠得太近时错开
    function placeTags() {
      const x0 = chart.timeScale().logicalToCoordinate(n - 1), plotW = mainEl.clientWidth - 64;
      if (x0 == null || x0 > plotW - 30) { tagsEl.replaceChildren(); return; }
      const items = Object.entries(d.ma).map(([k, vals]) => ({ k, y: vals[n - 1] == null ? null : maSeries[k].priceToCoordinate(vals[n - 1]) })).filter(o => o.y != null).sort((a, b) => a.y - b.y);
      let prev = -99;
      for (const o of items) { o.ty = Math.max(o.y, prev + 15); prev = o.ty; }
      tagsEl.replaceChildren(...items.map(o => h('span', { class: 'kl-tag', style: `left:${x0 + 7}px;top:${o.ty - 7}px;background:${maColor(o.k)};color:${ink(maColor(o.k))}` }, 'MA' + o.k)));
    }
    const draw = () => {
      raf = 0;
      placeTags();
      if (!chipEl) return;
      const cv = chipEl.firstChild, W = chipEl.clientWidth, Hh = chipEl.clientHeight, dpr = window.devicePixelRatio || 1;
      if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(Hh * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(Hh * dpr); cv.style.width = W + 'px'; cv.style.height = Hh + 'px'; }
      const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, Hh);
      const t = themeColors();
      const j = chipIdx.get(dt[cur]);
      if (j == null) { g.fillStyle = t.text; g.font = '12px ui-sans-serif, system-ui'; g.fillText('更早的日期没有筹码分布', 10, 24); return; }
      const dist = chips.dist[j], close = d.candles[cur][3], half = chips.step / 2;
      const mx = Math.max(...dist) || 1, maxW = W - 14;
      for (let k = 0; k < dist.length; k++) {
        if (!dist[k]) continue;
        const y1 = candle.priceToCoordinate(chips.bins[k] + half), y2 = candle.priceToCoordinate(chips.bins[k] - half);
        if (y1 == null || y2 == null || y2 < 0 || y1 > Hh) continue;
        g.globalAlpha = chips.bins[k] <= close ? 0.9 : 0.5;
        g.fillStyle = chips.bins[k] <= close ? t.accent : t.text;
        g.fillRect(0, y1, Math.max(1, (dist[k] / mx) * maxW), Math.max(1, y2 - y1 - 0.4));
      }
      g.globalAlpha = 1;
      const yc = candle.priceToCoordinate(close);
      if (yc != null) { g.strokeStyle = t.warn; g.lineWidth = 1; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(0, yc + .5); g.lineTo(W, yc + .5); g.stroke(); g.setLineDash([]); }
    };
    const redraw = () => { if (!raf) raf = requestAnimationFrame(draw); };

    // ---------- 顶部行情 / 均线 / 筹码统计：跟随十字光标 ----------
    const cls = v => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat');
    const show = i => {
      cur = i;
      const k = d.candles[i], prev = d.candles[i - 1]?.[3];
      const chg = prev ? k[3] / prev - 1 : null;
      quote.replaceChildren(h('b', { class: 'kl-name' }, d.index.name), h('span', { class: 'faint small' }, ' ' + (d.index.symbol || '').replace(/^\w+\./, '')),
        h('span', { class: 'kl-px num ' + cls(chg) }, fmtNum(k[3], 2)),
        h('span', { class: 'num ' + cls(chg) }, chg == null ? '' : `${chg > 0 ? '+' : ''}${fmtNum(k[3] - prev, 2)}  ${fmtPct(chg)}`),
        h('span', { class: 'kl-sep' }), h('span', { class: 'muted small' }, `${dt[i]}`),
        h('span', { class: 'small' }, '开 ', h('b', { class: 'num' }, fmtNum(k[0], 2))), h('span', { class: 'small' }, '高 ', h('b', { class: 'num up' }, fmtNum(k[1], 2))),
        h('span', { class: 'small' }, '低 ', h('b', { class: 'num down' }, fmtNum(k[2], 2))), h('span', { class: 'small' }, '额 ', h('b', { class: 'num' }, fmtAmount(d.amount[i]))));
      maLine.replaceChildren(...Object.keys(d.ma).map(m => h('span', { style: `color:${maColor(m)}` }, `MA${m} `, h('span', { class: 'num' }, isNum(d.ma[m][i]) ? fmtNum(d.ma[m][i], 2) : '—'))));
      if (stats) {
        const j = chipIdx.get(dt[i]), S = chips.stats;
        const conc = (lo, hi) => (lo + hi > 0 ? fmtPct((hi - lo) / (hi + lo), 2, false) : '—');
        stats.replaceChildren(...(j == null ? [h('span', { class: 'hint' }, '这一天更早，没有筹码分布。')] : [
          kv('收盘获利比例', fmtPct(S.profit[j], 2, false), '收盘价以下的筹码占全部筹码的比例'),
          kv('平均成本', fmtNum(S.avg[j], 2), '全部筹码的平均价格'),
          kv('90% 筹码区间', `${fmtNum(S.lo90[j], 2)} – ${fmtNum(S.hi90[j], 2)}`, '90% 的筹码集中在这个价格区间'),
          kv('90% 集中度', conc(S.lo90[j], S.hi90[j]), '(区间上沿 − 下沿) ÷ (上沿 + 下沿)，越小越集中'),
          kv('70% 筹码区间', `${fmtNum(S.lo70[j], 2)} – ${fmtNum(S.hi70[j], 2)}`, '70% 的筹码集中在这个价格区间'),
          kv('70% 集中度', conc(S.lo70[j], S.hi70[j]), '越小越集中')]));
      }
      redraw();
    };
    const kv = (k, v, tip) => h('div', { class: 'kv', title: tip }, h('span', { class: 'k' }, k), h('span', { class: 'v num' }, v));
    chart.subscribeCrosshairMove(p => { const i = p && p.time != null ? idxOf.get(p.time) : null; show(i == null ? n - 1 : i); });
    chart.timeScale().subscribeVisibleLogicalRangeChange(redraw);
    const setRange = m => { chart.timeScale().setVisibleLogicalRange({ from: m ? Math.max(0, n - m) : 0, to: n + 9 }); redraw(); };
    this._ro = new ResizeObserver(() => { chart.applyOptions({ width: mainEl.clientWidth, height: mainEl.clientHeight }); redraw(); });
    this._ro.observe(mainEl);
    setRange(126); show(n - 1);
    setTimeout(redraw, 120);                              // 价格刻度在第一帧之后才定下来，补画一次
  },
};
