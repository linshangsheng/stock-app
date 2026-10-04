// IndexedDB 展示缓存 + localStorage 界面偏好（1.2 / 1.3.3）。
// 注意：个人数据（自选 / 持仓 / 交易日志）的真源在后端 portfolio.db；这里只缓存「最近一次展示数据」，
// 断网时可查看最近快照，浏览器清缓存不会造成任何数据丢失。
const DB_NAME = 'stock-app-cache';
const STORE = 'kv';
let _dbp = null;

function open() {
  if (_dbp) return _dbp;
  _dbp = new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) return reject(new Error('no indexedDB'));
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return _dbp;
}

export async function cacheGet(key) {
  try {
    const db = await open();
    return await new Promise(res => {
      const r = db.transaction(STORE).objectStore(STORE).get(key);
      r.onsuccess = () => res(r.result || null);
      r.onerror = () => res(null);
    });
  } catch { return null; }
}

export async function cacheSet(key, data) {
  try {
    const db = await open();
    db.transaction(STORE, 'readwrite').objectStore(STORE).put({ data, ts: Date.now() }, key);
  } catch { /* 缓存失败不影响功能 */ }
}

export async function cacheClear() {
  try { const db = await open(); db.transaction(STORE, 'readwrite').objectStore(STORE).clear(); } catch { /* ignore */ }
}

// 轻量偏好（主题、涨跌配色模式、列表排序、访问口令）
export const prefs = {
  get(k, d = null) { try { const v = localStorage.getItem('sa.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('sa.' + k, JSON.stringify(v)); } catch { /* 隐私模式 */ } },
};
