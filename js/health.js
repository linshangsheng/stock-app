// 数据健康面板（设置页最上面）：一眼分清「是数据的问题，还是策略的问题」。
// 顶部一个结论 → 逐条「发现了什么、怎么办」→ 可展开的明细（新鲜度 / 当日覆盖与缺失名单 / 数据源成功率 / 更新任务 / 本机）。
// 数据来自 /api/data/health（server/data_health.py）。只读；这里没有任何会改数据的操作，除了「重启后端」按钮。
import { get } from './api.js';
import { h, clear, fmtPct } from './util.js';
import { restartBackend, manualRestartHelp } from './restart.js';

const VERDICT = { ok: ['数据正常', 'ok'], warn: ['需要留意', 'warn'], bad: ['有问题', 'bad'] };
const ICON = { bad: '✗', warn: '!', info: 'i' };

function ago(ts) {
  if (!ts) return '从未';
  const t = new Date(ts.replace(' ', 'T')).getTime();
  if (!Number.isFinite(t)) return ts;
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (m < 1) return '刚刚';
  if (m < 60) return `${m} 分钟前`;
  if (m < 48 * 60) return `${Math.round(m / 60)} 小时前`;
  return `${Math.round(m / 1440)} 天前`;
}
const when = ts => (ts ? ts.replace('T', ' ').slice(0, 16) : '—');
const kv = (k, v, tip, cls = '') => h('div', { class: 'kv', title: tip || '' }, h('span', { class: 'k' }, k), h('span', { class: 'v num ' + cls }, v));

export function healthPanel() {
  const root = h('div', { class: 'card' });
  let timer = null, loading = false, last = null;

  async function load(manual = false) {
    if (loading) return;
    loading = true;
    try { last = await get('/data/health', {}, { cache: false, timeout: 60000 }); render(last); }
    catch (e) {
      if (manual || !last) {
        clear(root);
        root.append(h('div', { class: 'card-title' }, h('h2', {}, '数据健康')), e.status === 404
          ? h('div', { class: 'alert warn small' }, '后端是旧版本，还没有「数据健康」功能：重启一次后端就有了。', h('button', { class: 'btn sm primary', style: 'margin-left:8px', onclick: () => manualRestartHelp() }, '怎么重启'))
          : h('div', { class: 'alert bad' }, '读取失败：' + e.message));
      }
    }
    finally { loading = false; }
  }

  function render(d) {
    clear(root);
    const [vt, vc] = VERDICT[d.status] || VERDICT.warn;
    root.append(h('div', { class: 'card-title' }, h('h2', {}, '数据健康'), h('span', { class: 'row', style: 'gap:8px' },
      h('span', { class: 'badge ' + vc }, vt), h('span', { class: 'hint' }, '更新于 ' + (d.generated_at || '').slice(11, 19)),
      h('button', { class: 'btn sm', onclick: () => load(true) }, '刷新'))));
    root.append(h('div', { class: 'alert ' + (d.status === 'ok' ? 'ok' : d.status) + ' small' }, d.headline));
    const issues = d.issues || [];
    if (issues.length) {
      root.append(h('div', { class: 'hl-issues mt-s' }, issues.map(i => h('div', { class: 'hl-issue ' + i.level },
        h('span', { class: 'hl-ico' }, ICON[i.level] || 'i'), h('div', {}, h('div', { class: 'hl-t' }, i.title), i.hint ? h('div', { class: 'small muted' }, i.hint) : null)))));
    }
    if (!d.has_data) { renderProcess(d); return; }
    renderFresh(d); renderCoverage(d); renderSources(d); renderJobs(d); renderProcess(d);
    root.append(h('p', { class: 'hint mt-s' }, '这里只检查「数据本身」（新不新、全不全、对不对）。数据没问题、结果却不如预期，才轮到去怀疑策略。成功率统计的是经过限速层的真实请求，保存在 data\\source_stats.json，重启后不清零，只保留最近 14 天。'));
  }

  const section = (title, summary, open, ...kids) => root.append(h('details', { class: 'mt', open: open ? true : null }, h('summary', {}, h('b', {}, title), summary ? h('span', { class: 'muted' }, '　' + summary) : null), ...kids));

  function renderFresh(d) {
    const f = d.freshness, behind = f.behind_days;
    section('新鲜度', `行情截至 ${f.data_asof || '—'}${behind ? `，落后 ${behind} 个交易日` : '，是最新的'}`, behind >= 2,
      h('div', { class: 'grid c4 mt-s' },
        kv('行情数据截止', f.data_asof || '—'), kv('最近已收盘交易日', f.last_closed || '—'),
        kv('落后', behind == null ? '—' : behind === 0 ? '没有落后' : `${behind} 个交易日`, '数据截止日到最近已收盘交易日之间隔了几个交易日', behind >= 2 ? 'hl-bad' : ''),
        kv('交易日历', f.calendar_source === 'fallback' ? '兜底（节假日不识别）' : f.calendar_source === 'xshg' ? '上交所日历（离线）' : f.calendar_source === 'source' ? '数据源' : '—', '', f.calendar_source === 'fallback' ? 'hl-bad' : ''),
        kv('行业映射更新于', f.industry_map_asof || '—')),
      h('div', { class: 'small mt-s' }, h('span', { class: 'muted' }, '基准指数（市场环境判断要用）：'), (f.benchmarks || []).map(b =>
        h('span', { class: 'badge ' + (b.lag || !b.last ? 'warn' : 'ok'), style: 'margin-right:6px', title: b.last ? '最新 ' + b.last : '没有数据' }, b.symbol + (b.lag ? ' 落后' : '')))));
  }

  function renderCoverage(d) {
    const c = d.coverage, g = d.gate;
    const pct = c.rate == null ? 0 : c.rate * 100;
    section('当日覆盖与完整性闸门', c.rate == null ? '' : `覆盖 ${(c.rate * 100).toFixed(1)}%（${c.have}/${c.expected}），缺 ${c.missing} 只`, c.missing > 0 && (c.rate ?? 1) < 0.995 || g.status !== 'PASS',
      h('div', { class: 'mt-s' }, h('div', { class: 'row between small' }, h('span', {}, '数据截止日有行情的股票'), h('span', { class: 'num' }, `${c.have} / ${c.expected}　（停牌 ${c.suspended} 只，停牌也有一根成交量为 0 的 K 线，算有数据）`)),
        h('div', { class: 'progress mt-s' }, h('div', { style: `width:${Math.min(100, pct).toFixed(1)}%` }))),
      c.recent?.length ? h('div', { class: 'mt-s' }, h('div', { class: 'small' }, h('b', {}, `当日没拿到的（最近 20 个交易日内还有过数据）${c.recent_total} 只`), c.recent_total > c.recent.length ? `，显示前 ${c.recent.length} 只` : ''),
        h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl small' }, h('thead', {}, h('tr', {}, ['名称', '代码', '最后一根 K 线', '落后（交易日）'].map((t, i) => h('th', { class: i > 1 ? 'r' : '' }, t)))),
          h('tbody', {}, c.recent.map(x => h('tr', {}, h('td', {}, x.name), h('td', { class: 'num muted' }, x.symbol), h('td', { class: 'num r' }, x.last || '—'), h('td', { class: 'num r' }, String(x.gap)))))))) : null,
      c.long_stale ? h('p', { class: 'hint mt-s' }, `另有 ${c.long_stale} 只已经很久（20 个交易日以上）没有数据：多半是退市、长期停牌，或上游已不提供，不算当日缺失。`) : null,
      h('div', { class: 'small mt', style: 'font-weight:600' }, `完整性闸门（${g.day}）：${g.status === 'PASS' ? '通过' : '没通过'}`),
      h('table', { class: 'tbl small' }, h('tbody', {}, (g.checks || []).map(x => h('tr', {}, h('td', { style: 'width:20px' }, x.ok ? '✓' : x.hard ? '✗' : '!'), h('td', {}, CHECK[x.name] || x.name), h('td', { class: 'small muted' }, x.msg))))),
      g.bad_symbols?.length ? h('p', { class: 'hint' }, '当日价格 / 涨跌幅异常、已排除出扫描的股票：' + g.bad_symbols.join('、')) : null);
  }
  const CHECK = { coverage: '当日覆盖率', benchmark: '基准指数', price_sanity: '价格合理性', continuity: '日期连续性', snapshot_recon: '快照对账', unit_drift: '成交额量级' };

  function renderSources(d) {
    const mine = d.sources.filter(s => s.mine), other = d.sources.filter(s => !s.mine);
    const row = s => {
      const used = s.ok7 + s.err7;
      const rate = s.rate7;
      return h('tr', { class: s.mine ? '' : 'dim' },
        h('td', {}, h('div', {}, s.label), h('div', { class: 'tiny muted' }, s.use)),
        h('td', {}, s.breaker_open ? h('span', { class: 'badge bad', title: '连续失败后自动暂停请求，避免被封' }, `熔断 ${s.breaker_remaining_s}s`)
          : used === 0 ? h('span', { class: 'badge' }, '没用过') : h('span', { class: 'badge ' + (rate >= 0.9 ? 'ok' : rate >= 0.7 ? 'warn' : 'bad') }, rate >= 0.9 ? '正常' : rate >= 0.7 ? '偶有失败' : '经常失败')),
        h('td', { class: 'num r', title: `近 7 天：成功 ${s.ok7} 次，失败 ${s.err7} 次` }, used === 0 ? '—' : h('span', {}, h('b', {}, fmtPct(rate, 0, false)), h('span', { class: 'muted' }, `　${s.ok7}/${used}`))),
        h('td', { class: 'num r muted' }, used === 0 ? '—' : `${s.today[0]} / ${s.today[1]}`),
        h('td', { class: 'small muted', title: s.last_ok || '' }, ago(s.last_ok)),
        h('td', { class: 'small', style: 'max-width:260px' }, s.errors?.length ? h('span', { class: 'muted', title: s.errors.map(e => `${when(e.at)}  ${e.msg}`).join('\n') }, `${ago(s.errors[0].at)}：${s.errors[0].msg.slice(0, 50)}`) : h('span', { class: 'muted' }, '—')));
    };
    const table = rows => h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl small' }, h('thead', {}, h('tr', {}, ['数据源', '状态', '近 7 天成功率', '今天 成功/失败', '上次成功', '最近一次报错'].map((t, i) => h('th', { class: i === 2 || i === 3 ? 'r' : '' }, t)))), h('tbody', {}, rows.map(row))));
    const bad = mine.filter(s => s.breaker_open || (s.rate7 != null && s.rate7 < 0.7)).length;
    section('数据源', bad ? `${bad} 个有问题` : '都正常', bad > 0, table(mine), other.length ? h('details', { class: 'mt-s' }, h('summary', {}, '另一个市场的数据源'), table(other)) : null,
      h('p', { class: 'hint mt-s' }, '软件会按顺序自动换用能连上的数据源（A 股：BaoStock → 腾讯 → 东方财富）。「检测 A 股数据源」按钮在下面「数据与更新状态」里，会实际连一次试试。'));
  }

  function renderJobs(d) {
    const j = d.jobs;
    section('更新任务', j.last_ok ? `上次成功更新：${ago(j.last_ok.ts)}` : '还没有成功更新的记录', !!j.last_error && (!j.last_ok || j.last_ok.ts < j.last_error.ts),
      h('div', { class: 'grid c4 mt-s' },
        kv('上次成功', j.last_ok ? when(j.last_ok.ts) : '—', j.last_ok?.detail || ''), kv('上次失败', j.last_error ? when(j.last_error.ts) : '无', j.last_error?.detail || '', j.last_error ? 'hl-bad' : ''),
        kv('自动更新', d.scheduler_on ? '开着（软件开着时会自动更新）' : '没开'), kv('现在', j.running ? `正在运行：${j.names[j.running] || j.running}` : '空闲')),
      j.last_scan ? h('div', { class: 'small mt-s muted' }, `最近一次选股扫描：${j.last_scan.scan_date}，数据闸门 ${j.last_scan.data_gate_status || '—'}`) : null,
      j.recent?.length ? h('div', { class: 'small mt-s' }, j.recent.map(l => h('div', { class: 'num ' + (l.tone === 'bad' ? 'hl-bad' : 'muted') },
        `${when(l.ts)}  ${j.names[l.job] || l.job}  ${l.text}${l.n > 1 ? ` ×${l.n}${l.n_min ? '+' : ''}（最早 ${when(l.first).slice(11)}）` : ''}  ${l.detail || ''}`))) : null);
  }

  function renderProcess(d) {
    const p = d.process, bk = d.backup;
    const upt = p.uptime_hours == null ? '—' : p.uptime_hours < 1 ? `${Math.round(p.uptime_hours * 60)} 分钟` : p.uptime_hours < 48 ? `${p.uptime_hours.toFixed(1)} 小时` : `${Math.round(p.uptime_hours / 24)} 天`;
    section('本机：备份、磁盘、后端', `后端内存 ${p.rss_mb ?? '—'} MB${p.needs_restart ? '，需要重启' : ''}`, p.needs_restart,
      h('div', { class: 'grid c4 mt-s' },
        bk ? kv('最近备份', bk.last ? `${bk.last}（${bk.age_days === 0 ? '今天' : bk.age_days + ' 天前'}）` : '没有', `共 ${bk.count} 份；目录 ${bk.dir}`, bk.stale ? 'hl-bad' : '') : null,
        kv('磁盘剩余', d.disk_free_gb == null ? '—' : d.disk_free_gb + ' GB', '', d.disk_free_gb != null && d.disk_free_gb < 2 ? 'hl-bad' : ''),
        kv('后端内存', p.rss_mb == null ? '—' : p.rss_mb + ' MB', '常驻内存；全市场面板约 2 GB 是正常的'), kv('后端已运行', upt, `进程号 ${p.pid}，启动于 ${p.started || '—'}`),
        kv('版本', p.version + ' · Python ' + p.python)),
      p.needs_restart ? h('div', { class: 'alert warn small mt-s' }, '磁盘上的软件文件比正在运行的后端新：需要重启后端才生效。') : null,
      h('div', { class: 'row wrap mt-s' }, h('button', { class: 'btn' + (p.needs_restart ? ' primary' : ''), onclick: () => restartBackend(), title: '后端自己退出再启动，约 5~15 秒；页面会自动刷新' }, '重启后端'),
        h('span', { class: 'hint' }, '改了设置、更新了软件文件、或后端卡住时用；有任务正在运行时会先问你。')));
  }

  return {
    el: root,
    mount() { root.append(h('div', { class: 'card-title' }, h('h2', {}, '数据健康')), h('div', { class: 'hint' }, h('span', { class: 'spinner' }), ' 检查中…')); load(true); timer = setInterval(() => { if (!document.hidden && document.body.contains(root)) load(); }, 30000); },
    destroy() { clearInterval(timer); },
  };
}
