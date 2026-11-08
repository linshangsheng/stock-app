// 软件升级（设置 → 软件升级）：检查 git 仓库里有没有新版本，一键升级。
// 升级时盖一层「进度环」：圆环一圈就是进度、中间是百分比，下面是 5 个步骤的状态；做完后端自己重启，页面等它回来自动刷新。
// 做法（后端 server/upgrade.py）：git fetch → 依赖变了先装依赖 → git merge --ff-only（只快进，不覆盖你的改动）→ 重启。
import { get, post } from './api.js';
import { h, clear, toast, confirmBox } from './util.js';
import { restartBackend } from './restart.js';

const sleep = ms => new Promise(r => setTimeout(r, ms));

/** 圆形进度环：一圈 = 100%，中间显示百分比。set(百分比, '' | 'ok' | 'bad')。 */
export function progressRing(size = 140) {
  const r = 58, c = 2 * Math.PI * r;
  const el = h('div', { class: 'up-ring', style: `width:${size}px;height:${size}px`, role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0', 'aria-label': '升级进度' });
  el.innerHTML = `<svg viewBox="0 0 140 140" width="${size}" height="${size}" aria-hidden="true"><circle class="trk" cx="70" cy="70" r="${r}"/><circle class="bar" cx="70" cy="70" r="${r}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${c.toFixed(1)}" transform="rotate(-90 70 70)"/></svg><div class="pct"><b>0</b><span>%</span></div>`;
  const bar = el.querySelector('.bar'), num = el.querySelector('.pct b');
  return { el, set(p, kind = '') {
    p = Math.max(0, Math.min(100, Math.round(p)));
    bar.setAttribute('stroke-dashoffset', (c * (1 - p / 100)).toFixed(1));
    num.textContent = String(p); el.setAttribute('aria-valuenow', String(p));
    el.dataset.kind = kind;
  } };
}

async function rawStatus() {                                   // 重启期间连不上是正常的：不走 api.js（它会弹「离线」提示）
  try {
    const r = await fetch('/api/upgrade/status?t=' + Date.now(), { cache: 'no-store' });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

const MARK = { todo: '○', doing: '', done: '✓', skip: '–', error: '✕' };
let running = false;

/** 开始升级并显示进度环。返回 true = 升级完成并已重启 / false = 没升级（已是最新、出错、取消）。 */
export async function runUpgrade() {
  if (running) return false;
  let force = false;
  for (;;) {
    try { await post('/upgrade/start', { force }, { market: false, timeout: 15000 }); break; }
    catch (e) {
      if (e.status === 409 && e.detail?.blocked) {
        const ok = await confirmBox(`现在有任务正在运行：${e.detail.blocked.join('；')}。升级完成后会重启软件，会中断它们（初始化可以接着续跑，回测要重新开始）。仍要升级吗？`);
        if (!ok) return false;
        force = true; continue;
      }
      if (e.status === 404 || e.status === 405) {                    // 后端是旧版：还没有升级接口，先重启一次让它加载新代码
        if (await confirmBox('正在运行的后端还是旧版本，没有升级功能。需要先重启一次后端（几秒钟，不丢数据），重启后再回来点升级。现在重启吗？')) restartBackend();
        return false;
      }
      toast('没能开始升级：' + e.message, 'bad');
      return false;
    }
  }
  running = true;
  const ring = progressRing(150);
  const title = h('div', { class: 'big' }, '正在升级…');
  const msg = h('div', { class: 'small muted up-msg' }, '准备中…');
  const steps = h('div', { class: 'up-steps' });
  const logBox = h('pre', { class: 'up-log' });
  const log = h('details', { class: 'up-details' }, h('summary', {}, '详细过程'), logBox);
  const action = h('div', { class: 'row mt', style: 'justify-content:center' });
  const ov = h('div', { class: 'restart-ov', role: 'alertdialog', 'aria-label': '正在升级' },
    h('div', { class: 'restart-card up-card' }, ring.el, title, msg, steps, log, action));
  document.body.append(ov);
  const t0 = Date.now();
  let pid0 = null, restartAt = null, lastS = null;
  const close = () => { ov.remove(); running = false; };
  const paint = s => {
    lastS = s;
    ring.set(s.percent, s.error ? 'bad' : '');
    msg.textContent = s.error ? '' : s.message || '';
    clear(steps).append(...s.steps.map(x => h('div', { class: 'up-step ' + x.state }, x.state === 'doing' ? h('span', { class: 'spinner' }) : h('i', {}, MARK[x.state]), h('span', {}, x.label))));
    logBox.textContent = (s.log || []).join('\n');
  };
  const finishBad = s => {
    title.textContent = '升级没有完成'; ring.set(s.percent, 'bad');
    msg.className = 'up-err'; msg.textContent = s.error;
    action.replaceChildren(h('button', { class: 'btn primary', onclick: close }, '知道了'));
    log.open = true;
  };
  try {
    for (;;) {
      await sleep(500);
      const s = await rawStatus();
      if (!s) {                                                    // 连不上：重启过程中的正常现象；一直连不上才报错
        if (restartAt) { ring.set(Math.min(99, 92 + (Date.now() - restartAt) / 1000 * 0.7)); msg.textContent = '后端正在重启，页面会在它回来后自动刷新…'; }
        if (Date.now() - (restartAt || t0) > 120000) {
          title.textContent = '后端没有回来'; ring.set(lastS?.percent ?? 0, 'bad');
          msg.className = 'up-err'; msg.textContent = '后端没有在预期时间内回来（可能启动失败）。请双击桌面的「顺势」重新打开；日志在软件目录 data\\server.log、data\\upgrade.log。';
          action.replaceChildren(h('button', { class: 'btn', onclick: close }, '关闭'));
          return false;
        }
        continue;
      }
      pid0 ??= s.pid;
      if (s.pid !== pid0) {                                        // 新进程：升级成功
        ring.set(100, 'ok'); title.textContent = '升级完成'; msg.textContent = '已重启，正在刷新页面…';
        clear(steps).append(...(lastS?.steps || []).map(x => h('div', { class: 'up-step done' }, h('i', {}, '✓'), h('span', {}, x.label))));
        await sleep(500); location.reload(); return true;
      }
      paint(s);
      if (s.error) { finishBad(s); return false; }
      if (s.restarting && !restartAt) { restartAt = Date.now(); title.textContent = '正在重启…'; }
      if (s.done && s.up_to_date) {
        ring.set(100, 'ok'); title.textContent = '已经是最新版本'; msg.textContent = '不需要升级。';
        action.replaceChildren(h('button', { class: 'btn primary', onclick: close }, '好的'));
        return false;
      }
    }
  } finally { if (!document.body.contains(ov)) running = false; }
}

/** 「设置 → 软件升级」里的卡片：当前版本、检查更新、新提交列表、立即升级。 */
export function upgradePanel() {
  const el = h('div', { class: 'card' });
  let alive = true;
  const body = h('div', {});
  const kv = (k, v) => h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, v));

  async function check() {
    clear(body).append(h('div', { class: 'row' }, h('span', { class: 'spinner' }), h('span', { class: 'muted small' }, ' 正在联网检查更新（要连一下 GitHub，几秒钟）…')));
    let about = null, r = null;
    try { about = await get('/about', {}, { market: false }); } catch { /* 只是少显示一行 */ }
    try { r = await get('/upgrade/check', {}, { market: false, cache: false, timeout: 120000 }); }
    catch (e) {
      if (!alive) return;
      clear(body).append(h('div', { class: 'alert warn' }, e.status === 404 ? '正在运行的后端还是旧版本，没有升级功能。先重启一次后端（点页面下方的「立即重启」，或下面的按钮），再回到这里。' : '检查失败：' + e.message),
        e.status === 404 ? h('div', { class: 'row mt' }, h('button', { class: 'btn primary', onclick: () => restartBackend() }, '立即重启后端')) : h('div', { class: 'row mt' }, h('button', { class: 'btn', onclick: check }, '重新检查')));
      return;
    }
    if (!alive) return;
    clear(body);
    const cur = r.current;
    body.append(h('div', { class: 'grid c4' }, kv('版本', about ? 'v' + about.version : '—'), kv('当前提交', cur ? `${cur.hash} · ${cur.date}` : '—'), kv('分支', r.branch || '—'), kv('上次检查', (r.checked_at || '').replace('T', ' ').slice(5, 16) || '—')));
    if (cur) body.append(h('p', { class: 'hint mt-s' }, '最近一次更新内容：' + cur.subject));
    if (!r.ok) {
      body.append(h('div', { class: 'alert bad mt', style: 'white-space:pre-wrap' }, r.error), h('div', { class: 'row mt' }, h('button', { class: 'btn', onclick: check }, '重新检查')));
      return;
    }
    if (r.up_to_date) {
      body.append(h('div', { class: 'row mt' }, h('span', { class: 'badge ok' }, '已是最新版本'), h('span', { class: 'hint' }, '远程仓库里没有比你这份更新的内容。')),
        h('div', { class: 'row mt' }, h('button', { class: 'btn', onclick: check }, '重新检查')));
      return;
    }
    body.append(h('div', { class: 'row mt' }, h('span', { class: 'badge accent' }, `发现 ${r.behind} 个新提交`), h('span', { class: 'hint' }, `涉及 ${r.changed_files} 个文件${r.requirements_changed ? '，包含依赖库变化（会自动安装）' : ''}`)),
      h('div', { class: 'up-commits' }, r.commits.map(c => h('div', { class: 'up-commit' }, h('span', { class: 'num faint' }, c.date.slice(5)), h('span', {}, c.subject)))),
      r.behind > r.commits.length ? h('p', { class: 'hint' }, `只列出最近 ${r.commits.length} 个。`) : null);
    if (!r.can_upgrade) {
      body.append(h('div', { class: 'alert warn mt', style: 'white-space:pre-wrap' }, r.reason || '现在不能自动升级。'), h('div', { class: 'row mt' }, h('button', { class: 'btn', onclick: check }, '重新检查')));
      return;
    }
    body.append(h('div', { class: 'row mt' }, h('button', { class: 'btn primary', onclick: async () => { await runUpgrade(); if (alive) check(); } }, '立即升级并重启'), h('button', { class: 'btn', onclick: check }, '重新检查')),
      h('p', { class: 'hint mt-s' }, '升级会下载新代码、自动重启软件（约 10~30 秒，页面会自己刷新）。你的数据（持仓、行情库、备份）不在升级范围内，不会动。'));
  }

  el.append(h('div', { class: 'card-title' }, h('h2', {}, '软件升级')),
    h('p', { class: 'small muted' }, '从软件的 git 仓库（GitHub）把新版本更新到这台电脑。只会「往前追上」远程的新内容：你本机改过、更新也要改的文件，或者本机有没推送的提交时，会停下来告诉你原因，不会覆盖任何东西。'),
    body);
  return { el, mount: check, destroy() { alive = false; } };
}
