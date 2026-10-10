"""M6：事件流（公告 / 新闻 / Insider）、可信度等级与缓存、行情总览、盘中报价。"""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import datasource_events as ev, db, ingest  # noqa: E402


def test_cn_title_classification_and_sentiment():
    assert ev.classify_cn("关于公司股东减持股份计划的公告") == "INSIDER"
    assert ev.classify_cn("2026年半年度报告") == "EARNINGS"
    assert ev.classify_cn("关于回购公司股份的进展公告") == "BUYBACK"
    assert ev.classify_cn("关于收到中国证监会立案告知书的公告") == "REGULATORY"
    assert ev.classify_cn("关于签订重大合同的公告") == "CONTRACT"
    assert ev.classify_cn("2025年年度权益分派实施公告") == "DIVIDEND"
    assert ev.classify_cn("关于召开股东大会的通知") == "OTHER"
    assert ev.cn_sentiment("2026年业绩预告：预增") > 0 and ev.cn_sentiment("业绩预告：首亏") < 0 and ev.cn_sentiment("普通公告") is None


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def fetch(self, symbol):
        self.calls += 1
        return [{"event_type": "NEWS", "event_time": "2026-09-29", "publish_time": "2026-09-29T08:00:00", "source": "Yahoo Finance", "level": 3,
                 "title": "headline A", "summary": "", "url": "http://x", "sentiment": None},
                {"event_type": "REGULATORY", "event_time": "2026-09-28", "publish_time": "2026-09-28", "source": "sec", "level": 1,
                 "title": "8-K 3.01", "summary": "", "url": None, "sentiment": -0.5}]


def test_ensure_events_dedups_caches_and_force_refreshes(demo_env):
    prov = FakeProvider()
    with db.market_db("CN") as c:
        syms = [r[0] for r in c.execute("SELECT symbol FROM securities LIMIT 2")]
        r1 = ingest.ensure_events(c, prov, syms, "CN")
        assert r1["ok"] == 2 and r1["new_events"] == 4 and prov.calls == 2
        r2 = ingest.ensure_events(c, prov, syms, "CN")                 # 6 小时缓存：不再请求上游
        assert r2["cached"] == 2 and r2["requested"] == 0 and prov.calls == 2
        r3 = ingest.ensure_events(c, prov, syms, "CN", force=True)     # 强制刷新：重复事件不重复入库
        assert r3["ok"] == 2 and r3["new_events"] == 0
        assert c.execute("SELECT COUNT(*) FROM event_stream").fetchone()[0] == 4


@pytest.fixture
def client(demo_env):
    from server import main
    return TestClient(main.app)


def test_events_api_levels_scope_and_forward_returns(client):
    sym = client.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    r = client.post("/api/events/refresh", json={"symbols": [sym]}).json()          # 演示事件源
    assert r["ok"] == 1 and r["new_events"] >= 4
    items = client.get("/api/events", params={"symbol": sym, "days": 400}).json()["items"]
    assert {i["event_type"] for i in items} >= {"NEWS", "REGULATORY", "EARNINGS", "INSIDER"}
    assert all(i["level"] in (1, 2, 3) and i["event_time"] and i["ingested_at"] for i in items)           # 三个时间字段（3.23）
    assert any("ret_1d" in i for i in items), "事件之后的 1/3/5 日收益"
    assert all(i["level"] <= 1 for i in client.get("/api/events", params={"symbol": sym, "days": 400, "min_level": 1}).json()["items"])
    only = client.get("/api/events", params={"symbol": sym, "days": 400, "type": "NEWS"}).json()["items"]
    assert only and {i["event_type"] for i in only} == {"NEWS"}
    client.put("/api/watchlist", json={"add": [{"symbol": sym}]})
    assert client.get("/api/events", params={"scope": "watch", "days": 400}).json()["items"]
    assert client.get("/api/events", params={"scope": "held", "days": 400}).json()["items"] == []


def test_market_overview_cn(client):
    o = client.get("/api/market/overview").json()
    assert o["date"] and len(o["indices"]) == 6 and o["breadth"]["pool"] > 50
    assert o["breadth"]["up"] + o["breadth"]["down"] + o["breadth"]["flat"] == o["breadth"]["pool"]
    assert o["breadth"]["regime"] in ("NORMAL", "CAUTION", "DEFENSIVE", "UNKNOWN")
    assert len(o["industries"]) >= 5 and o["industries"][0]["ret_20d"] >= o["industries"][-1]["ret_20d"]
    g = o["movers"]["gainers"]
    assert len(g) == 12 and g[0]["value"] >= g[-1]["value"]
    assert o["movers"]["losers"][0]["value"] <= o["movers"]["losers"][-1]["value"]


def test_market_overview_us_and_live_quote_support(us_env):
    from server import main
    cl = TestClient(main.app)
    o = cl.get("/api/market/overview", params={"market": "US"}).json()
    assert any(i["symbol"] == "^VIX" for i in o["indices"]) and o["breadth"]["vix"] is not None
    live = cl.get("/api/quote/live", params={"symbols": "AAA", "market": "US"}).json()
    assert live["supported"] is True and "AAA" in live["quotes"] and live["quotes"]["AAA"]["prev_close"] > 0


def test_financials_cached_and_point_in_time_flag(client):
    sym = client.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    d = client.get("/api/financials", params={"symbol": sym}).json()
    assert d["pit"] is True and len(d["periods"]) == 6 and all(p["pub_date"] for p in d["periods"]), "A 股财务带公告日（点时）"
    again = client.get("/api/financials", params={"symbol": sym}).json()
    assert again["fetched_at"] == d["fetched_at"], "7 天内走缓存"


def test_us_financials_marked_not_point_in_time(us_env):
    from server import main
    cl = TestClient(main.app)
    d = cl.get("/api/financials", params={"symbol": "AAA", "market": "US"}).json()
    assert d["pit"] is False and d["currency"] == "USD"
    assert all(p["pub_date"] is None for p in d["periods"]), "美股 yfinance 财务无披露时间，不进入回测特征"


def test_flows_cn_demo_and_cache(client):
    sym = client.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    d = client.get("/api/flows", params={"symbol": sym}).json()
    assert d["market"] == "CN" and d["margin"] and d["billboard"] and d["north"]
    assert client.get("/api/flows", params={"symbol": sym}).json()["fetched_at"] == d["fetched_at"], "12 小时缓存"


def test_flows_us_demo(us_env):
    from server import main
    cl = TestClient(main.app)
    u = cl.get("/api/flows", params={"symbol": "AAA", "market": "US"}).json()
    assert u["market"] == "US" and u["short"]["short_pct_float"] is not None and u["options"]["pc_volume_ratio"] is not None


def test_event_stats_groups_with_sample_size_flag(client):
    syms = [r["symbol"] for r in client.get("/api/search", params={"q": "演示股票0"}).json()["items"][:5]]
    client.post("/api/events/refresh", json={"symbols": syms})
    r = client.get("/api/events/stats", params={"days": 400, "min_n": 3}).json()
    assert r["n_events"] > 0 and r["groups"]
    g = next(x for x in r["groups"] if x["event_type"] == "NEWS" and x["tone"] == "全部")
    assert g["n"] >= 1 and g["mean_1d"] is not None
    assert all(x["enough"] == (x["n"] >= 3) for x in r["groups"])
    big = client.get("/api/events/stats", params={"days": 400}).json()
    assert not any(x["enough"] for x in big["groups"]), "样本不足 30 要明确标注"
