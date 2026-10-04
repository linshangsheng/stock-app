// 本地后端接口封装：Fetch + AbortController 超时；GET 成功后写展示缓存，断网时回退到最近快照并标注「数据延迟」。
// 前端不直接对接任何第三方行情源（1.1 核心原则 1）。
import { cacheGet, cacheSet, prefs } from './db.js';

export const state = { market: 'CN', offline: false, lastError: null };
const listeners = new Set();
export const onStatus = fn => listeners.add(fn);
const emit = () => listeners.forEach(f => f(state));

export class ApiError extends Error {
  constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
}

function qs(params = {}) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') u.set(k, v);
  const s = u.toString();
  return s ? '?' + s : '';
}

export async function api(path, { method = 'GET', body, params = {}, timeout = 60000, market = true, cache = true } = {}) {
  const p = { ...(market ? { market: state.market } : {}), ...params };
  const url = '/api' + path + qs(p);
  const headers = { 'Content-Type': 'application/json' };
  const token = prefs.get('token', '');
  if (token) headers['X-Token'] = token;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, signal: ctrl.signal });
  } catch (e) {
    clearTimeout(timer);
    state.offline = true; state.lastError = e.name === 'AbortError' ? '请求超时' : '无法连接本地后端'; emit();
    if (method === 'GET' && cache) {
      const c = await cacheGet(url);
      if (c) return { ...c.data, _stale: true, _cached_at: c.ts };       // 断网：最近快照 + 「数据延迟」标注
    }
    throw new ApiError(state.lastError, 0);
  }
  clearTimeout(timer);
  state.offline = false; state.lastError = null; emit();
  let data = null;
  try { data = await res.json(); } catch { /* 非 JSON */ }
  if (!res.ok) {
    const msg = (data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || res.statusText;
    if (res.status === 401) window.dispatchEvent(new CustomEvent('need-token'));
    throw new ApiError(msg, res.status, data);
  }
  if (method === 'GET' && cache && data && typeof data === 'object') cacheSet(url, data);
  return data;
}

export const get = (path, params, opt) => api(path, { params, ...opt });
export const post = (path, body, opt) => api(path, { method: 'POST', body, ...opt });
export const put = (path, body, opt) => api(path, { method: 'PUT', body, ...opt });
export const del = (path, opt) => api(path, { method: 'DELETE', ...opt });

/** 提交后台任务并轮询（回测） */
export async function runBacktest(kind, params, onTick) {
  const { job_id } = await post('/backtest', { kind, params });
  for (;;) {
    await new Promise(r => setTimeout(r, 1000));
    const j = await api('/backtest/job/' + job_id, { market: false, cache: false });
    onTick?.(j);
    if (j.status === 'done') return j.result;
    if (j.status === 'error') throw new ApiError(j.error, 500, j);
  }
}
