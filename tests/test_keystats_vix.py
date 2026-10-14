"""美股关键统计（接口、缓存、解读规则、批量后台补齐）与 VIX 面板（分区、期限结构、证据）——离线，演示数据。"""
import time
import warnings

import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import keystats, vix  # noqa: E402


def test_derive_describes_four_focus_areas_and_flags():
    d = keystats.demo_us("AAPL")
    a = keystats.derive(d)
    assert {"valuation", "margins", "growth", "cashflow"} <= set(a)
    assert "Forward P/E" in a["valuation"]["headline"] and a["valuation"]["changes"]
    assert a["margins"]["trend"] > 0 and a["margins"]["level"] == "good"             # 演示数据利润率逐季扩张
    assert "营收同比" in a["growth"]["headline"] and a["cashflow"]["fcf_margin"] > 0
    # 亏损 + 自由现金流为负 + 营收下降 → 留意项
    d["stats"].update(operating_margin=-0.1, profit_margin=-0.2, free_cashflow=-5e8, revenue_growth_q=-0.1, current_ratio=0.7)
    a2 = keystats.derive(d)
    assert a2["margins"]["level"] == "warn" and a2["cashflow"]["level"] == "warn" and a2["growth"]["level"] == "warn"
    assert {"公司目前亏损（利润率为负）", "自由现金流为负", "营收同比下降", "流动比率 0.70 < 1"} <= set(a2["flags"])


def test_keystats_api_cache_and_batch(demo_env):
    cl = TestClient(__import__("server.main", fromlist=["app"]).app)
    j = cl.get("/api/keystats", params={"symbol": "AAPL", "market": "US"}).json()
    assert j["status"] == "ok" and j["stats"]["forward_pe"] and j["analysis"]["flags"] is not None and j["fwd_pe_history"]
    assert cl.get("/api/keystats", params={"symbol": "AAPL", "market": "CN"}).json()["status"] == "unsupported"
    # 批量：第一次缓存里只有 AAPL，另两只在后台补齐
    b = cl.get("/api/keystats/batch", params={"symbols": "AAPL,MSFT,NVDA", "market": "US"}).json()
    assert "AAPL" in b["rows"] and set(b["rows"]["AAPL"]) >= {"forward_pe", "operating_margin", "free_cashflow", "flags"}
    for _ in range(40):
        b = cl.get("/api/keystats/batch", params={"symbols": "AAPL,MSFT,NVDA", "market": "US"}).json()
        if not b["pending"]:
            break
        time.sleep(0.25)
    assert not b["pending"] and set(b["rows"]) == {"AAPL", "MSFT", "NVDA"}


def _vix_series(n=400, level=16.0):
    idx = pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.Series(level, index=idx), pd.Series(level * 1.2, index=idx)


def test_vix_zone_term_structure_and_advice():
    ev = vix.evidence()
    assert ev["zones"] and len(ev["zones"]) == 6 and ev["term_zones"]
    v, v3 = _vix_series(level=16.0)
    a = vix.analyze(v, v3, ev)
    assert a["zone_name"] == "正常" and a["term"]["zone"] == 0 and a["evidence_line"]
    v, v3 = _vix_series(level=34.0)
    v3 = v3 * 0.8                                                                 # 近端高于远端：倒挂
    a = vix.analyze(v, v3, ev)
    assert a["zone_name"] == "恐慌" and a["term"]["zone"] == 2 and "不是买入信号" in a["read"]
    v, v3 = _vix_series(level=12.0)
    assert "不是卖出信号" in vix.analyze(v, v3, ev)["read"]


def test_vix_endpoint_us_only(demo_env, monkeypatch):
    def fake(sym, start, end):
        s, s3 = _vix_series(500, 18.0)
        x = s if sym == "^VIX" else s3
        return pd.DataFrame({"date": x.index, "close": x.values})
    monkeypatch.setattr(vix, "_fetch_yf", fake)
    cl = TestClient(__import__("server.main", fromlist=["app"]).app)
    assert cl.get("/api/market/vix", params={"market": "CN"}).json()["status"] == "unsupported"
    j = cl.get("/api/market/vix", params={"market": "US"}).json()
    assert j["status"] in ("ok", "no_data")
    if j["status"] == "ok":
        assert j["history"] and j["zones"] and "note" in j
