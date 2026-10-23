// 行情页顶部的合成图：指数日 K 线 + 市场温度 + 恐慌指数，三张图上下排列、共用一条时间轴（数据由 /api/market/combo 按交易日对齐）。
// lightweight-charts v4 不支持多面板：三个图表实例按「逻辑位置」同步缩放 / 拖动，十字光标用 setCrosshairPosition 联动。
import { get } from './api.js';
import { h, fmtNum } from './util.js';
import { themeColors } from './chart.js';

export async function loadCombo() {
  return get('/market/combo', {}, { cache: false, timeout: 120000 });
}

const PANIC_COLORS = ['#e24b4a', '#f0803c'];
const TEMP_COLOR = '#8b5cf6';
const RANGES = [['3 个月', 63], ['半年', 126], ['1 年', 250], ['3 年', 750], ['全部', 0]];

/** 返回 {el, init, destroy}。 */
export function comboCard(d) {
  const out = h('div', { class: 'card combo' });
  const title = d?.index ? `${d.index.name} · 市场温度 · 恐慌指数` : 'K 线 · 市场温度 · 恐慌指数';
  const btns = h('div', { class: 'seg' });
  out.append(h('div', { class: 'card-title' }, h('h3', {}, title), btns));
  if (!d || d.status !== 'ok') { out.append(h('div', { class: 'hint' }, d?.message || '暂不可用')); return { el: out, init() {}, destroy() {} }; }
  const legends = [h('div', { class: 'combo-legend' }), h('div', { class: 'combo-legend' }), h('div', { class: 'combo-legend' })];
  const boxes = [h('div', { class: 'combo-pane' }), h('div', { class: 'combo-pane' }), h('div', { class: 'combo-pane' })];
  const panes = boxes.map((b, i) => h('div', { class: 'combo-wrap' }, legends[i], b));
  out.append(...panes);
  const hasPanic = d.panic.some(p => p.values.some(v => v != null));
  if (!hasPanic) legends[2].textContent = '恐慌指数：数据还在下载（第一次使用约需几分钟），稍后刷新页面即可看到。';
  out.append(h('p', { class: 'hint mt-s' }, d.note));
  const charts = [];
  let ro = null;
  const n = d.dates.length;

  function init() {
    const L = window.LightweightCharts;
    const c = themeColors();
    const base = (height, showTime) => ({
      height, width: boxes[0].clientWidth,
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.text, fontSize: 11, fontFamily: 'ui-sans-serif, system-ui, "Microsoft YaHei"' },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.grid, minimumWidth: 72 },            // 三张图右侧刻度等宽，时间轴才能上下对齐
      timeScale: { borderColor: c.grid, visible: showTime, rightOffset: 3 },
      crosshair: { mode: 0 }, localization: { locale: 'zh-CN' },
      handleScale: { axisPressedMouseMove: { time: true, price: false } },
    });
    const mobile = window.innerWidth < 640;
    const ch = [L.createChart(boxes[0], base(mobile ? 220 : 300, false)), L.createChart(boxes[1], base(mobile ? 110 : 130, false)), L.createChart(boxes[2], base(mobile ? 120 : 150, true))];
    charts.push(...ch);
    const dt = d.dates;
    const k = ch[0].addCandlestickSeries({ upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down, priceLineVisible: false });
    k.setData(dt.map((t, i) => (d.candles[i][3] == null ? { time: t } : { time: t, open: d.candles[i][0], high: d.candles[i][1], low: d.candles[i][2], close: d.candles[i][3] })));
    const line = (chart, vals, color, title) => {
      const s = chart.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title });
      s.setData(dt.map((t, i) => (vals[i] == null ? { time: t } : { time: t, value: vals[i] })));
      return s;
    };
    const temp = line(ch[1], d.temp, TEMP_COLOR, '');
    temp.applyOptions({ priceFormat: { type: 'custom', formatter: v => v.toFixed(0) + '%' } });
    for (const z of d.temp_lines) temp.createPriceLine({ price: z.v, color: z.v < 50 ? '#2f7ed8' : '#e24b4a', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: z.label });
    const panic = d.panic.map((p, i) => line(ch[2], p.values, PANIC_COLORS[i % 2], ''));
    if (panic[0]) for (const z of d.panic[0].lines || []) panic[0].createPriceLine({ price: z.v, color: z.label.includes('恐慌') ? '#e24b4a' : '#e6a23c', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: z.label });

    // 同步缩放 / 拖动（三张图的数据点一一对应，按逻辑位置同步即可）
    let syncing = false;
    ch.forEach((a, i) => a.timeScale().subscribeVisibleLogicalRangeChange(r => {
      if (syncing || !r) return;
      syncing = true;
      ch.forEach((b, j) => { if (j !== i) b.timeScale().setVisibleLogicalRange(r); });
      syncing = false;
    }));
    // 十字光标联动 + 图例
    const val = (arr, i) => (i == null || i < 0 || i >= n ? null : arr[i]);
    const show = i => {
      const cd = val(d.candles, i);
      const prev = val(d.candles, i - 1);
      const chg = cd && prev && prev[3] ? cd[3] / prev[3] - 1 : null;
      legends[0].innerHTML = '';
      legends[0].append(h('b', {}, d.index.name), ` ${dt[i] || ''}  `,
        cd ? h('span', { class: 'num' }, `开 ${fmtNum(cd[0], 2)} 高 ${fmtNum(cd[1], 2)} 低 ${fmtNum(cd[2], 2)} 收 ${fmtNum(cd[3], 2)}`) : '',
        chg != null ? h('span', { class: 'num ' + (chg >= 0 ? 'up' : 'down') }, `  ${chg >= 0 ? '+' : ''}${(chg * 100).toFixed(2)}%`) : '');
      const tv = val(d.temp, i);
      legends[1].innerHTML = '';
      legends[1].append(h('b', { style: `color:${TEMP_COLOR}` }, '市场温度'), ' ', h('span', { class: 'num' }, tv == null ? '—' : `${tv.toFixed(0)}%`),
        h('span', { class: 'tiny muted' }, '（站上 20 日线的股票比例；虚线 = 冰点 / 过热）'));
      if (hasPanic) {
        legends[2].innerHTML = '';
        d.panic.forEach((p, j) => {
          const v = val(p.values, i);
          legends[2].append(h('b', { style: `color:${PANIC_COLORS[j % 2]}` }, p.name), ' ', h('span', { class: 'num' }, v == null ? '—' : fmtNum(v, 2)), '   ');
        });
        legends[2].append(h('span', { class: 'tiny muted' }, '（期权预期的年化波动率 %；虚线 = 略紧张 / 恐慌）'));
      }
    };
    show(n - 1);
    const series = [k, temp, panic[0]];
    const valueAt = (j, i) => (j === 0 ? val(d.candles, i)?.[3] : j === 1 ? val(d.temp, i) : val(d.panic[0]?.values || [], i));
    ch.forEach((a, j) => a.subscribeCrosshairMove(p => {
      if (syncing) return;
      if (!p || p.time == null || p.logical == null) {
        syncing = true; ch.forEach((b, x) => { if (x !== j) b.clearCrosshairPosition(); }); syncing = false;
        show(n - 1); return;
      }
      const i = Math.round(p.logical);
      syncing = true;
      ch.forEach((b, x) => {
        if (x === j || !series[x]) return;
        const v = valueAt(x, i);
        if (v == null) b.clearCrosshairPosition(); else b.setCrosshairPosition(v, p.time, series[x]);
      });
      syncing = false;
      show(i);
    }));
    // 时间范围按钮
    const setRange = m => ch[0].timeScale().setVisibleLogicalRange({ from: m ? Math.max(0, n - m) : 0, to: n + 2 });
    btns.replaceChildren(...RANGES.map(([lab, m]) => h('button', { class: m === 250 ? 'on' : '', onclick: e => {
      [...btns.children].forEach(b => b.classList.toggle('on', b === e.target)); setRange(m);
    } }, lab)));
    setRange(250);
    ro = new ResizeObserver(() => { const w = boxes[0].clientWidth; ch.forEach(x => x.applyOptions({ width: w })); });
    ro.observe(boxes[0]);
  }
  return { el: out, init, destroy() { ro?.disconnect(); for (const x of charts.splice(0)) x.remove(); } };
}
