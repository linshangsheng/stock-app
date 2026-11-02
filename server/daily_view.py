"""日线看盘辅助：个股相对大盘的强弱线、价格所处的位置。纯函数，不读库，不进选股 / 回测。
这两样都是「帮你看」的视角，没有做过回测验证，不参与任何买卖信号（页面上标「无」证据）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

BENCH_NAME = {"sh.000300": "沪深300", "SPY": "标普500（SPY）"}


def _series(s: pd.Series) -> list[dict]:
    return [{"time": t, "value": round(float(v), 4)} for t, v in zip(s.index, s.values) if v == v and np.isfinite(v)]


def rel_strength(close: pd.Series, bench: pd.Series | None, smooth: int = 20) -> dict | None:
    """个股 ÷ 基准，窗口第一天归一成 100。线往上 = 比大盘强（哪怕股价本身在跌）；线在自己的均线上方 = 近期一直在跑赢。

    close / bench 的索引都是日期字符串；基准缺的日子沿用前一个收盘。少于 30 个点不画。"""
    if bench is None or not len(bench):
        return None
    b = bench.reindex(close.index.union(bench.index)).ffill().reindex(close.index)
    r = (close / b).replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 30:
        return None
    line = r / r.iloc[0] * 100
    ma = line.rolling(smooth, min_periods=smooth).mean()

    def chg(n: int):
        return float(line.iloc[-1] / line.iloc[-1 - n] - 1) if len(line) > n else None

    above = bool(line.iloc[-1] > ma.iloc[-1]) if ma.iloc[-1] == ma.iloc[-1] else None
    c60 = chg(60)
    if above is None or c60 is None:
        state = None
    elif above and c60 > 0:
        state = "strong_up"          # 60 日来强于大盘，最近还在走强
    elif c60 > 0:
        state = "strong_pause"       # 60 日来强于大盘，但最近这一段在回落（回调中）
    elif above:
        state = "weak_bounce"        # 60 日来弱于大盘，但最近有所回升（反弹，还没扭转）
    else:
        state = "weak_down"          # 60 日来弱于大盘，最近还在走弱
    return {"line": _series(line), "ma": _series(ma), "smooth": smooth, "chg_20": chg(20), "chg_60": chg(60), "state": state}


def price_position(d: pd.DataFrame, window: int = 250, min_rows: int = 60) -> dict | None:
    """现价在近一年（约 250 个交易日）里的位置：距均线、距 52 周高低点、一年内的分位。d 需要 high / low / close 三列（日线、前复权）。"""
    if len(d) < min_rows:
        return None
    w = d.tail(window)
    c = float(d["close"].iloc[-1])
    hi, lo = float(w["high"].max()), float(w["low"].min())
    if not (hi > lo > 0):
        return None
    out = {"close": c, "hi52": hi, "lo52": lo, "pos52": (c - lo) / (hi - lo), "rank_1y": float((w["close"] <= c).mean()),
           "days_since_high": int(len(w) - 1 - int(np.argmax(w["high"].to_numpy()))),
           "days_since_low": int(len(w) - 1 - int(np.argmin(w["low"].to_numpy()))), "rows": int(len(w))}
    for n in (20, 50, 200):
        m = d["close"].rolling(n, min_periods=n).mean().iloc[-1]
        out[f"ma{n}"] = None if m != m else float(m)
        out[f"dist_ma{n}"] = None if m != m else float(c / m - 1)
    out["dist_hi52"] = c / hi - 1
    out["dist_lo52"] = c / lo - 1
    return out
