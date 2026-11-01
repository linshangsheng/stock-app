"""美股「恐慌日」用什么判断：市场温度（站上 20 日线的股票占比）还是 VIX？
事件 = 站上 200 日线且 RSI14<30 的股票（次日开盘买，扣成本），按当天的 VIX / 温度分组，看 T+5、T+20 胜率（对照 = 同期所有合格股票）。
用法：python -m tools.panic_vix_compare"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from tools.signal_zoo import rsi


def main():
    from server import db, panel, settings
    with settings.market_ctx("US"), db.market_db("US") as conn:
        P = panel.load_panel(conn, "2017-01-01", None, only_l1=False, float32=True, market="US")
        vix = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol='^VIX' ORDER BY date", conn).set_index("date")["close"]
    o, c = P.adj["open"].astype("float64"), P.adj["close"].astype("float64")
    ret1 = c.pct_change()
    liq_ok = (P.raw["close"].astype("float64") * P.volume.astype("float64")).rolling(20, min_periods=15).mean().rank(axis=1, pct=True) > 0.4
    open1 = o.shift(-1)
    bad = (ret1.abs() > 3.0).astype(float).rolling(30, min_periods=1).max() > 0
    elig = liq_ok & (P.raw["close"] >= 3) & (P.status.shift(-1) > 0) & open1.notna() & ~bad
    elig &= pd.Series(P.dates >= "2018-01-01", index=P.dates).to_numpy()[:, None]
    ev0 = (rsi(c, 14) < 30) & (c > c.rolling(200, min_periods=160).mean()) & elig
    ma20 = c.rolling(20, min_periods=16).mean()
    temp = ((c > ma20) & c.notna()).sum(axis=1) / c.notna().sum(axis=1)
    v = vix.reindex(P.dates).ffill()
    v_chg5 = v / v.shift(5) - 1
    v_ma = v / v.rolling(20, min_periods=15).mean() - 1
    dates = P.dates[P.dates >= "2018-01-01"]
    split = dates[int(len(dates) * .6)]
    print(f"US 样本内<{split}<=样本外；VIX 取信号日收盘（当天可知）", flush=True)
    for H in (5, 20):
        fwd = c.shift(-H) / open1 - 1 - 0.001
        ev = ev0 & fwd.notna()
        base_w = (fwd > 0).where(fwd.notna() & elig).mean(axis=1)
        print(f"\n===== 持有 {H} 个交易日 =====")
        groups = [("温度<15%", temp < .15), ("温度<20%", temp < .20), ("VIX>=20", v >= 20), ("VIX>=25", v >= 25), ("VIX>=30", v >= 30), ("VIX>=35", v >= 35),
                  ("VIX 5日涨幅>=40%", v_chg5 >= .4), ("VIX 高于20日均值30%以上", v_ma >= .3),
                  ("温度<15% 且 VIX>=25", (temp < .15) & (v >= 25)), ("温度<15% 且 VIX<25", (temp < .15) & (v < 25)),
                  ("VIX>=25 且 温度>=15%", (v >= 25) & (temp >= .15)), ("VIX<20（平静）", v < 20)]
        print(f"{'条件':26s}{'期间':5s}{'天数':>5s}{'事件':>8s}{'胜率':>8s}{'同期基础':>9s}{'平均':>8s}")
        for lab, cond in groups:
            for per, pm in (("全部", P.dates >= "2018-01-01"), ("样本内", (P.dates >= "2018-01-01") & (P.dates < split)), ("样本外", P.dates >= split)):
                m = cond.fillna(False).to_numpy() & pm
                f = fwd.where(ev)[m]
                n = int(f.notna().sum().sum())
                if n < 30:
                    print(f"{lab:26s}{per:5s}  事件<30"); continue
                days = int(f.notna().any(axis=1).sum())
                w = float((f > 0).where(f.notna()).sum().sum() / n)
                bw = float(base_w[m][f.notna().any(axis=1).to_numpy()].mean())
                print(f"{lab:26s}{per:5s}{days:5d}{n:8d}{w:8.1%}{bw:9.1%}{np.nanmean(f.to_numpy()):+8.2%}")


main()
