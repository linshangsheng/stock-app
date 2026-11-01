"""K线页与指数筹码分布：算法性质（总量守恒、涨跌方向、换手率衰减）+ 接口。"""
import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import chips  # noqa: E402


def _bars(close: np.ndarray, spread: float = 0.01) -> pd.DataFrame:
    idx = [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(len(close))]
    return pd.DataFrame({"open": close, "high": close * (1 + spread), "low": close * (1 - spread), "close": close}, index=idx)


def test_mass_is_conserved_and_stats_are_sane():
    close = 100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 300))
    b = _bars(np.maximum(close, 20))
    out = chips.compute(b, pd.Series(0.02, index=b.index), keep=50)
    assert len(out["dist"]) == 50 and all(abs(sum(r) - 10000) <= 120 for r in out["dist"])        # 每天的分布总量 ≈ 1（取整误差）
    s = out["stats"]
    for j in range(50):
        assert 0 <= s["profit"][j] <= 1
        assert s["lo90"][j] <= s["lo70"][j] <= s["hi70"][j] <= s["hi90"][j]                           # 区间由宽到窄
        assert s["lo90"][j] - 1e-6 <= s["avg"][j] <= s["hi90"][j] + 1e-6


def test_profit_ratio_follows_trend():
    up = chips.compute(_bars(np.linspace(50, 150, 400)), pd.Series(0.03, index=_bars(np.linspace(50, 150, 400)).index), keep=5)
    down = chips.compute(_bars(np.linspace(150, 50, 400)), pd.Series(0.03, index=_bars(np.linspace(150, 50, 400)).index), keep=5)
    assert up["stats"]["profit"][-1] > 0.9           # 一路上涨：几乎所有筹码都在收盘价下面（获利）
    assert down["stats"]["profit"][-1] < 0.1         # 一路下跌：几乎都被套
    assert up["stats"]["avg"][-1] < 150 and down["stats"]["avg"][-1] > 50


def test_higher_turnover_forgets_old_chips_faster():
    c = np.concatenate([np.full(200, 100.0), np.full(60, 140.0)])
    b = _bars(c, 0.005)
    slow = chips.compute(b, pd.Series(0.005, index=b.index), keep=1)["stats"]["avg"][-1]
    fast = chips.compute(b, pd.Series(0.10, index=b.index), keep=1)["stats"]["avg"][-1]
    assert 100 < slow < fast < 141                   # 换手率越高，平均成本越快移到新价位附近


def test_turnover_of_one_keeps_only_today():
    c = np.linspace(100, 120, 40)
    b = _bars(c, 0.02)
    out = chips.compute(b, pd.Series(1.0, index=b.index), keep=1)
    lo, hi = c[-1] * 0.98, c[-1] * 1.02
    assert out["stats"]["lo90"][0] >= lo - out["step"] and out["stats"]["hi90"][0] <= hi + out["step"]


@pytest.fixture
def client(demo_env):
    from server import main
    return TestClient(main.app)


def test_kline_endpoint_cn_has_chips_and_ma(client):
    j = client.get("/api/index/kline", params={"market": "CN", "index": "sz.399006"}).json()
    assert j["status"] == "ok" and j["index"]["name"] == "创业板指" and len(j["dates"]) == len(j["candles"]) == len(j["amount"])
    assert set(j["ma"]) == {"5", "20", "60", "200", "250"} and len(j["ma"]["5"]) == len(j["dates"])
    c = j["chips"]
    assert c and len(c["dates"]) == len(c["dist"]) and c["dates"][-1] == j["dates"][-1] and len(c["bins"]) == len(c["dist"][-1])
    assert [i["key"] for i in j["indices"]] == ["sh.000016", "sh.000300", "sz.399006"]
    # 没指定指数：用第一个有数据的（演示数据没有上证50）
    d = client.get("/api/index/kline", params={"market": "CN"}).json()
    assert d["status"] == "ok" and d["index"]["key"] == "sh.000300"


def test_kline_endpoint_unknown_data_and_us_has_no_chips(client):
    j = client.get("/api/index/kline", params={"market": "CN", "index": "sh.000016"}).json()
    assert j["status"] == "no_data" and "上证50" in j["message"]
    u = client.get("/api/index/kline", params={"market": "US"}).json()
    assert [i["key"] for i in u["indices"]] == ["^GSPC", "^NDX"] and u.get("chips") is None
