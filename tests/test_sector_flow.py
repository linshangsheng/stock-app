"""资金流向（估算净流入）：口径、窗口累加、板块内个股、接口。"""
import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, market_calendar as mc, sector_flow as sf  # noqa: E402


def _manual(conn, key, day):
    """不经过 sector_flow，直接用 SQL + 逐只计算某个板块某一天的成交额 / 估算净流入（对照用）。"""
    prev = mc.trading_days(conn, None, day)[-2]
    rows = conn.execute("SELECT m.symbol, a.close*a.adj_factor, a.amount, b.close*b.adj_factor FROM industry_map m "
                        "JOIN daily_bar a ON a.symbol=m.symbol AND a.date=? JOIN daily_bar b ON b.symbol=m.symbol AND b.date=? "
                        "WHERE m.industry=? AND a.trade_status=1 AND a.amount>0", (day, prev, key)).fetchall()
    amt = sum(r[2] for r in rows)
    net = sum(r[2] * np.sign(r[1] / r[3] - 1) for r in rows)
    return len(rows), amt, net


def test_build_matches_manual_calculation(demo_env):
    d = sf.build("CN", None, 1)
    assert d["status"] == "ok" and d["window"] == 1 and len(d["sectors"]) >= 5
    ss = d["sectors"]
    assert [s["strength"] for s in ss] == sorted((s["strength"] for s in ss), reverse=True) or all(
        (a["strength"], a["ret"]) >= (b["strength"], b["ret"]) for a, b in zip(ss, ss[1:]))          # 按强度排序（同强度按涨跌）
    for s in ss:
        assert s["n"] >= sf.MIN_STOCKS and abs(s["strength"] - s["net"] / s["amount"]) < 1e-9 and -1 <= s["strength"] <= 1
    assert sum(s["share"] for s in ss) <= 1 + 1e-9
    with db.market_db("CN") as c:
        s = ss[0]
        n, amt, net = _manual(c, s["key"], d["day"])
        assert n == s["n"] and abs(amt - s["amount"]) < 1 and abs(net - s["net"]) < 1


def test_window_net_is_sum_of_daily_nets(demo_env):
    w5 = sf.build("CN", None, 5)
    assert w5["window"] == 5 and len(w5["window_days"]) == 5
    daily = [sf.build("CN", day, 1) for day in w5["window_days"]]
    for s in w5["sectors"][:6]:
        net = sum(next((x["net"] for x in d["sectors"] if x["key"] == s["key"]), 0.0) for d in daily)
        amt = sum(next((x["amount"] for x in d["sectors"] if x["key"] == s["key"]), 0.0) for d in daily)
        assert abs(net - s["net"]) < 1 and abs(amt - s["amount"]) < 1


def test_stocks_in_sector_add_up_and_non_trading_day_falls_back(demo_env):
    d = sf.build("CN", None, 1)
    key = d["sectors"][0]["key"]
    st = sf.stocks("CN", d["day"], 1, key, top=10000)
    assert st["status"] == "ok" and st["n"] == d["sectors"][0]["n"]
    nets = [x["net"] for x in st["inflow"]] + [x["net"] for x in st["outflow"]]
    assert [x["net"] for x in st["inflow"]] == sorted((x["net"] for x in st["inflow"]), reverse=True) and all(n < 0 for n in (x["net"] for x in st["outflow"]))
    assert abs(sum(nets) - d["sectors"][0]["net"]) < 1                                             # 流入 + 流出的个股合计 = 板块估算净流入（平盘的为 0）
    sat = (pd.Timestamp(d["day"]) - pd.Timedelta(days=(pd.Timestamp(d["day"]).weekday() + 2) % 7 or 7)).strftime("%Y-%m-%d")
    got = sf.build("CN", sat, 1)
    assert got["status"] == "ok" and got["day"] <= sat


def test_short_names_and_api(demo_env):
    assert sf.short_name("CN", "C39计算机、通信和其他电子设备制造业") == "电子" and sf.short_name("CN", "J66货币金融服务") == "银行"
    assert len(set(sf.CN_SHORT.values())) == len(sf.CN_SHORT)                                     # 短名不重复
    assert sf.short_name("US", "Software—Application") == "应用软件" and sf.short_name("US", "Technology") == "科技"
    assert len(set(sf.US_ZH.values())) == len(sf.US_ZH) and all(len(v) <= 6 for v in sf.US_ZH.values())   # 美股短名不重复、不超过 6 个字（圆环里放得下）
    assert sf.short_name("US", "Some New—Industry") == "Some New"                                            # 没翻译的退回英文第一段
    from server import main
    c = TestClient(main.app)
    j = c.get("/api/flow/sectors", params={"market": "CN", "window": 5}).json()
    assert j["status"] == "ok" and j["market"] == "CN" and j["window"] == 5 and j["sectors"] and "不是主力资金" in j["note"]
    k = j["sectors"][0]["key"]
    s = c.get("/api/flow/stocks", params={"market": "CN", "day": j["day"], "window": 5, "key": k}).json()
    assert s["status"] == "ok" and s["key"] == k
    assert c.get("/api/flow/stocks", params={"market": "CN"}).status_code == 400
