"""恐慌日：买「超跌股清单」和直接买指数 / ETF，到底谁更好？按「行情段」统计（相隔不超过 5 个交易日的恐慌日算同一段，每段一个结果，对段做自助法）。
触发：A 股 全市场温度 < 15%；美股 VIX 收盘 >= 25（与软件一致）。事件 = 站上 200 日线且 RSI14 < 30 的股票，信号日收盘后、次日开盘买、持有 H 个交易日收盘卖，扣成本。
对比对象：
  - 清单（前 20）：当天事件股里按 20 日平均成交额取前 20 只、等权（软件实际给出的清单）
  - 清单（全部）：当天所有事件股等权
  - 同期随便买：当天所有合格股票等权（基础）
  - 指数 / ETF：A 股沪深300、创业板指；美股 SPY、QQQ（次日开盘买，成本按 0.1% 往返）
用法：python -m tools.panic_vs_etf --market CN|US"""
from __future__ import annotations
import argparse, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from tools.signal_zoo import rsi

GAP = 5                       # 相隔多少个交易日以内算同一段
TOP_N = 20


def _boot(x: np.ndarray, rng, n=10000):
    m = np.array([x[rng.integers(0, len(x), len(x))].mean() for _ in range(n)])
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--market", default="CN"); a = ap.parse_args(); mk = a.market.upper()
    from server import db, panel, settings, us_index
    cost = 0.0025 if mk == "CN" else 0.001
    idx_syms = {"沪深300": "sh.000300", "创业板指": "sz.399006"} if mk == "CN" else {"标普500 SPY": "SPY", "纳指100 QQQ": "QQQ"}
    with settings.market_ctx(mk), db.market_db(mk) as conn:
        P = panel.load_panel(conn, "2017-01-01", None, only_l1=(mk == "CN"), float32=True, market=mk)
        vix = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol='^VIX' ORDER BY date", conn).set_index("date")["close"] if mk == "US" else None
        ib = {n: pd.read_sql_query("SELECT date, open, close FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(s,)).set_index("date").reindex(P.dates) for n, s in idx_syms.items()}
    o, c = P.adj["open"].astype("float64"), P.adj["close"].astype("float64")
    ret1 = c.pct_change()
    amt = P.amount.astype("float64") if mk == "CN" else P.raw["close"].astype("float64") * P.volume.astype("float64")
    liq = amt.rolling(20, min_periods=15).mean().rank(axis=1, pct=True) > 0.4
    open1 = o.shift(-1)
    bad = (ret1.abs() > (0.215 if mk == "CN" else 3.0)).astype(float).rolling(30, min_periods=1).max() > 0
    elig = liq & (P.raw["close"] >= (2 if mk == "CN" else 3)) & (P.status.shift(-1) > 0) & open1.notna() & ~bad
    if mk == "CN":
        elig &= (P.is_st == 0) & (open1 / c - 1 < 0.095)
    elig &= pd.Series(P.dates >= "2018-01-01", index=P.dates).to_numpy()[:, None]
    ma20 = c.rolling(20, min_periods=16).mean()
    if mk == "CN":
        trig = (((c > ma20) & c.notna()).sum(axis=1) / c.notna().sum(axis=1) < .15).to_numpy()
    else:
        mem = us_index.members(); sel = pd.Series(P.symbols, index=P.symbols).isin(mem).to_numpy()
        elig, c, o, open1, amt = elig.loc[:, sel], c.loc[:, sel], o.loc[:, sel], open1.loc[:, sel], amt.loc[:, sel]
        trig = (vix.reindex(P.dates).ffill() >= 25).fillna(False).to_numpy()
    trig = trig & (P.dates >= "2018-01-01")
    ev0 = (rsi(c, 14) < 30) & (c > c.rolling(200, min_periods=160).mean()) & elig
    amt20 = amt.rolling(20, min_periods=15).mean()
    topmask0 = amt20.where(ev0).rank(axis=1, ascending=False) <= TOP_N
    dates = P.dates[P.dates >= "2018-01-01"]; split = dates[int(len(dates) * .6)]
    rng = np.random.default_rng(11)
    print(f"{mk}  触发={'温度<15%' if mk == 'CN' else 'VIX>=25（标普500+纳指100）'}  样本内<{split}<=样本外  分段间隔>{GAP}个交易日")
    for H in (5, 10, 20):
        fwd = c.shift(-H) / open1 - 1 - cost
        ev = ev0 & fwd.notna(); top = topmask0 & ev
        series = {"清单（前20）": fwd.where(top), "清单（全部）": fwd.where(ev), "同期随便买": fwd.where(elig & fwd.notna())}
        ret = {k: v.mean(axis=1) for k, v in series.items()}
        win = {k: (v > 0).where(v.notna()).mean(axis=1) for k, v in series.items()}
        for n, d in ib.items():
            f = d["close"].shift(-H) / d["open"].shift(-1) - 1 - 0.001
            ret[n] = f; win[n] = (f > 0).astype(float).where(f.notna())
        days = np.where(trig & top.any(axis=1).to_numpy())[0]
        seg = np.cumsum(np.r_[1, (np.diff(days) > GAP).astype(int)])
        rows = []
        for sg in np.unique(seg):
            ix = days[seg == sg]
            r = {"start": P.dates[ix[0]], "days": len(ix)}
            for k in ret:
                r["r_" + k] = float(np.nanmean(ret[k].iloc[ix].to_numpy())); r["w_" + k] = float(np.nanmean(win[k].iloc[ix].to_numpy()))
            rows.append(r)
        df = pd.DataFrame(rows).dropna()
        print(f"\n===== 持有 {H} 个交易日：{len(df)} 段（恐慌日 {int(df.days.sum())} 天） =====")
        print(f"{'对象':14s}{'平均涨跌':>9s}{'胜率':>8s}{'段间波动':>9s}{'最差一段':>9s}")
        for k in ret:
            x = df["r_" + k].to_numpy()
            print(f"{k:14s}{x.mean():+9.2%}{df['w_' + k].mean():8.1%}{x.std():9.2%}{x.min():+9.1%}")
        print("  -- 清单（前20）减去对方，逐段配对（正 = 清单更好）：平均涨跌差（90% 范围）  清单更好的段数  样本内 / 样本外 平均差")
        for k in [x for x in ret if x != "清单（前20）"]:
            d = (df["r_清单（前20）"] - df["r_" + k]).to_numpy()
            lo, hi = _boot(d, rng)
            isd = d[df["start"].to_numpy() < split]; oos = d[df["start"].to_numpy() >= split]
            print(f"  vs {k:12s} {d.mean():+.2%}（{lo:+.2%} ~ {hi:+.2%}）  {int((d > 0).sum())}/{len(d)}  IS {isd.mean():+.2%}({len(isd)}段) / OOS {oos.mean():+.2%}({len(oos)}段)")


main()
