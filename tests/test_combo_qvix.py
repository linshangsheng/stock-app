"""A 股恐慌指数 QVIX（解析 / 入库 / 分区与解读 / 首次加载）、行情页合成图（K 线 + 市场温度 + 恐慌指数按交易日对齐）、
市场温度在数据补齐后重算。离线：演示数据 + 合成的 QVIX 文件。"""
import json
import warnings

import pandas as pd

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, qvix  # noqa: E402


def _row(d, v300=None, vcyb=None, n=90):
    r = ["#NAME?"] * n
    r[0] = d
    if v300 is not None:
        r[9:13] = [str(v300), str(v300 + 1), str(v300 - 1), str(v300)]
    if vcyb is not None:
        r[67:71] = ["9", "9", "9", "9"]                              # 中证500ETF 的那几列：不能被当成创业板
        r[71:75] = [str(vcyb), str(vcyb + 2), str(vcyb - 2), str(vcyb)]
    return ",".join(r)


def _csv(days=300):
    dates = pd.bdate_range("2025-01-02", periods=days)
    lines = [",".join(str(i) for i in range(90))]
    for k, d in enumerate(dates):
        lines.append(_row(f"{d.year}/{d.month}/{d.day}", 15 + (k % 20), 25 + (k % 30) if k > 50 else None))
    return ("\n".join(lines)).encode("gbk"), dates


def test_parse_columns_and_partial_tail():
    raw, dates = _csv(120)
    d = qvix.parse(raw)
    assert len(d["QVIX300"]) == 120 and d["QVIX300"].iloc[0]["date"] == dates[0].date().isoformat()
    assert len(d["QVIXCYB"]) == 120 - 51 and d["QVIXCYB"]["close"].min() >= 25        # 取的是 71~74 列，不是 67~70 列（中证500）
    tail = raw[-3000:]                                                                 # 从文件中间截一段：第一行是残行
    t = qvix.parse(tail, partial_head=True)
    assert len(t["QVIX300"]) >= 3 and t["QVIX300"]["date"].is_monotonic_increasing


def test_build_loads_history_then_analyzes(demo_env, monkeypatch):
    started = []
    monkeypatch.setattr(qvix, "refresh_async", lambda: started.append(1) or True)
    first = qvix.build()
    assert first["status"] == "loading" and started                                    # 第一次：后台下载历史，页面显示「正在下载」
    raw, _ = _csv(400)
    with db.market_db("CN") as c:
        qvix.store(c, qvix.parse(raw))
        db.set_meta(c, "qvix_refreshed", json.dumps({"at": "2099-01-01T00:00:00"}))
        c.commit()
    ev = {"series": {"QVIX300": {"cuts": [17.5, 20, 23, 25.5, 31.5], "from": "2025-01-02", "zones": [{"fwd20": {"n": 0}, "fwd60": {"n": 0}, "episodes": 0}] * 6,
                                 "unconditional": {}}}}
    monkeypatch.setattr(qvix, "evidence", lambda: ev)
    d = qvix.build()
    assert d["status"] == "ok" and [s["symbol"] for s in d["series"]] == ["QVIX300", "QVIXCYB"]
    s300 = d["series"][0]
    assert s300["zone_name"] == qvix.ZONE_NAMES[qvix.zone_of(s300["value"], [17.5, 20, 23, 25.5, 31.5])] and s300["read"] and len(s300["zones"]) == 6
    assert "zone" not in d["series"][1]                                                # 没有研究结果的序列：只给数值，不乱分区


def test_combo_aligns_series_on_index_dates(demo_env, monkeypatch):
    monkeypatch.setattr(qvix, "refresh_async", lambda: True)
    raw, _ = _csv(400)
    with db.market_db("CN") as c:
        qvix.store(c, qvix.parse(raw))
        from server import market_view
        market_view.ensure_breadth(c, "CN")
    cl = TestClient(__import__("server.main", fromlist=["app"]).app)
    j = cl.get("/api/market/combo", params={"market": "CN"}).json()
    assert j["status"] == "ok" and j["index"]["symbol"] == "sz.399006"
    n = len(j["dates"])
    assert n > 250 and len(j["candles"]) == len(j["temp"]) == n and all(len(p["values"]) == n for p in j["panic"])
    assert j["dates"] == sorted(j["dates"]) and any(v is not None for v in j["temp"])
    assert [p["key"] for p in j["panic"]] == ["QVIX300", "QVIXCYB"] and j["temp_lines"][0]["v"] == 15
    assert cl.get("/api/market/qvix", params={"market": "US"}).json()["status"] == "unsupported"


def test_combo_us_uses_nasdaq100_and_vix(us_env, monkeypatch):
    from server import vix
    monkeypatch.setattr(vix, "_fetch_yf", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    cl = TestClient(__import__("server.main", fromlist=["app"]).app)
    j = cl.get("/api/market/combo", params={"market": "US"}).json()
    assert j["status"] == "ok" and j["index"]["name"].startswith("纳斯达克100")
    assert [p["key"] for p in j["panic"]] == ["VIX"] and len(j["panic"][0]["values"]) == len(j["dates"])


def test_breadth_recomputed_after_partial_day(demo_env):
    from server import market_view
    with db.market_db("CN") as c:
        market_view.ensure_breadth(c, "CN")
        last = c.execute("SELECT MAX(date) FROM market_breadth").fetchone()[0]
        full_n = c.execute("SELECT n FROM market_breadth WHERE date=?", (last,)).fetchone()[0]
        c.execute("UPDATE market_breadth SET n=1, b20=0 WHERE date=?", (last,))      # 模拟「数据还不全时算的」
        c.commit()
        assert market_view.ensure_breadth(c, "CN") > 0
        assert c.execute("SELECT n FROM market_breadth WHERE date=?", (last,)).fetchone()[0] == full_n
        assert market_view.ensure_breadth(c, "CN") == 0                                 # 已经是完整的：不再重算
