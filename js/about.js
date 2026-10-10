// 关于：软件名称、版本、作者与联系方式、版权、免责声明、数据来源与第三方组件。数据来自 /api/about（只在 server/__init__.py 写一次）。
// 用在「设置」页底部，以及点导航栏左上角 Logo 弹出的小窗。
import { get } from './api.js';
import { h, toast, copyText, modal } from './util.js';

export async function loadAbout() {
  return get('/about', {}, { market: false, cache: false });
}

/** 关于的主体内容。full=false 时只给简要信息（弹窗用）。 */
export function aboutBlock(a, { full = true } = {}) {
  const mail = h('a', { href: `mailto:${a.email}?subject=${encodeURIComponent(`【趋势波段 v${a.version}】反馈`)}` }, a.email);
  const out = h('div', { class: 'about' },
    h('div', { class: 'about-head' },
      h('img', { src: 'icon.svg', width: 56, height: 56, alt: '' }),
      h('div', {}, h('div', { class: 'about-name' }, a.name), h('div', { class: 'small muted num' }, `版本 v${a.version} · 构建 ${a.build_time || '—'}`))),
    h('div', { class: 'grid c2 mt about-kv' },
      kv('作者', a.author),
      kv('联系邮箱', h('span', {}, mail, ' ', h('button', { class: 'btn sm ghost', onclick: () => { copyText(a.email); toast('邮箱已复制', 'ok'); } }, '复制'))),
      kv('版权与许可', h('span', {}, `${a.copyright} · `, h('a', { href: 'LICENSE', target: '_blank', rel: 'noopener' }, `${a.license} 开源许可`))),
      kv('性质', '个人研究工具 · 本地运行，数据只存在这台电脑上')),
    h('p', { class: 'small mt-s' }, h('b', {}, '反馈与建议：'), `发邮件到 ${a.email}。遇到问题时请写上版本号（v${a.version}），必要时附上软件目录里 data\\server.log 的最后几十行。`),
    h('div', { class: 'alert warn small mt-s' }, h('b', {}, '免责声明：'), a.disclaimer.join('；') + '。'),
    full ? h('p', { class: 'small muted mt-s' }, h('b', {}, `${a.license} 许可：`), a.license_note) : null);
  if (!full) return out;
  out.append(
    h('details', { class: 'mt' }, h('summary', {}, '数据来源'),
      h('p', { class: 'hint' }, '均为公开 / 免费接口，只用于个人研究；请遵守各自的使用条款。行情有延迟或错误时，以交易所和券商为准。'),
      h('table', { class: 'mini' }, h('tbody', {}, a.data_sources.map(s => h('tr', {}, h('td', {}, h('b', {}, s.name)), h('td', { class: 'small muted' }, s.use)))))),
    h('details', { class: 'mt-s' }, h('summary', {}, '第三方组件与许可'),
      h('table', { class: 'mini' }, h('tbody', {}, a.third_party.map(s => h('tr', {}, h('td', {}, h('b', {}, s.name)), h('td', { class: 'small' }, s.license), h('td', { class: 'small muted' }, s.note || '')))))),
    h('p', { class: 'tiny muted mt-s' }, `数据目录：${a.data_dir} · Python ${a.python}`));
  return out;
}

/** 点导航栏 Logo：弹出简要的「关于」。 */
export async function openAbout() {
  let a;
  try { a = await loadAbout(); } catch (e) { toast(e.message, 'bad'); return; }
  modal('关于', h('div', {}, aboutBlock(a, { full: false }),
    h('p', { class: 'hint mt-s' }, '数据来源、第三方组件与许可：见「设置」页底部。')), [{ label: '去设置页', onclick: () => { location.hash = '#/settings'; } }, { label: '关闭', primary: true }]);
}

function kv(k, v) {
  return h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v', style: 'font-size:14px' }, v));
}
