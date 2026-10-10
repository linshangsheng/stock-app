"""回测补全：固定目标位 / 分批止盈 / 结构化止损 / 仓位方式 / Deflated Sharpe / Walk-forward 隔离期 / 方案对比。"""
import math

import pytest

from server import backtest, db, selection
from tests.helpers import bar, flat, make_market

H = 80


def ctx_for(bars_after, exits=None):
    make_market({"sh.600001": flat(H) + bars_after})
    with db.market_db("CN") as c:
        ctx = backtest.BtContext(c, "CN")
    st = backtest.merge_strategy({"exits": {"exit_below_ma": 0, "trail": "none", "max_hold_days": 50, **(exits or {})}})
    return ctx, st


def test_fixed_take_profit_fills_at_target_and_gap_up_at_open(fresh_env):
    ctx, st = ctx_for([bar(10), bar(10.2, h=11.8, l=10.1, c=11.5)], {"take_profit_pct": 0.10})
    p = ctx.trade_path(0, H, 10.0, 9.0, st)
    assert p["reason"] == "固定止盈" and p["exit_adj"] == pytest.approx(11.0 * (1 - st["slippage"]))
    ctx2, st2 = ctx_for([bar(10), bar(11.6, h=11.9, l=11.5, c=11.8)], {"take_profit_pct": 0.10})
    p2 = ctx2.trade_path(0, H, 10.0, 9.0, st2)
    assert p2["exit_adj"] == pytest.approx(11.6 * (1 - st2["slippage"])), "跳空越过目标位：按开盘价成交"


def test_stop_and_target_same_day_worst_case_is_stop(fresh_env):
    ctx, st = ctx_for([bar(10), bar(10.0, h=11.5, l=8.8, c=10.5)], {"take_profit_pct": 0.10})
    p = ctx.trade_path(0, H, 10.0, 9.0, st)
    assert "止损" in p["reason"], "止损与止盈同日触及：日线无法判断先后，一律先止损（5.5.1-3）"


def test_take_profit_in_r_multiples(fresh_env):
    ctx, st = ctx_for([bar(10), bar(10.1, h=12.2, l=10.0, c=12.0)], {"take_profit_r": 2.0, "take_profit_pct": 0})
    p = ctx.trade_path(0, H, 10.0, 9.0, st)               # 风险 1，目标 = 10 + 2×1 = 12
    assert p["reason"] == "固定止盈" and p["exit_adj"] == pytest.approx(12.0 * (1 - st["slippage"]))


def test_partial_take_profit_records_and_moves_stop_to_breakeven(fresh_env):
    ctx, st = ctx_for([bar(10), bar(10.2, h=11.6, l=10.1, c=11.4), bar(11.0, h=11.2, l=9.9, c=10.0)], {"partial_r": 1.5, "partial_fraction": 0.5, "take_profit_pct": 0})
    p = ctx.trade_path(0, H, 10.0, 9.0, st)
    t, px, frac = p["partial"]
    assert t == H + 1 and frac == 0.5 and px == pytest.approx(11.5 * (1 - st["slippage"]))
    assert p["exit_t"] == H + 2 and p["exit_adj"] == pytest.approx(10.0 * (1 - st["slippage"])), "分批后止损上移到成本价，随后在成本价附近出场"


def test_partial_in_portfolio_run_books_both_legs(demo_env):
    with db.market_db("CN") as c:
        ctx = backtest.BtContext(c, "CN", "2022-01-01", None)
    r = ctx.run({"exits": {"partial_r": 1.0, "partial_fraction": 0.5}})
    parts = [t for t in r["trades"] if t["partial"]]
    assert parts, "应有触发分批止盈的交易"
    # 分批止盈后总盈亏 = 两段盈亏之和；R 仍以初始风险计
    assert all(t["shares"] > 0 and t["r"] is not None for t in parts)


def test_structure_stop_uses_pattern_low_with_atr_fallback_and_hard_clamp():
    c = {"close": 10.0, "high": 10.3, "atr14": 0.5, "struct_low": 9.2}
    _, _, stop = selection.plan_entry(c, "next_open", "structure", {"stop_atr_k": 2, "hard_stop_pct": 0.10})
    assert stop == pytest.approx(9.19)                                         # 形态低点下方一个价位
    far = {**c, "struct_low": 7.0}
    assert selection.plan_entry(far, "next_open", "structure", {"stop_atr_k": 2, "hard_stop_pct": 0.10})[2] == pytest.approx(9.0), "受硬止损约束"
    above = {**c, "struct_low": 10.5}
    assert selection.plan_entry(above, "next_open", "structure", {"stop_atr_k": 2, "hard_stop_pct": 0.10})[2] == pytest.approx(9.0), "低点不在入场价之下：回退 ATR（10-2×0.5=9）"
    assert selection.plan_entry(c, "next_open", "atr", {"stop_atr_k": 3, "hard_stop_pct": 0.5})[2] == pytest.approx(8.5)    # 策略自己的 k 生效


def test_sizing_modes():
    kw = dict(equity=1_000_000, risk_pct=0.005, ref=20.0, stop=18.0, board="main", adv=2e8, max_adv_pct=0.01, max_positions=8, atr_pct=0.03)
    risk = selection.size_position("risk", **kw)
    equal = selection.size_position("equal", **kw)
    fixed = selection.size_position("fixed_pct", fixed_pct=0.1, **kw)
    vol_hi = selection.size_position("vol_inverse", **{**kw, "atr_pct": 0.06})
    vol_lo = selection.size_position("vol_inverse", **{**kw, "atr_pct": 0.015})
    assert risk["shares"] == 2500 and risk["risk_amount"] == 5000
    assert equal["shares"] == 6200 and fixed["shares"] == 5000                 # 125,000/20 与 100,000/20，整手取整
    assert vol_hi["shares"] < equal["shares"] < vol_lo["shares"], "ATR% 越高仓位越小"
    with pytest.raises(ValueError):
        selection.size_position("bogus", **kw)


def test_deflated_sharpe_penalizes_many_trials():
    mom = {"sr_daily": 0.08, "skew": 0.0, "kurt": 3.0, "T": 1500}
    one = backtest.deflated_sharpe(mom, 1)
    many = backtest.deflated_sharpe(mom, 200)
    assert one["dsr"] > many["dsr"] and many["benchmark_sr_daily"] > 0
    weak = backtest.deflated_sharpe({"sr_daily": 0.01, "skew": 0.0, "kurt": 3.0, "T": 1000}, 100)
    assert weak["dsr"] < 0.5, "试验很多、夏普很弱：多半只是运气"
    assert math.isclose(backtest.deflated_sharpe(mom, 1)["benchmark_sr_daily"], 0.0)


def test_walk_forward_embargo_and_oos_only(demo_env):
    with db.market_db("CN") as c:
        ctx = backtest.BtContext(c, "CN", "2021-01-01", None)
    res = backtest.walk_forward(ctx, {"start": "2021-01-01"}, "breakout", {"vol_ratio_min": [1.2, 1.8]}, train_years=2, test_months=6)
    assert res["n_windows"] >= 3 and res["embargo_days"] >= 5
    dates = ctx.dates
    for w in res["windows"]:
        gap = dates.index(w["test"][0]) - dates.index(w["train"][1]) - 1
        assert gap >= res["embargo_days"], "训练段与测试段之间留出不少于持仓周期的隔离期"
        assert w["test"][0] > w["train"][1]
    # 测试段互不重叠且样本外汇总只含测试段的交易
    tests = [w["test"] for w in res["windows"]]
    assert all(a[1] < b[0] for a, b in zip(tests, tests[1:]))
    assert res["oos"]["n"] == sum(w["test_n"] for w in res["windows"] if w.get("chosen"))
    with pytest.raises(ValueError):
        backtest.walk_forward(ctx, None, "breakout", {"a": [1], "b": [1], "c": [1], "d": [1]})


def test_compare_variants_covers_spec_items(demo_env):
    with db.market_db("CN") as c:
        ctx = backtest.BtContext(c, "CN", "2023-01-01", None)
    res = backtest.compare_variants(ctx, {"start": "2023-01-01"})
    groups = {r["group"] for r in res["rows"]}
    assert groups >= {"入场模式", "止盈方式", "止损", "仓位", "候选取舍", "成本敏感性"}
    by = {r["variant"]: r for r in res["rows"]}
    assert by["止盈：固定目标 +15%（对照）"]["n"] > 0 and by["成本：费用与滑点 ×4"]["expectancy_r"] <= by["基准（默认配置）"]["expectancy_r"]


def test_run_job_strategy_attaches_deflated_sharpe_with_trial_count(demo_env):
    res = backtest.run_job("CN", "strategy", {"strategy": {"id": "dsr-test", "start": "2022-01-01"}})
    d = res["metrics"]["deflated_sharpe"]
    assert d["trials"] == res["trial_count"] == 1
    res2 = backtest.run_job("CN", "strategy", {"strategy": {"id": "dsr-test", "start": "2022-01-01"}})
    assert res2["metrics"]["deflated_sharpe"]["trials"] == 2, "同一策略多次尝试：试验次数累加，DSR 随之收紧"
