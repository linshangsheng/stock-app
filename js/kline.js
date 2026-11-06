// 「K线」页：左边 = 搜索框 + 固定的「大盘指数」+ 自己的「我的股票」名单；右边 = 选中的指数或股票的 K 线。
//  · 指数：日 K 线 + 均线 + 成交额；A 股指数（上证50 / 沪深300 / 创业板指）右侧带筹码分布（估算，见 server/chips.py）。美股指数只有 K 线。
//    鼠标指到哪天，顶部行情和右侧筹码分布就显示那天的。
//  · 股票：搜索（代码 / 中文名 / 拼音首字母，如 gldq）后点一下就看；图上的 ＋ / － 把它加进 / 移出左边的名单（只存本机，和自选股是两份）。
//  · 两种图上都能临时画线（js/draw.js）：线只在内存里，刷新或重启就没了。
// 展示用，不进入选股与回测。
import { get, put, state } from './api.js';
import { prefs } from './db.js';
import { h, clear, fmtNum, fmtPct, fmtPrice, fmtSigned, fmtAmount, isNum, evBadge, dirClass, code, toast, debounce } from './util.js';
import { themeColors, makeChart, disposeChart, onThemeChange, StockChart } from './chart.js';
import { createDrawing } from './draw.js';

const RANGES = [['3 个月', 63], ['半年', 126], ['1 年', 250], ['2 年', 500], ['全部', 0]];
const MA_COLORS = { 5: '#f59e0b', 20: '#2563eb', 60: '#8b5cf6', 200: '#0ea5a4', 250: '#e11d48' };      // MA250 = 关键线：红色、画粗
const wide = () => window.innerWidth >= 900;
const maColor = k => MA_COLORS[k] || '#64748b';
/** 标签文字用黑还是白：按底色亮度挑，保证青色 / 橙色这类浅色底上也看得清 */
function ink(hex) {
  const [r, g, b] = [1, 3, 5].map(i => { const v = parseInt(hex.slice(i, i + 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.25 ? '#0b1220' : '#ffffff';
}
const seg = (items, cur, onPick) => {
  const wrap = h('div', { class: 'seg' });
  for (const [v, label] of items) {
    const b = h('button', { class: v === cur ? 'on' : '', onclick: () => { [...wrap.children].forEach(c => c.classList.remove('on')); b.classList.add('on'); onPick(v); } }, label);
    wrap.append(b);
  }
  return wrap;
};

export const klineView = {
  layout: 'split',
  ownDeepLink: true,                      // #/kline/sh.600519：直接看这只股票的 K 线
  async mount(ctx) {
    const gen = ctx.gen, mk = state.market;
    const alive = () => ctx.gen === gen && mk === state.market;          // 切换市场 / 页面后，迟到的数据不再画
    const el = ctx.listEl, pane = ctx.detailEl;
    const S = { items: [], indices: [], sel: prefs.get('klineSel.' + mk, null), period: 'D', R: [], A: -1 };
    let seq = 0, sgSeq = 0;

    // ---------- 左栏：搜索框 + 两组名单 ----------
    const q = h('input', { class: 'grow', type: 'text', autocomplete: 'off', spellcheck: 'false', id: 'kl-q',
      placeholder: mk === 'CN' ? '搜索 代码 / 名称 / 拼音，如 格力电器、gldq' : '搜索 代码 / 名称，如 AAPL',
      oninput: debounce(onSearch, 150), onkeydown: onSearchKey, onfocus: () => { if (q.value.trim()) onSearch(); } });
    const sug = h('div', { class: 'kl-suggest', hidden: true });
    const badge = h('span', { class: 'badge' }, '0 只');
    const body = h('div', { class: 'kl-body' });
    el.append(h('div', { class: 'pane-head' }, h('div', { class: 'row between' }, h('h1', {}, 'K线'), badge), h('div', { class: 'kl-search' }, q, sug)), body);
    pane.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'big' }, '选一个指数或搜一只股票'), '左边搜索代码、中文名或拼音首字母（如 gldq），点一下就能在这里看 K 线；图上可以画线。'));
    const onDocClick = e => { if (!e.target.closest('.kl-search')) sug.hidden = true; };
    document.addEventListener('click', onDocClick);
    this._offDoc = () => document.removeEventListener('click', onDocClick);

    const inList = s => S.items.some(i => i.symbol === s);
    const selKey = () => (S.sel ? (S.sel.t === 'idx' ? S.sel.k : S.sel.s) : null);
    function markSel() { el.querySelectorAll('.list-item').forEach(x => x.classList.toggle('sel', x.dataset.symbol === selKey())); }
    function setSel(sel) { S.sel = sel; prefs.set('klineSel.' + mk, sel); markSel(); }

    function renderBody() {
      badge.textContent = `${S.items.length} 只`;
      const top = el.scrollTop;
      body.replaceChildren(...[
        h('div', { class: 'kl-group' }, h('span', {}, '大盘指数')),
        S.indices.map(ix => h('button', { class: 'list-item' + (ix.ok ? '' : ' dim'), dataset: { symbol: ix.key }, title: ix.ok ? '' : '这个指数还没有日线数据，请先初始化数据（设置页）',
          onclick: () => showIndex(ix.key) },
        h('div', { class: 't1' }, h('span', { class: 'name' }, ix.name), h('span', { class: 'code num' }, ix.key.replace(/^\w+\./, '')), h('span', { class: 'grow' }), ix.chips ? h('span', { class: 'badge' }, '筹码分布') : null))),
        h('div', { class: 'kl-group' }, h('span', {}, '我的股票'), h('span', { class: 'faint' }, S.items.length ? `${S.items.length} 只` : '')),
        S.items.length ? S.items.map(stockRow) : h('div', { class: 'hint', style: 'padding:6px 20px 16px' }, '还没有。上面搜一只股票，在右边图上点「＋ 加入名单」，下次就能直接点这里。')].flat());
      markSel(); el.scrollTop = top;
    }
    function stockRow(it) {
      const qt = it.quote;
      return h('button', { class: 'list-item', dataset: { symbol: it.symbol }, onclick: () => showStock(it.symbol) },
        h('div', { class: 't1' }, h('span', { class: 'name' }, it.name), h('span', { class: 'code num' }, it.code), h('span', { class: 'grow' }),
          h('span', { class: 'num', style: 'font-weight:600' }, qt ? fmtPrice(qt.close) : '—'),
          h('span', { class: 'num ' + dirClass(qt?.change_pct), style: 'min-width:64px;text-align:right;font-weight:600' }, qt ? fmtPct(qt.change_pct) : '—')));
    }

    // ---------- 搜索 ----------
    async function onSearch() {
      const v = q.value.trim(), my = ++sgSeq;
      if (!v) { sug.hidden = true; S.R = []; return; }
      let r;
      try { r = await get('/search', { q: v }, { cache: false }); } catch { return; }
      if (my !== sgSeq || !alive()) return;
      S.R = r.items; S.A = S.R.length ? 0 : -1;
      sug.hidden = false;
      if (!S.R.length) {
        const noPy = r.pinyin === false && mk === 'CN' && /^[a-z]+$/i.test(v);
        sug.replaceChildren(h('div', { class: 'hint', style: 'padding:8px 10px' }, noPy ? '没装拼音组件，现在只能搜代码和中文名。双击「安装.bat」装一次（或运行 pip install pypinyin）再重启软件就能用拼音了。' : '没有匹配（只能搜索已入库的股票）'));
        return;
      }
      renderSug();
    }
    function renderSug() {
      sug.replaceChildren(...S.R.map((s, i) => h('button', { class: 'sg-item' + (i === S.A ? ' on' : ''), onmousedown: e => e.preventDefault(), onclick: () => pickStock(s.symbol) },
        h('span', { class: 'name' }, s.name), h('span', { class: 'code num' }, s.code), s.industry ? h('span', { class: 'badge' }, s.industry) : null,
        s.status === 'delisted' ? h('span', { class: 'badge bad' }, '已退市') : null, h('span', { class: 'grow' }), inList(s.symbol) ? h('span', { class: 'faint small' }, '已在名单') : null)));
      sug.querySelector('.on')?.scrollIntoView({ block: 'nearest' });
    }
    function onSearchKey(e) {
      if (e.key === 'Escape') { sug.hidden = true; q.blur(); return; }
      if (sug.hidden || !S.R.length) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); S.A = (S.A + (e.key === 'ArrowDown' ? 1 : -1) + S.R.length) % S.R.length; renderSug(); }
      else if (e.key === 'Enter') { e.preventDefault(); pickStock(S.R[Math.max(0, S.A)].symbol); }
    }
    function pickStock(sym) { sug.hidden = true; q.value = ''; S.R = []; q.blur(); showStock(sym); }

    // ---------- 右栏 ----------
    const mobileTop = () => h('div', { class: 'mobile-top' }, h('button', { class: 'btn sm', onclick: () => ctx.setMobileDetail(false) }, '← 返回'));
    const loading = txt => h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' ' + txt);

    const showIndex = async key => {
      const my = ++seq; this._dispose(); setSel({ t: 'idx', k: key }); ctx.setMobileDetail(true);
      pane.replaceChildren(mobileTop(), loading('加载 K 线…（第一次打开筹码分布要算几秒）'));
      let d;
      try { d = await get('/index/kline', { index: key }, { cache: false, timeout: 120000 }); } catch (e) { d = { status: 'error', message: e.message }; }
      if (my !== seq || !alive()) return;
      pane.replaceChildren(mobileTop());
      const page = h('div', { class: 'pane-body page' }); pane.append(page);
      this._buildIndex(d, page, mk, () => showIndex(key));
    };

    const showStock = async symbol => {
      const my = ++seq; this._dispose(); setSel({ t: 'stk', s: symbol }); ctx.setMobileDetail(true);
      pane.replaceChildren(mobileTop(), loading('加载 ' + symbol + ' …'));
      let k;
      try { k = await get('/kline', { symbol, period: S.period }, { cache: false }); } catch (e) {
        if (my !== seq || !alive()) return;
        pane.replaceChildren(mobileTop(), h('div', { class: 'empty' }, h('div', { class: 'big' }, '无法加载 ' + symbol), e.message));
        return;
      }
      if (my !== seq || !alive()) return;
      const bars = k.bars, last = bars[bars.length - 1], prev = bars[bars.length - 2];
      const chg = prev ? last.close - prev.close : null, pct = prev ? last.close / prev.close - 1 : null;

      // 图上的 ＋ / － ：不在左边名单里 = ＋（点一下加入）；已经在 = －（点一下移出）
      const addBtn = h('button', { class: 'kl-add' });
      const paintAdd = () => {
        const on = inList(symbol);
        addBtn.classList.toggle('on', on); addBtn.setAttribute('aria-pressed', on);
        addBtn.title = on ? '从左边「我的股票」里移出这只股票' : '加到左边「我的股票」，下次直接点就能看';
        addBtn.replaceChildren(h('span', { class: 'kl-add-ico' }, on ? '−' : '+'), h('span', {}, on ? '移出名单' : '加入名单'));
      };
      addBtn.onclick = async () => {
        const on = inList(symbol); addBtn.disabled = true;
        try {
          const r = await put('/kline/list', on ? { remove: [symbol] } : { add: [symbol] });
          if (!alive()) return;
          S.items = r.items; renderBody(); paintAdd(); toast(on ? `已把 ${k.name} 移出名单` : `已把 ${k.name} 加入名单`, 'ok');
        } catch (e) { toast(e.message, 'bad'); } finally { addBtn.disabled = false; }
      };
      paintAdd();

      const head = h('div', { class: 'quote-head' },
        h('div', {},
          h('div', { class: 'row' }, h('h1', {}, k.name), h('span', { class: 'muted num' }, code(symbol)),
            k.industry ? h('span', { class: 'badge' }, k.industry) : null,
            k.board === 'star' ? h('span', { class: 'badge' }, '科创板') : k.board === 'chinext' ? h('span', { class: 'badge' }, '创业板') : null, addBtn),
          h('div', { class: 'row', style: 'align-items:baseline;gap:12px;margin-top:4px' },
            h('span', { class: 'price ' + dirClass(chg) }, fmtPrice(last.close)),
            h('span', { class: 'num ' + dirClass(chg), style: 'font-size:15px;font-weight:600' }, `${fmtSigned(chg)}  ${fmtPct(pct)}`)),
          h('div', { class: 'small muted num mt-s' }, `${k.asof_bar} 收盘　今开 ${fmtPrice(last.open)}　最高 ${fmtPrice(last.high)}　最低 ${fmtPrice(last.low)}　成交额 ${fmtAmount(last.amount)}`)),
        h('div', { class: 'row wrap' }, h('button', { class: 'btn', title: '到选股页看这只股票的完整详情（指标、财务、资金、加自选、记录买入）', onclick: () => { location.hash = '#/screener/' + encodeURIComponent(symbol); } }, '个股详情 →')));

      const mainEl = h('div', { class: 'chart-main' }), subEl = h('div', { class: 'chart-sub' });
      const box = h('div', { class: 'chart-box' }, mainEl, subEl);
      const sub0 = prefs.get('klineSub', 'macd');
      const kind0 = sub0 === 'rs' && !k.rs ? 'macd' : sub0;
      const tools = h('div', { class: 'chart-tools' },
        seg([['D', '日K'], ['W', '周K']], S.period, v => { S.period = v; showStock(symbol); }),
        seg([['macd', 'MACD'], ['rsi', 'RSI'], ...(k.rs ? [['rs', '相对大盘']] : [])], kind0, v => { prefs.set('klineSub', v); sc.setSub(v); }),
        h('span', { class: 'hint' }, `前复权 · ${k.period === 'W' ? '周线' : '日线'} · 数据截止 ${k.data_asof || k.asof_bar}`));
      const drawBar = h('div', { class: 'chart-tools' });
      const legendHost = h('div', { class: 'pane-body', style: 'padding-top:10px;padding-bottom:0' });
      pane.replaceChildren(mobileTop(), head, tools, drawBar, box, legendHost,
        h('div', { class: 'pane-body', style: 'padding-top:6px' }, h('p', { class: 'hint' }, '画线是临时的：只在软件开着的时候保留，刷新页面或重启后会消失。线两端的数字就是那个点的价格（纵坐标的值）。')));
      const sc = new StockChart(mainEl, subEl);
      sc.subKind = kind0; sc.build(); sc.setData(k);
      const dr = createDrawing({ mainEl, chart: () => sc.main, series: () => sc.candle, dates: bars.map(b => b.time), bars: bars.map(b => [b.open, b.high, b.low, b.close]), key: `${mk}|${symbol}|${S.period}` });
      drawBar.append(dr.toolbar); legendHost.append(dr.legend);
      const offTheme = onThemeChange(() => { if (sc.payload && sc.main) sc.setData(sc.payload); });
      this._stock = { sc, dr, offTheme };
    };

    // ---------- 启动：读名单，恢复上次看的 ----------
    try {
      const [li, ix] = await Promise.all([get('/kline/list', {}, { cache: false }), get('/index/list', {}, { cache: false })]);
      if (!alive()) return;
      S.items = li.items; S.indices = ix.indices;
    } catch (e) { if (alive()) body.replaceChildren(h('div', { class: 'alert bad', style: 'margin:12px 16px' }, e.message)); return; }
    renderBody();
    if (ctx.deep) { showStock(ctx.deep); return; }
    if (window.innerWidth <= 860) return;                     // 手机 / 窄屏（左右栏收成一栏，见 style.css 的 860px 断点）：先显示名单，点了再打开图
    const sel = S.sel;
    if (sel?.t === 'stk') showStock(sel.s);
    else showIndex(sel?.t === 'idx' && S.indices.some(i => i.key === sel.k) ? sel.k : (S.indices.find(i => i.ok) || S.indices[0])?.key);
  },

  _dispose() {
    this._off?.(); this._off = null; this._ro?.disconnect(); this._ro = null; this._dr?.destroy(); this._dr = null;
    if (this._chart) { disposeChart(this._chart); this._chart = null; }
    if (this._stock) { this._stock.offTheme?.(); this._stock.dr.destroy(); this._stock.sc.destroy(); this._stock = null; }
  },
  cleanup() { this._dispose(); this._offDoc?.(); this._offDoc = null; },

  /** 右栏：指数 K 线（+ 均线 + 成交额 + 筹码分布） */
  _buildIndex(d, page, mk, reload) {
    const asof = h('span', { class: 'hint' }, d.data_asof ? `数据截止 ${d.data_asof}（盘后日线）` : '');
    const card = h('div', { class: 'card kl' });
    page.append(card);
    if (d.status !== 'ok') {
      card.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '暂无 K 线数据'), d.message || '请先到设置页初始化数据。'));
      return;
    }
    const chips = d.chips;
    const n = d.dates.length;
    const showChips = !!chips && prefs.get('klineChips', wide());
    const rangeSeg = h('div', { class: 'seg' }, RANGES.map(([lab, m]) => h('button', { class: m === 126 ? 'on' : '', onclick: e => { [...rangeSeg.children].forEach(b => b.classList.toggle('on', b === e.target)); setRange(m); } }, lab)));
    const chipBtn = chips ? h('button', { class: 'btn sm', onclick: () => { prefs.set('klineChips', !showChips); this._dispose(); clear(page); this._buildIndex(d, page, mk, reload); } }, showChips ? '隐藏筹码分布' : '显示筹码分布') : null;
    const quote = h('div', { class: 'kl-quote' });
    const maLine = h('div', { class: 'kl-ma' });
    const mainEl = h('div', { class: 'kl-main' });
    const chipEl = showChips ? h('div', { class: 'kl-chips' }, h('canvas')) : null;
    const stats = chips ? h('div', { class: 'kl-stats' }) : null;
    const drawBar = h('div', { class: 'chart-tools', style: 'padding:0 0 8px' }), drawLegend = h('div', { class: 'draw-wrap' });
    card.append(h('div', { class: 'row between wrap kl-tools' }, asof, h('div', { class: 'row wrap' }, rangeSeg, chipBtn)),
      h('div', { class: 'kl-draw' }, drawBar), quote, maLine, h('div', { class: 'kl-grid' }, mainEl, chipEl), drawLegend, stats,
      h('p', { class: 'hint mt-s' }, chips ? [evBadge('无', '看盘辅助：筹码分布是用价格区间和换手率推算的估算值，没有任何回测证据说它能提高胜率，不是买卖信号。'), ' ', chips.note] : (state.market === 'US' ? '美股日线没有换手率，所以没有筹码分布。' : '')),
      h('p', { class: 'hint' }, '画线是临时的：刷新页面或重启后会消失；线两端的数字是那个点的价格。'),
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
    this._dr = createDrawing({ mainEl, chart: () => chart, series: () => candle, dates: dt, bars: d.candles, key: `${mk}|idx:${d.index.key}|D` });
    drawBar.append(this._dr.toolbar); drawLegend.append(this._dr.legend);
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
    let curM = 126, sized = mainEl.clientWidth > 40;
    const setRange = m => { curM = m; chart.timeScale().setVisibleLogicalRange({ from: m ? Math.max(0, n - m) : 0, to: n + 9 }); redraw(); };
    this._ro = new ResizeObserver(() => {
      chart.applyOptions({ width: mainEl.clientWidth, height: mainEl.clientHeight });
      if (!sized && mainEl.clientWidth > 40) { sized = true; setRange(curM); }      // 在隐藏的容器里创建时宽度是 0，第一次拿到真实宽度时重新按当前范围铺开
      else redraw();
    });
    this._ro.observe(mainEl);
    setRange(126); show(n - 1);
    setTimeout(redraw, 120);                              // 价格刻度在第一帧之后才定下来，补画一次
  },
};
