"""API 冒烟 + 访问口令 + 市场隔离。"""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(demo_env):
    from server import main
    return TestClient(main.app)


def test_core_endpoints(client):
    assert client.get("/api/ping").json()["demo"]["CN"] is True
    j = client.get("/api/jobs").json()
    assert j["has_data"] and j["gate"]["status"] == "PASS" and j["data_asof"]
    run = client.post("/api/scan/run").json()
    assert run["run"]["official"] is True and run["run"]["gate"]["status"] == "PASS"
    got = client.get("/api/scan").json()
    assert got["run"]["run_id"] == run["run"]["run_id"] and len(got["candidates"]) == len(run["candidates"])
    sym = client.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    k = client.get("/api/kline", params={"symbol": sym}).json()
    assert len(k["bars"]) > 200 and "macd" in k and k["data_asof"]
    w = client.get("/api/kline", params={"symbol": sym, "period": "W"}).json()
    assert 50 < len(w["bars"]) < len(k["bars"])
    assert client.get("/api/universe").json()["l2"] > 50
    assert client.post("/api/analysis", json={"conditions": {"min_rps20": 70}}).json()["total"] >= 0


def test_us_market_without_data_is_empty_not_error(client):
    r = client.get("/api/scan", params={"market": "US"})
    assert r.status_code == 200 and r.json()["run"] is None and r.json()["candidates"] == []


def test_token_required_when_configured(client):
    from server import settings
    settings.cfg()["server"]["token"] = "s3cret"
    try:
        assert client.get("/api/watchlist").status_code == 401
        assert client.get("/api/watchlist", headers={"X-Token": "wrong"}).status_code == 401
        assert client.get("/api/watchlist", headers={"X-Token": "s3cret"}).status_code == 200
        assert client.get("/api/ping").status_code == 200                      # ping 用于探测是否需要口令
    finally:
        settings.cfg()["server"]["token"] = ""


def test_watchlist_account_and_trades_roundtrip(client):
    sym = client.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    assert len(client.put("/api/watchlist", json={"add": [{"symbol": sym}]}).json()["items"]) == 1
    acct = client.put("/api/account", json={"equity": 500000, "cash": 500000, "risk_per_trade": 0.005}).json()["account"]
    assert acct["equity"] == 500000
    r = client.post("/api/trades", json={"symbol": sym, "date": "2026-09-01", "side": "buy", "price": 10, "qty": 100})
    assert r.status_code == 400 and "止损" in r.json()["detail"]
    ok = client.post("/api/trades", json={"symbol": sym, "date": "2026-09-01", "side": "buy", "price": 10, "qty": 100, "initial_stop": 9})
    assert ok.status_code == 200
    assert len(client.get("/api/positions").json()["items"]) == 1
    imp = client.post("/api/trades", json={"rows": [{"symbol": sym, "date": "2026-09-05", "side": "sell", "price": 11, "qty": 100,
                                                      "exit_reason": "移动止盈"}]})
    assert imp.status_code == 200 and client.get("/api/journal/stats").json()["overall"]["n"] == 1


def test_settings_whitelist(client):
    assert client.put("/api/settings", json={"server": {"token": "x"}}).status_code == 400
    r = client.put("/api/settings", json={"execution": {"slippage": 0.002}})
    assert r.status_code == 200 and r.json()["execution"]["slippage"] == 0.002


def test_alerts_trigger_on_latest_close(client):
    sym = client.get("/api/search", params={"q": "演示股票00"}).json()["items"][0]["symbol"]
    assert client.post("/api/alerts", json={"symbol": sym, "kind": "price_above", "value": 1e9}).status_code == 200
    assert client.post("/api/alerts", json={"symbol": sym, "kind": "price_below", "value": 1e9}).status_code == 200
    assert client.post("/api/alerts", json={"symbol": sym, "kind": "bogus", "value": 1}).status_code == 400
    items = client.get("/api/alerts").json()["items"]
    by = {a["rule"].split(":")[0]: a["triggered"] for a in items}
    assert by == {"price_above": False, "price_below": True}
