// 自选股（4.5.1）：数据存后端 portfolio.db，清缓存不丢；自选只用于手动关注与加分，不限制漏斗的扫描范围（5.3）。
import { get, put, state } from './api.js';
import { h, clear, fmtPrice, fmtPct, fmtSigned, fmtAmount, dirClass, code, toast, debounce, modal, download } from './util.js';
import { showDetail } from './detail.js';
import { parseWatchlist, fileToText } from './parse.js';
import { prefs } from './db.js';

export const watchlist = {
  layout: 'split',
  async mount(ctx) {
    const el = ctx.listEl;
    const S = { items: [], sort: prefs.get('wl.sort', 'added'), q: '', sel: null, live: false, liveQuotes: {} };

    let liveTimer = null;
    async function pollLive() {
      if (!S.live || !S.items.length) return;
      try {
        const r = await get('/quote/live', { symbols: S.items.map(i => i.symbol).join(',') }, { cache: false });
        const prev = S.liveQuotes || {}; S.liveQuotes = r.quotes || {}; S.flash = new Set(Object.keys(S.liveQuotes).filter(k => prev[k] && prev[k].price !== S.liveQuotes[k].price)); render(); S.flash = new Set();
      } catch { /* 盘中刷新失败不影响盘后数据 */ }
    }
    function setLive(on) {
      S.live = on; prefs.set('wl.live', on); clearInterval(liveTimer);
      if (on) { pollLive(); liveTimer = setInterval(() => { if (!document.hidden) pollLive(); }, 60000); } else { S.liveQuotes = {}; render(); }
    }
    ctx.cleanup = () => clearInterval(liveTimer);

    async function load() {
      try { const d = await get('/watchlist'); S.items = d.items; S.stale = d._stale; } catch (e) { clear(el); el.append(h('div', { class: 'empty' }, e.message)); return; }
      render();
    }
    ctx.refreshList = load;

    function render() {
      clear(el);
      const head = h('div', { class: 'pane-head' },
        h('div', { class: 'row between' }, h('h1', {}, '自选股'), h('span', { class: 'badge' }, `${S.items.length} 只`)),
        h('div', { class: 'row mt-s' },
          h('input', { class: 'grow', type: 'text', placeholder: '搜索代码 / 名称，回车添加', id: 'wl-q', oninput: debounce(onSearch, 200), onkeydown: e => { if (e.key === 'Enter') quickAdd(e.target.value); } }),
          h('select', { onchange: e => { S.sort = e.target.value; prefs.set('wl.sort', S.sort); render(); } },
            [['added', '加入时间'], ['pct', '涨跌幅'], ['amount', '成交额'], ['price', '最新价']].map(([v, l]) => h('option', { value: v, selected: S.sort === v ? true : null }, l)))),
        h('div', { class: 'row mt-s' }, h('button', { class: 'btn sm', onclick: importDlg }, '批量导入'),
          h('label', { class: 'chk small', title: '盘中低频刷新：≥60 秒，仅页面打开时生效；' + (state.market === 'US' ? '基于 yfinance，可能有延迟' : '基于东财行情接口，可能有延迟') }, h('input', { type: 'checkbox', checked: S.live ? true : null, onchange: e => setLive(e.target.checked) }), '盘中刷新'),
          h('button', { class: 'btn sm', onclick: () => download('watchlist.json', JSON.stringify(S.items.map(i => ({ symbol: i.symbol, note: i.note, added_at: i.added_at })), null, 1)) }, '导出')),
        h('div', { id: 'wl-suggest' }));
      el.append(head);
      if (S.stale) el.append(h('div', { class: 'alert warn', style: 'margin:10px 16px' }, '数据延迟（离线快照）'));
      if (!S.items.length) {
        el.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '添加你的第一只股票'), h('div', {}, '在上方搜索代码或名称，或从「选股」页一键加入候选。')));
        return;
      }
      const key = { pct: i => i.quote?.change_pct ?? -9, amount: i => i.quote?.amount ?? -1, price: i => i.quote?.close ?? -1, added: i => i.added_at };
      const items = [...S.items].sort((a, b) => S.sort === 'added' ? (key.added(b) > key.added(a) ? 1 : -1) : key[S.sort](b) - key[S.sort](a));
      for (const it of items) el.append(row(it));
    }

    function row(it) {
      const q = it.quote;
      return h('button', { class: 'list-item' + (S.sel === it.symbol ? ' sel' : '') + (S.flash?.has(it.symbol) ? ' flash' : ''), dataset: { symbol: it.symbol },
        onclick: () => { S.sel = it.symbol; [...el.querySelectorAll('.list-item')].forEach(x => x.classList.toggle('sel', x.dataset.symbol === it.symbol)); showDetail(ctx, it.symbol); } },
        h('div', { class: 't1' }, h('span', { class: 'name' }, it.name), h('span', { class: 'code num' }, code(it.symbol)), it.industry ? h('span', { class: 'badge' }, it.industry) : null,
          h('span', { class: 'grow' }),
          S.liveQuotes[it.symbol] ? h('span', { class: 'live-dot', title: '盘中报价' }) : null,
          h('span', { class: 'num', style: 'font-weight:600' }, S.liveQuotes[it.symbol] ? fmtPrice(S.liveQuotes[it.symbol].price) : q ? fmtPrice(q.close) : '—'),
          h('span', { class: 'num ' + dirClass(S.liveQuotes[it.symbol]?.change_pct ?? q?.change_pct), style: 'min-width:64px;text-align:right;font-weight:600' }, S.liveQuotes[it.symbol] ? fmtPct(S.liveQuotes[it.symbol].change_pct) : q ? fmtPct(q.change_pct) : '—')),
        h('div', { class: 't2 num' }, h('span', { class: dirClass(q?.change) }, q ? fmtSigned(q.change) : ''), h('span', {}, '额 ' + (q ? fmtAmount(q.amount) : '—')),
          q?.is_temp ? h('span', { class: 'badge warn' }, '临时数据') : null, q?.trade_status === 0 ? h('span', { class: 'badge' }, '停牌') : null,
          it.note ? h('span', {}, '📝 ' + it.note) : null,
          h('span', { class: 'grow' }), h('span', { class: 'btn sm ghost', title: '移出自选', onclick: async e => { e.stopPropagation(); await put('/watchlist', { remove: [it.symbol] }); await ctx.reloadWatch(); load(); } }, '移出')));
    }

    async function onSearch(e) {
      const q = e.target.value.trim(); const box = document.getElementById('wl-suggest'); clear(box);
      if (!q) return;
      try {
        const r = await get('/search', { q }, { cache: false });
        box.append(h('div', { class: 'card', style: 'margin-top:6px;padding:4px 0' }, r.items.length ? r.items.map(s =>
          h('button', { class: 'list-item', style: 'padding:6px 12px', onclick: async () => { await put('/watchlist', { add: [{ symbol: s.symbol }] }); await ctx.reloadWatch(); toast(`已添加 ${s.name}`, 'ok'); load(); } },
            h('span', { class: 'name' }, s.name), ' ', h('span', { class: 'code num' }, s.code), s.industry ? h('span', { class: 'badge', style: 'margin-left:6px' }, s.industry) : null,
            s.status === 'delisted' ? h('span', { class: 'badge bad', style: 'margin-left:6px' }, '已退市') : null)) : h('div', { class: 'hint', style: 'padding:8px 12px' }, '没有匹配（只能搜索已入库的股票）')));
      } catch { /* 忽略搜索失败 */ }
    }
    async function quickAdd(q) {
      const r = await get('/search', { q: q.trim() }, { cache: false });
      if (r.items.length === 1) { await put('/watchlist', { add: [{ symbol: r.items[0].symbol }] }); await ctx.reloadWatch(); toast(`已添加 ${r.items[0].name}`, 'ok'); load(); }
    }
    function importDlg() {
      const ta = h('textarea', { rows: 8, style: 'width:100%', placeholder: state.market === 'US' ? '每行一个代码，可带备注：\nAAPL 核心仓\nNVDA' : '每行一个代码，可带备注：\n600519 核心仓\nsz.000001' });
      const file = h('input', { type: 'file', accept: '.csv,.txt,.tsv,.xlsx', onchange: async e => { const f = e.target.files[0]; if (!f) return; try { ta.value = await fileToText(f); } catch (err) { toast(err.message, 'bad'); } } });
      modal('批量导入自选（文本 / CSV / Excel）', h('div', {}, file, ta, h('p', { class: 'hint' }, '支持 600519 / sh.600519 / 600519.SH；未入库的代码会被忽略。')), [
        { label: '取消' }, { label: '导入', primary: true, onclick: async () => {
          const { items, errors } = parseWatchlist(ta.value, state.market);
          if (!items.length) { toast('没有可识别的代码', 'bad'); return false; }
          await put('/watchlist', { add: items }); await ctx.reloadWatch();
          toast(`已导入 ${items.length} 只` + (errors.length ? `，${errors.length} 行无法识别` : ''), errors.length ? '' : 'ok'); load();
        } }]);
    }

    await load();
  },
};
