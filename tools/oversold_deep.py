"""「长期上升趋势里的个股超跌」深挖（signal_zoo 里唯一在 A 股 / 美股、样本内外都有优势的一类）。
做三件事：
 1) 稳健性：剔除疑似数据错误（近 30 天出现超出涨跌停的单日涨跌）、剔除次日开盘涨停买不到 / 卖出日跌停卖不掉；
 2) 排序：事件内部用哪个特征排序，Top N 的胜率最高（先在样本内挑，再看样本外）；
 3) 市场闸门：大盘（等权指数）在 / 不在 200 日线上，各自的胜率。
用法：python tools/oversold_deep.py --market CN|US"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from tools.signal_zoo import rsi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="CN")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--hold", type=int, default=5)
    a = ap.parse_args()
    from server import db, panel, settings
    H = a.hold
    cost = 0.0025 if a.market == "CN" else 0.001
    t0 = time.time()
    with settings.market_ctx(a.market), db.market_db(a.market) as conn:
        P = panel.load_panel(conn, "2017-01-01", None, only_l1=(a.market == "CN"), float32=True, market=a.market)
    o, h_, l, c = (P.adj[k].astype("float64") for k in ("open", "high", "low", "close"))
    ret1 = c.pct_change()
    lim_err = 0.215 if a.market == "CN" else 3.0                     # 超过这个单日涨跌几乎只可能是数据错误（A 股创业板 / 科创板 20%）
    bad = (ret1.abs() > lim_err).astype(float).rolling(30, min_periods=1).max() > 0
    amt = P.amount.astype("float64") if a.market == "CN" else (P.raw["close"].astype("float64") * P.volume.astype("float64"))
    liq = amt.rolling(20, min_periods=15).mean()
    liq_rank = liq.rank(axis=1, pct=True)
    open1 = o.shift(-1)
    gap_up = open1 / c - 1
    limit_up_open = gap_up > (0.095 if a.market == "CN" else 9)
    exit_c = c.shift(-H)
    exit_blocked = ((exit_c / c.shift(-H + 1) - 1) < -0.095) & (l.shift(-H) >= exit_c * 0.999) if a.market == "CN" else (exit_c * 0)
    exit_blocked = exit_blocked.fillna(False).astype(bool) if a.market == "CN" else pd.DataFrame(False, index=c.index, columns=c.columns)
    elig = (liq_rank > 0.4) & (P.raw["close"] >= (2 if a.market == "CN" else 3)) & (P.status.shift(-1) > 0) & (P.is_st == 0) \
        & ~limit_up_open & open1.notna() & c.notna() & ~bad
    elig &= pd.Series(P.dates >= a.start, index=P.dates).to_numpy()[:, None]
    fwd = c.shift(-H) / open1 - 1 - cost
    fwd_ok = fwd.where(~exit_blocked)                                # 卖出日跌停锁死的算作无法卖出：剔除（偏乐观的一侧，下面单独报告个数）
    ma200, ma20 = c.rolling(200, min_periods=160).mean(), c.rolling(20, min_periods=16).mean()
    r14 = rsi(c, 14)
    tr = np.maximum(h_ - l, np.maximum((h_ - c.shift()).abs(), (l - c.shift()).abs()))
    atr_pct = tr.rolling(14, min_periods=10).mean() / c
    vol20 = ret1.rolling(20, min_periods=16).std()
    hh252 = c.rolling(252, min_periods=200).max()
    mom60 = c / c.shift(60) - 1
    mom120 = c / c.shift(120) - 1
    volr = P.volume.astype("float64") / P.volume.astype("float64").rolling(20, min_periods=16).mean()
    feats = {"RSI14(低好)": -r14, "距MA20的ATR数(深好)": -((c - ma20) / (atr_pct * c)), "距52周高点回撤(深好)": -(c / hh252),
             "ATR%(低波动好)": -atr_pct, "20日波动(低好)": -vol20, "60日动量(强好)": mom60, "120日动量(强好)": mom120,
             "量比(缩量好)": -volr, "成交额(大好)": liq, "当日跌幅(深好)": -ret1,
             "站上200日线幅度(高好)": c / ma200 - 1}
    mk = ret1.where(P.status > 0).mean(axis=1).fillna(0)
    mk_idx = (1 + mk).cumprod()
    bull = (mk_idx > mk_idx.rolling(200, min_periods=150).mean())
    dates = P.dates[P.dates >= a.start]
    split = dates[int(len(dates) * 0.6)]
    is_m = pd.Series((P.dates >= a.start) & (P.dates < split), index=P.dates)
    oos_m = pd.Series(P.dates >= split, index=P.dates)
    base_win = (fwd_ok > 0).where(fwd_ok.notna() & elig).mean(axis=1)
    base_mean = fwd_ok.where(elig).mean(axis=1)
    print(f"{a.market}: 加载+准备 {time.time()-t0:.0f}s；持有 {H} 天；样本内 < {split} <= 样本外；卖出日跌停锁死剔除 {int((exit_blocked & elig).sum().sum())} 个事件", flush=True)

    def report(name, sig, extra=None):
        sig = sig & elig & fwd_ok.notna()
        f = fwd_ok.where(sig)
        out = [f"{name:44s}"]
        for lab, m in (("全部", pd.Series(True, index=P.dates)), ("样本内", is_m), ("样本外", oos_m), ("牛", bull), ("熊", ~bull)):
            fm = f[m.to_numpy()]
            n = int(fm.notna().sum().sum())
            if n < 50:
                out.append(f"{lab} n<50")
                continue
            w = float((fm > 0).where(fm.notna()).sum().sum() / n)
            bw = float(base_win[m.to_numpy()][fm.notna().any(axis=1).to_numpy()].mean())
            out.append(f"{lab} n={n:6d} 胜率{w:.1%}(同期基础{bw:.1%}) 均值{np.nanmean(fm.to_numpy()):+.2%}")
        print(" | ".join(out), flush=True)
        return f

    base_sig = (r14 < 30) & (c > ma200)
    print("\n== 1) 稳健性（已剔除数据错误、涨停买不到）==")
    report("RSI14<30 且 站上200日线", base_sig)
    report("  + 大盘在200日线上（牛）", base_sig & bull.to_numpy()[:, None])
    d3 = ((c - ma20) / (atr_pct * c) < -3) & (c > ma200)
    report("距MA20<-3ATR 且 站上200日线", d3)
    report("  + 大盘在200日线上（牛）", d3 & bull.to_numpy()[:, None])
    both = base_sig & d3
    report("两者同时满足", both)
    report("  + 大盘在200日线上（牛）", both & bull.to_numpy()[:, None])

    print("\n== 2) 事件内排序：每天取 Top N（事件=RSI14<30 且 站上200日线），按特征排序 ==")
    ev = base_sig & elig & fwd_ok.notna()
    ev_n = ev.sum(axis=1)
    print(f"事件日数 {int((ev_n > 0).sum())}，平均每天 {ev_n[ev_n > 0].mean():.1f} 只，中位 {ev_n[ev_n > 0].median():.0f}")
    res = []
    for fname, fv in feats.items():
        rk = fv.where(ev).rank(axis=1, ascending=False)
        for N in (3, 5, 10):
            sel = ev & (rk <= N)
            f = fwd_ok.where(sel)
            row = [fname, N]
            for m in (is_m, oos_m):
                fm = f[m.to_numpy()]
                n = int(fm.notna().sum().sum())
                row += [n, float((fm > 0).where(fm.notna()).sum().sum() / max(n, 1)), float(np.nanmean(fm.to_numpy())) if n else np.nan]
            res.append(row)
    df = pd.DataFrame(res, columns=["特征", "N", "n_is", "win_is", "mean_is", "n_oos", "win_oos", "mean_oos"]).sort_values("win_is", ascending=False)
    print(df.round(4).to_string(index=False))
    print(f"\n用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
