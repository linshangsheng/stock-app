// 各视图共用的小组件（避免 app.js 与视图之间循环依赖）
import { h, clear } from './util.js';

export function notImplemented(el) {
  clear(el);
  el.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '美股（M5）尚未实现'),
    h('div', {}, '第一阶段聚焦「选股 → 验证 → 持仓管理」，A 股先行（需求书 1.7）。美股沿同一框架接入 yfinance 数据层后启用，参数需独立验证、回测明示幸存者偏差。'),
    h('div', { class: 'mt' }, h('button', { class: 'btn primary', onclick: () => document.querySelector('[data-market="CN"]').click() }, '切回 A 股'))));
}
