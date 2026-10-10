"""关键均线：明天触发价的算法、带滞回的规则状态、证据角色（主线 / 辅助 / 仅参考）、接口（离线：美股指数用假数据）。"""
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=DeprecationWarning)

from server import key_ma  # noqa: E402


def _px(n=600, seed=1, drift=0.0004):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d")
    return pd.Series(1000 * np.exp(np.cumsum(rng.normal(drift, 0.012, n))), index=idx)


def test_trigger_prices_are_exact_cross_levels():
    px = _px()
    for n in (20, 200, 250):
        for band in (0.0, 0.02):
            tp = key_ma.trigger_prices(px, n, band)
            nxt = "2099-01-01"
            for p, below in ((tp["below"] * (1 - 1e-6), True), (tp["below"] * (1 + 1e-6), False)):
                s = pd.concat([px, pd.Series([p], index=[nxt])])
                assert bool(s.iloc[-1] < s.rolling(n).mean().iloc[-1] * (1 - band)) == below
            for p, above in ((tp["above"] * (1 + 1e-6), True), (tp["above"] * (1 - 1e-6), False)):
                s = pd.concat([px, pd.Series([p], index=[nxt])])
                assert bool(s.iloc[-1] > s.rolling(n).mean().iloc[-1] * (1 + band)) == above


def test_rule_state_hysteresis_keeps_previous_state_inside_band():
    idx = pd.bdate_range("2024-01-01", periods=43).strftime("%Y-%m-%d")
    px = pd.Series([100.0] * 40 + [103.0, 100.5, 99.9], index=idx)     # 先站上均线 ×1.02，再落回均线略下方（仍在 ±2% 带内）
    holding, since = key_ma.rule_state(px, 20, 0.02)
    assert holding and since is not None
    holding0, _ = key_ma.rule_state(px, 20, 0.0)
    assert not holding0                                                     # 不带滞回：已经跌破


def test_analyze_roles_and_advice_follow_evidence():
    px = _px(drift=0.001)
    line = lambda ok: {"all": {"cagr": 0.1, "mdd": -0.2}, "is": {"cagr": 0.1, "mdd": -0.2}, "oos": {"cagr": 0.1, "mdd": -0.2},  # noqa: E731
                       "switches_per_year": 3.0, "actionable": ok}
    ev = {"primary": {"n": 200, "band": 0.0}, "hold": {"all": {"cagr": 0.1, "mdd": -0.5}, "oos": {"cagr": 0.1, "mdd": -0.5}},
          "mas": {"20_0": line(False), "20_2": line(False), "60_0": line(False), "60_2": line(False), "120_0": line(False), "120_2": line(False),
                  "200_0": line(True), "200_2": line(True), "250_0": line(True), "250_2": line(False)},
          "zones": [{"lo": -1, "hi": 9, "episodes": 30, "days": 100, "fwd60": {"n": 1, "mean": 0.02, "win": 0.6}, "fwd120": {"n": 1, "mean": 0.04, "win": 0.7}}],
          "unconditional": {"fwd120": {"mean": 0.04, "win": 0.7}}}
    r = key_ma.analyze({"key": "spx", "name": "标普500", "market": "US", "symbol": "^GSPC"}, px, ev)
    roles = {x["n"]: x["role"] for x in r["lines"]}
    assert roles == {20: "reference", 60: "reference", 120: "reference", 200: "primary", 250: "actionable"}
    p = next(x for x in r["lines"] if x["n"] == 200)
    assert ("持有" in p["advice"]) == p["holding"] and "QDII" in p["advice"]
    assert r["headline"].startswith("持有" if p["holding"] else "空仓") and r["zone"]["tone"] == "接近平均"
    assert len(r["history"]) == 520 and r["history"][-1]["ma200"] is not None


def test_api_builds_three_indices_offline(demo_env, monkeypatch):
    from fastapi.testclient import TestClient
    from server import main

    def fake(symbol, start, end):
        s = _px(800, seed=7 if symbol == "^GSPC" else 8)
        return pd.DataFrame({"date": s.index, "close": s.values})

    monkeypatch.setattr(key_ma, "_fetch_us", fake)
    j = TestClient(main.app).get("/api/market/key_ma").json()
    got = {x["key"]: x for x in j["indices"]}
    assert set(got) == {"cyb", "spx", "ndx"}
    for k in ("spx", "ndx"):
        assert got[k]["status"] == "ok" and len(got[k]["lines"]) == 5
        assert sum(1 for x in got[k]["lines"] if x["role"] == "primary") == 1
    assert got["cyb"]["status"] in ("ok", "no_data")
    # 第二次读缓存，不再请求
    monkeypatch.setattr(key_ma, "_fetch_us", lambda *a: (_ for _ in ()).throw(RuntimeError("不应再请求")))
    j2 = TestClient(main.app).get("/api/market/key_ma").json()
    assert {x["key"]: x["status"] for x in j2["indices"]}["spx"] == "ok"
