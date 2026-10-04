"""形态规格（5.4.1）：突破 / 回踩 / 波动收缩后突破 的参数化定义。
扫描与回测共用同一份定义；全部是**待验证的假设**（默认值取常见经验区间，不代表有效）。
信号日 T 收盘后判定；HH_N 不含当日（3.6.1）。每个形态最多 3 个自由参数参与网格搜索，其余固定。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import settings
from .features import Features
from .panel import Panel

SETUP_LABEL = {"breakout": "突破", "pullback": "回踩", "vcp": "波动收缩突破"}
# 可网格搜索的自由参数（5.4.1：每形态最多 3 个）
FREE_PARAMS = {
    "breakout": ["n", "vol_ratio_min", "close_pos_min"],
    "pullback": ["rps60_min", "vol_ratio_max", "depth_atr_min"],
    "vcp": ["atr_ratio_max", "bb_pct_max", "vol_ratio_min"],
}


def default_params() -> dict:
    return settings.cfg()["setups"]


def _hh(F: dict, n: int) -> pd.DataFrame:
    if f"hh{n}" in F:
        return F[f"hh{n}"]
    raise KeyError(f"HH_{n} 未计算（支持 5/10/20/50/252）")


def breakout(feat: Features, p: dict) -> pd.DataFrame:
    F = feat.f
    n = int(p.get("n", 20))
    hh = _hh(F, n)
    c = F["close"]
    ext = (c - hh) / F["atr14"]
    return ((c > hh) & (F["vol_ratio"] >= p["vol_ratio_min"]) & (F["close_pos"] >= p["close_pos_min"])
            & (ext >= p["ext_atr_min"]) & (ext <= p["ext_atr_max"])
            & (c > F["ma50"]) & (F["ma50_slope"] > 0))


def pullback(feat: Features, p: dict) -> pd.DataFrame:
    F = feat.f
    c = F["close"]
    uptrend = (c > F["ma50"]) & (F["ma20"] > F["ma50"]) & (F["ma20_slope"] > 0)
    strong = F["rps_60"] >= p["rps60_min"]
    pushed = F["breakout20_10d"] >= 1
    depth = (F["pb_depth_atr"] >= p["depth_atr_min"]) & (F["pb_depth_atr"] <= p["depth_atr_max"])
    dry = F["pb_vol_ratio"] <= p["vol_ratio_max"]
    held50 = ((c - F["ma50"]).rolling(5, min_periods=3).min() >= 0) & (F["big_down_5d"] == 0)
    stable = (c >= F["ma10"]) & (F["close_pos"] >= p["close_pos_min"])
    return uptrend & strong & pushed & depth & dry & held50 & stable


def vcp(feat: Features, p: dict) -> pd.DataFrame:
    F = feat.f
    c = F["close"]
    # 「收缩」描述突破日之前的状态：ATR 比与布林带宽分位取前一日值（突破日本身的宽幅会稀释收缩）
    contraction = (F["atr_ratio"].shift(1) <= p["atr_ratio_max"]) & (F["bb_pct120"].shift(1) <= p["bb_pct_max"]) \
        & (F["range10_atr"] <= p["range10_atr_max"])
    breakout_ = c > F["box_hi10"]
    return (contraction & breakout_ & (F["vol_ratio"] >= p["vol_ratio_min"]) & (F["close_pos"] >= p["close_pos_min"])
            & (F["dist_52w_high"] <= p["high52_dist_max"]) & (c > F["ma50"]))


DETECTORS = {"breakout": breakout, "pullback": pullback, "vcp": vcp}


def detect_all(feat: Features, params: dict | None = None, enabled: list[str] | None = None) -> dict[str, pd.DataFrame]:
    params = params or default_params()
    enabled = enabled or params.get("enabled", list(DETECTORS))
    out = {}
    for name in enabled:
        out[name] = DETECTORS[name](feat, params[name]).fillna(False)
    return out


def apply_cooldown(sig: pd.DataFrame, days: int) -> pd.DataFrame:
    """同一标的触发后 days 个交易日内不重复出信号（5.4.1）。"""
    a = sig.to_numpy(dtype=bool)
    out = np.zeros_like(a)
    last = np.full(a.shape[1], -10 ** 9)
    for t in range(a.shape[0]):
        ok = a[t] & (t - last > days)
        out[t] = ok
        last = np.where(ok, t, last)
    return pd.DataFrame(out, index=sig.index, columns=sig.columns)


def merge_setups(sigs: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """同日多形态：保留一个主形态（按启用顺序优先），其余记入标签。返回 (任一形态信号, 主形态编码 0..k)。"""
    names = list(sigs)
    any_sig = None
    primary = None
    for i, n in enumerate(names):
        s = sigs[n]
        any_sig = s if any_sig is None else (any_sig | s)
        code = s.astype(np.int8) * (i + 1)
        primary = code if primary is None else primary.where(primary > 0, code)
    return any_sig, primary


def score_frame(feat: Features, weights: dict | None = None) -> pd.DataFrame:
    """综合打分（第一阶段保持简单）：各因子截面分位的加权和，初始等权；缺失分量跳过。
    只有通过 6.6.1 检验的因子才应加入——初始仅 RPS（强度）与行业强弱两个「打分项」（5.4）。"""
    fcfg = settings.cfg()["funnel"]
    w_rps = (weights or {}).get("rps", fcfg["rps"]["weight"])
    w_ind = (weights or {}).get("industry", fcfg["industry_strength"]["weight"])
    F = feat.f
    comps = []
    rps = (F["rps_20"] + F["rps_60"]) / 2
    comps.append((rps, w_rps))
    if "ind_rps_20" in F:
        comps.append((F["ind_rps_20"], w_ind))
    num = sum(x.fillna(0) * w for x, w in comps)
    den = sum(x.notna().astype(float) * w for x, w in comps)
    return (num / den.replace(0, np.nan))
