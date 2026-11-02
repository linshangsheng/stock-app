"""日线看盘辅助：相对强弱线、价格位置（server/daily_view.py）。纯函数 + K 线接口带出这两块。"""
import numpy as np
import pandas as pd

from server import daily_view as dv


def _idx(n):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-01", periods=n)]


def test_rs_line_up_when_stock_beats_market_even_if_price_falls():
    idx = _idx(120)
    bench = pd.Series(np.linspace(100, 70, 120), index=idx)          # 大盘跌 30%
    stock = pd.Series(np.linspace(50, 45, 120), index=idx)           # 个股只跌 10%
    rs = dv.rel_strength(stock, bench)
    assert rs["line"][0]["value"] == 100.0                            # 窗口第一天归一成 100
    assert rs["line"][-1]["value"] > 100 and rs["chg_60"] > 0
    assert rs["state"] == "strong_up"


def test_rs_states_cover_the_four_cases():
    idx = _idx(150)
    flat = pd.Series(100.0, index=idx)
    up = pd.Series(np.linspace(100, 160, 150), index=idx)
    down = pd.Series(np.linspace(160, 100, 150), index=idx)
    assert dv.rel_strength(up, flat)["state"] == "strong_up"
    assert dv.rel_strength(down, flat)["state"] == "weak_down"
    # 涨了很久、最近回落一小段：60 日整体仍强于大盘，但线已经跌到自己的均线下面
    pause = pd.Series(np.r_[np.linspace(100, 200, 120), np.linspace(200, 190, 30)], index=idx)
    assert dv.rel_strength(pause, flat)["state"] == "strong_pause"
    # 跌了很久、最近小幅回升：60 日整体仍弱于大盘，但线在均线上方
    bounce = pd.Series(np.r_[np.linspace(100, 50, 120), np.linspace(50, 55, 30)], index=idx)
    assert dv.rel_strength(bounce, flat)["state"] == "weak_bounce"


def test_rs_needs_benchmark_and_enough_points_and_fills_gaps():
    idx = _idx(100)
    s = pd.Series(np.linspace(10, 20, 100), index=idx)
    assert dv.rel_strength(s, None) is None
    assert dv.rel_strength(s.iloc[:20], pd.Series(100.0, index=idx)) is None
    b = pd.Series(100.0, index=idx[::2])                              # 基准缺一半日期：沿用前一个收盘，不是丢掉个股的点
    assert len(dv.rel_strength(s, b)["line"]) == 100


def test_price_position_basics():
    idx = _idx(300)
    close = pd.Series(np.r_[np.linspace(10, 30, 200), np.linspace(30, 24, 100)], index=idx)
    d = pd.DataFrame({"close": close, "high": close + 0.5, "low": close - 0.5})
    p = dv.price_position(d)
    assert p["close"] == 24.0 and p["hi52"] == d["high"].tail(250).max() and p["lo52"] == d["low"].tail(250).min()
    assert 0 < p["pos52"] < 1 and abs(p["dist_hi52"] - (24.0 / p["hi52"] - 1)) < 1e-9
    assert p["days_since_high"] == 100 and p["days_since_low"] == 249                  # 高点在 100 根之前；一年窗口里最低点是窗口第一根
    assert abs(p["dist_ma20"] - (24.0 / close.tail(20).mean() - 1)) < 1e-9
    assert p["ma200"] is not None and 0 < p["rank_1y"] < 1


def test_price_position_short_history_and_degenerate():
    idx = _idx(40)
    c = pd.Series(10.0, index=idx)
    assert dv.price_position(pd.DataFrame({"close": c, "high": c, "low": c})) is None           # 少于 60 根
    idx = _idx(100)
    c = pd.Series(10.0, index=idx)
    assert dv.price_position(pd.DataFrame({"close": c, "high": c, "low": c})) is None           # 一条直线：高 = 低，没有「位置」可言
    p = dv.price_position(pd.DataFrame({"close": c + np.arange(100) * 0.1, "high": c + np.arange(100) * 0.1 + .1, "low": c + np.arange(100) * 0.1 - .1}))
    assert p["ma200"] is None and p["dist_ma200"] is None                                        # 不足 200 根的均线给 None，不给假数


def test_kline_api_carries_rs_and_position(demo_env):
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from fastapi.testclient import TestClient

    from server import main
    client = TestClient(main.app)
    sym = client.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    k = client.get("/api/kline", params={"symbol": sym}).json()
    assert k["rs"] and k["rs"]["line"] and k["rs"]["line"][0]["value"] == 100.0 and k["bench_name"] == "沪深300"
    assert k["position"]["hi52"] >= k["position"]["close"] >= k["position"]["lo52"] and 0 <= k["position"]["pos52"] <= 1
    w = client.get("/api/kline", params={"symbol": sym, "period": "W"}).json()
    assert w["rs"]["smooth"] == 10 and len(w["rs"]["line"]) == len(w["bars"])
    assert w["position"]["close"] == k["position"]["close"]                      # 周线图上的价格位置仍按日线算，和日线一致
