"""持仓风险总览（server/risk_view.py）：集中度、同涨同跌、等效只数、大盘下跌估算、按现在持仓重放过去一年。"""
import numpy as np
import pandas as pd

from server import risk_view as rv


def _dates(n):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-01-01", periods=n)]


def _paths(n=200, seed=1):
    rng = np.random.default_rng(seed)
    idx = _dates(n)
    m = rng.normal(0, 0.01, n)                                          # 大盘日收益
    mk = lambda r: pd.Series(100 * np.cumprod(1 + r), index=idx)        # noqa: E731
    a = mk(m * 1.0 + rng.normal(0, 0.002, n))                           # A：几乎就是大盘（beta≈1）
    b = mk(m * 1.0 + rng.normal(0, 0.002, n))                           # B：也几乎就是大盘 —— 和 A 同涨同跌
    c = mk(rng.normal(0, 0.01, n))                                      # C：和大盘无关
    return idx, mk(m), a, b, c


def test_identical_holdings_are_flagged_and_count_as_one():
    idx, bench, a, b, c = _paths()
    adj = pd.DataFrame({"A": a, "B": b})
    r = rv.analyze(adj, {"A": 50_000, "B": 50_000}, {"A": "银行", "B": "银行"}, {"A": "甲", "B": "乙"}, bench)
    assert r["pairs"] and r["pairs"][0]["corr"] > 0.9
    assert 1.0 <= r["eff_n"] < 1.3                                      # 两只几乎同涨同跌 ≈ 1 只
    assert any("同涨同跌" in f["text"] for f in r["flags"])
    assert r["industry"][0] == {"name": "银行", "share": 1.0}


def test_independent_holdings_count_as_more_than_one():
    idx, bench, a, b, c = _paths()
    r = rv.analyze(pd.DataFrame({"A": a, "C": c}), {"A": 50_000, "C": 50_000}, {}, {}, bench)
    assert r["pairs"] == [] and r["eff_n"] > 1.6                        # 互不相关的两只 ≈ √2² = 2


def test_beta_and_market_drop_estimate():
    idx, bench, a, b, c = _paths()
    r = rv.analyze(pd.DataFrame({"A": a, "C": c}), {"A": 80_000, "C": 20_000}, {}, {}, bench, equity=200_000)
    beta_a = next(h["beta"] for h in r["holdings"] if h["symbol"] == "A")
    assert 0.9 < beta_a < 1.1                                           # 构造的 A 就是大盘
    assert abs(r["beta"] - (0.8 * beta_a + 0.2 * next(h["beta"] for h in r["holdings"] if h["symbol"] == "C"))) < 0.02
    s = r["shock"]
    assert s["market_drop"] == 0.10 and abs(s["loss_pct"] - r["beta"] * 0.10) < 1e-3
    assert abs(s["loss_amount"] - 100_000 * s["loss_pct"]) < 1                 # 亏的是「持仓市值」的比例，不是总资金
    assert abs(s["loss_of_equity"] - s["loss_amount"] / 200_000) < 1e-3
    assert r["exposure"] == 0.5


def test_replay_uses_current_weights_and_reports_worst_day():
    idx, bench, a, b, c = _paths()
    a2 = a.copy()
    a2.iloc[100:] = a2.iloc[100:] * 0.8                                 # 第 100 天一根 -20% 的大阴线
    r = rv.analyze(pd.DataFrame({"A": a2}), {"A": 10_000}, {}, {}, bench)
    rp = r["replay"]
    assert abs(rp["worst_day"] - (-0.2 + (a.iloc[100] / a.iloc[99] - 1) * 0.8)) < 1e-3 and rp["worst_day_date"] == idx[100]
    assert rp["max_dd"] <= rp["worst_day"] + 1e-9 and rp["days"] >= 150
    assert rp["bench_max_dd"] is not None


def test_concentration_flags_and_short_history_and_empty():
    idx, bench, a, b, c = _paths()
    adj = pd.DataFrame({"A": a, "B": b, "C": c})
    adj.loc[idx[:170], "C"] = np.nan                                    # C 只有 30 天数据：不够，不参与相关性 / 重放，但要单独说明
    r = rv.analyze(adj, {"A": 70_000, "B": 20_000, "C": 10_000}, {"A": "银行", "B": "银行", "C": "医药"}, {"A": "甲", "B": "乙", "C": "丙"}, bench)
    assert r["short"] == ["C"]
    texts = " ".join(f["text"] for f in r["flags"])
    assert "「银行」行业占持仓的 90%" in texts and "甲一只就占 70%" in texts and "丙" in texts
    assert next(h for h in r["holdings"] if h["symbol"] == "C")["enough"] is False
    assert rv.analyze(adj, {}, {}, {})["status"] == "empty"
    # 没有基准：照样给集中度 / 相关性，只是没有 beta / 大盘下跌估算
    nb = rv.analyze(adj[["A", "B"]], {"A": 1, "B": 1}, {}, {}, None)
    assert nb["beta"] is None and nb["shock"] is None and nb["replay"]["bench_max_dd"] is None


def test_risk_api_empty_and_with_positions(demo_env):
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from fastapi.testclient import TestClient

    from server import main, portfolio
    client = TestClient(main.app)
    assert client.get("/api/portfolio/risk").json()["status"] == "empty"
    for sym in ("sh.600001", "sh.600002", "sh.600003"):
        portfolio.upsert_position("CN", {"symbol": sym, "qty": 1000, "avg_cost": 10, "initial_stop": 9})
    j = client.get("/api/portfolio/risk").json()
    assert j["status"] == "ok" and j["n"] == 3 and j["date"] and j["bench_name"] == "沪深300"
    assert abs(sum(h["weight"] for h in j["holdings"]) - 1) < 0.01 and j["replay"] and j["eff_n"] is not None
