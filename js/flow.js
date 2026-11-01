// 「资金流向」（行情页第 4 个标签）：某一日 / 近 5 / 20 天累计，资金流入哪些板块、流出哪些板块。**估算，不是主力资金**（口径见 server/sector_flow.py）。
// 圆心分布：最中心是净流入强度最高的板块，往外一圈圈排下去，最外圈是流出最多的板块。5 圈 = 5 道圆角扇环，颜色取柔和版的「市场温度」色阶（中心红 → 最外圈蓝），
// 同一种颜色里按数值深浅；圆环由内向外逐渐变薄——最重要的「流入最多」占最大的面积；只给最重要的几个板块写大字，其余安静地留成色块，指上去看详情。
import { get, state } from './api.js';
import { h, clear, fmtAmount, isNum, evBadge, code } from './util.js';
import { onThemeChange } from './chart.js';

const RING_NAME = ['流入最多（中心）', '流入', '持平', '流出', '流出最多（外圈）'];
const R = 335, CX = 360, CY = 350;
const THICK = [1.4, 1.2, 1, 0.88, 0.78];         // 每圈的厚度比例：越往里越厚
const HOLE = 0.28, GAPPX = 3.5, CR = 6, MINARC = 26;
const EDGES = (() => { const u = R / THICK.reduce((a, b) => a + b, 0), e = [0]; THICK.forEach(t => e.push(e[e.length - 1] + t * u)); return e; })();
const ringMid = (k, c) => (k === 0 ? (c === 1 ? EDGES[1] * 0.62 : (EDGES[1] * HOLE + EDGES[1]) / 2) : (EDGES[k] + EDGES[k + 1]) / 2);

const ink = hex => {                              // 色块里的字用黑还是白：按底色亮度挑
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim());
  if (!m) return '#fff';
  const [r, g, b] = [0, 2, 4].map(i => { const v = parseInt(m[1].slice(i, i + 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.3 ? '#0b1220' : '#ffffff';
};
/** 颜色渐变：同一圈（同一种颜色）里，按板块自己的数值决定浓淡——红 / 橙：流入越多越浓；浅蓝 / 蓝：流出越多越浓；灰：只做很轻的渐变。浓淡 = 这种颜色占多少、其余用页面底色冲淡，深浅色模式都成立。 */
const hex2rgb = hex => { const m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim()); return m ? [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16)) : null; };
const rgb2hex = c => '#' + c.map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join('');
const mixHex = (fg, bg, p) => { const a = hex2rgb(fg), b = hex2rgb(bg); return a && b ? rgb2hex(a.map((v, i) => v * p + b[i] * (1 - p))) : fg; };
const FLOOR = 0.55;                                  // 最淡的块也有 55% 的颜色，一眼能认出是这个颜色
export function depthShare(ring, d) { return ring === 2 ? 0.5 + 0.3 * d : FLOOR + (1 - FLOOR) * Math.pow(d, 0.9); }
const FL_DEF = ['#4f8ccf', '#93bbe2', '#c4c7cd', '#efb066', '#e2504f'];
const cssVar = (n, d = '') => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || d;
const flc = k => cssVar(`--fl${k}`, FL_DEF[k]);

const sgn = (v, d = 1) => (isNum(v) ? (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v * 100).toFixed(d) + '%' : '—');
const money = v => (isNum(v) ? (v > 0 ? '+' : v < 0 ? '−' : '') + fmtAmount(Math.abs(v)) : '—');

/** 每圈放几个：按圆周长的比例分配（外圈长，放得多），最大余数法凑整；中心圈至少 1 个。这样每一块的弧长大致相等。 */
export function ringCounts(N) {
  const W = [0, 1, 2, 3, 4].map(k => 2 * Math.PI * ringMid(k, 2)), tot = W.reduce((a, b) => a + b, 0);
  const raw = W.map(w => (w / tot) * N), cnt = raw.map(Math.floor);
  let rest = N - cnt.reduce((a, b) => a + b, 0);
  raw.map((x, i) => [x - Math.floor(x), i]).sort((a, b) => b[0] - a[0]).forEach(([, i]) => { if (rest > 0) { cnt[i]++; rest--; } });
  if (N >= 1 && cnt[0] === 0) { const j = cnt.indexOf(Math.max(...cnt)); cnt[j]--; cnt[0] = 1; }
  return cnt;
}

export function flowPanel() {
  const S = { day: null, window: 1, rank: 'strength', level: 'industry', data: null, sel: null, stocks: null };
  const el = h('div', { class: 'fl' });
  let tip = null, off = null, alive = true;

  const load = async () => {
    clear(el).append(h('div', { class: 'card' }, h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 计算资金流向…（第一次约 1~2 秒）')));
    let d;
    try { d = await get('/flow/sectors', { day: S.day || undefined, window: S.window, level: S.level }, { cache: false, timeout: 120000 }); } catch (e) { d = { status: 'error', message: e.message }; }
    if (!alive) return;
    S.data = d; S.day = d.day || S.day; S.sel = null; S.stocks = null; S.fresh = true;
    render();
  };

  const ranked = () => {
    const a = [...(S.data?.sectors || [])];
    return S.rank === 'net' ? a.sort((x, y) => y.net - x.net) : a.sort((x, y) => (y.strength - x.strength) || (y.ret - x.ret));
  };

  const seg = (opts, cur, fn) => h('div', { class: 'seg' }, opts.map(([v, l]) => h('button', { class: v === cur ? 'on' : '', onclick: () => fn(v) }, l)));

  /** 圆角扇环：第 k 圈是 [EDGES[k], EDGES[k+1]] 的一道圆环（中心圈是实心圆盘 / 带小孔的圆盘），每个板块占其中一段弧；弧的宽度 = 一半平均 + 一半按成交额（开方），最窄也有 MINARC 像素。 */
  function layout(items) {
    const N = items.length, cnt = ringCounts(N), sq = items.map(s => Math.sqrt(Math.max(s.amount, 0)));
    const out = [];
    let i = 0;
    cnt.forEach((c, k) => {
      const Ro = EDGES[k + 1] - GAPPX / 2, Ri = k === 0 ? (c === 1 ? 0 : EDGES[1] * HOLE) : EDGES[k] + GAPPX / 2, rm = ringMid(k, c);
      const idx = Array.from({ length: Math.min(c, N - i) }, (_, t) => i + t);
      if (!idx.length) return;
      const gapA = idx.length > 1 || k > 0 ? GAPPX / rm : 0, avail = 2 * Math.PI - idx.length * gapA;
      const tot = idx.reduce((a, t) => a + sq[t], 0) || 1;
      let ang = idx.map(t => (0.5 / idx.length + 0.5 * sq[t] / tot) * avail);
      const minA = Math.min(MINARC / rm, avail / idx.length);
      for (let pass = 0; pass < 4; pass++) {
        const small = ang.map(a => a < minA - 1e-9), nS = small.filter(Boolean).length;
        if (!nS) break;
        const rest = ang.reduce((a, v, t) => a + (small[t] ? 0 : v), 0);
        if (rest <= 0) break;
        const sc = (avail - nS * minA) / rest;
        ang = ang.map((v, t) => (small[t] ? minA : v * sc));
      }
      let a0 = -Math.PI / 2 + gapA / 2;
      idx.forEach((t, q) => {
        const a1 = a0 + ang[q];
        out.push({ s: items[t], ring: k, Ri, Ro, a0, a1, full: k === 0 && idx.length === 1, rm });
        a0 = a1 + gapA;
      });
      i += idx.length;
    });
    return out;
  }

  /** 圆角扇环的路径：先把扇环向内缩 cr，再用同色、线宽 2·cr、圆角连接的描边把四个角「吹」圆。扇形太窄、缩不进去时逐步减小圆角，返回 {d, cr}。 */
  function cellPath(n, crMax) {
    const P = (r, a) => `${(CX + r * Math.cos(a)).toFixed(2)},${(CY + r * Math.sin(a)).toFixed(2)}`;
    if (n.full) { const cr = crMax, r = n.Ro - cr; return { cr, d: `M${P(r, 0)} A${r} ${r} 0 1 1 ${P(r, Math.PI)} A${r} ${r} 0 1 1 ${P(r, 0)} Z` }; }
    for (let cr = crMax; cr >= 1.2; cr *= 0.75) {
      const ri = n.Ri + cr, ro = n.Ro - cr, di = cr / ri, dO = cr / ro, si = n.a0 + di, ei = n.a1 - di, so = n.a0 + dO, eo = n.a1 - dO;
      if (ro > ri && eo > so && ei > si) return { cr, d: `M${P(ro, so)} A${ro} ${ro} 0 ${eo - so > Math.PI ? 1 : 0} 1 ${P(ro, eo)} L${P(ri, ei)} A${ri} ${ri} 0 ${ei - si > Math.PI ? 1 : 0} 0 ${P(ri, si)} Z` };
    }
    return null;
  }

  /** 一块扇环里怎么写名字：从大到小试字号，先水平、再沿半径方向写（窄而厚的块沿半径写正合适）；放不下就截断加「…」，实在放不下才不写。
   *  「放得下」是精确判断的：把文字框的四个角和四条边的中点，逐个检查是否都落在这块扇环里（离边缘留 2.5~4 像素）。 */
  const tw = (str, fs) => [...str].reduce((a, ch) => a + (/[一-鿿]/.test(ch) ? fs : fs * 0.58), 0);
  const inCell = (n, x, y, mg) => {
    const X = x - CX, Y = y - CY, r = Math.hypot(X, Y);
    let a = Math.atan2(Y, X);
    while (a < n.a0) a += 2 * Math.PI;
    while (a >= n.a0 + 2 * Math.PI) a -= 2 * Math.PI;
    return r <= n.Ro - mg && r >= n.Ri + mg && (a - n.a0) * r >= mg && (n.a1 - a) * r >= mg;
  };
  function fits(n, str, fs, mode, am, mg) {
    const w = tw(str, fs), hh = fs * 1.1, cx = CX + n.rm * Math.cos(am), cy = CY + n.rm * Math.sin(am);
    const rot = mode === 'h' ? 0 : (Math.cos(am) < 0 ? am + Math.PI : am), ux = Math.cos(rot), uy = Math.sin(rot);
    for (const [p, q] of [[-1, -1], [1, -1], [-1, 1], [1, 1], [0, -1], [0, 1], [-1, 0], [1, 0]]) {
      const dx = (p * w) / 2, dy = (q * hh) / 2;
      if (!inCell(n, cx + dx * ux - dy * uy, cy + dx * uy + dy * ux, mg)) return false;
    }
    return true;
  }
  function fitLabel(n, str) {
    const am = (n.a0 + n.a1) / 2;
    for (const fs of [17, 16, 15, 14, 13, 12]) {
      const mg = fs > 12 ? 4 : 2.5;
      for (const mode of ['h', 'r']) if (fits(n, str, fs, mode, am, mg)) return { mode, fs, text: str, am };
    }
    for (const mode of ['h', 'r']) {
      let t = str;
      while (t.length > 2 && !fits(n, t + '…', 12, mode, am, 2.5)) t = t.slice(0, -1);
      if (t.length >= 2 && fits(n, t + '…', 12, mode, am, 2.5)) return { mode, fs: 12, text: t + '…', am };
    }
    return null;
  }

  function showTip(ev, n) {
    if (!tip) return;
    const s = n.s, d = S.data;
    tip.replaceChildren(h('div', { class: 'row', style: 'gap:7px' }, h('i', { class: 'fl-dot', style: `background:${n.fill}` }), h('b', {}, s.short)), s.short !== s.name ? h('div', { class: 'tiny muted' }, s.name) : null,
      h('div', { class: 'tiny muted' }, `${RING_NAME[n.ring]} · 第 ${n.rank} / ${d.sectors.length} 名`),
      h('div', { style: 'margin-top:4px' }, '估算净流入 ', h('b', { class: 'num' }, money(s.net)), ` · 强度 `, h('b', { class: 'num' }, sgn(s.strength))),
      h('div', {}, `成交额 ${fmtAmount(s.amount)} · 占全市场 ${(s.share * 100).toFixed(1)}%`, isNum(s.share_chg) ? `（较此前 20 日 ${s.share_chg >= 0 ? '+' : '−'}${Math.abs(s.share_chg * 100).toFixed(2)} 个百分点）` : ''),
      h('div', {}, `平均涨跌 ${sgn(s.ret, 2)} · ${s.n} 只股票`));
    const box = el.querySelector('.fl-chart').getBoundingClientRect();
    tip.style.display = 'block';
    const x = Math.min(ev.clientX - box.left + 14, box.width - 250), y = Math.min(ev.clientY - box.top + 14, box.height - 130);
    tip.style.left = Math.max(4, x) + 'px'; tip.style.top = Math.max(4, y) + 'px';
  }

  /** 圆环和名单互相联动：指到圆环上的一块，名单里对应的一行亮起；指到名单里的一行，圆环上对应的一块凸起、其余淡下去。 */
  const hover = (key, on) => {
    el.querySelector('.fl-chart svg')?.classList.toggle('has-hov', on);
    el.querySelectorAll('.fl-cell').forEach(g => g.classList.toggle('hov', on && g.getAttribute('data-key') === key));
    el.querySelectorAll('.fl-row').forEach(r => r.classList.toggle('hov', on && r.dataset.key === key));
  };

  async function select(key) {
    S.sel = key; S.stocks = null;
    el.querySelector('.fl-chart svg')?.classList.add('has-sel');
    el.querySelectorAll('.fl-cell').forEach(g => g.classList.toggle('sel', g.getAttribute('data-key') === key));
    el.querySelectorAll('.fl-row').forEach(r => r.classList.toggle('on', r.dataset.key === key));
    renderDetail();
    try { S.stocks = await get('/flow/stocks', { day: S.data.day, window: S.window, key, level: S.level }, { cache: false, timeout: 60000 }); } catch (e) { S.stocks = { status: 'error', message: e.message, inflow: [], outflow: [] }; }
    if (alive && S.sel === key) renderDetail();
  }

  const detailBox = h('div', { class: 'mt' });
  function renderDetail() {
    clear(detailBox);
    const s = S.data?.sectors.find(x => x.key === S.sel);
    if (!s) { detailBox.append(h('div', { class: 'hint' }, '点圆环上的一块，或名单里的一行，看这个板块里谁流入最多、谁流出最多。')); return; }
    const st = S.stocks;
    const tbl = (rows, title) => h('div', {}, h('h3', {}, title), rows.length ? h('table', { class: 'tbl small mt-s' }, h('tbody', {}, rows.map(r =>
      h('tr', { style: 'cursor:pointer', onclick: () => { location.hash = '#/screener/' + encodeURIComponent(r.symbol); } },
        h('td', {}, r.name, h('span', { class: 'muted tiny' }, ' ' + code(r.symbol))), h('td', { class: 'num r' }, money(r.net)),
        h('td', { class: 'num r ' + (r.ret >= 0 ? 'up' : 'down') }, sgn(r.ret, 2)))))) : h('div', { class: 'hint mt-s' }, '没有'));
    detailBox.append(h('div', { class: 'card soft' }, h('div', { class: 'row between wrap' }, h('h3', {}, s.short, s.short !== s.name ? h('span', { class: 'tiny muted', style: 'font-weight:400' }, '  ' + s.name) : null), h('span', { class: 'small' }, `估算净流入 `, h('b', { class: 'num' }, money(s.net)), ` · 强度 `, h('b', { class: 'num' }, sgn(s.strength)))),
      !st ? h('div', { class: 'mt-s' }, h('span', { class: 'spinner' })) : st.status !== 'ok' ? h('div', { class: 'hint mt-s' }, st.message || '暂无') :
        h('div', { class: 'grid c2 mt-s' }, tbl(st.inflow, '估算净流入最多的股票'), tbl(st.outflow, '估算净流出最多的股票'))));
  }

  function render() {
    const d = S.data;
    clear(el);
    if (!d || d.status !== 'ok') {
      el.append(h('div', { class: 'card' }, h('div', { class: 'empty' }, h('div', { class: 'big' }, '暂无资金流向'), d?.message || '请先到设置页初始化数据。')));
      return;
    }
    const narrow = window.innerWidth < 700;                    // 手机上圆环只有 300 多像素宽，字会小到看不清：只留颜色和形状，以名单为主
    const items = ranked(), nodes = layout(items), N = nodes.length;
    nodes.forEach((n, i) => { n.rank = i + 1; });
    const bgHex = cssVar('--bg', '#ffffff');
    const val = n => { const x = S.rank === 'net' ? n.s.net : n.s.strength; return S.rank === 'net' ? Math.sign(x) * Math.sqrt(Math.abs(x)) : x; };   // 金额差距悬殊（电子几千亿 vs 小板块几千万），开方压一压
    for (let k = 0; k < 5; k++) {
      const grp = nodes.filter(n => n.ring === k);
      if (!grp.length) continue;
      const vs = grp.map(val), lo = Math.min(...vs), hi = Math.max(...vs), sorted = [...vs].sort((a, b) => a - b);
      grp.forEach(n => {
        const v = val(n), rk = grp.length > 1 ? sorted.indexOf(v) / (grp.length - 1) : 1;
        const t = hi === lo ? 1 : 0.5 * ((v - lo) / (hi - lo)) + 0.5 * rk;   // 圈内位置：一半看数值、一半看名次（只看数值的话，一个特别大的值会把其他块都「压」得很淡）
        n.depth = k >= 3 ? 1 - t : t;                                   // 流出一侧（浅蓝 / 蓝）：流出越多越浓
        n.fill = mixHex(flc(4 - k), bgHex, depthShare(k, n.depth));
      });
    }
    const days = d.available_days || [];
    const idx = days.indexOf(d.day);
    const go = i => { if (i >= 0 && i < days.length) { S.day = days[i]; load(); } };
    const ctl = h('div', { class: 'row between wrap fl-ctl' },
      h('div', { class: 'row wrap', style: 'gap:6px' },
        h('button', { class: 'btn sm', disabled: idx <= 0 ? true : null, onclick: () => go(idx - 1), title: '前一个交易日' }, '‹'),
        h('input', { type: 'date', value: d.day, min: days[0], max: days[days.length - 1], onchange: e => { if (e.target.value) { S.day = e.target.value; load(); } } }),
        h('button', { class: 'btn sm', disabled: idx >= days.length - 1 ? true : null, onclick: () => go(idx + 1), title: '后一个交易日' }, '›'),
        h('button', { class: 'btn sm', onclick: () => { S.day = null; load(); } }, '最新'),
        seg([[1, '当天'], [5, '近 5 天'], [20, '近 20 天']], S.window, v => { S.window = v; load(); })),
      h('div', { class: 'row wrap', style: 'gap:6px' },
        seg([['strength', '按净流入强度'], ['net', '按净流入金额']], S.rank, v => { S.rank = v; render(); }),
        state.market === 'US' ? seg([['industry', '细分行业'], ['sector', '大板块']], S.level, v => { S.level = v; S.sel = null; load(); }) : null));

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    if (S.sel) svg.classList.add('has-sel');
    if (S.fresh) svg.classList.add('fl-anim');                  // 入场动画只在刚加载 / 换日期时播一次，点选板块、切换主题不再重播
    S.fresh = false;
    svg.setAttribute('viewBox', '0 0 720 700'); svg.setAttribute('width', '100%'); svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', '板块资金流向圆环图：中心是流入最多的板块，外圈是流出最多的板块');
    const ns = (n, a) => { const e = document.createElementNS('http://www.w3.org/2000/svg', n); for (const k in a) e.setAttribute(k, a[k]); return e; };
        nodes.forEach((n, idx) => {
      const g = ns('g', { class: 'fl-cell' + (S.sel === n.s.key ? ' sel' : ''), 'data-key': n.s.key, 'data-geom': [n.Ri, n.Ro, n.a0, n.a1].map(v => v.toFixed(4)).join(','), style: `--i:${idx}`, tabindex: 0, role: 'button', 'aria-label': `${n.s.name}，强度 ${sgn(n.s.strength)}` });
      const cp = cellPath(n, CR);
      if (!cp) return;
      g.append(ns('path', { d: cp.d, fill: n.fill, stroke: n.fill, 'stroke-width': cp.cr * 2, 'stroke-linejoin': 'round' }));
      if (!narrow) {
        const hero = n.full, lab = hero ? { mode: 'h', fs: 18, text: n.s.short, am: 0 } : fitLabel(n, n.s.short);
        if (lab) {
          const x = hero ? CX : CX + n.rm * Math.cos(lab.am), y = (hero ? CY - 9 : CY + n.rm * Math.sin(lab.am));
          const t = ns('text', { x, y, 'text-anchor': 'middle', 'dominant-baseline': 'central', 'font-size': lab.fs, 'font-weight': 600, fill: ink(n.fill), 'pointer-events': 'none' });
          if (lab.mode === 'r') { const deg = (lab.am * 180) / Math.PI; t.setAttribute('transform', `rotate(${(Math.cos(lab.am) < 0 ? deg + 180 : deg).toFixed(1)} ${x.toFixed(1)} ${y.toFixed(1)})`); }
          t.textContent = lab.text;
          g.append(t);
          if (hero) { const v = ns('text', { x: CX, y: CY + 13, 'text-anchor': 'middle', 'dominant-baseline': 'central', 'font-size': 14, fill: ink(n.fill), 'pointer-events': 'none', opacity: 0.9 }); v.textContent = sgn(n.s.strength, 0); g.append(v); }
        }
      }
      g.addEventListener('mousemove', ev => showTip(ev, n));
      g.addEventListener('mouseenter', () => hover(n.s.key, true));
      g.addEventListener('mouseleave', () => { hover(n.s.key, false); if (tip) tip.style.display = 'none'; });
      g.addEventListener('click', () => select(n.s.key)); g.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') select(n.s.key); });
      svg.append(g);
    });
    tip = h('div', { class: 'fl-tip', style: 'display:none' });
    const legend = h('div', { class: 'fl-legend' }, RING_NAME.map((l, k) => {
      const base = flc(4 - k);
      return h('span', {}, h('i', { style: `background:linear-gradient(90deg,${mixHex(base, bgHex, depthShare(k, 0))},${mixHex(base, bgHex, depthShare(k, 1))})` }), l);
    }), h('span', { class: 'fl-hint' }, '同一种颜色里，越浓表示流入（红、橙）或流出（浅蓝、蓝）越多'));

    // 右边：一个大数字 + 一句话 + 名单
    const T = d.total, tint = new Map(nodes.map(n => [n.s.key, n.fill]));
    const byNet = [...items].sort((a, b) => b.net - a.net);
    const inflow = byNet.filter(s => s.net > 0).slice(0, 3).map(s => s.short), outflow = byNet.filter(s => s.net < 0).slice(-3).reverse().map(s => s.short);
    const sentence = `按金额：${inflow.length ? `资金主要流向${inflow.join('、')}` : '没有明显流入的板块'}；${outflow.length ? `流出最多的是${outflow.join('、')}` : '没有明显流出的板块'}。`;
    const heroColor = T.net >= 0 ? flc(4) : flc(0);
    const listRow = (s, rank) => h('div', { class: 'fl-row' + (rank ? ' rk' : '') + (S.sel === s.key ? ' on' : ''), dataset: { key: s.key }, onclick: () => select(s.key), title: s.name,
      onmouseenter: () => hover(s.key, true), onmouseleave: () => hover(s.key, false) },
      rank ? h('span', { class: 'fl-rk' }, String(rank)) : null,
      h('span', { class: 'fl-nm' }, h('i', { style: `background:${tint.get(s.key)}` }), s.short),
      h('span', { class: 'fl-bar' }, h('b', { style: `width:${Math.min(100, Math.abs(s.strength) * 100)}%;background:${tint.get(s.key)}` })),
      h('span', { class: 'num small' }, sgn(s.strength, 0)), h('span', { class: 'num muted small' }, money(s.net)));
    const top = items.slice(0, 5), bot = items.slice(-5).reverse();
    const side = h('div', { class: 'fl-side' },
      h('div', { class: 'card soft' },
        h('div', { class: 'row between' }, h('span', { class: 'small muted' }, d.window === 1 ? d.day : `近 ${d.window} 天（${d.window_days[0]} ~ ${d.day}）`),
          evBadge('无', '看盘辅助：估算净流入是用成交额和涨跌推算的，不是主力资金，没有任何回测证据说它能提高胜率。')),
        h('div', { class: 'small muted', style: 'margin-top:8px' }, '全市场估算净流入'),
        h('div', { class: 'fl-hero num', style: `color:${heroColor}` }, money(T.net), h('span', { class: 'fl-hero-s' }, `  强度 ${sgn(T.strength, 0)}`)),
        h('p', { class: 'fl-sum' }, sentence),
        h('div', { class: 'fl-mini' },
          h('span', {}, '成交额 ', h('b', { class: 'num' }, fmtAmount(T.amount))),
          h('span', {}, d.window === 1 ? '上涨 / 下跌 ' : '末日 上涨 / 下跌 ', h('b', { class: 'num' }, `${T.up} / ${T.down}`)),
          h('span', {}, '板块 ', h('b', { class: 'num' }, `${N}${state.market === 'CN' ? '（证监会行业）' : ''}`)))),
      detailBox,
      h('div', { class: 'mt' }, h('h3', {}, '流入最多'), h('div', { class: 'mt-s' }, top.map(s => listRow(s)))), h('div', { class: 'mt' }, h('h3', {}, '流出最多'), h('div', { class: 'mt-s' }, bot.map(s => listRow(s)))),
      h('details', { class: 'mt' }, h('summary', {}, `全部 ${N} 个板块（按当前排序）`), h('div', { class: 'fl-all mt-s' }, items.map((s, i) => listRow(s, i + 1)))));
    el.append(h('div', { class: 'card' }, ctl,
      h('div', { class: 'fl-wrap mt' }, h('div', { class: 'fl-chart' }, svg, tip), side),
      legend,
      h('details', { class: 'mt-s' }, h('summary', {}, '怎么看、怎么估算的'),
        h('p', { class: 'hint' }, '最中间是当前排序下流入最多的板块，往外一圈圈排下去，最外圈是流出最多的；圆环越往外越薄，面积最大的是中心——最重要的流入；5 圈对应市场温度的 5 档色（这里红色只表示「钱多」，不是涨跌）；每一块的宽窄大致跟成交额走。每一块放得下就写名字（太窄的只能指上去看）。'),
        h('p', { class: 'hint' }, d.note))));
    renderDetail();
  }

  off = onThemeChange(() => { if (S.data?.status === 'ok') render(); });
  return { el, mount() { load(); }, destroy() { alive = false; off?.(); } };
}
