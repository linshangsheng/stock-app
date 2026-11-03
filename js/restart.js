// 重启后端（更新提示条 / 数据健康面板里的按钮）：不用再去任务管理器结束 Python。
// 流程：请求 /api/restart（有任务在跑会先问一句）→ 盖一层「正在重启」→ 轮询 /api/ping，看到换了新的进程号就刷新页面。
// 后端那头的做法见 server/restart.py（新进程接班：等旧进程退出、端口空出来再启动）。
import { post } from './api.js';
import { h, confirmBox, toast, modal } from './util.js';

let running = false;

/** 后端是旧版（没有「重启」接口）时：告诉用户这一次怎么手动重启。以后后端是新版了，就不会再走到这里。 */
export function manualRestartHelp() {
  modal('这一次需要手动重启一次', h('div', {},
    h('p', {}, '你正在运行的后端是旧版本，还没有「自动重启」功能，所以这一次点按钮没用。重启成功之后，以后就可以直接点按钮了。'),
    h('ol', { style: 'line-height:1.9;padding-left:20px;margin:6px 0' },
      h('li', {}, '按 ', h('b', {}, 'Ctrl + Shift + Esc'), ' 打开任务管理器'),
      h('li', {}, '在列表里找到名字叫 ', h('b', {}, 'Python'), ' 的进程（图标是个蓝黄小蛇），右键 → ', h('b', {}, '结束任务')),
      h('li', {}, '双击桌面上的「', h('b', {}, 'Stock App'), '」重新打开软件')),
    h('p', { class: 'small muted' }, '只结束 Python 就行，不会丢任何数据（持仓、交易记录都存在文件里）。如果找不到，可以关掉软件窗口后再双击「Stock App」，它会自己判断。')),
    [{ label: '知道了', primary: true }]);
}

async function ping() {
  try {
    const r = await fetch('/api/ping?t=' + Date.now(), { cache: 'no-store' });
    return r.ok ? await r.json() : null;
  } catch { return null; }                       // 后端正在重启：连不上是正常的
}

export async function restartBackend() {
  if (running) return false;
  const before = await ping();
  let force = false;
  for (;;) {
    try { await post('/restart', { force }, { market: false, timeout: 15000 }); break; }
    catch (e) {
      if (e.status === 409 && e.detail?.blocked) {                          // 有任务在跑：重启会中断，先问清楚
        const ok = await confirmBox(`现在有任务正在运行：${e.detail.blocked.join('；')}。重启会中断它们（初始化可以接着续跑，回测要重新开始）。仍要重启吗？`);
        if (!ok) return false;
        force = true; continue;
      }
      if (e.status === 404 || e.status === 405) { manualRestartHelp(); return false; }           // 旧版后端没有这个接口
      toast('重启失败：' + e.message, 'bad');
      return false;
    }
  }
  running = true;
  const t0 = Date.now();
  const secs = h('b', {}, '0');
  const msg = h('div', { class: 'small muted mt-s' }, '页面会在后端回来之后自动刷新，通常 5~15 秒。');
  const action = h('div', { class: 'row mt', style: 'justify-content:center' });
  const ov = h('div', { class: 'restart-ov', role: 'alertdialog', 'aria-label': '正在重启后端' },
    h('div', { class: 'restart-card' }, h('span', { class: 'spinner' }), h('div', { class: 'big mt-s' }, '正在重启后端…'),
      h('div', { class: 'small muted' }, '已等 ', secs, ' 秒'), msg, action));
  document.body.append(ov);
  const tick = setInterval(() => { secs.textContent = String(Math.round((Date.now() - t0) / 1000)); }, 500);
  try {
    for (let i = 0; i < 150; i++) {                                         // 最多等约 90 秒
      await new Promise(r => setTimeout(r, 600));
      const p = await ping();
      if (p && (!before || p.pid !== before.pid)) {
        msg.textContent = '已重启，正在刷新页面…';
        await new Promise(r => setTimeout(r, 350));
        location.reload();
        return true;
      }
    }
    msg.textContent = '后端没有在预期时间内回来（可能换了端口，或启动失败）。请双击桌面的「Stock App」重新打开；日志在软件目录 data\\server.log。';
    action.append(h('button', { class: 'btn', onclick: () => ov.remove() }, '关闭这个提示'));
    return false;
  } finally { clearInterval(tick); running = false; }
}
