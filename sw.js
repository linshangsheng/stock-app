// Service Worker：应用外壳「网络优先 / 缓存兜底」，行情接口 GET「缓存优先 / 后台更新」（1.3.3）。
// 断网时可查看最近一次行情快照与自选列表。POST / PUT / DELETE 一律直连，绝不缓存（个人数据真源在后端）。
// 注意：浏览器只允许 HTTPS 或 localhost 注册 Service Worker；手机经局域网 HTTP 访问时仅能在线浏览（1.6）。
const VERSION = 'v0.4.0';
const SHELL = `stock-shell-${VERSION}`;
const DATA = `stock-data-${VERSION}`;
const SHELL_FILES = ['/', '/index.html', '/css/style.css', '/js/app.js', '/js/api.js', '/js/db.js', '/js/util.js', '/js/chart.js', '/js/detail.js',
  '/js/screener.js', '/js/watchlist.js', '/js/portfolio.js', '/js/backtest.js', '/js/settings.js', '/js/parse.js', '/js/app-shared.js', '/js/market.js', '/js/news.js',
  '/vendor/lightweight-charts.standalone.production.js', '/manifest.json', '/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES).catch(() => { /* 个别文件缺失不阻塞安装 */ })).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => ![SHELL, DATA].includes(k)).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) {
    // 任务轮询 / 回测 / 探活不缓存
    if (url.pathname.startsWith('/api/jobs') || url.pathname.startsWith('/api/backtest') || url.pathname === '/api/ping') return;
    const offlineResp = () => new Response(JSON.stringify({ detail: '离线且无缓存' }), { status: 503, headers: { 'Content-Type': 'application/json' } });
    // 纯行情类（K 线 / 指标 / 报价 / 搜索）：缓存优先、后台更新（1.3.3）——历史行情变化慢，秒开更重要
    if (/^\/api\/(kline|indicators|quote|search)/.test(url.pathname)) {
      e.respondWith((async () => {
        const cache = await caches.open(DATA);
        const hit = await cache.match(req);
        const net = fetch(req).then(r => { if (r.ok) cache.put(req, r.clone()); return r; }).catch(() => null);
        if (hit) { e.waitUntil(net); return hit; }
        return (await net) || offlineResp();
      })());
      return;
    }
    // 其余（持仓 / 交易 / 账户 / 自选 / 体检 / 选股 / 预警 …）：网络优先，断网才回退到最近快照。
    // 绝不能缓存优先：写入后立刻重读必须看到最新数据，否则界面会显示过期的持仓与体检。
    e.respondWith((async () => {
      const cache = await caches.open(DATA);
      try {
        const r = await fetch(req);
        if (r.ok) cache.put(req, r.clone());
        return r;
      } catch {
        return (await cache.match(req)) || offlineResp();
      }
    })());
    return;
  }
  // 外壳：网络优先，失败回退缓存
  e.respondWith(fetch(req).then(r => { if (r.ok) { const c = r.clone(); caches.open(SHELL).then(ch => ch.put(req, c)); } return r; })
    .catch(() => caches.match(req).then(m => m || caches.match('/index.html'))));
});
