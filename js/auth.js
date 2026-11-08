// 登录 / 退出 / 强制改密码（后端 server/accounts.py）。
//   · 启动时 ensureLogin()：已登录（或本机预览没开登录）直接继续，否则盖一层登录页，登录成功才继续。
//   · 一个账号同一时间只有一处登录：在别处登录会把这里顶下线，下一次请求收到 401（code=kicked），回到登录页并说明原因。
//   · 登录凭证是服务端设的 HttpOnly Cookie，页面里的脚本读不到，也不会放进网址。
import { state } from './api.js';
import { h, toast } from './util.js';
import { cacheClear, prefs } from './db.js';

export const isAdmin = () => state.user?.role === 'admin';
export const applyRole = () => { document.documentElement.dataset.role = state.user?.role || 'user'; };   // CSS：html[data-role=user] .admin-only { display:none }

async function jfetch(path, body) {
  const r = await fetch('/api' + path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store' });
  let data = {}; try { data = await r.json(); } catch { /* 非 JSON */ }
  return { ok: r.ok, status: r.status, data };
}

/** 换账号 / 退出时清掉上一个人留在本机的缓存（展示缓存和 Service Worker 里的接口缓存里有他的持仓和自选）。 */
export async function clearLocalCaches() {
  await cacheClear();
  try { for (const k of await caches.keys()) if (k.startsWith('stock-data')) await caches.delete(k); } catch { /* 没有 Cache API */ }
}

const screen = (...kids) => { document.getElementById('login-root')?.remove(); const el = h('div', { id: 'login-root', class: 'login-wrap' }, ...kids); document.body.append(el); return el; };
const brand = () => h('div', { class: 'login-brand' }, h('img', { src: 'icon.svg', width: 56, height: 56, alt: '' }), h('b', {}, '顺势'), h('small', {}, 'TREND · SWING'));
export const loginShowing = () => !!document.getElementById('login-root');

/** 登录页。登录成功后 resolve（页面还没刷新，由调用方决定接着做什么）。 */
export function loginScreen(message = '') {
  return new Promise(resolve => {
    const err = h('div', { class: 'alert bad login-err', role: 'alert', hidden: message ? null : true }, message);
    const u = h('input', { type: 'text', name: 'username', autocomplete: 'username', placeholder: '用户名', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', value: prefs.get('lastUser', '') });
    const p = h('input', { type: 'password', name: 'password', autocomplete: 'current-password', placeholder: '密码' });
    const btn = h('button', { class: 'btn primary', type: 'submit' }, '登录');
    const show = t => { err.textContent = t; err.hidden = !t; };
    const form = h('form', { class: 'login-card', onsubmit: async e => {
      e.preventDefault();
      if (!u.value.trim() || !p.value) { show('请输入用户名和密码'); return; }
      btn.disabled = true; btn.textContent = '登录中…'; show('');
      let r;
      try { r = await jfetch('/auth/login', { username: u.value.trim(), password: p.value }); }
      catch { show('连不上后端，请确认软件在运行'); btn.disabled = false; btn.textContent = '登录'; return; }
      if (!r.ok) { show(r.data.detail || '登录失败'); btn.disabled = false; btn.textContent = '登录'; p.value = ''; p.focus(); return; }
      prefs.set('lastUser', u.value.trim());
      await clearLocalCaches();
      document.getElementById('login-root')?.remove();
      if (r.data.kicked) toast('这个账号在另一台设备上的登录已被挤下线（同一账号同一时间只能一处登录）', 'ok');
      resolve(r.data);
    } }, brand(), err, u, p, btn, h('p', { class: 'hint login-hint' }, '同一个账号同一时间只能在一台设备上登录。忘记密码请联系管理员重置。'));
    screen(form);
    (u.value ? p : u).focus();
  });
}

/** 必须先改密码（管理员设的临时密码、或还在用默认密码）：关不掉，改好才能继续。 */
export function forcePasswordScreen(defaultPw = false) {
  return new Promise(resolve => {
    const err = h('div', { class: 'alert bad login-err', role: 'alert', hidden: true });
    const inp = (name, ph, ac) => h('input', { type: 'password', name, placeholder: ph, autocomplete: ac });
    const [o, n1, n2] = [inp('old', defaultPw ? '当前密码（默认密码）' : '当前密码（管理员给你的临时密码）', 'current-password'), inp('n1', '新密码（至少 6 位）', 'new-password'), inp('n2', '再输一遍新密码', 'new-password')];
    const btn = h('button', { class: 'btn primary', type: 'submit' }, '修改密码并进入');
    const show = t => { err.textContent = t; err.hidden = !t; };
    const form = h('form', { class: 'login-card', onsubmit: async e => {
      e.preventDefault();
      if (n1.value.length < 6) return show('新密码至少 6 位');
      if (n1.value !== n2.value) return show('两次输入的新密码不一样');
      btn.disabled = true; show('');
      const r = await jfetch('/auth/password', { old: o.value, new: n1.value });
      btn.disabled = false;
      if (!r.ok) return show(r.data.detail || '修改失败');
      document.getElementById('login-root')?.remove();
      toast('密码已修改', 'ok');
      resolve();
    } }, brand(), h('h2', { class: 'login-title' }, '先改一个自己的密码'),
    h('p', { class: 'hint' }, defaultPw ? '你现在用的是默认密码，别人也能猜到。改好之后才能继续使用。' : '这是管理员给你设的临时密码，管理员知道它。改成只有你自己知道的密码，管理员也看不到。'),
    err, o, n1, n2, btn, h('button', { class: 'btn ghost', type: 'button', onclick: () => logout() }, '退出登录'));
    screen(form);
    o.focus();
  });
}

/** 启动时：确认已登录。成功返回当前账号（后端连不上返回 null，交给 api.js 的离线提示）。 */
export async function ensureLogin(message = '') {
  let me;
  try { me = await jfetch('/auth/me'); } catch { return null; }
  if (me.ok) {
    state.user = me.data.user; state.auth = me.data.auth !== false; applyRole();
    if (me.data.must_change) { await forcePasswordScreen(!!me.data.default_password); return ensureLogin(); }
    return state.user;
  }
  await loginScreen(message || (me.data.code && me.data.code !== 'none' ? me.data.detail : ''));
  return ensureLogin();
}

export async function logout() {
  try { await jfetch('/auth/logout', {}); } catch { /* 连不上也照样回登录页 */ }
  await clearLocalCaches();
  location.reload();
}
