"""VIX 恐慌指数研究：VIX（1990 年起）处在不同水平 / 期限结构（VIX ÷ VIX3M）之后，标普500 接下来 20 / 60 个交易日的涨跌。
结果写入 server/vix_evidence.json，供「美股行情」页的 VIX 面板引用。只描述历史统计，不预设方向。

分区（事先定好，不调参）：VIX < 15 / 15~20 / 20~25 / 25~30 / 30~40 / ≥ 40；期限结构 VIX ÷ VIX3M：< 0.90 / 0.90~1.00 / ≥ 1.00（≥ 1 = 近端比远端更恐慌）。
前瞻收益用标普500 指数价格（不含股息），次日起算；样本重叠，所以同时给出「独立次数」（进入该区的次数）。
样本内 = 前 60% 日期、样本外 = 后 40%：只看两段方向是否一致。

用法：python tools/vix_research.py        （数据缓存到 data/cache/ma_research/，与 ma_research.py 共用）
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache" / "ma_research"
OUT = ROOT / "server" / "vix_evidence.json"
ZONES = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 999)]
TS_ZONES = [(0, 0.90), (0.90, 1.00), (1.00, 9)]
HZ = (20, 60)


def fetch(t: str, name: str):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{name}.csv"
    if f.exists():
        return
    import yfinance as yf
    df = yf.download(t, period="max", auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.strftime("%Y-%m-%d")
    df.to_csv(f)
    print("下载", t, df.index[0], df.index[-1], len(df), flush=True)


def stats_for(mask: pd.Series, px: pd.Series, split: str) -> dict:
    row = {"days": int(mask.sum()), "episodes": int((mask & ~mask.shift(1, fill_value=False)).sum())}
    for h in HZ:
        f = (px.shift(-h - 1) / px.shift(-1) - 1)            # 信号日收盘后，次日收盘起算
        a = f[mask].dropna()
        i = f[mask & (f.index < split)].dropna()
        o = f[mask & (f.index >= split)].dropna()
        row[f"fwd{h}"] = {"n": int(len(a)), "mean": round(float(a.mean()), 4) if len(a) else None, "median": round(float(a.median()), 4) if len(a) else None,
                          "win": round(float((a > 0).mean()), 3) if len(a) else None, "worst": round(float(a.min()), 4) if len(a) else None,
                          "is_mean": round(float(i.mean()), 4) if len(i) else None, "oos_mean": round(float(o.mean()), 4) if len(o) else None}
    return row


def main():
    fetch("^VIX", "VIX"); fetch("^VIX3M", "VIX3M"); fetch("^GSPC", "GSPC")
    rd = lambda n: pd.read_csv(CACHE / f"{n}.csv", index_col=0)["Close"].astype(float).dropna()
    vix, v3, spx = rd("VIX"), rd("VIX3M"), rd("GSPC")
    df = pd.concat({"vix": vix, "v3": v3, "spx": spx}, axis=1).dropna(subset=["vix", "spx"])
    split = df.index[int(len(df) * 0.6)]
    allf = {}
    for h in HZ:
        f = (df["spx"].shift(-h - 1) / df["spx"].shift(-1) - 1).dropna()
        allf[f"fwd{h}"] = {"mean": round(float(f.mean()), 4), "win": round(float((f > 0).mean()), 3)}
    zones = []
    for lo, hi in ZONES:
        zones.append({"lo": lo, "hi": hi, **stats_for((df["vix"] >= lo) & (df["vix"] < hi), df["spx"], split)})
    d3 = df.dropna(subset=["v3"]).copy()
    d3["ratio"] = d3["vix"] / d3["v3"]
    ts = [{"lo": lo, "hi": hi, **stats_for((d3["ratio"] >= lo) & (d3["ratio"] < hi), d3["spx"], split)} for lo, hi in TS_ZONES]
    # 历史分位的参考：VIX 全部历史的分位点
    q = {str(p): round(float(vix.quantile(p / 100)), 1) for p in (10, 25, 50, 75, 90, 95)}
    res = {"generated": date.today().isoformat(), "vix_from": df.index[0], "vix3m_from": d3.index[0], "to": df.index[-1], "split": split,
           "zones": zones, "term_zones": ts, "unconditional": allf, "quantiles": q,
           "rule": "标普500 价格指数，信号日收盘后次日起算；样本重叠，episodes = 进入该区的独立次数"}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"VIX {df.index[0]} ~ {df.index[-1]}，样本内到 {split}；全部日子 20 日 {allf['fwd20']['mean']:+.1%}/{allf['fwd20']['win']:.0%}，60 日 {allf['fwd60']['mean']:+.1%}/{allf['fwd60']['win']:.0%}")
    print("VIX 分区 → 之后 20 / 60 日（均值 / 上涨概率 / 最差 | 样本内均值 / 样本外均值 | 天数 / 独立次数）")
    for z in zones:
        a, b = z["fwd20"], z["fwd60"]
        print(f"  [{z['lo']:>3},{z['hi']:>3})  20日 {a['mean']:+.1%} {a['win']:.0%} 最差 {a['worst']:+.0%} | IS {a['is_mean']:+.1%} OOS {a['oos_mean'] if a['oos_mean'] is None else format(a['oos_mean'], '+.1%')}   "
              f"60日 {b['mean']:+.1%} {b['win']:.0%} 最差 {b['worst']:+.0%} | IS {b['is_mean']:+.1%} OOS {b['oos_mean'] if b['oos_mean'] is None else format(b['oos_mean'], '+.1%')}   ({z['days']} 天 / {z['episodes']} 次)")
    print("期限结构 VIX/VIX3M：")
    for z in ts:
        a, b = z["fwd20"], z["fwd60"]
        print(f"  [{z['lo']:.2f},{z['hi']:.2f})  20日 {a['mean']:+.1%} {a['win']:.0%} | 60日 {b['mean']:+.1%} {b['win']:.0%} 最差 {b['worst']:+.0%}  ({z['days']} 天 / {z['episodes']} 次)")
    print("写入", OUT)


if __name__ == "__main__":
    main()
