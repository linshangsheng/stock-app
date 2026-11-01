"""超跌信号的优势来自哪里：按「当天同时出现几只超跌股」分组（几只 = 个股孤立超跌；几十只 = 全市场普跌）。用法：python -m tools.oversold_breadth --market CN|US"""
from __future__ import annotations
import argparse, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from tools.signal_zoo import rsi

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--market", default="CN"); ap.add_argument("--hold", type=int, default=5)
    a = ap.parse_args(); H = a.hold
    from server import db, panel, settings
    cost = 0.0025 if a.market == "CN" else 0.001
    with settings.market_ctx(a.market), db.market_db(a.market) as conn:
        P = panel.load_panel(conn, "2017-01-01", None, only_l1=(a.market == "CN"), float32=True, market=a.market)
    o, c = P.adj["open"].astype("float64"), P.adj["close"].astype("float64")
    ret1 = c.pct_change()
    amt = P.amount.astype("float64") if a.market == "CN" else P.raw["close"].astype("float64") * P.volume.astype("float64")
    liq_ok = amt.rolling(20, min_periods=15).mean().rank(axis=1, pct=True) > 0.4
    open1 = o.shift(-1)
    bad = (ret1.abs() > (0.215 if a.market == "CN" else 3.0)).astype(float).rolling(30, min_periods=1).max() > 0
    elig = liq_ok & (P.raw["close"] >= (2 if a.market == "CN" else 3)) & (P.status.shift(-1) > 0) & (P.is_st == 0) \
        & (open1 / c - 1 < (0.095 if a.market == "CN" else 9)) & open1.notna() & ~bad
    elig &= pd.Series(P.dates >= "2018-01-01", index=P.dates).to_numpy()[:, None]
    fwd = c.shift(-H) / open1 - 1 - cost
    ma200 = c.rolling(200, min_periods=160).mean()
    ev = (rsi(c, 14) < 30) & (c > ma200) & elig & fwd.notna()
    n_ev = ev.sum(axis=1)
    # 市场层面：全市场有多少比例的股票站在 20 日线上（温度）
    ma20 = c.rolling(20, min_periods=16).mean()
    temp = ((c > ma20) & c.notna()).sum(axis=1) / c.notna().sum(axis=1)
    base_w = (fwd > 0).where(fwd.notna() & elig).mean(axis=1)
    dates = P.dates[P.dates >= "2018-01-01"]; split = dates[int(len(dates) * .6)]
    print(f"{a.market} 持有{H}天 样本内<{split}<=样本外")
    def show(title, grp):
        print(f"\n-- {title}")
        for lab, cond in grp:
            for per, pm in (("全部", P.dates >= "2018-01-01"), ("样本内", (P.dates >= "2018-01-01") & (P.dates < split)), ("样本外", P.dates >= split)):
                m = cond.to_numpy() & pm
                f = fwd.where(ev)[m]
                n = int(f.notna().sum().sum())
                if n < 30: print(f"{lab:22s}{per} n<30"); continue
                w = float((f > 0).where(f.notna()).sum().sum() / n)
                bw = float(base_w[m][f.notna().any(axis=1).to_numpy()].mean())
                print(f"{lab:22s}{per:4s} 天数{int((n_ev[m] > 0).sum()):4d} 事件{n:6d} 胜率{w:.1%} (同期基础{bw:.1%}) 均值{np.nanmean(f.to_numpy()):+.2%}")
    show("按当天超跌股数量分组", [("1~2 只", (n_ev >= 1) & (n_ev <= 2)), ("3~9 只", (n_ev >= 3) & (n_ev <= 9)), ("10~29 只", (n_ev >= 10) & (n_ev <= 29)), ("30~99 只", (n_ev >= 30) & (n_ev <= 99)), ("100 只以上", n_ev >= 100)])
    show("按当天市场温度(站上20日线比例)分组", [("温度<15%", temp < .15), ("15%~30%", (temp >= .15) & (temp < .30)), ("30%~50%", (temp >= .30) & (temp < .50)), ("50%~70%", (temp >= .50) & (temp < .70)), (">=70%", temp >= .70)])

main()
