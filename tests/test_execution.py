"""日线成交规则单测（5.5.1，6.7）：用构造的价格路径验证跳空、止损、A 股 T+1、跌停卖不出、时间退出、涨停买不进。"""
import pytest

from server import backtest, db, execution
from tests.helpers import bar, flat, make_market

H = 80          # 历史长度（让特征窗口就绪）


def ctx_for(bars_after, strategy=None):
    make_market({"sh.600001": flat(H) + bars_after})
    with db.market_db("CN") as c:
        ctx = backtest.BtContext(c, "CN")
    st = backtest.merge_strategy({"exits": {"exit_below_ma": 0, "trail": "none", "max_hold_days": 50}, **(strategy or {})})
    return ctx, st


def test_stop_hit_intraday_fills_at_stop(fresh_env):
    ctx, st = ctx_for([bar(10), bar(9.9, h=9.95, l=8.9, c=9.2)])
    p = ctx.trade_path(0, H, 10.0, 9.0, st)
    assert p["exit_t"] == H + 1 and "止损" in p["reason"] and "跳空" not in p["reason"]
    assert p["exit_adj"] == pytest.approx(9.0 * (1 - st["slippage"]))


def test_gap_below_stop_fills_at_open_worse_than_stop(fresh_env):
    # 主板跌停价 9.0：开盘 9.2（未跌停）低于止损位 9.6 -> 按开盘价成交，劣于止损价
    ctx, st = ctx_for([bar(10), bar(9.2, h=9.3, l=9.1, c=9.2)])
    p = ctx.trade_path(0, H, 10.0, 9.6, st)
    assert "跳空" in p["reason"]
    assert p["exit_adj"] == pytest.approx(9.2 * (1 - st["slippage"])) and p["exit_adj"] < 9.6


def test_t_plus_1_stop_not_active_on_entry_day(fresh_env):
    # 买入日盘中跌破止损但收盘回到止损之上：买入日不能卖出，次日也没有触发 -> 继续持有
    ctx, st = ctx_for([bar(10, h=10.1, l=8.5, c=9.6), bar(9.7, h=9.8, l=9.5, c=9.7)] + flat(3, 9.7))
    p = ctx.trade_path(0, H, 10.0, 9.0, st)
    assert p["exit_t"] is None or p["exit_t"] > H


def test_entry_day_close_below_stop_sells_next_open(fresh_env):
    ctx, st = ctx_for([bar(10, h=10.0, l=8.4, c=8.8), bar(8.7, h=8.9, l=8.6, c=8.7)])
    p = ctx.trade_path(0, H, 10.0, 9.0, st)
    assert p["exit_t"] == H + 1 and "买入日" in p["reason"]
    assert p["exit_adj"] == pytest.approx(8.7 * (1 - st["slippage"]))      # 次日开盘卖出（8.7 高于跌停价 7.92，可成交）


def test_limit_down_open_defers_exit(fresh_env):
    # 前收 10 -> 一字跌停 9.0（主板 10%）：止损位 9.5 被触及但卖不出，顺延到下一个可成交日按开盘价成交，其间亏损照常计入
    ctx, st = ctx_for([bar(10), bar(9.0, h=9.0, l=9.0, c=9.0, vol=100), bar(8.8, h=8.9, l=8.5, c=8.6)])
    p = ctx.trade_path(0, H, 10.0, 9.5, st)
    assert p["exit_t"] == H + 2 and "跌停" in p["reason"]
    assert p["exit_adj"] == pytest.approx(8.8 * (1 - st["slippage"]))


def test_time_exit_sells_on_next_open_after_n_days(fresh_env):
    ctx, st = ctx_for([bar(10)] + flat(8, 10.2))
    st["exits"]["max_hold_days"] = 5
    p = ctx.trade_path(0, H, 10.0, 8.0, st)
    assert p["reason"] == "时间退出" and p["exit_t"] == H + 6, "持有满 N 个交易日后，在第 N+1 日开盘价卖出"


def test_entry_abandoned_when_open_limit_up(fresh_env):
    ctx, st = ctx_for([bar(10.0, h=10.0, l=10.0, c=10.0), bar(11.0, h=11.0, l=11.0, c=11.0, vol=100)])
    k, fill, why = ctx.resolve_entry(0, H, 10.1, 9.0, st)
    assert k is None and "涨停" in why, "涨停买不进：保守处理，放弃该信号"


def test_stop_entry_fills_at_trigger_when_touched_intraday(fresh_env):
    ctx, st = ctx_for([bar(10.0, h=10.5, l=9.9, c=10.4), bar(10.2, h=10.8, l=10.1, c=10.7)], {"entry_mode": "stop_entry"})
    k, fill, why = ctx.resolve_entry(0, H, 10.5, 9.0, st)       # 触发价 10.5，次日开盘 10.2 < 触发价，最高 10.8 >= 触发价
    assert k == H + 1 and fill == pytest.approx(10.5 * (1 + st["slippage"]))


def test_stop_entry_gap_up_fills_at_open(fresh_env):
    ctx, st = ctx_for([bar(10.0, h=10.5, l=9.9, c=10.4), bar(10.9, h=11.0, l=10.8, c=10.95)], {"entry_mode": "stop_entry"})
    k, fill, _ = ctx.resolve_entry(0, H, 10.5, 9.0, st)
    assert fill == pytest.approx(10.9 * (1 + st["slippage"])), "跳空高开：开盘价 >= 触发价时按开盘价成交"


def test_trailing_stop_only_moves_up():
    assert execution.trailing_stop(9.0, 12.0, 0.5, mode="atr", k=3) == pytest.approx(10.5)
    assert execution.trailing_stop(11.0, 12.0, 0.5, mode="atr", k=3) == 11.0          # 不下移
    assert execution.trailing_stop(9.0, 12.0, float("nan"), mode="atr") == 9.0


def test_lot_rounding_and_risk_sizing():
    assert execution.lot_round("main", 1234) == 1200
    assert execution.lot_round("star", 150) == 0 and execution.lot_round("star", 233) == 233      # 科创板 200 股起、之后 1 股递增
    s = execution.size_by_risk(1_000_000, 0.005, 20.0, 18.0, "main", 2e8, 0.01)
    assert s["shares"] == 2500 and s["risk_amount"] == 5000
    s2 = execution.size_by_risk(1_000_000, 0.005, 20.0, 18.0, "main", 1e6, 0.01)               # 单笔容量 1% × 20 日均额
    assert s2["shares"] == 500 and s2["capped_by_adv"]
    s3 = execution.size_by_risk(10_000, 0.005, 20.0, 18.0, "main")                              # 50 元风险不足一手 -> 放弃
    assert s3["shares"] == 0


def test_fees_stamp_duty_only_on_sell():
    buy = execution.trade_fees("buy", 10.0, 1000)
    sell = execution.trade_fees("sell", 10.0, 1000)
    assert sell > buy and buy >= 5.0                       # 佣金最低 5 元；卖出多一笔印花税
