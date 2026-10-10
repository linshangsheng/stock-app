"""观察清单的「操作计算」：次日开盘买几股、最多亏、高开时按开盘价少买（只少不多）、放弃价；以及录入资金后清单接口带上操作。"""
from server import selection


def test_operation_plan_resizes_down_on_gap_and_gives_up():
    o = selection.operation_plan(8.57, 7.71, "chinext", 100000, 0.005)
    assert o["risk_budget"] == 500 and o["planned_shares"] == 500 and o["planned_loss"] == 430
    sh = {round(s["gap"], 3): s["shares"] for s in o["scenarios"]}
    assert {g: sh[g] for g in (0.0, 0.03, 0.05)} == {0.0: 500, 0.03: 400, 0.05: 300}     # 高开越多买得越少
    assert set(sh) == {0.0, 0.005, 0.01, 0.02, 0.03, 0.05}
    assert all(s["max_loss"] <= 500 for s in o["scenarios"])          # 无论高开多少，打到止损都不超预算
    assert o["give_up_above"] == round(7.71 + 500 / 100, 2)            # 高于它连一手都超预算


def test_operation_plan_never_buys_more_than_planned():
    o = selection.operation_plan(10.0, 9.0, "main", 1_000_000, 0.005)
    planned = o["planned_shares"]
    assert all(s["shares"] <= planned for s in o["scenarios"])


def test_operation_plan_star_board_min_lot_and_too_small_account():
    o = selection.operation_plan(22.56, 19.56, "star", 100000, 0.005)    # 科创板 200 股起：一手风险 600 > 500
    assert o["planned_shares"] == 0 and o["note"]
    assert selection.operation_plan(10, 11, "main", 100000, 0.005) is None     # 止损不在参考价下方：不给操作


def test_scan_api_attaches_operation_after_account_entered(demo_env):
    from fastapi.testclient import TestClient
    from server import main, scanner
    from tests.test_scanner_jobs import _day_with_candidates
    scanner.run_scan("CN", scan_date=_day_with_candidates())
    cl = TestClient(main.app)
    before = cl.get("/api/scan").json()
    assert before["candidates"] and all(c.get("op") is None for c in before["candidates"])
    assert all(c["open_plan"] for c in before["candidates"]), "没填资金也给出开盘情景（只是没有股数）"
    cl.put("/api/account", json={"equity": 500000, "cash": 500000, "risk_per_trade": 0.005})
    after = cl.get("/api/scan").json()
    ops = [c["op"] for c in after["candidates"] if c.get("op")]
    assert ops and all(o["risk_budget"] == 2500 for o in ops)
    assert after["account"]["equity"] == 500000


def test_rescan_when_latest_list_used_old_config(demo_env):
    from server import db, scanner, settings
    with db.market_db("CN") as c:
        day = db.get_meta(c, "data_asof")
    scanner.run_scan("CN", scan_date=day)
    assert scanner.rescan_if_config_changed("CN") is None            # 参数没变：不重扫
    settings.cfg()["exits"]["max_hold_days"] = 17                     # 模拟升级后参数变化
    r = scanner.rescan_if_config_changed("CN")
    assert r is not None and r["scan_date"] == day
    assert scanner.rescan_if_config_changed("CN") is None            # 重扫一次后就一致了


def test_open_plan_covers_gap_down_up_and_limit():
    rows = selection.open_plan(10.58, 10.09, "main")
    cases = [r["case"] for r in rows]
    assert cases[:3] == ["低开到止损价以下", "低开", "平开"] and "涨停开盘" in cases
    assert rows[0]["action"].startswith("不买") and rows[-1]["open"] == "= 11.64"
    op = selection.operation_plan(10.58, 10.09, "main", 100000, 0.005)
    rows = selection.open_plan(10.58, 10.09, "main", op)
    by = {r["case"]: r for r in rows}
    assert [r["case"] for r in rows] == ["低开到止损价以下", "低开", "平开", "高开 0.5%", "高开 1%", "高开 2%", "高开 3%", "高开 5%", "涨停开盘"]
    assert by["低开"]["shares"] == by["平开"]["shares"] == 1000 and by["高开 3%"]["shares"] == 600 and by["高开 5%"]["shares"] == 400
    assert by["低开到止损价以下"]["shares"] == 0 and by["涨停开盘"]["shares"] == 0
    assert all(r["max_loss"] <= 500 for r in rows if r["max_loss"] is not None), "任何开盘价下碰到止损都不超过最多亏"
    sh = [by[k]["shares"] for k in ("平开", "高开 0.5%", "高开 1%", "高开 2%", "高开 3%", "高开 5%")]
    assert sh == sorted(sh, reverse=True), "高开越多买得越少"
    assert not any(r["case"] == "高开太多" for r in rows), "放弃价高于涨停价：不可能发生，不列"
    poor = selection.operation_plan(50.0, 44.0, "chinext", 100000, 0.005)
    assert [r["case"] for r in selection.open_plan(50.0, 44.0, "chinext", poor)] == ["任何开盘价"]


def test_index_next_open_action(demo_env):
    from server import db, market_view as mv
    with db.market_db("CN") as c:
        mv.ensure_breadth(c, "CN")
    mv._cache.clear()
    for i in mv.build("CN")["indices"]:
        no = i["next_open"]
        assert "不论高开低开" in no["text"] and no["gap_median"] is not None
        assert no["tomorrow"][0]["when"] == "明天开盘" and any(r["when"] == "明天收盘" for r in no["tomorrow"])



def test_index_tomorrow_close_threshold_matches_engine(demo_env):
    """明天收盘价的触发点位与规则引擎一致：略低于点位不触发、略高于就触发（或持有时反向）。"""
    import pandas as pd
    from server import db, market_view as mv
    with db.market_db("CN") as c:
        mv.ensure_breadth(c, "CN")
        mv._cache.clear()
        d = mv.build("CN")
        br = mv.load_breadth(c)
        i = d["indices"][0]
        px = mv._index_px(c, i["symbol"])
    rule_on = i["trend"]["rule_on"]                      # 均线规则本身的状态（轮动只决定能不能持有，不改变点位）
    lvl = i["next_open"]["exit_close" if rule_on else "buy_close"]
    got = []
    for cl in (lvl * 0.999, lvl * 1.001):
        nxt = pd.DataFrame({"open": [cl], "high": [cl], "low": [cl], "close": [cl]}, index=["2099-01-01"])
        sim = mv.simulate(pd.concat([px, nxt]), pd.concat([br["b20"], pd.Series([0.5], index=["2099-01-01"])]))
        got.append(int(sim["trend_rule"]))
    assert got == [0, 1]


def test_market_view_allocation_follows_account(demo_env):
    from fastapi.testclient import TestClient
    from server import db, main, market_view as mv
    with db.market_db("CN") as c:
        mv.ensure_breadth(c, "CN")
    mv._cache.clear()
    cl = TestClient(main.app)
    assert "allocation" not in cl.get("/api/market/view").json()          # 没填资金：不给金额
    cl.put("/api/account", json={"equity": 100000, "cash": 100000, "risk_per_trade": 0.0075})
    d = cl.get("/api/market/view").json()
    al, n = d["allocation"], len(d["indices"])
    assert al["etf_amount"] == 70000 and al["stock_amount"] == 30000 and al["stock_max_positions"] == 2
    assert al["per_index"] == round(70000 / n, 2) and al["trend_amount"] == al["washout_amount"] == round(70000 / n / 2, 2)
    assert all(i["hold_amount"] == round(al["per_index"] * i["position"], 2) for i in d["indices"])



def test_rotation_only_top_k_hold_trend_sleeve(demo_env):
    """轮动：同一天最多只有 top_k 个指数持有趋势仓；名次超出 top_k 的不持有。"""
    from server import db, market_view as mv, settings
    with db.market_db("CN") as c:
        mv.ensure_breadth(c, "CN")
    mv._cache.clear()
    d = mv.build("CN")
    k = settings.cfg()["market_view"]["index_rule"]["trend"]["top_k"]
    holders = [i for i in d["indices"] if i["trend"]["holding"]]
    assert len(holders) <= k and all(i["trend"]["rank"] <= k for i in holders)
    assert d["portfolio"]["oos"]["rule"] and d["portfolio"]["oos"]["hold"]



def test_portfolio_health_and_yearly_table(demo_env):
    """组合分年度表与策略健康度：当前回撤不会比历史最大回撤更深（同一条权益曲线）；状态只取三档之一。"""
    from server import db, market_view as mv
    with db.market_db("CN") as c:
        mv.ensure_breadth(c, "CN")
    mv._cache.clear()
    pf = mv.build("CN")["portfolio"]
    hl = pf["health"]
    assert hl["status"] in ("正常", "注意", "警告") and hl["max_dd"] <= hl["current_dd"] <= 0
    assert pf["by_year"] and all({"year", "rule", "hold"} <= set(y) for y in pf["by_year"])
