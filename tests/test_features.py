"""口径单测（3.6.1）：量比 / 滚动高点不含当日、ATR Wilder、收盘位置、RPS 样本下限、停牌处理。"""
import numpy as np
import pandas as pd

from server import db, features as feats
from server.panel import load_panel
from tests.helpers import bar, flat, make_market


def _feat(bars, extra=None):
    syms = {"sh.600001": bars, **(extra or {})}
    make_market(syms)
    with db.market_db("CN") as c:
        panel = load_panel(c, float32=False)
    return panel, feats.compute_features(panel)


def test_volume_ratio_excludes_today(fresh_env):
    bars = [bar(10, vol=100) for _ in range(30)] + [bar(10, vol=300)]
    _, f = _feat(bars)
    vr = f["vol_ratio"]["sh.600001"].iloc[-1]
    assert abs(vr - 3.0) < 1e-6, f"量比分母应为前 20 日均量（不含当日），得到 {vr}"


def test_rolling_high_excludes_today(fresh_env):
    hs = list(np.linspace(10, 12, 40))
    bars = [bar(h - 0.2, h=h, l=h - 0.4, c=h - 0.1) for h in hs]
    bars.append(bar(12.5, h=13, l=12.4, c=12.9))
    _, f = _feat(bars)
    s = f["hh20"]["sh.600001"]
    assert abs(s.iloc[-1] - max(hs[-20:])) < 1e-9, "HH_20(t) 应为 t-20..t-1 的最高价，不含当日"
    assert f["close"]["sh.600001"].iloc[-1] > s.iloc[-1]          # 当日收盘突破


def test_atr_is_wilder(fresh_env):
    rng = np.random.default_rng(1)
    c = 10 + np.cumsum(rng.normal(0, 0.2, 60))
    bars = [bar(x - 0.05, h=x + abs(rng.normal(0.1, 0.05)), l=x - abs(rng.normal(0.1, 0.05)), c=x) for x in c]
    panel, f = _feat(bars)
    h, l, cl = (panel.adj[k]["sh.600001"].to_numpy() for k in ("high", "low", "close"))
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - cl[:-1]), np.abs(l[1:] - cl[:-1])])
    tr = np.r_[h[0] - l[0], tr]
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().to_numpy()
    got = f["atr14"]["sh.600001"].to_numpy()
    assert np.allclose(got[20:], atr[20:], rtol=1e-4)


def test_close_position_flat_bar_is_half(fresh_env):
    _, f = _feat(flat(30) + [bar(10, h=10, l=10, c=10)])
    assert f["close_pos"]["sh.600001"].iloc[-1] == 0.5


def test_rps_not_computed_below_min_sample(fresh_env):
    extra = {f"sh.60000{i}": flat(70, price=10 + i * 0.1) for i in range(2, 6)}
    _, f = _feat(flat(70), extra)
    assert f["rps_20"].iloc[-1].isna().all(), "有效样本数低于下限时不计算 RPS，避免小样本排名失真"


def test_suspended_days_not_filled(fresh_env):
    bars = flat(60) + [bar(10, status=0, vol=0)] + flat(5)
    panel, f = _feat(bars)
    assert np.isnan(panel.adj["close"]["sh.600001"].iloc[60])           # 停牌日价格不填充
    assert not np.isnan(f["ma20"]["sh.600001"].iloc[-1])                # 有效天数占比足够时仍可计算均线


def test_forward_adjustment_uses_last_factor(fresh_env):
    # 第 30 日除权（factor 1.0 -> 2.0）：原始价腰斩，前复权序列应连续
    bars = [bar(20, factor=1.0) for _ in range(30)] + [bar(10, factor=2.0) for _ in range(30)]
    panel, _ = _feat(bars)
    adj = panel.adj["close"]["sh.600001"].to_numpy()
    raw = panel.raw["close"]["sh.600001"].to_numpy()
    assert raw[29] == 20 and raw[30] == 10
    assert np.allclose(adj, 10), "前复权以序列最后一日因子为基准：除权前价格应折算为 10"
