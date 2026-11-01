"""恐慌日名单的两项补充验证。
  US：触发 = VIX>=25（实际规则）。对比三个池子——全部美股 / 只看标普500∪纳斯达克100（实际选股范围）/ 其余股票——T+5、T+10、T+20。
      成分股是「现在的名单」，天然都是活下来的公司，若它们明显好于其余股票，说明回测有幸存者偏差。
  CN：触发 = 温度<15%（实际规则），T+5、T+10、T+20，对照 = 同期所有合格股票。
用法：python -m tools.panic_validate --market US|CN"""
from __future__ import annotations
import argparse, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from tools.signal_zoo import rsi


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--market", default="US")
    a = ap.parse_args(); mk = a.market.upper()
    from server import db, panel, settings, us_index
    cost = 0.0025 if mk == "CN" else 0.001
    with settings.market_ctx(mk), db.market_db(mk) as conn:
        P = panel.load_panel(conn, "2017-01-01", None, only_l1=(mk == "CN"), float32=True, market=mk)
        vix = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol='^VIX' ORDER BY date", conn).set_index("date")["close"] if mk == "US" else None
    o, c = P.adj["open"].astype("float64"), P.adj["close"].astype("float64")
    ret1 = c.pct_change()
    amt = P.amount.astype("float64") if mk == "CN" else P.raw["close"].astype("float64") * P.volume.astype("float64")
    liq_ok = amt.rolling(20, min_periods=15).mean().rank(axis=1, pct=True) > 0.4
    open1 = o.shift(-1)
    bad = (ret1.abs() > (0.215 if mk == "CN" else 3.0)).astype(float).rolling(30, min_periods=1).max() > 0
    elig = liq_ok & (P.raw["close"] >= (2 if mk == "CN" else 3)) & (P.status.shift(-1) > 0) & open1.notna() & ~bad
    if mk == "CN":
        elig &= (P.is_st == 0) & (open1 / c - 1 < 0.095)
    elig &= pd.Series(P.dates >= "2018-01-01", index=P.dates).to_numpy()[:, None]
    ev0 = (rsi(c, 14) < 30) & (c > c.rolling(200, min_periods=160).mean()) & elig
    ma20 = c.rolling(20, min_periods=16).mean()
    temp = ((c > ma20) & c.notna()).sum(axis=1) / c.notna().sum(axis=1)
    if mk == "US":
        trig = (vix.reindex(P.dates).ffill() >= 25).to_numpy(); tl = "VIX>=25"
        mem = us_index.members()
        is_mem = pd.Series(P.symbols, index=P.symbols).isin(mem).to_numpy()
        pools = [("全部美股", np.ones(len(is_mem), bool)), ("标普500∪纳指100", is_mem), ("其余股票", ~is_mem)]
        print(f"成分股 {int(is_mem.sum())} 只在库里（名单共 {len(mem)} 只）")
    else:
        trig = (temp < .15).to_numpy(); tl = "温度<15%"
        pools = [("全部合格股", np.ones(c.shape[1], bool))]
    dates = P.dates[P.dates >= "2018-01-01"]; split = dates[int(len(dates) * .6)]
    print(f"{mk} 触发={tl}  样本内<{split}<=样本外；事件 = 站上200日线且RSI14<30，次日开盘买")
    for H in (5, 10, 20):
        fwd = c.shift(-H) / open1 - 1 - cost
        print(f"\n===== 持有 {H} 个交易日 =====")
        print(f"{'池子':16s}{'期间':5s}{'天数':>5s}{'事件':>8s}{'胜率':>8s}{'同池同期基础':>12s}{'平均':>8s}{'每日等权平均':>12s}")
        for lab, pm_ in pools:
            sel = pd.Series(pm_, index=c.columns).to_numpy()
            ev = ev0.loc[:, sel] & fwd.loc[:, sel].notna()
            fw = fwd.loc[:, sel]
            base_w = (fw > 0).where(fw.notna() & elig.loc[:, sel]).mean(axis=1)
            for per, pm in (("全部", P.dates >= "2018-01-01"), ("样本内", (P.dates >= "2018-01-01") & (P.dates < split)), ("样本外", P.dates >= split)):
                m = trig & pm
                f = fw.where(ev)[m]
                n = int(f.notna().sum().sum())
                if n < 30:
                    print(f"{lab:16s}{per:5s}  事件<30"); continue
                days = int(f.notna().any(axis=1).sum())
                w = float((f > 0).where(f.notna()).sum().sum() / n)
                bw = float(base_w[m][f.notna().any(axis=1).to_numpy()].mean())
                dm = float(f.mean(axis=1).mean())
                print(f"{lab:16s}{per:5s}{days:5d}{n:8d}{w:8.1%}{bw:12.1%}{np.nanmean(f.to_numpy()):+8.2%}{dm:+12.2%}")


main()
