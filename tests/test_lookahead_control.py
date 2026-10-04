"""对照实验：纯随机游走（无趋势、无漂移）上，策略期望值应≈0（扣成本后略负）。
若回测引擎有前视泄漏或成交假设过于乐观，这里会出现显著正收益——这是最重要的一条回归测试。"""
import pytest

from server import backtest, db, ingest, universe
from server.datasource_demo import DemoSource


@pytest.mark.slow
def test_neutral_market_has_no_edge(fresh_env):
    src = DemoSource(neutral=True, trend_sd=0.0, seed=3)
    with db.market_db("CN") as c:
        ingest.ensure_calendar(c, src)
        ingest.refresh_securities(c, src)
        ingest.refresh_industry(c, src)
        universe.apply_l1(c)
        ingest.init_history(c, src)
        ingest.refresh_indices(c, src)
        db.set_meta(c, "data_asof", c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0])
        ctx = backtest.BtContext(c, "CN", "2022-01-01", None)
    m = ctx.run()["metrics"]
    assert m["n"] > 100
    assert abs(m["expectancy_r"]) < 0.3, f"随机游走上期望值应≈0，得到 {m['expectancy_r']}R（疑似前视泄漏）"
    assert m["cagr"] < 0.05, f"随机游走上年化不应显著为正，得到 {m['cagr']}"


def test_trend_world_has_positive_edge_and_cost_sensitivity(demo_env):
    with db.market_db("CN") as c:
        ctx = backtest.BtContext(c, "CN", "2022-01-01", None)
    base = ctx.run()["metrics"]
    costly = ctx.run({"cost_mult": 4.0, "slip_mult": 4.0})["metrics"]
    assert base["expectancy_r"] > 0.2                                           # 合成数据里确有趋势延续
    assert costly["expectancy_r"] < base["expectancy_r"], "成本与滑点加倍后期望值应下降（成本敏感性，6.6）"


def test_event_study_random_baseline_is_reproducible(demo_env):
    with db.market_db("CN") as c:
        ctx = backtest.BtContext(c, "CN", "2023-01-01", None)
    a = backtest.event_study(ctx, None, n_random=20, seed=5, setup_names=["breakout"])["setups"][0]
    b = backtest.event_study(ctx, None, n_random=20, seed=5, setup_names=["breakout"])["setups"][0]
    assert a["random_baseline"] == b["random_baseline"] and a["mean_r"] == b["mean_r"]
