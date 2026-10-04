// lightweight-charts（TradingView 开源，v4.2.3，本地 vendor/ 引用，非 CDN）的薄封装（1.3.2）。
// 只做「数据格式适配 + 主题（涨跌配色、深浅色）+ 叠加物（均线、触发价、止损线、成本线、信号 / 买卖点标记）」，不自己实现 K 线渲染。
// v4 不支持多面板，副图（MACD / RSI）用第二个与主图时间轴同步的图表实例实现（1.3.2 版本与副图）。
import { esc, fmtPrice, fmtSigned } from './util.js';

const LWC = () => window.LightweightCharts;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export function themeColors() {
  return { up: css('--up'), down: css('--down'), text: css('--muted'), grid: css('--line'), bg: css('--bg'), accent: css('--accent'),
    warn: css('--warn'), flat: css('--flat') };
}

function baseOptions(c, height) {
  return {
    height,
    layout: { background: { type: 'solid', color: c.bg }, textColor: c.text, fontFamily: 'ui-sans-serif, system-ui, "Microsoft YaHei"', fontSize: 11 },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.grid },
    timeScale: { borderColor: c.grid, rightOffset: 4, timeVisible: false },
    crosshair: { mode: 0 },     // 0 = Normal：十字光标精确读数（4.7）
    localization: { locale: 'zh-CN' },
  };
}

const MA_COLORS = { ma5: '#f59e0b', ma10: '#8b5cf6', ma20: '#2563eb', ma50: '#0ea5a4', ma200: '#9ca3af' };

export class StockChart {
  constructor(mainEl, subEl) {
    this.mainEl = mainEl; this.subEl = subEl;
    this.main = null; this.sub = null; this.priceLines = []; this.maSeries = {}; this.subSeries = [];
    this.legendEl = null; this.subKind = 'macd'; this.payload = null; this._ro = null; this._syncing = false;
  }

  build() {
    const c = themeColors();
    this.destroy();
    const L = LWC();
    this.main = L.createChart(this.mainEl, { ...baseOptions(c, this.mainEl.clientHeight || 400), width: this.mainEl.clientWidth });
    this.candle = this.main.addCandlestickSeries({
      upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down,
      priceLineVisible: false,
    });
    this.candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.24 } });
    this.vol = this.main.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', priceLineVisible: false, lastValueVisible: false });
    this.main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    this.legendEl = document.createElement('div');
    this.legendEl.className = 'chart-legend';
    this.mainEl.parentElement.append(this.legendEl);
    this.main.subscribeCrosshairMove(p => this._legend(p));
    if (this.subEl) {
      this.sub = L.createChart(this.subEl, { ...baseOptions(c, this.subEl.clientHeight || 130), width: this.subEl.clientWidth });
      this.main.timeScale().subscribeVisibleLogicalRangeChange(r => this._sync(this.main, this.sub, r));
      this.sub.timeScale().subscribeVisibleLogicalRangeChange(r => this._sync(this.sub, this.main, r));
    }
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(this.mainEl);
  }

  _sync(from, to, r) {
    if (this._syncing || !r) return;
    this._syncing = true;
    try { to.timeScale().setVisibleLogicalRange(r); } finally { this._syncing = false; }
  }

  resize() {
    if (this.main) this.main.applyOptions({ width: this.mainEl.clientWidth, height: this.mainEl.clientHeight });
    if (this.sub) this.sub.applyOptions({ width: this.subEl.clientWidth, height: this.subEl.clientHeight });
  }

  _legend(p) {
    if (!this.legendEl || !this.payload) return;
    const bar = p && p.seriesData && p.seriesData.get(this.candle);
    const bars = this.payload.bars;
    const cur = bar ? bar : bars[bars.length - 1];
    const idx = bar ? bars.findIndex(b => b.time === p.time) : bars.length - 1;
    const prev = idx > 0 ? bars[idx - 1].close : null;
    const chg = prev ? cur.close / prev - 1 : null;
    const ma = Object.entries(this.maSeries).map(([k, s]) => {
      const v = p && p.seriesData ? p.seriesData.get(s) : null;
      return v ? `<span style="color:${MA_COLORS[k]}">${k.toUpperCase()} ${fmtPrice(v.value)}</span>` : '';
    }).join('');
    this.legendEl.innerHTML = `<span>${esc(p && p.time ? p.time : bars[bars.length - 1]?.time || '')}</span>` +
      `<span>开 ${fmtPrice(cur.open)} 高 ${fmtPrice(cur.high)} 低 ${fmtPrice(cur.low)} 收 ${fmtPrice(cur.close)}</span>` +
      (chg != null ? `<span>${chg > 0 ? '+' : ''}${(chg * 100).toFixed(2)}%</span>` : '') + ma;
  }

  setData(payload, opts = {}) {
    this.payload = payload;
    const c = themeColors();
    if (!this.main) this.build();
    const bars = payload.bars;
    this.candle.setData(bars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
    this.vol.setData(bars.map(b => ({ time: b.time, value: b.volume ?? 0, color: (b.close >= b.open ? c.up : c.down) + '66' })));
    // 均线
    for (const s of Object.values(this.maSeries)) this.main.removeSeries(s);
    this.maSeries = {};
    for (const [k, data] of Object.entries(payload.ma || {})) {
      if (opts.hideMa?.includes(k)) continue;
      const s = this.main.addLineSeries({ color: MA_COLORS[k] || c.accent, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      s.setData(data);
      this.maSeries[k] = s;
    }
    this.setOverlays(payload);
    this.setSub(this.subKind);
    this.main.timeScale().fitContent();
    const n = bars.length;
    if (n > 140) this.main.timeScale().setVisibleLogicalRange({ from: n - 140, to: n + 4 });
    this._legend(null);
  }

  /** 叠加物：触发价 / 止损位（价格线）、持仓成本与当前移动止损线、信号日与买卖点标记、财报日 */
  setOverlays(payload) {
    const c = themeColors();
    for (const pl of this.priceLines) this.candle.removePriceLine(pl);
    this.priceLines = [];
    const add = (price, color, title, dash = 2) => {
      if (price == null) return;
      this.priceLines.push(this.candle.createPriceLine({ price, color, lineWidth: 1, lineStyle: dash, axisLabelVisible: true, title }));
    };
    const lv = payload.levels || {};
    if (lv.scan) { add(lv.scan.trigger_price, c.accent, '触发价'); add(lv.scan.stop_price, c.down === c.up ? c.flat : '#e11d48', '止损位'); }
    if (lv.position) { add(lv.position.avg_cost, c.warn, '成本', 0); add(lv.position.current_stop, '#e11d48', '移动止损', 1); }
    const times = new Set(payload.bars.map(b => b.time));
    const nearest = d => {                       // 标记日期不在 K 线上（如停牌）时取其后第一根
      if (times.has(d)) return d;
      return payload.bars.find(b => b.time > d)?.time || null;
    };
    const mk = [];
    for (const s of payload.signals || []) { const t = nearest(s.scan_date); if (t) mk.push({ time: t, position: 'belowBar', color: c.accent, shape: 'arrowUp', text: '信号' }); }
    for (const t of payload.trades || []) {
      const tm = nearest(t.date); if (!tm) continue;
      mk.push(t.side === 'buy'
        ? { time: tm, position: 'belowBar', color: '#2563eb', shape: 'arrowUp', text: '买 ' + fmtPrice(t.price) }
        : { time: tm, position: 'aboveBar', color: '#f59e0b', shape: 'arrowDown', text: '卖 ' + fmtPrice(t.price) + (t.exit_reason ? ' ' + t.exit_reason : '') });
    }
    for (const d of payload.earnings || []) { const t = nearest(d); if (t) mk.push({ time: t, position: 'aboveBar', color: c.flat, shape: 'circle', text: '财报' }); }
    mk.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
    this.candle.setMarkers(mk);
  }

  setSub(kind) {
    this.subKind = kind;
    if (!this.sub) return;
    const c = themeColors(), p = this.payload;
    for (const s of this.subSeries) this.sub.removeSeries(s);
    this.subSeries = [];
    if (kind === 'macd' && p.macd) {
      const h = this.sub.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
      h.setData(p.macd.hist.map(x => ({ time: x.time, value: x.value, color: (x.value >= 0 ? c.up : c.down) + 'aa' })));
      const dif = this.sub.addLineSeries({ color: '#2563eb', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      const dea = this.sub.addLineSeries({ color: '#f59e0b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      dif.setData(p.macd.dif); dea.setData(p.macd.dea);
      this.subSeries.push(h, dif, dea);
    } else if (kind === 'rsi' && p.rsi) {
      const r = this.sub.addLineSeries({ color: '#8b5cf6', lineWidth: 1, priceLineVisible: false });
      r.setData(p.rsi);
      r.createPriceLine({ price: 70, color: c.grid, lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
      r.createPriceLine({ price: 30, color: c.grid, lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
      this.subSeries.push(r);
    }
    const r = this.main.timeScale().getVisibleLogicalRange();
    if (r) this.sub.timeScale().setVisibleLogicalRange(r);
  }

  destroy() {
    this._ro?.disconnect();
    if (this.main) { this.main.remove(); this.main = null; }
    if (this.sub) { this.sub.remove(); this.sub = null; }
    this.legendEl?.remove(); this.legendEl = null;
    this.priceLines = []; this.maSeries = {}; this.subSeries = [];
  }
}

/** 通用折线图（回测权益曲线 / 回撤 / 基准对比）。series: [{name, color, data:[{time,value}], area?:bool}] */
export function lineChart(el, series, { height = 260, percent = false } = {}) {
  const c = themeColors();
  const L = LWC();
  const chart = L.createChart(el, { ...baseOptions(c, height), width: el.clientWidth, rightPriceScale: { borderColor: c.grid } });
  for (const s of series) {
    const ser = s.area
      ? chart.addAreaSeries({ lineColor: s.color, topColor: s.color + '08', bottomColor: s.color + '55', lineWidth: 1, priceLineVisible: false,
        invertFilledArea: true,            // 回撤 <= 0：填充线与 0 轴之间的区域
        priceFormat: percent ? { type: 'custom', formatter: v => (v * 100).toFixed(1) + '%' } : undefined })
      : chart.addLineSeries({ color: s.color, lineWidth: 2, priceLineVisible: false, title: s.name,
        priceFormat: percent ? undefined : { type: 'custom', formatter: v => Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + '万' : v.toFixed(2) } });
    ser.setData(s.data);
  }
  chart.timeScale().fitContent();
  const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
  ro.observe(el);
  return { chart, destroy() { ro.disconnect(); chart.remove(); } };
}
