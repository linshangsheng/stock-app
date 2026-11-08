// 安装成 App：设置 → 我的账号 里的「安装」卡片。
//   · 电脑 / 安卓上的 Edge、Chrome（含 macOS 的 Chrome、Edge）：浏览器会发 beforeinstallprompt 事件，我们把它存起来；点按钮就弹出系统安装框，再点一次「安装」就装好（要 HTTPS 或 localhost）。
//   · iPhone / iPad（Safari、以及 iOS 上的 Chrome）：苹果不让网页触发安装，只能自己点「分享 → 添加到主屏幕」，所以点按钮弹出图文引导。
//   · macOS 的 Safari（14 以上）：「文件 → 添加到程序坞」，同样不能由网页触发，给图文引导。
//   · 其余（微信等内置浏览器、Firefox 电脑版、http 地址、浏览器没给安装事件）：说清楚为什么装不了、怎么办。
//   · 已经是从桌面图标打开的：显示「已安装」。
// detectState() 是纯函数（传入环境、返回类别），方便在浏览器里用各种 UA 逐个检查。
import { h, toast, modal } from './util.js';

let deferred = null, installedNow = false;
const subs = new Set();
const emit = () => subs.forEach(f => f());
window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); deferred = e; emit(); });      // 越早接住越好：app.js 一启动就 import 本文件
window.addEventListener('appinstalled', () => { installedNow = true; deferred = null; emit(); });

const readEnv = () => ({
  ua: navigator.userAgent || '', platform: navigator.platform || '', touch: navigator.maxTouchPoints || 0,
  standalone: matchMedia('(display-mode: standalone)').matches || navigator.standalone === true,
  installed: installedNow, secure: window.isSecureContext, hasPrompt: !!deferred,
});

/** 返回：installed | prompt | ios | inapp | mac-safari | firefox | insecure | manual */
export function detectState(e) {
  const ua = e.ua || '';
  if (e.standalone || e.installed) return 'installed';
  if (e.hasPrompt) return 'prompt';
  const inapp = /MicroMessenger|QQ\/|DingTalk|Weibo|baiduboxapp|AlipayClient/i.test(ua);
  const ios = /iPhone|iPad|iPod/.test(ua) || (e.platform === 'MacIntel' && e.touch > 1);               // iPadOS 的 Safari 假装成 Mac，靠触摸点数区分
  if (ios) return inapp ? 'inapp' : 'ios';
  if (/Macintosh/.test(ua) && !e.touch && /Safari/.test(ua) && !/Chrome|Chromium|Edg|OPR|Firefox|Quark|UCBrowser/.test(ua)) return 'mac-safari';
  if (inapp) return 'inapp';
  if (/Firefox/.test(ua) && !/Android/.test(ua)) return 'firefox';
  if (!e.secure) return 'insecure';
  return 'manual';                                                                                    // Chromium、HTTPS，但没给安装事件：多半已经装过了，或者浏览器还没判定可以安装
}

const isPad = e => /iPad/.test(e.ua) || (e.platform === 'MacIntel' && e.touch > 1);
const deviceWord = e => (e.touch > 1 || /Android|iPhone|iPad/.test(e.ua) ? '手机' : '电脑');

// ---------- 图文引导（内联 SVG，跟随深浅主题）----------
const SVG_OPEN = '<svg viewBox="0 0 300 118" role="img" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,PingFang SC,Microsoft YaHei,sans-serif">';
const HL = 'fill="none" stroke="var(--accent)" stroke-width="2.5"';
const shareIcon = (x, y, c = 'var(--text)') => `<g fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M${x} ${y + 12}V${y - 2}M${x - 5} ${y + 3}l5-5 5 5"/><path d="M${x - 8} ${y + 6}h-3v13h22V${y + 6}h-3"/></g>`;
const IMG_SHARE_BOTTOM = `${SVG_OPEN}<rect x="8" y="6" width="284" height="106" rx="16" fill="var(--panel)"/><rect x="26" y="20" width="150" height="9" rx="4.5" fill="var(--field-2)"/><rect x="26" y="36" width="210" height="9" rx="4.5" fill="var(--field)"/><rect x="26" y="52" width="120" height="9" rx="4.5" fill="var(--field)"/>
<rect x="8" y="76" width="284" height="36" rx="0" fill="var(--bg)"/><rect x="8" y="108" width="284" height="4" fill="var(--bg)"/><path d="M8 76h284" stroke="var(--line-strong)" stroke-width=".6"/>
<g fill="none" stroke="var(--muted)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M50 87l-6 7 6 7M100 87l6 7-6 7"/><path d="M205 86h12v16h-12zM258 86h14v14h-14z"/></g>
${shareIcon(150, 84)}<circle cx="150" cy="94" r="19" ${HL}/><text x="150" y="71" text-anchor="middle" font-size="11" font-weight="700" fill="var(--accent)">分享</text></svg>`;
const IMG_SHARE_TOP = `${SVG_OPEN}<rect x="8" y="6" width="284" height="106" rx="16" fill="var(--panel)"/><rect x="8" y="6" width="284" height="34" rx="16" fill="var(--bg)"/><rect x="70" y="15" width="150" height="16" rx="8" fill="var(--field)"/>
<g fill="none" stroke="var(--muted)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M30 17l-6 7 6 7M48 17l6 7-6 7"/></g>${shareIcon(248, 16)}<circle cx="248" cy="24" r="17" ${HL}/><text x="248" y="62" text-anchor="middle" font-size="11" font-weight="700" fill="var(--accent)">分享</text>
<rect x="26" y="70" width="150" height="9" rx="4.5" fill="var(--field-2)"/><rect x="26" y="86" width="210" height="9" rx="4.5" fill="var(--field)"/></svg>`;
const IMG_SHEET = `${SVG_OPEN}<rect x="8" y="4" width="284" height="110" rx="16" fill="var(--panel)"/>
<g font-size="13" fill="var(--text)"><text x="26" y="30">拷贝</text><text x="26" y="58">添加书签</text></g><path d="M20 38h260M20 66h260" stroke="var(--line-strong)" stroke-width=".6"/>
<rect x="14" y="72" width="272" height="30" rx="9" fill="var(--accent-soft)"/><rect x="14" y="72" width="272" height="30" rx="9" ${HL}/>
<g fill="none" stroke="var(--text)" stroke-width="1.8" stroke-linecap="round"><rect x="256" y="77" width="19" height="19" rx="4"/><path d="M265.5 82v9M261 86.5h9"/></g><text x="26" y="92" font-size="13" font-weight="700" fill="var(--text)">添加到主屏幕</text></svg>`;
const IMG_ADD = `${SVG_OPEN}<rect x="8" y="4" width="284" height="110" rx="16" fill="var(--panel)"/>
<text x="26" y="28" font-size="13" fill="var(--accent)">取消</text><text x="150" y="28" font-size="13" font-weight="700" text-anchor="middle" fill="var(--text)">添加到主屏幕</text>
<rect x="244" y="12" width="44" height="22" rx="11" fill="var(--accent-soft)"/><rect x="244" y="12" width="44" height="22" rx="11" ${HL}/><text x="266" y="28" font-size="13" font-weight="700" text-anchor="middle" fill="var(--accent)">添加</text>
<image href="icon.svg" x="26" y="46" width="52" height="52"/><rect x="92" y="54" width="186" height="36" rx="9" fill="var(--field)"/><text x="104" y="77" font-size="15" fill="var(--text)">顺势</text></svg>`;
const IMG_MAC_MENU = `${SVG_OPEN}<rect x="8" y="4" width="284" height="110" rx="12" fill="var(--panel)"/><rect x="8" y="4" width="284" height="24" rx="12" fill="var(--bg)"/>
<g font-size="12" fill="var(--muted)"><text x="22" y="20">Safari</text><text x="76" y="20" font-weight="700" fill="var(--accent)">文件</text><text x="116" y="20">编辑</text><text x="156" y="20">显示</text></g><rect x="66" y="12" width="36" height="16" rx="6" ${HL}/>
<rect x="60" y="32" width="170" height="76" rx="8" fill="var(--bg)" stroke="var(--line-strong)" stroke-width=".6"/><g font-size="12" fill="var(--text)"><text x="72" y="50">新建窗口</text><text x="72" y="68">打开文件…</text></g>
<rect x="64" y="76" width="162" height="22" rx="6" fill="var(--accent-soft)"/><rect x="64" y="76" width="162" height="22" rx="6" ${HL}/><text x="72" y="91" font-size="12" font-weight="700" fill="var(--text)">添加到程序坞…</text></svg>`;

const step = (n, title, img, note) => h('div', { class: 'ig-step' }, h('div', { class: 'ig-n' }, String(n)), h('div', { class: 'ig-body' }, h('div', { class: 'ig-t' }, title), h('div', { class: 'ig-img', html: img }), note ? h('div', { class: 'hint' }, note) : null));

export function iosGuide(e = readEnv()) {
  const pad = isPad(e);
  modal(pad ? '在 iPad 上安装' : '在 iPhone 上安装', h('div', { class: 'ig' },
    h('p', { class: 'small' }, '苹果不允许网页自己安装，要手动点三下。装好后桌面上会有「顺势」图标，点开就是全屏的，没有地址栏。'),
    step(1, pad ? '点地址栏右边的「分享」按钮（方框加向上箭头）' : '点屏幕最下面中间的「分享」按钮（方框加向上箭头）', pad ? IMG_SHARE_TOP : IMG_SHARE_BOTTOM,
      /CriOS|EdgiOS|FxiOS/.test(e.ua) ? '你现在用的不是 Safari：分享按钮在地址栏右边。用 Safari 打开最稳。' : (pad ? null : '看不到底部的按钮？往下轻轻滑一下页面，工具栏就会出来。')),
    step(2, '往上滑，找到「添加到主屏幕」点它', IMG_SHEET, '列表里没有这一项？拉到最下面点「编辑操作…」，把「添加到主屏幕」加进来。'),
    step(3, '点右上角「添加」', IMG_ADD, '名字保持「顺势」就行。'),
    h('p', { class: 'hint' }, '然后回到手机桌面，点「顺势」图标打开。第一次打开要重新登录一次。'),
    e.secure ? null : h('div', { class: 'alert warn small' }, '提醒：现在的网址是 http（不是 https），也能添加到主屏幕，但打开时可能还带着 Safari 的地址栏。让管理员给服务器配上 https，才是完整的全屏 App。')),
  [{ label: '知道了', primary: true }]);
}

export function macSafariGuide() {
  modal('在 Mac 的 Safari 上安装', h('div', { class: 'ig' },
    h('p', { class: 'small' }, 'Safari 不允许网页自己安装，要在菜单里点一下。装好后「顺势」会出现在程序坞（Dock）和「启动台」里，点开是独立的窗口。'),
    step(1, '点屏幕最上面菜单栏的「文件」，选「添加到程序坞…」', IMG_MAC_MENU, '需要 macOS Sonoma（14）或更新的系统、Safari 17 以上。系统比较旧的话，改用 Chrome 或 Edge 打开这个网址，再来这里点「安装」。'),
    step(2, '在弹出的小窗口里点「添加」', IMG_ADD.replace('添加到主屏幕', '添加到程序坞'), null)),
  [{ label: '知道了', primary: true }]);
}

/** 设置 → 我的账号 里的「安装」卡片。返回 DOM 节点。 */
export function installCard() {
  const box = h('div', { class: 'card install-card' });
  let seen = false;
  const render = () => {
    if (box.isConnected) seen = true; else if (seen) { subs.delete(render); return; }          // 页面换走以后不再重画
    const e = readEnv(), kind = detectState(e), dev = deviceWord(e);
    const head = (badge, cls = '') => h('div', { class: 'card-title' }, h('h2', {}, '安装到' + dev), h('span', { class: 'badge ' + cls }, badge));
    const info = (cls, ...kids) => h('div', { class: 'alert ' + cls + ' small mt-s' }, ...kids);
    box.replaceChildren();
    if (kind === 'installed') {
      box.append(head('已安装', 'ok'), h('p', { class: 'small muted' }, '你现在就是从安装好的图标打开的，不用再装了。'));
    } else if (kind === 'prompt') {
      box.append(head('可以一键安装', 'accent'),
        h('p', { class: 'small muted' }, `装好后「顺势」会出现在${dev === '电脑' ? '桌面和开始菜单（Mac 在启动台和程序坞）' : '手机桌面'}里，点开是独立的全屏窗口，没有浏览器的地址栏。`),
        h('div', { class: 'row mt-s' }, h('button', { class: 'btn primary', onclick: async () => {
          if (!deferred) { render(); return; }
          deferred.prompt();
          const r = await deferred.userChoice.catch(() => ({ outcome: 'dismissed' }));
          deferred = null;
          toast(r.outcome === 'accepted' ? '正在安装，稍等一下就能在桌面上看到「顺势」' : '已取消。想装的时候再回到这里点就行', r.outcome === 'accepted' ? 'ok' : '');
          render();
        } }, '安装到' + dev)),
        h('p', { class: 'hint mt-s' }, '点一下之后，浏览器会弹出一个确认框，再点一次「安装」就好。'));
    } else if (kind === 'ios') {
      box.append(head('手动添加，3 步'),
        h('p', { class: 'small muted' }, `${isPad(e) ? 'iPad' : 'iPhone'} 上要自己点「分享 → 添加到主屏幕」（苹果不允许网页代劳）。点下面的按钮看图文步骤。`),
        h('div', { class: 'row mt-s' }, h('button', { class: 'btn primary', onclick: () => iosGuide(e) }, '怎么安装（图文）')));
    } else if (kind === 'mac-safari') {
      box.append(head('手动添加，2 步'),
        h('p', { class: 'small muted' }, 'Safari 要在菜单里点「文件 → 添加到程序坞」。用 Chrome 或 Edge 打开这个网址的话，这里会直接出现一键安装按钮。'),
        h('div', { class: 'row mt-s' }, h('button', { class: 'btn primary', onclick: macSafariGuide }, '怎么安装（图文）')));
    } else if (kind === 'inapp') {
      box.append(head('这里装不了', 'warn'), info('warn', '你现在在微信、QQ 这类软件自带的浏览器里，它们不允许安装。请点右上角「…」，选「在浏览器中打开」（iPhone 选 Safari，安卓选 Chrome 或 Edge），再回到这里。'));
    } else if (kind === 'firefox') {
      box.append(head('这个浏览器装不了', 'warn'), info('warn', 'Firefox 电脑版不支持把网页安装成应用。请用 Edge 或 Chrome 打开这个网址，再回到这里。'));
    } else if (kind === 'insecure') {
      box.append(head('现在装不了', 'warn'),
        info('warn', '现在的网址是 http（不是 https），浏览器不允许把它安装成应用，所以这里没有「一键安装」按钮。让管理员给服务器配上 https（域名加证书）后，这里就会出现按钮。'),
        h('p', { class: 'hint mt-s' }, '临时办法：浏览器菜单里的「添加到主屏幕」只是一个快捷方式，打开时还是带着浏览器的地址栏。'));
    } else {
      box.append(head('没有一键安装', ''),
        h('p', { class: 'small muted' }, '浏览器没有提供一键安装：多半是已经装过了，或者浏览器还没判定这个网站可以安装。可以手动装：'),
        h('ul', { class: 'small muted', style: 'margin:6px 0 0;padding-left:18px;line-height:1.8' },
          h('li', {}, 'Chrome：地址栏最右边的「安装」图标，或菜单 ⋮ → 「安装“顺势”」（安卓是「安装应用」）'),
          h('li', {}, 'Edge：菜单 … → 「应用」→「将此站点作为应用安装」')),
        h('div', { class: 'row mt-s' }, h('button', { class: 'btn', onclick: () => { render(); toast(deferred ? '现在可以一键安装了' : '浏览器还是没有给出安装按钮'); } }, '再检查一次')));
    }
  };
  subs.add(render);
  render();
  return box;
}
