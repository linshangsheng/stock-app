// 批量导入解析（复用 word-app 的 parse.js 模式，字段由「单词 / 释义」改为「代码 / 名称」与交易记录）。
// 支持 CSV / TSV / 粘贴文本；表头中英文均可；也支持 JSON 备份。

/** 轻量 CSV 解析：支持引号、逗号 / 制表符 / 分号分隔 */
export function parseDelimited(text) {
  const t = text.replace(/^﻿/, '').trim();
  if (!t) return [];
  const first = t.split(/\r?\n/, 1)[0];
  const delim = (first.match(/\t/g) || []).length ? '\t' : (first.match(/;/g) || []).length > (first.match(/,/g) || []).length ? ';' : ',';
  const rows = [];
  let row = [], cur = '', q = false;
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (q) {
      if (ch === '"' && t[i + 1] === '"') { cur += '"'; i++; }
      else if (ch === '"') q = false;
      else cur += ch;
    } else if (ch === '"') q = true;
    else if (ch === delim) { row.push(cur); cur = ''; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && t[i + 1] === '\n') i++;
      row.push(cur); cur = '';
      if (row.some(x => x.trim() !== '')) rows.push(row);
      row = [];
    } else cur += ch;
  }
  row.push(cur);
  if (row.some(x => x.trim() !== '')) rows.push(row);
  return rows.map(r => r.map(x => x.trim()));
}

const HEAD = {
  symbol: ['symbol', 'code', '代码', '股票代码', '证券代码'], name: ['name', '名称', '股票名称', '证券名称'],
  date: ['date', '日期', '成交日期', '交易日期'], side: ['side', '方向', '买卖', '买卖方向', '操作'],
  price: ['price', '价格', '成交价', '成交价格'], qty: ['qty', 'quantity', '数量', '股数', '成交数量'],
  fee: ['fee', '费用', '手续费', '总费用'], setup: ['setup', '形态', '形态标签'],
  exit_reason: ['exit_reason', '出场原因', '卖出原因'], initial_stop: ['initial_stop', 'stop', '止损', '止损价', '初始止损'],
  note: ['note', '备注'], signal_run_id: ['signal_run_id', 'run_id'], planned_trigger: ['planned_trigger', '触发价', '计划触发价'],
};

function mapHeader(h) {
  const k = h.trim().toLowerCase();
  for (const [f, names] of Object.entries(HEAD)) if (names.some(n => n.toLowerCase() === k)) return f;
  return null;
}

/** 股票代码规范化：A 股为后端格式 sh.600519 / sz.000001；美股为大写代码（BRK.B -> BRK-B，与 yfinance 一致）。 */
export function normSymbol(raw, market = 'CN') {
  if (market === 'US') {
    const u = String(raw || '').trim().toUpperCase().replace(/\./g, '-');
    return /^[A-Z]{1,5}(-[A-Z])?$/.test(u) ? u : null;
  }
  let s = String(raw || '').trim().toLowerCase().replace(/\s/g, '');
  if (/^(sh|sz)\.\d{6}$/.test(s)) return s;
  const m = s.match(/^(sh|sz)?(\d{6})(\.(sh|sz|ss))?$/);
  if (!m) return null;
  const code = m[2];
  const ex = m[1] || (m[4] ? (m[4] === 'sz' ? 'sz' : 'sh') : (code.startsWith('6') ? 'sh' : 'sz'));
  return `${ex}.${code}`;
}

const SIDE = { buy: 'buy', b: 'buy', 买: 'buy', 买入: 'buy', 证券买入: 'buy', sell: 'sell', s: 'sell', 卖: 'sell', 卖出: 'sell', 证券卖出: 'sell' };

/** 交易记录 CSV -> [{symbol,date,side,price,qty,...}] + 错误列表 */
export function parseTrades(text, market = 'CN') {
  const rows = parseDelimited(text);
  if (rows.length < 2) return { rows: [], errors: [{ row: 0, error: '没有数据行（需要表头 + 至少一行）' }] };
  const cols = rows[0].map(mapHeader);
  for (const need of ['symbol', 'date', 'side', 'price', 'qty']) {
    if (!cols.includes(need)) return { rows: [], errors: [{ row: 0, error: `缺少必需列：${need}（可用中文表头：代码 / 日期 / 方向 / 价格 / 数量）` }] };
  }
  const out = [], errors = [];
  rows.slice(1).forEach((r, i) => {
    const o = {};
    cols.forEach((c, j) => { if (c && r[j] !== undefined && r[j] !== '') o[c] = r[j]; });
    const sym = normSymbol(o.symbol, market);
    const side = SIDE[String(o.side || '').toLowerCase()];
    let date = (o.date || '').replace(/[./]/g, '-').replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3');
    if (/^\d{5}(\.\d+)?$/.test(date)) date = new Date(Date.UTC(1899, 11, 30) + Math.floor(+date) * 86400000).toISOString().slice(0, 10);   // Excel 日期序列号
    const price = parseFloat(o.price), qty = parseInt(String(o.qty).replace(/,/g, ''), 10);
    if (!sym) return errors.push({ row: i + 2, error: `代码无法识别：${o.symbol}` });
    if (!side) return errors.push({ row: i + 2, error: `方向无法识别：${o.side}` });
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return errors.push({ row: i + 2, error: `日期格式应为 YYYY-MM-DD：${o.date}` });
    if (!(price > 0) || !(qty > 0)) return errors.push({ row: i + 2, error: '价格 / 数量须为正数' });
    const rec = { symbol: sym, date, side, price, qty: Math.abs(qty) };
    for (const k of ['fee', 'initial_stop', 'planned_trigger']) if (o[k] !== undefined && !Number.isNaN(parseFloat(o[k]))) rec[k] = parseFloat(o[k]);
    for (const k of ['setup', 'exit_reason', 'note', 'signal_run_id']) if (o[k]) rec[k] = o[k];
    out.push(rec);
  });
  // 按日期排序，保证买入先于卖出
  out.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.side === 'buy' ? -1 : 1));
  return { rows: out, errors };
}

/** 自选股导入：每行一个代码（可带名称 / 备注） */
export function parseWatchlist(text, market = 'CN') {
  const items = [], errors = [];
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim(); if (!t) continue;
    const parts = t.split(/[,\t;，\s]+/);
    const sym = normSymbol(parts[0], market);
    if (sym) items.push({ symbol: sym, note: parts.slice(1).join(' ') || null }); else errors.push(t);
  }
  return { items, errors };
}

export function toCSV(rows, columns) {
  const esc = v => { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  return '﻿' + [columns.map(c => c.label).join(','), ...rows.map(r => columns.map(c => esc(r[c.key])).join(','))].join('\n');
}


// ---- Excel (.xlsx)：不引入任何库——xlsx 是 zip，用浏览器内置 DecompressionStream 解压，再解析 sharedStrings 与第一个工作表 ----
async function inflateRaw(bytes) {
  const ds = new DecompressionStream('deflate-raw');
  const stream = new Blob([bytes]).stream().pipeThrough(ds);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function unzip(buf) {
  const dv = new DataView(buf), u8 = new Uint8Array(buf);
  let eocd = -1;
  for (let i = u8.length - 22; i >= Math.max(0, u8.length - 70000); i--) if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  if (eocd < 0) throw new Error('不是有效的 xlsx 文件');
  const n = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const files = {};
  const dec = new TextDecoder();
  for (let i = 0; i < n; i++) {
    if (dv.getUint32(p, true) !== 0x02014b50) break;
    const method = dv.getUint16(p + 10, true), csize = dv.getUint32(p + 20, true);
    const nlen = dv.getUint16(p + 28, true), elen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true), off = dv.getUint32(p + 42, true);
    const name = dec.decode(u8.subarray(p + 46, p + 46 + nlen));
    files[name] = { method, csize, off };
    p += 46 + nlen + elen + clen;
  }
  const read = async name => {
    const f = files[name]; if (!f) return null;
    const lnlen = dv.getUint16(f.off + 26, true), lelen = dv.getUint16(f.off + 28, true);
    const data = u8.subarray(f.off + 30 + lnlen + lelen, f.off + 30 + lnlen + lelen + f.csize);
    return dec.decode(f.method === 0 ? data : await inflateRaw(data));
  };
  return { read, names: Object.keys(files) };
}

const colIndex = ref => { let n = 0; for (const ch of ref.replace(/[0-9]/g, '')) n = n * 26 + (ch.charCodeAt(0) - 64); return n - 1; };

/** 读取 .xlsx 第一个工作表 -> 二维字符串数组（与 parseDelimited 同形，可直接喂给交易 / 自选解析） */
export async function readXlsx(file) {
  const z = await unzip(await file.arrayBuffer());
  const dp = new DOMParser();
  const shared = [];
  const ss = await z.read('xl/sharedStrings.xml');
  if (ss) for (const si of dp.parseFromString(ss, 'application/xml').getElementsByTagName('si')) shared.push([...si.getElementsByTagName('t')].map(t => t.textContent).join(''));
  const sheetName = z.names.filter(n => /^xl\/worksheets\/sheet\d+\.xml$/.test(n)).sort()[0];
  if (!sheetName) throw new Error('xlsx 中没有工作表');
  const doc = dp.parseFromString(await z.read(sheetName), 'application/xml');
  const rows = [];
  for (const r of doc.getElementsByTagName('row')) {
    const row = [];
    for (const c of r.getElementsByTagName('c')) {
      const i = colIndex(c.getAttribute('r') || 'A1'), t = c.getAttribute('t');
      const v = c.getElementsByTagName('v')[0]?.textContent;
      const is = c.getElementsByTagName('is')[0];
      row[i] = t === 's' ? (shared[+v] ?? '') : t === 'inlineStr' ? [...(is?.getElementsByTagName('t') || [])].map(x => x.textContent).join('') : (v ?? '');
    }
    for (let k = 0; k < row.length; k++) row[k] = row[k] === undefined ? '' : String(row[k]).trim();
    if (row.some(x => x !== '')) rows.push(row);
  }
  return rows;
}

/** 二维数组 -> 制表符分隔文本（交给既有的 parseTrades / parseWatchlist） */
export const rowsToTSV = rows => rows.map(r => r.map(x => String(x).replace(/[\t\r\n]+/g, ' ')).join('\t')).join('\n');

/** 文件 -> 文本：.xlsx 走 readXlsx，其余按文本读取 */
export async function fileToText(file) {
  return /\.xlsx$/i.test(file.name) ? rowsToTSV(await readXlsx(file)) : await file.text();
}
