// 设置 → 「我的账号」（所有账号）和「账号管理」（只有管理员）。
//   · 我的账号：改密码、（普通账号）提醒收件邮箱、涨跌配色 / 主题（存在这台设备上）、退出登录。
//   · 账号管理：新建、重置密码（设临时密码，对方登录后必须改成自己的）、暂停 / 恢复、改名、设有效期（到期自动登录不了、数据保留，续期后恢复）、删除（连数据一起删）、登录记录。
//   管理员看不到任何人的密码：库里只存哈希；管理员能做的只有「设一个新的临时密码」。
import { get, post, put, del, state } from './api.js';
import { h, clear, toast, modal, confirmBox, mountIcons } from './util.js';
import { prefs } from './db.js';
import { isAdmin, logout } from './auth.js';

const kv = (k, v) => h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, v));
const seg = (items, cur, on) => { const w = h('div', { class: 'seg' }); items.forEach(([v, l]) => { const b = h('button', { class: v === cur ? 'on' : '', onclick: () => { [...w.children].forEach(c => c.classList.remove('on')); b.classList.add('on'); on(v); } }, l); w.append(b); }); return w; };
const fmtTime = t => (t ? t.replace('T', ' ').slice(0, 16) : '—');
const pwInput = (ph, ac = 'new-password') => h('input', { type: 'password', placeholder: ph, autocomplete: ac, style: 'width:100%' });
const field = (label, input) => h('label', { class: 'f', style: 'display:block;margin-top:10px' }, h('span', { class: 'small muted' }, label), input);
const ymd = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const inDays = n => { const d = new Date(); d.setDate(d.getDate() + n); return ymd(d); };
/** 有效期的文字：永久 / 2026-12-31（还剩 12 天）/ 已到期。 */
const expText = u => (u.role === 'admin' || !u.expires_at ? '永久' : u.expired ? `${u.expires_at} 已到期` : `${u.expires_at}（${u.days_left > 0 ? '还剩 ' + u.days_left + ' 天' : '今天是最后一天'}）`);
const randomPw = () => { const c = 'abcdefghjkmnpqrstuvwxyz23456789'; return Array.from(crypto.getRandomValues(new Uint8Array(8)), x => c[x % c.length]).join(''); };

/** 改自己的密码（要输入旧密码）。 */
export function changePasswordDialog() {
  const [o, n1, n2] = [pwInput('当前密码', 'current-password'), pwInput('新密码（至少 6 位）'), pwInput('再输一遍新密码')];
  modal('修改密码', h('div', {}, field('当前密码', o), field('新密码', n1), field('确认新密码', n2),
    h('p', { class: 'hint mt-s' }, '改好后这台设备继续保持登录；管理员看不到你的密码。')),
  [{ label: '取消' }, { label: '修改', primary: true, onclick: async () => {
    if (n1.value.length < 6) { toast('新密码至少 6 位', 'bad'); return false; }
    if (n1.value !== n2.value) { toast('两次输入的新密码不一样', 'bad'); return false; }
    try { await post('/auth/password', { old: o.value, new: n1.value }, { market: false }); } catch (e) { toast(e.message, 'bad'); return false; }
    if (state.user) state.user.default_password = false;
    document.getElementById('pw-bar')?.setAttribute('hidden', '');
    toast('密码已修改', 'ok');
  } }]);
}

/** 设置 → 我的账号。ctx.applyColors / applyTheme 来自 app.js。 */
export function accountPanel(ctx) {
  const card = h('div', { class: 'card' }), look = h('div', { class: 'card mt' });
  const u = state.user || {};
  const admin = isAdmin();
  const mail = h('input', { type: 'text', placeholder: 'you@qq.com（多个用逗号分隔）', style: 'width:min(320px,100%)' });
  const mailNote = h('div', { class: 'hint mt-s' });
  card.append(h('div', { class: 'card-title' }, h('h2', {}, '我的账号'), h('span', { class: 'badge ' + (admin ? 'ok' : '') }, admin ? '管理员' : '普通账号')),
    h('div', { class: 'grid c4' }, kv('用户名', u.username || '—'), kv('身份', admin ? '管理员' : '普通账号'), kv('有效期', expText(u)), kv('最近登录', fmtTime(u.last_login_at))),
    !admin && u.expires_at && !u.expired && u.days_left <= 7 ? h('div', { class: 'alert warn mt' }, `账号还有 ${u.days_left > 0 ? u.days_left + ' 天' : '不到 1 天'}到期。到期后登录不了（数据会保留），请联系管理员续期。`) : null,
    h('p', { class: 'hint mt-s' }, admin ? '管理员可以管理所有账号和全部设置。' : '自选股、持仓和账户是你自己的，别人看不到；数据更新、交易参数等其他设置由管理员统一管理。'),
    u.default_password && state.auth ? h('div', { class: 'alert warn mt' }, '你还在用默认密码（root / 123456），请马上改掉。') : null,
    h('div', { class: 'row wrap mt' }, h('button', { class: 'btn primary', onclick: changePasswordDialog }, '修改密码'),
      state.auth ? h('button', { class: 'btn', onclick: async () => { if (await confirmBox('退出登录？下次打开要重新输入用户名和密码。')) logout(); } }, '退出登录') : null),
    h('p', { class: 'hint mt-s' }, '一个账号同一时间只能在一台设备上登录：在别处登录会把这里挤下线。忘记密码请联系管理员重置（管理员设的是临时密码，你登录后要改成自己的）。'));
  if (state.auth && !admin) {
    card.append(h('h3', { class: 'mt' }, '提醒邮箱'),
      h('p', { class: 'small muted' }, '你的持仓出现需要处理的情况（止损 / 止盈 / 上移止损）、或恐慌日、宽基 ETF 有信号时，会发到这里。发件用的是管理员配置的邮箱服务；留空 = 不发。'),
      h('div', { class: 'row wrap mt-s' }, mail, h('button', { class: 'btn', onclick: async () => {
        try { const r = await put('/me/notify', { notify_to: mail.value.trim() }, { market: false }); mail.value = r.notify_to; toast(r.notify_to ? '已保存' : '已清空，不再发提醒', 'ok'); } catch (e) { toast(e.message, 'bad'); }
      } }, '保存')), mailNote);
    get('/me/notify', {}, { market: false, cache: false }).then(r => { mail.value = r.notify_to || ''; }).catch(() => { /* 取不到就留空 */ });
  }
  look.append(h('h2', { class: 'mb' }, '外观（只影响这台设备）'),
    h('div', { class: 'row between wrap mb' }, h('div', {}, h('div', {}, '涨跌配色'), h('div', { class: 'hint' }, '默认随市场自适应：A股、港股 红涨绿跌，美股 绿涨红跌。')),
      seg([['auto', '随市场自适应'], ['red', '全局红涨绿跌'], ['green', '全局绿涨红跌']], prefs.get('colorMode', 'auto'), v => { prefs.set('colorMode', v); ctx.applyColors(); })),
    h('div', { class: 'row between wrap' }, h('div', {}, '主题'), seg([['auto', '跟随系统'], ['light', '浅色'], ['dark', '深色']], prefs.get('theme', 'auto'), v => { prefs.set('theme', v); ctx.applyTheme(); })));
  return { els: admin ? [card] : [card, look] };
}

/** 设置 → 账号管理（管理员）。 */
export function usersPanel() {
  const card = h('div', { class: 'card' }), logCard = h('div', { class: 'card mt' });
  let dead = false;

  async function act(fn, ok) { try { await fn(); if (ok) toast(ok, 'ok'); } catch (e) { toast(e.message, 'bad'); return false; } if (!dead) load(); }

  function createDlg() {
    const name = h('input', { type: 'text', placeholder: '2~32 个字符：中文、字母、数字、_ -', autocomplete: 'off', style: 'width:100%' });
    const pw = h('input', { type: 'text', value: randomPw(), autocomplete: 'off', style: 'width:100%' });
    const exp = h('select', { style: 'width:100%', onchange: () => { day.hidden = exp.value !== 'custom'; } },
      [['', '永久（不设有效期）'], ['30', '30 天'], ['90', '90 天'], ['365', '1 年'], ['custom', '指定到哪一天…']].map(([v, l]) => h('option', { value: v }, l)));
    const day = h('input', { type: 'date', min: ymd(new Date()), hidden: true, style: 'width:100%;margin-top:6px' });
    modal('新建账号', h('div', {}, field('用户名', name), field('初始密码（临时，至少 6 位）', pw),
      h('div', { class: 'row mt-s' }, h('button', { class: 'btn sm', type: 'button', onclick: () => { pw.value = randomPw(); } }, '换一个随机的')),
      field('有效期（到这一天为止，含当天；到期后登录不了，数据保留，可以续期）', h('div', {}, exp, day)),
      h('p', { class: 'hint mt-s' }, '把用户名和这个初始密码告诉对方。对方第一次登录必须改成自己的密码，改完以后你也不知道。新账号是普通账号：有自己的自选、持仓和账户，设置由你统一管理。')),
    [{ label: '取消' }, { label: '创建', primary: true, onclick: async () => {
      const expires_at = exp.value === 'custom' ? (day.value || null) : exp.value ? inDays(+exp.value) : null;
      if (exp.value === 'custom' && !day.value) { toast('请选一个日期', 'bad'); return false; }
      try { await post('/accounts', { username: name.value.trim(), password: pw.value, expires_at }, { market: false }); } catch (e) { toast(e.message, 'bad'); return false; }
      toast(`已创建账号 ${name.value.trim()}`, 'ok'); load();
    } }]);
  }

  function resetDlg(u) {
    const pw = h('input', { type: 'text', value: randomPw(), autocomplete: 'off', style: 'width:100%' });
    modal(`重置「${u.username}」的密码`, h('div', {}, field('新的临时密码（至少 6 位）', pw),
      h('p', { class: 'hint mt-s' }, '设好后对方会被立刻挤下线，要用这个临时密码登录，并且登录后必须改成自己的。旧密码作废。')),
    [{ label: '取消' }, { label: '重置', primary: true, onclick: () => act(() => put(`/accounts/${u.id}/password`, { password: pw.value }, { market: false }), `已重置，把临时密码告诉 ${u.username}`).then(r => r !== false) }]);
  }

  function expiryDlg(u) {
    const day = h('input', { type: 'date', value: u.expires_at || '', style: 'width:100%' });
    let m;
    const go = (patch, ok) => act(() => put(`/accounts/${u.id}`, patch, { market: false }), ok).then(r => { if (r !== false) m.close(); });
    const quick = (label, patch, ok) => h('button', { class: 'btn sm', type: 'button', onclick: () => go(patch, ok) }, label);
    m = modal(`「${u.username}」的有效期`, h('div', {},
      h('p', { class: 'small' }, '现在：', h('b', {}, expText(u))),
      h('div', { class: 'row wrap mt-s', style: 'gap:8px' }, quick('续 30 天', { extend_days: 30 }, '已续 30 天'), quick('续 90 天', { extend_days: 90 }, '已续 90 天'), quick('续 1 年', { extend_days: 365 }, '已续 1 年'), quick('改成永久', { expires_at: null }, '已改成永久')),
      h('p', { class: 'hint mt-s' }, '「续」是在今天和现在的到期日里较晚的那天上再加（已经过期的从今天算起；原来是永久的从今天算起）。'),
      field('或者直接指定到哪一天为止（含当天）', day),
      h('p', { class: 'hint mt-s' }, '到期后：对方立刻被挤下线、登录不了，自选 / 持仓等数据保留，续期后恢复。管理员账号没有有效期。')),
    [{ label: '关闭' }, { label: '保存这个日期', primary: true, onclick: async () => {
      if (!day.value) { toast('请选一个日期，或者点「改成永久」', 'bad'); return false; }
      return act(() => put(`/accounts/${u.id}`, { expires_at: day.value }, { market: false }), `有效期改到 ${day.value}`).then(r => r !== false);
    } }]);
  }

  function renameDlg(u) {
    const name = h('input', { type: 'text', value: u.username, autocomplete: 'off', style: 'width:100%' });
    modal(`改「${u.username}」的用户名`, h('div', {}, field('新用户名', name)),
      [{ label: '取消' }, { label: '保存', primary: true, onclick: () => act(() => put(`/accounts/${u.id}`, { username: name.value.trim() }, { market: false }), '已改名').then(r => r !== false) }]);
  }

  function deleteDlg(u) {
    const sure = h('input', { type: 'text', placeholder: u.username, autocomplete: 'off', style: 'width:100%' });
    modal(`删除账号「${u.username}」`, h('div', {}, h('div', { class: 'alert bad' }, h('b', {}, '这会把这个账号和它的全部数据一起删掉，不能恢复：'), '自选股、K 线名单、持仓、交易记录、账户资金、提醒。'),
      h('p', { class: 'small mt-s' }, '只想让对方暂时登录不了、数据保留的话，用「暂停」。'), field(`输入用户名「${u.username}」确认删除`, sure)),
    [{ label: '取消' }, { label: '删除账号和数据', primary: true, onclick: async () => {
      if (sure.value.trim() !== u.username) { toast('用户名对不上，没有删除', 'bad'); return false; }
      return act(() => del(`/accounts/${u.id}`, { market: false }), `已删除 ${u.username} 和他的全部数据`).then(r => r !== false);
    } }]);
  }

  async function load() {
    let r;
    try { r = await get('/accounts', {}, { market: false, cache: false }); } catch (e) { clear(card).append(h('div', { class: 'alert bad' }, e.message)); return; }
    if (dead) return;
    clear(card);
    card.append(h('div', { class: 'card-title' }, h('h2', {}, '账号管理'), h('button', { class: 'btn primary', onclick: createDlg }, '+ 新建账号')),
      h('p', { class: 'small muted' }, `共 ${r.users.length} 个账号。你是管理员（最高权限，数据在原来的个人库里）；其他账号是普通账号，只能改自己的密码，看不到别人的数据。管理员也看不到任何人的密码。`),
      h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, ['用户名', '状态', '有效期', '最近登录', '创建时间', '操作'].map(t => h('th', {}, t)))),
        h('tbody', {}, r.users.map(u => h('tr', {},
          h('td', {}, h('b', {}, u.username), u.role === 'admin' ? h('span', { class: 'badge ok', style: 'margin-left:6px' }, '管理员') : null, u.online ? h('span', { class: 'badge accent', style: 'margin-left:6px' }, '在线') : null),
          h('td', {}, u.status === 'paused' ? h('span', { class: 'badge warn' }, '已暂停') : u.expired ? h('span', { class: 'badge warn' }, '已到期') : u.must_change ? h('span', { class: 'badge' }, '待改密码') : h('span', { class: 'badge ok' }, '正常')),
          h('td', { class: 'small num' + (u.expired ? '' : u.days_left != null && u.days_left <= 7 ? '' : ' muted'), style: u.expired || (u.days_left != null && u.days_left <= 7) ? 'color:var(--warn)' : '' }, expText(u)),
          h('td', { class: 'small muted num' }, fmtTime(u.last_login_at), u.last_login_ip ? h('div', { class: 'tiny faint' }, u.last_login_ip) : null),
          h('td', { class: 'small muted num' }, fmtTime(u.created_at)),
          h('td', {}, h('div', { class: 'row wrap', style: 'gap:6px' },
            h('button', { class: 'btn sm', onclick: () => renameDlg(u) }, '改名'),
            u.role === 'admin' ? null : [
              h('button', { class: 'btn sm', onclick: () => expiryDlg(u) }, '有效期'),
              h('button', { class: 'btn sm', onclick: () => resetDlg(u) }, '重置密码'),
              u.status === 'paused'
                ? h('button', { class: 'btn sm primary', onclick: () => act(() => put(`/accounts/${u.id}`, { status: 'active' }, { market: false }), `已恢复 ${u.username}`) }, '恢复')
                : h('button', { class: 'btn sm', onclick: async () => { if (await confirmBox(`暂停「${u.username}」？对方会被立刻挤下线、登录不了，数据保留；随时可以恢复。`)) act(() => put(`/accounts/${u.id}`, { status: 'paused' }, { market: false }), `已暂停 ${u.username}`); } }, '暂停'),
              h('button', { class: 'btn sm', style: 'color:var(--danger)', onclick: () => deleteDlg(u) }, '删除')]))))))));
    clear(logCard);
    logCard.append(h('details', {}, h('summary', {}, `登录记录（最近 ${r.log.length} 条）`),
      h('p', { class: 'hint' }, '连续输错 5 次（同一用户名、同一 IP）或同一 IP 累计输错 30 次，会被锁定 15 分钟。'),
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'mini' }, h('thead', {}, h('tr', {}, ['时间', '用户名', 'IP', '结果', '备注'].map(t => h('th', {}, t)))),
        h('tbody', {}, r.log.map(x => h('tr', {}, h('td', { class: 'num small' }, fmtTime(x.ts)), h('td', {}, x.username || '—'), h('td', { class: 'num small' }, x.ip || '—'),
          h('td', {}, x.ok ? h('span', { class: 'badge ok' }, '成功') : h('span', { class: 'badge warn' }, '失败')), h('td', { class: 'small muted' }, x.note || ''))))))));
    mountIcons(card);
  }
  card.append(h('div', { class: 'empty' }, h('span', { class: 'spinner' }), ' 读取账号…'));
  load();
  return { els: [card, logCard], destroy() { dead = true; } };
}
