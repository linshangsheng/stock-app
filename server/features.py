"""指标与因子计算（2.4 / 3.6.1 / 3.7~3.13.1）。**唯一**的特征计算位置：盘后扫描、回测、界面展示共用，口径只此一份。
pandas 宽表（日期 × 股票）向量化，不逐只循环；特征现算、不落库（3.26）。

口径（3.6.1，变更须升级 FEATURES_VERSION 并重跑历史）：
  * 价格用前复权序列；成交量 / 成交额用原值（A 股单位：股 / 元）
  * MA_N 含当日；EMA 取 adjust=False；ATR 用 Wilder 平滑（alpha=1/N），TR 的前收取向前填充的最近有效收盘
  * 突破用的 N 日最高价 HH_N **不含当日**；量比分母为前 20 日均量（**不含当日**）；52 周 = 252 个交易日
  * 滚动窗口内有效交易日占比 < min_valid_ratio 时输出缺失（停牌日价格不填充；收益率基准价取向前填充 ≤10 日的最近有效收盘）
  * RPS = 当日交易池 L2 内的截面百分位（0~100）；当日有效样本数低于下限时不计算
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from . import markets, settings
from .panel import Panel

FEATURES_VERSION = "1"


@dataclass
class Features:
    f: dict[str, pd.DataFrame] = field(default_factory=dict)       # 日期 × 股票
    m: dict[str, pd.Series] = field(default_factory=dict)          # 市场级序列（日期索引）
    regime: pd.Series | None = None
    industry_of: pd.Series | None = None

    def __getitem__(self, k: str) -> pd.DataFrame:
        return self.f[k]


def _mp(window: int, ratio: float) -> int:
    return max(2, int(np.ceil(window * ratio)))


def rolling_rank_pct(df: pd.DataFrame, window: int, min_periods: int, chunk: int = 100) -> pd.DataFrame:
    """每列：当日值在近 window 日（含当日）内的分位（≤当日值的个数 / 有效个数）。分块避免内存爆炸。"""
    a = df.to_numpy(dtype=np.float32)
    T, N = a.shape
    out = np.full((T, N), np.nan, dtype=np.float32)
    if T >= window:
        for j0 in range(0, N, chunk):
            w = sliding_window_view(a[:, j0:j0 + chunk], window, axis=0)       # (T-w+1, n, window)
            last = w[..., -1][..., None]
            valid = ~np.isnan(w)
            cnt = valid.sum(-1)
            le = ((w <= last) & valid).sum(-1)
            r = np.where((cnt >= min_periods) & ~np.isnan(last[..., 0]), le / np.maximum(cnt, 1), np.nan)
            out[window - 1:, j0:j0 + chunk] = r
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def cross_section_rank(df: pd.DataFrame, member: pd.DataFrame | None, min_sample: int) -> pd.DataFrame:
    """截面百分位 0~100（并列取平均名次）；样本数不足下限的日期输出 NaN。"""
    x = df.where(member) if member is not None else df
    r = x.rank(axis=1, pct=True, method="average") * 100
    n = x.notna().sum(axis=1)
    return r.where(n >= min_sample, np.nan)


def wilder(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = close.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    rs = wilder(up, n) / wilder(dn, n).replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(wilder(dn, n) != 0, 100.0).where(wilder(up, n).notna())


def effective_rps_min_sample() -> int:
    n = settings.cfg()["features"]["rps_min_sample"]
    if markets.is_demo(settings.current_market()):
        return min(n, 30)
    return n


def compute_features(panel: Panel, l2: pd.DataFrame | None = None, bench_close: pd.Series | None = None,
                     industry_of: pd.Series | None = None, params: dict | None = None,
                     vix: pd.Series | None = None) -> Features:
    c = settings.cfg()
    fc = c["features"]
    vr = fc["min_valid_ratio"]
    atr_n = fc.get("atr_period", 14)
    rc = c["regime"]

    close, high, low, open_ = panel.adj["close"].astype(np.float32), panel.adj["high"].astype(np.float32), \
        panel.adj["low"].astype(np.float32), panel.adj["open"].astype(np.float32)
    vol, amt = panel.volume.astype(np.float32), panel.amount.astype(np.float32)
    close_ff = close.ffill(limit=10)
    F: dict[str, pd.DataFrame] = {"close": close}

    # ---- 趋势 ----
    for n in (5, 10, 20, 50, 100, 200):
        F[f"ma{n}"] = close.rolling(n, min_periods=_mp(n, vr)).mean()
    for n in (20, 50, 200):
        F[f"close_ma{n}"] = close / F[f"ma{n}"]
    F["ma20_ma50"] = F["ma20"] / F["ma50"]
    F["ma50_ma200"] = F["ma50"] / F["ma200"]
    F["ma20_slope"] = F["ma20"] / F["ma20"].shift(5) - 1
    F["ma50_slope"] = F["ma50"] / F["ma50"].shift(5) - 1

    # ---- 动量 ----
    for n in (1, 3, 5, 10, 20, 60):
        F[f"ret_{n}"] = close / close_ff.shift(n) - 1

    # ---- 成交量 / 成交额 ----
    for n in (5, 10, 20):
        F[f"vol_ma{n}"] = vol.rolling(n, min_periods=_mp(n, vr)).mean()
    F["amt_ma5"] = amt.rolling(5, min_periods=_mp(5, vr)).mean()
    F["amt_ma20"] = amt.rolling(20, min_periods=_mp(20, vr)).mean()
    base = vol.rolling(20, min_periods=_mp(20, vr)).mean().shift(1)         # 不含当日
    F["vol_ratio"] = vol / base

    # ---- 波动率 ----
    prev_c = close_ff.shift(1)
    tr = pd.DataFrame(np.fmax(np.fmax((high - low).to_numpy(), (high - prev_c).abs().to_numpy()),
                              (low - prev_c).abs().to_numpy()), index=close.index, columns=close.columns)
    F["atr14"] = wilder(tr, atr_n)
    F["atr20"], F["atr60"] = wilder(tr, 20), wilder(tr, 60)
    F["atr_pct"] = F["atr14"] / close
    lr = np.log(close / prev_c)
    F["hv20"] = lr.rolling(20, min_periods=_mp(20, vr)).std() * np.sqrt(252)
    F["hv60"] = lr.rolling(60, min_periods=_mp(60, vr)).std() * np.sqrt(252)
    F["atr_ratio"] = F["atr20"] / F["atr60"]
    sd20 = close.rolling(20, min_periods=_mp(20, vr)).std(ddof=0)
    F["bb_width"] = 4 * sd20 / F["ma20"]
    F["bb_pct120"] = rolling_rank_pct(F["bb_width"], 120, 90)

    # ---- 价格突破 / 位置（滚动高低点不含当日）----
    for n in (5, 10, 20, 50):
        F[f"hh{n}"] = high.rolling(n, min_periods=_mp(n, vr)).max().shift(1)
    h252 = fc.get("high_52w_days", 252)
    F["hh252"] = high.rolling(h252, min_periods=_mp(h252, 0.5)).max().shift(1)
    F["ll5"] = low.rolling(5, min_periods=3).min().shift(1)
    F["ll20"] = low.rolling(20, min_periods=_mp(20, vr)).min().shift(1)
    hi52_incl = high.rolling(h252, min_periods=_mp(h252, 0.5)).max()
    F["dist_52w_high"] = 1 - close / hi52_incl
    rng = (high - low)
    F["close_pos"] = ((close - low) / rng.where(rng > 0)).fillna(0.5).where(close.notna())
    F["ext_atr_hh20"] = (close - F["hh20"]) / F["atr14"]
    # 收敛区间：近 10 日（不含当日）最高 - 最低，以 ATR 计
    box_hi, box_lo = F["hh10"], low.rolling(10, min_periods=_mp(10, vr)).min().shift(1)
    F["box_hi10"], F["box_lo10"] = box_hi, box_lo
    F["range10_atr"] = (box_hi - box_lo) / F["atr14"].shift(1)
    F["ll5_incl"] = low.rolling(5, min_periods=3).min()                  # 含当日：回踩期间最低点（结构化止损）
    F["struct_low_breakout"] = box_lo                                   # 突破前整理区（近 10 日，不含当日）低点

    # ---- 回踩质量 ----
    F["pb_depth_atr"] = (close - F["ma20"]) / F["atr14"]
    F["dist_ma10_atr"] = (close - F["ma10"]) / F["atr14"]
    F["pb_vol_ratio"] = F["vol_ratio"].rolling(4, min_periods=3).mean()
    big_down = (F["vol_ratio"] >= 1.5) & (F["ret_1"] <= -1.5 * F["atr_pct"])
    F["big_down_5d"] = big_down.astype(np.float32).rolling(5, min_periods=1).sum()
    F["breakout20_10d"] = (close > F["hh20"]).astype(np.float32).rolling(10, min_periods=1).max()

    # ---- 趋势质量 / 风险调整动量 ----
    F["pct_above_ma20_20d"] = (close > F["ma20"]).astype(np.float32).where(F["ma20"].notna()).rolling(20, min_periods=15).mean()
    t = pd.Series(np.arange(len(close), dtype=np.float32), index=close.index)
    logc = np.log(close)
    F["trend_r2"] = (logc.rolling(20, min_periods=18).corr(t)) ** 2
    F["risk_adj_mom20"] = F["ret_20"] / F["hv20"]

    # ---- RSI / MACD ----
    F["rsi14"] = rsi(close, 14)
    ema12, ema26 = close.ewm(span=12, adjust=False).mean(), close.ewm(span=26, adjust=False).mean()
    F["macd_dif"] = ema12 - ema26
    F["macd_dea"] = F["macd_dif"].ewm(span=9, adjust=False).mean()
    F["macd_hist"] = (F["macd_dif"] - F["macd_dea"]) * 2

    # ---- 交易池内的截面排名 ----
    member = l2 if l2 is not None else None
    min_s = effective_rps_min_sample()
    for n in (10, 20, 60):
        F[f"rps_{n}"] = cross_section_rank(F[f"ret_{n}"], member, min_s)

    out = Features(f=F)

    # ---- 相对强度（vs 基准）----
    if bench_close is not None and len(bench_close):
        b = bench_close.reindex(close.index).ffill()
        for n in (10, 20):
            F[f"rs_{n}"] = F[f"ret_{n}"].sub(b / b.shift(n) - 1, axis=0)
        out.m["bench_close"] = b
        out.m["bench_ma50"] = b.rolling(rc["ma_mid"], min_periods=40).mean()
        out.m["bench_ma200"] = b.rolling(rc["ma_long"], min_periods=150).mean()
        out.m["bench_ret_20"] = b / b.shift(20) - 1

    # ---- 行业强弱：由库内个股日收益等权合成（3.14）----
    if industry_of is not None and len(industry_of):
        ind = industry_of.reindex(close.columns)
        r1 = (close / prev_c - 1).clip(-0.25, 0.25)
        r1 = r1.where(member) if member is not None else r1
        grp = r1.T.groupby(ind.to_numpy()).mean().T                                  # 日期 × 行业
        idx = (1 + grp.fillna(0)).cumprod()
        ind_ret = {n: (idx / idx.shift(n) - 1) for n in (5, 10, 20)}
        ind_rps20 = ind_ret[20].rank(axis=1, pct=True) * 100
        cols = ind.to_numpy()
        for n in (10, 20):
            F[f"ind_ret_{n}"] = pd.DataFrame(ind_ret[n].reindex(columns=cols).to_numpy(), index=close.index, columns=close.columns)
        F["ind_rps_20"] = pd.DataFrame(ind_rps20.reindex(columns=cols).to_numpy(), index=close.index, columns=close.columns)
        F["ind_rs_20"] = F["ret_20"] - F["ind_ret_20"]
        out.industry_of = ind

    # ---- 市场宽度（统计口径为当日交易池 L2，3.5）----
    mem = member if member is not None else close.notna()
    def share(cond):
        ok = mem & close.notna() & cond.notna()
        v = cond.astype(float).where(ok)
        return v.sum(axis=1) / ok.sum(axis=1).replace(0, np.nan)
    ret1 = F["ret_1"]
    up = ((ret1 > 0) & mem).sum(axis=1)
    dn = ((ret1 < 0) & mem).sum(axis=1)
    out.m["adv_ratio"] = up / (up + dn).replace(0, np.nan)
    b50 = rc.get("breadth_ma", 50)
    ma_b = F["ma50"] if b50 == 50 else close.rolling(b50, min_periods=_mp(b50, vr)).mean()
    out.m["breadth_ma20"] = share(close > F["ma20"])
    out.m["breadth_ma50"] = share(close > ma_b)
    out.m["breadth_ma200"] = share(close > F["ma200"])
    out.m["new_high_cnt"] = ((close > F["hh252"]) & mem).sum(axis=1)
    out.m["pool_size"] = mem.sum(axis=1)

    if vix is not None and len(vix):
        out.m["vix"] = vix.reindex(close.index).ffill()
    out.regime = classify_regime(out.m, rc)
    return out


def classify_regime(m: dict[str, pd.Series], rc: dict) -> pd.Series:
    """市场环境闸门（5.4，候选规则，参数待回测）：NORMAL / CAUTION / DEFENSIVE。基准缺失时为 UNKNOWN（按谨慎处理）。"""
    if "bench_close" not in m:
        idx = m["breadth_ma50"].index
        return pd.Series("UNKNOWN", index=idx)
    b, ma50, ma200 = m["bench_close"], m["bench_ma50"], m["bench_ma200"]
    br = m["breadth_ma50"]
    above50 = b > ma50
    defensive = (b < ma200) & (br < rc["breadth_defensive"])
    normal = above50 & (br >= rc["breadth_normal"])
    reg = pd.Series("CAUTION", index=b.index)
    reg[normal] = "NORMAL"
    reg[defensive] = "DEFENSIVE"
    thr = rc.get("vix_caution") or 0
    if thr and "vix" in m:                                  # 美股：VIX 过高时 NORMAL 降为 CAUTION（候选规则，参数待回测）
        reg[(reg == "NORMAL") & (m["vix"] > thr)] = "CAUTION"
    reg[ma50.isna() | br.isna()] = "UNKNOWN"
    return reg
