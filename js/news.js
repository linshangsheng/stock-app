// 资讯（M6，4.5.5）：统一事件流（Event 模型）——公告 / 新闻 / Insider / 财报日历。现在是「选股」页里的一个标签（以前是单独的页面）。
// 左栏：筛选 + 事件列表（点一条，右边看这只股票的详情）；右栏（没点股票时）：事件之后的价格表现（事件研究，3.18）。
// 标注来源可信度等级（1 官方原始信息 / 2 结构化金融数据 / 3 媒体）与 event_time / publish_time；
// 事件之后 1 / 3 / 5 日的价格表现用于判断消息是否真有交易价值。事件只对「候选 + 持仓」按需拉取并缓存 6 小时。
import { get, post, state } from './api.js';
import { h, clear, fmtPct, dirClass, code, toast } from './util.js';
import { prefs } from './db.js';

const LEVEL = { 1: ['官方', 'lvl1', '等级 1：官方原始信息（公司公告 / SEC）'], 2: ['结构化', 'lvl2', '等级 2：结构化金融数据'], 3: ['媒体', 'lvl3', '等级 3：媒体新闻，仅补充背景'] };
const TYPES = { EARNINGS: '财报 / 业绩', DIVIDEND: '分红', BUYBACK: '回购', INSIDER: '增减持 / Insider', M_AND_A: '并购重组', CONTRACT: '重大合同', REGULATORY: '监管', NEWS: '新闻', OTHER: '其他', ANALYST: '分析师' };
const SOURCE_NOTE = () => (state.market === 'CN' ? '来源：巨潮资讯公告（官方）+ BaoStock 业绩预告 / 快报 / 分红' : '来源：SEC EDGAR（官方）+ Yahoo Finance 新闻 / Insider');

/** 事件之后的价格表现表（右栏用；窄屏放在左栏列表上方的折叠框里）。S = 当前筛选（范围 / 时间）。 */
export function newsStats(S) {
  const box = h('div', {}, h('span', { class: 'hint' }, '加载中…'));
  async function load() {
    clear(box); box.append(h('span', { class: 'spinner' }));
    let r;
    try { r = await get('/events/stats', { scope: S.scope === 'all' ? '' : S.scope, days: Math.max(+S.days, 365) }); } catch (e) { clear(box); box.append(h('div', { class: 'alert bad' }, e.message)); return; }
    clear(box);
    if (!r.groups.length) { box.append(h('div', { class: 'hint' }, '暂无足够的已入库事件。')); return; }
    box.append(h('div', { class: 'tbl-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, ['事件类型', '倾向', '样本', '1日均值', '1日上涨', '3日均值', '5日均值', '5日上涨'].map((t, i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
      h('tbody', {}, r.groups.map(g => h('tr', { style: g.enough ? '' : 'opacity:.6' }, h('td', {}, TYPES[g.event_type] || g.event_type), h('td', { class: 'small' }, g.tone), h('td', { class: 'num' }, g.n, g.enough ? '' : ' ⚠'),
        h('td', { class: 'num ' + dirClass(g.mean_1d) }, fmtPct(g.mean_1d)), h('td', { class: 'num' }, g.up_1d != null ? fmtPct(g.up_1d, 0, false) : '—'), h('td', { class: 'num ' + dirClass(g.mean_3d) }, fmtPct(g.mean_3d)),
        h('td', { class: 'num ' + dirClass(g.mean_5d) }, fmtPct(g.mean_5d)), h('td', { class: 'num' }, g.up_5d != null ? fmtPct(g.up_5d, 0, false) : '—')))))));
    box.append(h('p', { class: 'hint mt-s' }, `⚠ 样本 < ${r.min_n}：仅供参考，不下结论。${r.note}`));
  }
  return { el: box, load };
}

/** 右栏（宽屏、没点开股票时）：资讯标签的说明 + 事件之后的价格表现。返回 { el, init }，init 在挂到页面后调用。 */
export function newsRight(tab) {
  const st = newsStats(tab.S);
  const el = h('div', {},
    h('p', { class: 'hint mb' }, SOURCE_NOTE()),
    h('div', { class: 'card soft' }, h('div', { class: 'card-title' }, h('h3', {}, '事件之后的价格表现（事件研究，3.18）'), h('span', { class: 'hint' }, `范围：${tab.scopeName()}`)), st.el),
    h('p', { class: 'hint mt' }, '消息本身不等于利好 / 利空：上表是这类事件发生后，这些股票实际的价格表现（基准 = 事件当日收盘），样本很少时不要下结论。'
      + '左边每条事件末尾的 1d / 3d / 5d 是那一条之后的实际涨跌。公告类分类按标题关键词判定，仅供筛选。点左边的一条，这里换成那只股票的详情。'));
  return { el, init: st.load, refresh: st.load, destroy() {} };
}

/**
 * 资讯标签（放在选股页左栏）。
 *  onPick(symbol)  点一条事件：右边显示这只股票的详情
 *  onFilter()      筛选条件变了（选股页据此刷新右栏的统计）
 * 返回 { el, S, scopeName, destroy }
 */
export function newsTab({ onPick, onFilter }) {
  const S = Object.assign({ scope: 'candidates', type: '', level: '', days: 30 }, prefs.get('news.filter', {}));
  const SCOPES = [['candidates', '今日候选'], ['held', '持仓'], ['watch', '自选'], ['all', '全部已入库']];
  let dead = false;
  const list = h('div', {});
  const narrow = !matchMedia('(min-width: 900px)').matches;
  const sel = (k, opts, label) => h('label', { class: 'f' }, label, h('select', { onchange: e => { S[k] = e.target.value; prefs.set('news.filter', S); load(); onFilter?.(); } },
    opts.map(([v, l]) => h('option', { value: v, selected: String(S[k]) === String(v) ? true : null }, l))));
  const refreshBtn = h('button', { class: 'btn sm primary', title: '到上游拉取这个范围里股票的最新公告 / 新闻', onclick: refresh }, '刷新事件');
  const statsBox = narrow ? newsStats(S) : null;
  const head = h('div', { style: 'padding:10px 14px 4px' },
    h('div', { class: 'row wrap', style: 'gap:8px' },
      sel('scope', SCOPES, '范围'),
      sel('type', [['', '全部'], ...Object.entries(TYPES).filter(([k]) => k !== 'ANALYST')], '类型'),
      h('div', { class: 'grow' }), refreshBtn),
    h('details', { class: 'mt-s' }, h('summary', { class: 'small muted' }, '更多筛选'),
      h('div', { class: 'row wrap mt-s', style: 'gap:8px' },
        sel('level', [['', '全部'], ['1', '仅官方（等级 1）'], ['2', '官方 + 结构化']], '可信度'),
        sel('days', [[7, '近 7 天'], [30, '近 30 天'], [90, '近 90 天'], [365, '近一年']], '时间范围'))),
    h('div', { class: 'hint mt-s' }, SOURCE_NOTE()),
    statsBox ? h('details', { class: 'card soft mt-s', style: 'padding:8px 12px' }, h('summary', {}, '事件之后的价格表现'), statsBox.el) : null);
  statsBox?.el.parentElement?.addEventListener('toggle', e => { if (e.target.open) statsBox.load(); }, true);
  const el = h('div', {}, head, list);

  async function refresh() {
    refreshBtn.disabled = true; refreshBtn.textContent = '拉取中…';
    try {
      const scope = S.scope === 'all' ? 'candidates' : S.scope;
      const r = await post('/events/refresh', { scope, force: true }, { timeout: 180000 });
      toast(`已处理 ${r.ok} 只，新增 ${r.new_events} 条事件${r.failed ? `，${r.failed} 只失败（上游不可用）` : ''}`, r.failed ? 'bad' : 'ok');
    } catch (e) { toast(e.message, 'bad'); }
    refreshBtn.disabled = false; refreshBtn.textContent = '刷新事件';
    if (!dead) { load(); onFilter?.(); }
  }

  async function load() {
    clear(list); list.append(h('div', { class: 'empty' }, h('span', { class: 'spinner' })));
    let r;
    try { r = await get('/events', { scope: S.scope === 'all' ? '' : S.scope, type: S.type, min_level: S.level, days: S.days, limit: 300 }); } catch (e) { if (!dead) { clear(list); list.append(h('div', { class: 'alert bad', style: 'margin:10px 14px' }, e.message)); } return; }
    if (dead) return;
    clear(list);
    if (!r.items.length) {
      list.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '暂无事件'), h('div', {}, '事件只对候选与持仓按需拉取。点上面的「刷新事件」，或先运行盘后任务（会自动为候选 + 持仓拉取）。')));
      return;
    }
    let day = '';
    for (const e of r.items) {
      if (e.event_time !== day) { day = e.event_time; list.append(h('div', { class: 'group-h' }, h('span', {}, day), h('span', {}, ''))); }
      const [ll, lc, lt] = LEVEL[e.level] || ['—', 'lvl3', ''];
      const rets = ['1d', '3d', '5d'].map(k => (e['ret_' + k] != null ? h('span', { class: 'num ' + dirClass(e['ret_' + k]) }, `${k} ${fmtPct(e['ret_' + k])}`) : null));
      list.append(h('div', { class: 'list-item ev-item', role: 'button', tabindex: '0', dataset: { symbol: e.symbol }, onclick: () => onPick(e.symbol),
        onkeydown: ev => { if (ev.key === 'Enter') onPick(e.symbol); } },
      h('div', { class: 't1' }, h('span', { class: 'name' }, e.name || e.symbol), h('span', { class: 'code num' }, code(e.symbol)), h('span', { class: 'grow' }),
        h('span', { class: 'small muted num' }, (e.publish_time || '').slice(11, 16) || e.event_time.slice(5))),
      h('div', { class: 'ev-ttl' }, e.title, e.sentiment != null ? h('span', { class: 'sent ' + (e.sentiment > 0 ? 'pos' : 'neg'), title: '情绪：' + e.sentiment, style: 'margin-left:6px' }) : null),
      e.summary ? h('div', { class: 'small muted ev-sum' }, e.summary.slice(0, 120)) : null,
      h('div', { class: 't2' }, h('span', { class: 'badge' }, r.types[e.event_type] || e.event_type), h('span', { class: 'badge ' + lc, title: lt }, ll), h('span', {}, e.source),
        e.calendar ? h('span', { class: 'badge warn' }, '财报日历') : null, ...rets.filter(Boolean),
        e.url ? h('a', { href: e.url, target: '_blank', rel: 'noopener noreferrer', onclick: ev => ev.stopPropagation(), title: '在浏览器里打开原文' }, '原文 ↗') : null)));
    }
  }
  load();
  return { el, S, scopeName: () => SCOPES.find(([v]) => v === S.scope)?.[1] || '', destroy() { dead = true; } };
}
