// 个股「明天怎么操作」卡片：点开股票（详情页）时展示。数据来自后端 selection.operation_plan / open_plan，前端只展示。
import { put } from './api.js';
import { h, fmtPrice, fmtMoney, fmtPct, toast } from './util.js';

const money = v => (v == null ? '—' : fmtMoney(v));

/** s = 清单里这只股票（含 op / open_plan / take_profit / account）；onSaved = 填完资金后的刷新。 */
export function planCard(s, onSaved) {
  if (!s || !s.open_plan?.length) return null;
  const o = s.op, acct = s.account || {};
  const head = o && o.planned_shares
    ? h('div', {}, h('b', {}, '计划：'), `明天开盘买 `, h('b', { class: 'num' }, `${o.planned_shares} 股`), `（约 ${money(o.planned_cost)}），`,
        '跌到 ', h('b', { class: 'num' }, fmtPrice(s.stop_price)), ' 止损，最多亏 ', h('b', { class: 'num' }, money(o.planned_loss)),
        o.capped_by_adv ? '（受成交额容量限制）' : '')
    : h('div', {}, h('b', {}, '计划：'), '明天开盘买，跌到 ', h('b', { class: 'num' }, fmtPrice(s.stop_price)), ' 止损',
        o ? h('span', { style: 'color:var(--red)' }, `（按你的资金连一手都不够：${o.note || '止损距离太大'}）`) : '');
  const tp = s.take_profit ? h('div', {}, h('b', {}, '止盈：'), '买入后挂条件单，涨到 ', h('b', { class: 'num' }, fmtPrice(s.take_profit)),
    `（按参考价估算；实际 = 你的买入价 × ${(1 + s.take_profit_pct).toFixed(2)}）全部卖出；没涨到就按「持仓」页每天更新的移动止损走`) : null;
  const rows = s.open_plan.map(r => h('tr', { class: r.shares === 0 ? 'no' : '' },
    h('td', { style: 'white-space:nowrap' }, r.case), h('td', { class: 'num', style: 'white-space:nowrap' }, r.open),
    h('td', {}, r.action),
    h('td', { class: 'num r' }, r.shares == null ? '—' : r.shares ? `${r.shares} 股` : '不买'),
    h('td', { class: 'num r' }, r.shares ? money(r.cost) : '—'),
    h('td', { class: 'num r' }, r.shares ? money(r.max_loss) : '—')));
  let eq = '';
  const acctBox = o ? h('div', { class: 'tiny muted mt-s' }, `按账户资金 ${money(acct.equity)}、每笔最多亏 ${fmtPct(acct.risk_per_trade, 2, false)}（${money((acct.equity || 0) * (acct.risk_per_trade || 0))}）计算。`, h('a', { href: '#/portfolio' }, '修改'))
    : h('div', { class: 'alert info small mt-s' }, h('b', {}, '填账户资金，表里就会出现具体股数：'),
        h('input', { type: 'number', min: 1000, step: 1000, placeholder: '例如 100000', style: 'width:130px;margin:0 6px', oninput: e => { eq = e.target.value; } }), '元 ',
        h('button', { class: 'btn sm primary', onclick: async () => {
          const v = +eq; if (!(v >= 1000)) { toast('请输入资金（元），至少 1000', 'bad'); return; }
          try { await put('/account', { equity: v, cash: v, risk_per_trade: acct.risk_per_trade ?? 0.0075 }); toast('已保存', 'ok'); onSaved?.(); } catch (e) { toast(e.message, 'bad'); }
        } }, '保存并计算'), h('span', { class: 'tiny muted' }, ' 每笔最多亏 = 资金 × 0.75%（10 万 = 750 元）'));
  return h('div', { class: 'card plan-card mt' },
    h('div', { class: 'card-title' }, h('h3', {}, `明天怎么操作（${s.scan_date || '最新清单'} 收盘生成）`), h('span', { class: 'hint' }, `参考价 ${fmtPrice(s.entry_ref ?? s.close)}（信号日收盘）`)),
    h('div', { class: 'op-line' }, head, tp), acctBox,
    h('div', { class: 'tbl-wrap mt-s' }, h('table', { class: 'mini plan' },
      h('thead', {}, h('tr', {}, ['明天开盘', '开盘价', '怎么做', '买多少', '约花', '碰到止损最多亏'].map((t, i) => h('th', { class: i >= 3 ? 'r' : '' }, t)))),
      h('tbody', {}, rows))),
    h('p', { class: 'tiny muted mt-s' }, '规则与回测一致：高开只少买不多买，保证碰到止损时亏的钱不超过计划；低开不加仓；开盘就在止损价下方或涨停开盘都不买。买入后在券商挂两张条件单：「价格 ≤ 止损价 卖出」和「价格 ≥ 止盈价 卖出」。'));
}
