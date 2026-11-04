// K 线上的临时画线：两点线段，起点 / 终点各标出价格（纵坐标的值），可选颜色、可画多条。
// 只存在内存里（模块级 Map）：切换股票、切换日 / 周线时还在，刷新页面或重启软件就没了——这是故意的，不写进数据库。
// lightweight-charts 没有画线接口：这里在图上盖一张 canvas，用图表自己的「时间 ↔ 横坐标」「价格 ↔ 纵坐标」换算来画，
// 所以缩放、拖动、价格刻度变化后线都会跟着走（用 requestAnimationFrame 比较一个坐标签名，变了才重画）。
import { h, fmtPrice, fmtPct, dirClass } from './util.js';
import { prefs } from './db.js';

export const PALETTE = ['#f59e0b', '#2563eb', '#8b5cf6', '#0ea5a4', '#e11d48', '#64748b'];
const store = new Map();                         // key -> [{ id, color, a: { t, p }, b: { t, p } }]，t = 日期字符串，p = 价格
let uid = 0;

/** 标签文字用黑还是白：按底色亮度挑，浅色底上也看得清 */
function ink(hex) {
  const [r, g, b] = [1, 3, 5].map(i => { const v = parseInt(hex.slice(i, i + 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.25 ? '#0b1220' : '#ffffff';
}

/**
 * 给一张 K 线图加画线功能。
 *  mainEl     图表所在的容器（画布盖在它上面，需要是定位元素）
 *  chart / series  取得当前图表 / K 线系列的函数（主题切换会重建图表，所以每次现取）
 *  dates      每根 K 线的日期（和图表数据同序）；bars：[[开, 高, 低, 收], …]，用于「吸附」到最近的价格
 *  key        这张图的画线存放位置，例如 CN|sh.600519|D
 * 返回 { toolbar, legend, destroy }：toolbar 放在图上方的工具条里，legend 放在图下面（线的清单）。
 */
export function createDrawing({ mainEl, chart, series, dates, bars, key }) {
  if (!store.has(key)) store.set(key, []);
  const lines = store.get(key);
  const idxOf = new Map(dates.map((t, i) => [t, i]));
  const n = dates.length;
  const S = { on: false, color: prefs.get('drawColor', PALETTE[0]), magnet: prefs.get('drawMagnet', true), start: null, hover: null, down: null };
  let raf = 0, sig = '', dead = false, lastChart = null;

  const cv = h('canvas', { class: 'draw-cv' });
  mainEl.append(cv);

  // ---------- 坐标换算 ----------
  const plot = () => {
    const c = chart();
    const W = mainEl.clientWidth, Hh = mainEl.clientHeight;
    let aw = 64, th = 26;
    try { aw = c.priceScale('right').width() || aw; th = c.timeScale().height() || th; } catch { /* 图表正在重建 */ }
    return { W, H: Hh, w: W - aw, h: Hh - th };
  };
  const xOf = i => chart().timeScale().logicalToCoordinate(i);
  const yOf = p => series().priceToCoordinate(p);
  /** 鼠标位置 -> 最近的一根 K 线 + 价格（可吸附到该 K 线的开 / 高 / 低 / 收） */
  function pick(px, py) {
    const lg = chart().timeScale().coordinateToLogical(px);
    if (lg == null || !n) return null;
    const i = Math.min(n - 1, Math.max(0, Math.round(lg)));
    let p = series().coordinateToPrice(py);
    if (p == null) return null;
    if (S.magnet && bars[i]) {
      let best = null, bd = 12;                                  // 离某个价格不到 12 像素就吸过去
      for (const v of bars[i]) { const y = yOf(v); if (y != null && Math.abs(y - py) < bd) { bd = Math.abs(y - py); best = v; } }
      if (best != null) p = best;
    }
    return { t: dates[i], p };
  }

  // ---------- 绘制 ----------
  function paint() {
    const g = cv.getContext('2d'), dpr = window.devicePixelRatio || 1, { W, H, w, h: ph } = plot();
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); cv.style.width = W + 'px'; cv.style.height = H + 'px'; }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    g.save(); g.beginPath(); g.rect(0, 0, w, ph); g.clip();
    g.font = '600 11px ui-sans-serif, system-ui, "Microsoft YaHei"'; g.textBaseline = 'middle';
    const pts = o => { const i = idxOf.get(o.t); if (i == null) return null; const x = xOf(i), y = yOf(o.p); return x == null || y == null ? null : { x, y }; };
    const label = (pt, price, color, side) => {
      const txt = fmtPrice(price), tw = g.measureText(txt).width + 10, hh = 16;
      let x = side === 'left' ? pt.x - 8 - tw : pt.x + 8;
      x = Math.max(2, Math.min(w - tw - 2, x));
      const y = Math.max(hh / 2 + 1, Math.min(ph - hh / 2 - 1, pt.y));
      g.fillStyle = color; g.beginPath(); g.roundRect(x, y - hh / 2, tw, hh, 4); g.fill();
      g.fillStyle = ink(color); g.fillText(txt, x + 5, y + 0.5);
    };
    const dot = (pt, color) => { g.fillStyle = color; g.beginPath(); g.arc(pt.x, pt.y, 3.5, 0, 7); g.fill(); g.strokeStyle = '#ffffff'; g.lineWidth = 1.2; g.stroke(); };
    for (const L of lines) {
      const a = pts(L.a), b = pts(L.b);
      if (!a || !b) continue;
      g.strokeStyle = L.color; g.lineWidth = 2; g.lineCap = 'round'; g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); g.stroke();
      dot(a, L.color); dot(b, L.color);
      const aLeft = a.x <= b.x;
      label(a, L.a.p, L.color, aLeft ? 'left' : 'right'); label(b, L.b.p, L.color, aLeft ? 'right' : 'left');
    }
    if (S.on && S.start) {                                          // 画到一半：起点到鼠标的虚线
      const a = pts(S.start), b = S.hover && pts(S.hover);
      g.strokeStyle = S.color; g.lineWidth = 2; g.setLineDash([6, 4]);
      if (a && b) { g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); g.stroke(); }
      g.setLineDash([]);
      if (a) { dot(a, S.color); label(a, S.start.p, S.color, !b || a.x <= b.x ? 'left' : 'right'); }
      if (b) { dot(b, S.color); label(b, S.hover.p, S.color, !a || a.x <= b.x ? 'right' : 'left'); }
    } else if (S.on && S.hover) {                                   // 还没点起点：鼠标处的小圆点和当前价格
      const b = pts(S.hover);
      if (b) { dot(b, S.color); label(b, S.hover.p, S.color, 'right'); }
    }
    g.restore();
  }
  /** 每一帧算一个「坐标签名」：图被缩放 / 拖动 / 价格刻度变了就重画；没有线也没在画就不空转 */
  function tick() {
    raf = 0;
    if (dead) return;
    let s = '';
    try {
      const c = chart(), ts = c.timeScale(), { W, H } = plot();
      s = [W, H, c === lastChart ? 1 : 0, ts.logicalToCoordinate(0), ts.logicalToCoordinate(n - 1), S.on, S.start?.t, S.start?.p, S.hover?.t, S.hover?.p, lines.length,
        ...lines.flatMap(L => [yOf(L.a.p), yOf(L.b.p)])].join('|');
      lastChart = c;
    } catch { /* 图表正在重建：下一帧再来 */ }
    if (s !== sig) { sig = s; try { paint(); } catch { /* 同上 */ } }
    if (lines.length || S.on) raf = requestAnimationFrame(tick);
  }
  const kick = () => { sig = ''; if (!raf && !dead) raf = requestAnimationFrame(tick); };

  // ---------- 鼠标 / 触屏：画布不挡图表的缩放拖动，在容器上监听「几乎没动的点击」 ----------
  const rel = e => { const r = mainEl.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  const inPlot = p => { const a = plot(); return p.x >= 0 && p.y >= 0 && p.x <= a.w && p.y <= a.h; };
  const onDown = e => { if (S.on) S.down = rel(e); };
  const onMove = e => {
    if (!S.on) return;
    const p = rel(e);
    S.hover = inPlot(p) ? pick(p.x, p.y) : null;
    kick();
  };
  const onUp = e => {
    if (!S.on || !S.down) return;
    const p = rel(e), d = S.down; S.down = null;
    if (Math.hypot(p.x - d.x, p.y - d.y) > 5 || !inPlot(p)) return;       // 拖动图表不算点击；点到价格轴 / 时间轴上也不算
    const pt = pick(p.x, p.y);
    if (!pt) return;
    if (!S.start) S.start = pt;
    else if (S.start.t !== pt.t || S.start.p !== pt.p) { lines.push({ id: ++uid, color: S.color, a: S.start, b: pt }); S.start = null; renderLegend(); }
    S.hover = pt; kick();
  };
  const onKey = e => {
    if (!S.on || e.key !== 'Escape' || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
    if (S.start) { S.start = null; kick(); } else setOn(false);
  };
  mainEl.addEventListener('pointerdown', onDown); mainEl.addEventListener('pointermove', onMove); mainEl.addEventListener('pointerup', onUp);
  mainEl.addEventListener('pointerleave', () => { if (S.on) { S.hover = null; kick(); } });
  document.addEventListener('keydown', onKey);

  // ---------- 工具条 / 清单 ----------
  const drawBtn = h('button', { class: 'btn sm draw-btn', title: '在图上点两下画一条线（先点起点，再点终点）；Esc 取消', onclick: () => setOn(!S.on) }, '✎ 画线');
  const swatches = PALETTE.map(c => h('button', { class: 'draw-sw', style: `background:${c}`, title: '线的颜色', 'aria-label': '颜色 ' + c, onclick: () => setColor(c) }));
  const picker = h('input', { type: 'color', class: 'draw-pick', value: S.color, title: '自选颜色', oninput: e => setColor(e.target.value) });
  const magnetBox = h('label', { class: 'chk small', title: '鼠标靠近某根 K 线的开 / 高 / 低 / 收盘价时，自动吸到那个价格上' },
    h('input', { type: 'checkbox', checked: S.magnet ? true : null, onchange: e => { S.magnet = e.target.checked; prefs.set('drawMagnet', S.magnet); } }), '吸附价格');
  const clearBtn = h('button', { class: 'btn sm ghost', title: '删除这张图上的所有线', onclick: () => { lines.length = 0; S.start = null; renderLegend(); kick(); } }, '清除全部');
  const hint = h('span', { class: 'hint draw-hint', hidden: true });
  const toolbar = h('div', { class: 'draw-tools' }, drawBtn, h('span', { class: 'draw-colors' }, swatches, picker), magnetBox, clearBtn, hint);
  const legend = h('div', { class: 'draw-legend' });

  function setColor(c) {
    S.color = c; prefs.set('drawColor', c); picker.value = c;
    swatches.forEach((b, i) => b.classList.toggle('on', PALETTE[i].toLowerCase() === c.toLowerCase()));
    kick();
  }
  function setOn(on) {
    S.on = on; S.start = null; S.hover = null;
    drawBtn.classList.toggle('on', on); mainEl.classList.toggle('draw-on', on);
    hint.hidden = !on; hint.textContent = '点一下定起点，再点一下定终点（Esc 取消）';
    kick();
  }
  function renderLegend() {
    clearBtn.hidden = !lines.length;
    legend.replaceChildren(...lines.map((L, k) => {
      const ia = idxOf.get(L.a.t), ib = idxOf.get(L.b.t), pct = L.b.p / L.a.p - 1;
      const out = ia == null || ib == null;
      return h('div', { class: 'draw-row' },
        h('span', { class: 'draw-dot', style: `background:${L.color}` }),
        h('span', { class: 'num' }, `线 ${k + 1}`),
        h('span', { class: 'num' }, `起 ${fmtPrice(L.a.p)}`, h('span', { class: 'faint' }, ` (${L.a.t})`)),
        h('span', { class: 'faint' }, '→'),
        h('span', { class: 'num' }, `终 ${fmtPrice(L.b.p)}`, h('span', { class: 'faint' }, ` (${L.b.t})`)),
        h('span', { class: 'num ' + dirClass(pct) }, fmtPct(pct)),
        out ? h('span', { class: 'badge warn', title: '这条线的起点或终点早于当前图上的数据，图上不显示' }, '超出范围') : h('span', { class: 'faint num' }, `${Math.abs(ia - ib)} 根`),
        h('span', { class: 'grow' }),
        h('button', { class: 'btn sm ghost', title: '删除这条线', onclick: () => { lines.splice(lines.indexOf(L), 1); renderLegend(); kick(); } }, '删除'));
    }));
  }
  setColor(S.color); renderLegend(); if (lines.length) kick();

  return {
    toolbar, legend,
    destroy() { dead = true; cancelAnimationFrame(raf); document.removeEventListener('keydown', onKey); mainEl.classList.remove('draw-on'); mainEl.removeEventListener('pointerdown', onDown); mainEl.removeEventListener('pointermove', onMove); mainEl.removeEventListener('pointerup', onUp); cv.remove(); },
  };
}

/** 测试 / 排错用：当前内存里有多少条线 */
export const _drawStore = store;
