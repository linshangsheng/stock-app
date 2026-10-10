"""市场温度 / 指数择时：无未来函数、次日开盘执行、止损与离场规则、宽度增量与全量一致、API 冒烟（离线，演示数据）。"""
import numpy as np
import pandas as pd

from server import db, market_view as mv


def _px(close, open_=None):
    idx = pd.Index([f"2020-01-{i + 1:02d}" if i < 31 else f"2020-02-{i - 30:02d}" for i in range(len(close))])
    c = pd.Series(close, index=idx, dtype=float)
    o = pd.Series(open_ if open_ is not None else close, index=idx, dtype=float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.005, "low": np.minimum(o, c) * 0.995, "close": c})


RULE = {"cost_per_side": 0.0, "trend": {"weight": 0.5, "ma": 5, "band": 0.02},
        "washout": {"weight": 0.5, "enter_below": 0.15, "exit_above": 0.8, "max_hold_days": 5, "stop_atr_k": 3.0, "atr_n": 3}}


def test_washout_enters_next_open_and_exits_on_time():
    px = _px([100.0] * 30, open_=[100.0 + i for i in range(30)])
    b = pd.Series(0.5, index=px.index)
    b.iloc[10] = 0.10                                  # 第 10 天收盘出现冰点
    sim = mv.simulate(px, b, RULE)
    assert sim["washout"].iloc[9] == 0 and sim["washout"].iloc[10] == 1    # 收盘决定
    t = [x for x in sim["trades"] if x["sleeve"] == "washout"][0]
    assert t["entry_date"] == px.index[11] and t["entry"] == px["open"].iloc[11]   # 次日开盘成交
    assert t["exit_signal_date"] == px.index[15] and "持有满" in t["reason"]


def test_washout_stop_on_close_below():
    close = [100.0] * 12 + [80.0] + [80.0] * 10
    px = _px(close)
    b = pd.Series(0.5, index=px.index)
    b.iloc[10] = 0.10
    sim = mv.simulate(px, b, RULE)
    t = [x for x in sim["trades"] if x["sleeve"] == "washout"][0]
    assert t["reason"] == "收盘跌破止损价" and t["exit_signal_date"] == px.index[12]


def test_no_lookahead_signals_unchanged_by_future_bars():
    rng = np.random.default_rng(3)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.02, 60))
    px = _px(close)
    b = pd.Series(rng.uniform(0.05, 0.9, 60), index=px.index)
    full = mv.simulate(px, b, RULE)["pos"]
    cut = mv.simulate(px.iloc[:40], b.iloc[:40], RULE)["pos"]
    assert (full.iloc[:40].to_numpy() == cut.to_numpy()).all()


def test_daily_returns_execute_next_open():
    px = _px([100, 100, 110, 121], open_=[100, 100, 105, 121])
    pos = pd.Series([0, 1, 1, 1], index=px.index, dtype=float)       # 第 1 天收盘决定买入
    r = mv.daily_returns(px, pos, 0.0)
    assert r.iloc[1] == 0                                             # 当天不享受收益
    assert abs(r.iloc[2] - (110 / 105 - 1)) < 1e-12                   # 次日开盘 105 买入，收于 110
    assert abs(r.iloc[3] - (121 / 110 - 1)) < 1e-12


def test_zone_of():
    assert [mv.zone_of(x) for x in (0.05, 0.2, 0.5, 0.7, 0.9)] == [0, 1, 2, 3, 4]
    assert mv.zone_of(None) is None


def test_breadth_incremental_matches_full(demo_env):
    with db.market_db("CN") as c:
        c.execute("DELETE FROM market_breadth")
        full = mv.compute_breadth(c)
        mv.ensure_breadth(c, "CN")
        last = c.execute("SELECT MAX(date) FROM market_breadth").fetchone()[0]
        c.execute("DELETE FROM market_breadth WHERE date >= (SELECT date FROM market_breadth ORDER BY date DESC LIMIT 1 OFFSET 3)")
        mv.ensure_breadth(c, "CN")                                    # 增量：只重算最近一段
        got = mv.load_breadth(c)
    assert got.index[-1] == last
    exp = full.loc[got.index[-5:], "b20"].to_numpy()
    assert np.allclose(got["b20"].tail(5).to_numpy(), exp)


def test_market_view_api(demo_env):
    from fastapi.testclient import TestClient
    from server import main
    with db.market_db("CN") as c:
        mv.ensure_breadth(c, "CN")
    mv._cache.clear()
    d = TestClient(main.app).get("/api/market/view").json()
    assert d["status"] == "ok"
    t = d["thermometer"]
    assert 0 <= t["b20"] <= 1 and t["zone_name"] in mv.ZONES[t["zone"]]
    assert d["indices"], "至少一个宽基指数"
    i = d["indices"][0]
    assert i["position"] in (0.0, 0.5, 1.0)
    assert ("exit_level" in i["trend"]) and ("buy_level" in i["trend"])
    assert set(i["stats"]) >= {"all", "is", "oos", "washout_trades"}
