// 资讯页（M6，4.5.5）：统一事件流（Event 模型）——公告 / 新闻 / Insider / 财报日历。
// 标注来源可信度等级（1 官方原始信息 / 2 结构化金融数据 / 3 媒体）与 event_time / publish_time；
// 事件之后 1 / 3 / 5 日的价格表现用于判断消息是否真有交易价值（3.18）。事件只对「候选 + 持仓」按需拉取并缓存 6 小时。
import { get, post, state } from './api.js';
import { h, clear, fmtPct, dirClass, code, toast } from './util.js';
import { prefs } from './db.js';

const LEVEL = { 1: ['官方', 'lvl1', '等级 1：官方原始信息（公司公告 / SEC）'], 2: ['结构化', 'lvl2', '等级 2：结构化金融数据'], 3: ['媒体', 'lvl3', '等级 3：媒体新闻，仅补充背景'] };

export const newsView = {
  layout: 'page',
  async mount(ctx) {
    const el = ctx.pageEl; clear(el);
    const S = Object.assign({ scope: 'candidates', type: '', level: '', days: 30 }, prefs.get('news.filter', {}));
    const list = h('div', {});
    const statsBox = h('details', { class: 'card soft mb' }, h('summary', {}, '事件之后的价格表现（事件研究，3.18）'), h('div', { id: 'ev-stats' }, h('span', { class: 'hint' }, '展开后加载…')));
    statsBox.addEventListener('toggle', () => { if (statsBox.open) loadStats(); });
    const sel = (k, opts) => h('select', { onchange: e => { S[k] = e.target.value; prefs.set('news.filter', S); load(); } }, opts.map(([v, l]) => h('option', { value: v, selected: String(S[k]) === String(v) ? true : null }, l)));
    const head = h('div', { class: 'row wrap mb' },
      h('label', { class: 'f' }, '范围', sel('scope', [['candidates', '今日候选'], ['held', '持仓'], ['watch', '自选'], ['all', '全部已入库']])),
      h('label', { class: 'f' }, '类型', sel('type', [['', '全部'], ['EARNINGS', '财报 / 业绩'], ['DIVIDEND', '分红'], ['BUYBACK', '回购'], ['INSIDER', '增减持 / Insider'], ['M_AND_A', '并购重组'], ['CONTRACT', '重大合同'], ['REGULATORY', '监管'], ['NEWS', '新闻'], ['OTHER', '其他']])),
      h('label', { class: 'f' }, '可信度', sel('level', [['', '全部'], ['1', '仅官方（等级 1）'], ['2', '官方 + 结构化']])),
      h('label', { class: 'f' }, '时间范围', sel('days', [[7, '近 7 天'], [30, '近 30 天'], [90, '近 90 天'], [365, '近一年']])),
      h('div', { class: 'grow' }),
      h('button', { class: 'btn primary', id: 'ev-refresh', onclick: refresh }, '刷新事件（拉取最新）'));
    el.append(h('div', { class: 'pane-body page' }, h('div', { class: 'row between mb' }, h('h1', {}, '资讯 · 事件流'), h('span', { class: 'hint' }, state.market === 'CN' ? '来源：巨潮资讯公告（官方）+ BaoStock 业绩预告 / 快报 / 分红' : '来源：SEC EDGAR（官方）+ Yahoo Finance 新闻 / Insider')),
      head, statsBox, list, h('p', { class: 'hint mt' }, '消息本身不等于利好 / 利空：右侧的事件后 1 / 3 / 5 日收益是该股票实际发生的价格表现（基准 = 事件当日收盘），样本很少时不要下结论。公告类分类按标题关键词判定，仅供筛选。')));

    async function refresh() {
      const b = document.getElementById('ev-refresh'); b.disabled = true; b.textContent = '拉取中…';
      try {
        const scope = S.scope === 'all' ? 'candidates' : S.scope;
        const r = await post('/events/refresh', { scope, force: true }, { timeout: 180000 });
        toast(`已处理 ${r.ok} 只，新增 ${r.new_events} 条事件${r.failed ? `，${r.failed} 只失败（上游不可用）` : ''}`, r.failed ? 'bad' : 'ok');
      } catch (e) { toast(e.message, 'bad'); }
      b.disabled = false; b.textContent = '刷新事件（拉取最新）'; load();
    }

    async function loadStats() {
      const box = document.getElementById('ev-stats'); clear(box); box.append(h('span', { class: 'spinner' }));
      let r; try { r = await get('/events/stats', { scope: S.scope === 'all' ? '' : S.scope, days: Math.max(+S.days, 365) }); } catch (e) { clear(box); box.append(h('div', { class: 'alert bad' }, e.message)); return; }
      clear(box);
      if (!r.groups.length) { box.append(h('div', { class: 'hint' }, '暂无足够的已入库事件。')); return; }
      const T = { EARNINGS: '财报 / 业绩', DIVIDEND: '分红', BUYBACK: '回购', INSIDER: '增减持 / Insider', M_AND_A: '并购重组', CONTRACT: '重大合同', REGULATORY: '监管', NEWS: '新闻', OTHER: '其他', ANALYST: '分析师' };
      box.append(h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['事件类型', '倾向', '样本', '1日均值', '1日上涨', '3日均值', '5日均值', '5日上涨'].map((t, i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
        h('tbody', {}, r.groups.map(g => h('tr', { style: g.enough ? '' : 'opacity:.6' }, h('td', {}, T[g.event_type] || g.event_type), h('td', { class: 'small' }, g.tone), h('td', { class: 'num' }, g.n, g.enough ? '' : ' ⚠'),
          h('td', { class: 'num ' + dirClass(g.mean_1d) }, fmtPct(g.mean_1d)), h('td', { class: 'num' }, g.up_1d != null ? fmtPct(g.up_1d, 0, false) : '—'), h('td', { class: 'num ' + dirClass(g.mean_3d) }, fmtPct(g.mean_3d)),
          h('td', { class: 'num ' + dirClass(g.mean_5d) }, fmtPct(g.mean_5d)), h('td', { class: 'num' }, g.up_5d != null ? fmtPct(g.up_5d, 0, false) : '—')))))));
      box.append(h('p', { class: 'hint mt-s' }, `⚠ 样本 < ${r.min_n}：仅供参考，不下结论。${r.note}`));
    }

    async function load() {
      clear(list); list.append(h('div', { class: 'empty' }, h('span', { class: 'spinner' })));
      let r;
      try { r = await get('/events', { scope: S.scope === 'all' ? '' : S.scope, type: S.type, min_level: S.level, days: S.days, limit: 300 }); } catch (e) { clear(list); list.append(h('div', { class: 'alert bad' }, e.message)); return; }
      clear(list);
      if (!r.items.length) {
        list.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '暂无事件'), h('div', {}, '事件只对候选与持仓按需拉取。点右上角「刷新事件」，或先运行盘后任务（会自动为候选 + 持仓拉取）。')));
        return;
      }
      let day = '';
      for (const e of r.items) {
        if (e.event_time !== day) { day = e.event_time; list.append(h('div', { class: 'group-h', style: 'margin:10px 0 0;border:0;border-radius:6px' }, h('span', {}, day), h('span', {}, ''))); }
        const [ll, lc, lt] = LEVEL[e.level] || ['—', 'lvl3', ''];
        const rets = ['1d', '3d', '5d'].map(k => e['ret_' + k] != null ? h('span', { class: 'num ' + dirClass(e['ret_' + k]) }, `${k} ${fmtPct(e['ret_' + k])}`) : null);
        list.append(h('div', { class: 'ev-row' },
          h('div', { class: 'when' }, (e.publish_time || '').slice(11, 16) || e.event_time.slice(5)),
          h('div', {}, h('div', { class: 'ttl' }, e.url ? h('a', { href: e.url, target: '_blank', rel: 'noopener noreferrer' }, e.title) : e.title,
            e.sentiment != null ? h('span', { class: 'sent ' + (e.sentiment > 0 ? 'pos' : 'neg'), title: '情绪：' + e.sentiment, style: 'margin-left:6px' }) : null),
            e.summary ? h('div', { class: 'small muted' }, e.summary.slice(0, 160)) : null,
            h('div', { class: 'meta' }, h('a', { href: '#/screener/' + encodeURIComponent(e.symbol) }, `${e.name || e.symbol} ${code(e.symbol)}`),
              h('span', { class: 'badge' }, r.types[e.event_type] || e.event_type), h('span', { class: 'badge ' + lc, title: lt }, ll), h('span', {}, e.source),
              e.calendar ? h('span', { class: 'badge warn' }, '财报日历') : null, ...rets.filter(Boolean)))));
      }
    }
    load();
  },
};
