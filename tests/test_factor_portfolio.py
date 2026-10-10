"""低风险组合：调仓规则、调仓日历、无未来函数、实盘名单（卖 / 买 / 继续持有）、「组合」持仓不做止损体检、回测接口（离线，演示数据）。"""
import json
import warnings

import numpy as np
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from server import db, factor_portfolio as fp, market_calendar as mc, portfolio, settings  # noqa: E402


def test_select_targets_keeps_buffered_holdings_and_fills_by_rank():
    order = [f"s{i}" for i in range(1, 41)]                     # s1 排第 1 …
    target, rank = fp.select_targets(order, ["s15", "s25", "gone"], n=10, buffer=2.0)
    assert len(target) == 10 and target[0] == "s15"            # 第 15 名在前 20 名内：继续持有（排在名单最前）
    assert "s25" not in target and "gone" not in target        # 跌出前 20 名 / 不在交易池：卖出
    assert target[1:] == [f"s{i}" for i in range(1, 10)]       # 其余名额按排名补满
    assert rank["s25"] == 25 and "gone" not in rank


def test_rank_order_is_stable_and_skips_nan():
    v = np.array([1.0, np.nan, 3.0, 3.0, 2.0])
    assert list(fp.rank_order(v)) == [2, 3, 4, 0]


def test_schedule_matches_list_days(demo_env):
    with db.market_db("CN") as c:
        lists = fp.list_days(c, every=20)
        asof = db.get_meta(c, "data_asof")
        days = mc.trading_days(c, None, asof)
        d0 = [d for d in lists if d <= asof][-1]
        s0 = fp.schedule(c, d0, every=20)
        assert s0["is_rebalance_day"] and s0["days_left"] == 0 and s0["next_rebalance"] == d0
        nxt = days[days.index(d0) + 1]
        s1 = fp.schedule(c, nxt, every=20)
        assert not s1["is_rebalance_day"] and s1["days_left"] == 19 and s1["last_rebalance"] == d0
        cal = mc.trading_days(c, fp._anchor(), None)
        assert cal.index(d0) % 20 == 0                          # 从 anchor 起每 20 个交易日一次


def test_scores_have_no_lookahead(demo_env):
    """只用截至当日的数据：截断数据重新加载，截断日之前的综合分完全一样。"""
    with db.market_db("CN") as c:
        days = mc.trading_days(c, None, db.get_meta(c, "data_asof"))
        start, cut = days[-400], days[-150]
        full = fp.Data(c, start, None, methods=["lowrisk"])
        part = fp.Data(c, start, cut, methods=["lowrisk"])
    j = [full.syms.index(s) for s in part.syms]
    a, b = full.score("lowrisk")[: len(part.dates)][:, j], part.score("lowrisk")
    assert full.dates[: len(part.dates)] == part.dates
    assert np.allclose(np.nan_to_num(a, nan=-1), np.nan_to_num(b, nan=-1), atol=1e-6)


def test_run_mechanics_cash_lots_and_rebalance_alignment(demo_env):
    with db.market_db("CN") as c:
        days = mc.trading_days(c, None, db.get_meta(c, "data_asof"))
        D = fp.Data(c, days[-500], None, methods=["lowrisk"])
        lists = fp.list_days(c, days[-380], None, every=20)
    r = fp.run(D, {"n": 5, "rebalance_days": 20}, lists[0], None, 100_000, rb_dates=set(lists))
    eq = r["curve"]
    assert eq.index[0] == lists[0] and eq.iloc[0] == 100_000
    assert (r["holdings"] <= 6).all() and r["holdings"].iloc[-1] >= 3        # 等金额 5 只（个别跌停卖不出时可能多 1 只）
    assert r["n_rebalance"] == len([d for d in lists if d < eq.index[-1]])
    assert r["fees"] > 0 and 0 < r["invested"].iloc[-1] <= 1.0001
    assert r["trades"]
    for t in r["trades"]:                                         # 只在调仓日的下一个交易日开盘买入（买不进就放弃，不追）
        assert D.dates[D.dates.index(t["entry"]) - 1] in lists and t["exit"] > t["entry"]


def test_live_plan_sell_buy_keep_and_shares(demo_env):
    from server import scanner
    with settings.market_ctx("CN"), db.market_db("CN") as c:
        day = db.get_meta(c, "data_asof")
        ctx = scanner.build_context(c, day, "CN")
        acct = {"equity": 100_000}
        first = fp.live_plan(c, ctx, acct, [], "CN")
        assert first["mode"] == "start" and len(first["targets"]) == settings.cfg()["factor_portfolio"]["n"]
        assert first["allocation"]["sleeve"] == 30_000 and first["allocation"]["per_stock"] == 3000
        for t in first["targets"]:
            if t.get("shares"):
                assert t["shares"] % 100 == 0 and t["amount"] <= 3000 + 1e-6
        keep_sym = first["targets"][2]["symbol"]
        ranks = sorted((t["rank"], t["symbol"]) for t in first["targets"])
        uni = ctx["l2"].loc[day]
        far = [s for s in uni.index[uni.values] if s not in {t["symbol"] for t in first["targets"]}]
        far_rank = fp.select_targets(fp_order(ctx, day), [], 10 ** 6, 1)[1]
        out_sym = max(far, key=lambda s: far_rank.get(s, 0))          # 排名最靠后的一只：一定在 2N 名之外
        pos = [{"id": 1, "symbol": keep_sym, "qty": 100, "avg_cost": 10.0, "open_date": day, "setup": "factor"},
               {"id": 2, "symbol": out_sym, "qty": 100, "avg_cost": 10.0, "open_date": day, "setup": "factor"},
               {"id": 3, "symbol": ranks[0][1], "qty": 100, "avg_cost": 10.0, "open_date": day, "setup": "vcp"}]   # 波段持仓不算组合
        plan = fp.live_plan(c, ctx, acct, pos, "CN")
    h = {x["symbol"]: x for x in plan["holdings"]}
    assert set(h) == {keep_sym, out_sym}
    assert h[keep_sym]["in_target"] and not h[out_sym]["in_target"] and "排名" in h[out_sym]["why"]
    assert plan["mode"] in ("rebalance", "hold")
    assert keep_sym in {t["symbol"] for t in plan["targets"] if t["held"]}
    if plan["mode"] == "rebalance":
        assert plan["actions"]["sell"] == [out_sym] and keep_sym not in plan["actions"]["buy"]


def fp_order(ctx, day):
    F, l2 = ctx["feat"].f, ctx["l2"]
    s = fp.scores({k: F[k].loc[[day]] for k in ("turnover_ma20", "atr_pct")}, l2.loc[[day]], "lowrisk").iloc[0]
    return [s.index[j] for j in fp.rank_order(s.to_numpy(dtype=float))]


def test_factor_position_needs_no_stop_and_skips_stop_checks(demo_env):
    with db.market_db("CN") as c:
        day = db.get_meta(c, "data_asof")
        sym = c.execute("SELECT symbol FROM daily_bar WHERE date=? AND trade_status=1 LIMIT 1", (day,)).fetchone()[0]
        px = c.execute("SELECT close FROM daily_bar WHERE symbol=? AND date=?", (sym, day)).fetchone()[0]
    with pytest.raises(ValueError):
        portfolio.record_trade("CN", {"symbol": sym, "date": day, "side": "buy", "price": px, "qty": 100, "setup": "vcp"})
    portfolio.record_trade("CN", {"symbol": sym, "date": day, "side": "buy", "price": px * 1.5, "qty": 100, "setup": "factor"})
    hl = portfolio.health("CN", day, save=False)
    p = hl["positions"][0]
    assert p["setup"] == "factor" and p["level"] == "ok" and p["risk_to_stop"] == 0 and p["current_stop"] is None
    assert "调仓日" in p["action"]
    sm = hl["summary"]
    assert sm["factor_count"] == 1 and sm["swing_count"] == 0 and sm["slots_free"] == sm["max_positions"] and sm["risk_to_stop"] == 0
    portfolio.record_trade("CN", {"symbol": sym, "date": day, "side": "sell", "price": px, "qty": 100, "exit_reason": "调仓"})
    assert not portfolio.list_positions("CN")


def test_backtest_job_and_summary_cache(demo_env):
    from server import backtest
    res = backtest.run_job("CN", "factor", {"factor": {"n": 5, "equity": 100000}})
    assert res["kind"] == "factor" and res["run_id"].startswith("BT-FAC")
    assert res["rows"][0]["name"] == "低风险组合" and res["rows"][0]["all"]["cagr"] is not None
    assert any(r["name"] == "交易池等权" for r in res["rows"]) and res["by_year"] and res["curve"]
    assert res["health"]["status"] in ("正常", "注意", "警告") and res["params"]["n"] == 5
    assert fp.refresh_summary("CN") == "done"
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
    got = fp.get_summary("CN", asof, None, background=False)
    assert got["status"] == "ok" and got["params"]["n"] == settings.cfg()["factor_portfolio"]["n"]
    assert fp.refresh_summary("CN") == "cached"
    cached = json.loads(fp._cache_file("CN").read_text(encoding="utf-8"))
    assert cached["key"] == fp.summary_key(asof, fp.bt_equity(None))


def test_factor_plan_api_and_allocation(demo_env):
    from fastapi.testclient import TestClient
    from server import main
    cl = TestClient(main.app)
    cl.put("/api/account", json={"equity": 100000, "cash": 100000, "risk_per_trade": 0.0075})
    j = cl.get("/api/factor/plan", params={"summary": 0}).json()
    assert j["status"] == "ok" and j["mode"] == "start" and j["targets"] and j["schedule"]["every"] == 20
    assert j["allocation"]["etf"] == 0.7 and j["allocation"]["factor"] == 0.3 and j["allocation"]["swing"] == 0
    assert cl.get("/api/factor/plan", params={"market": "US"}).json()["status"] == "unsupported"
    mv = cl.get("/api/market/view").json()
    if mv.get("allocation"):
        assert mv["allocation"]["factor_amount"] == 30000 and mv["allocation"]["stock_amount"] == 0
