"""信号动物园：一次性检验一批「日线信号」——收盘出信号，次日开盘买，持有 h 天（收盘卖）的胜率与净收益。
对照 = 同一天、同一流动性门槛内的全部股票（基础胜率）；超额 = 信号 - 对照（按交易日聚合后求 t 值，避免同一天大量股票被当成独立样本）。
样本内 = 前 60% 的日期，样本外 = 后 40%；另按「市场多空」（等权指数是否在 200 日线上）拆分。
A 股剔除：次日开盘涨停买不到、停牌；成本按往返 0.25%（A 股）/ 0.10%（美股）。
用法：python tools/signal_zoo.py --market CN|US [--start 2018-01-01] [--out data/signal_zoo_CN.json]"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

HOLDS = (1, 3, 5, 10)


def rsi(c: pd.DataFrame, n: int) -> pd.DataFrame:
    d = c.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    a = 1.0 / n
    ru, rd = up.ewm(alpha=a, adjust=False, min_periods=n).mean(), dn.ewm(alpha=a, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


def xrank(df: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    return df.where(mask).rank(axis=1, pct=True)


def build_signals(P, elig: pd.DataFrame) -> dict[str, pd.DataFrame]:
    o, h, l, c = (P.adj[k].astype("float64") for k in ("open", "high", "low", "close"))
    v = P.volume.astype("float64")
    ret1 = c.pct_change()
    ma = {n: c.rolling(n, min_periods=int(n * .8)).mean() for n in (5, 10, 20, 50, 200)}
    sd20 = c.rolling(20, min_periods=16).std()
    ibs = (c - l) / (h - l).replace(0, np.nan)
    r2, r3, r14 = rsi(c, 2), rsi(c, 3), rsi(c, 14)
    hh252, ll252 = c.rolling(252, min_periods=200).max(), c.rolling(252, min_periods=200).min()
    atr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()]).groupby(level=0).max() if False else \
        np.maximum(h - l, np.maximum((h - c.shift()).abs(), (l - c.shift()).abs()))
    atr_pct = atr.rolling(14, min_periods=10).mean() / c
    vol20 = ret1.rolling(20, min_periods=16).std()
    volr = v / v.rolling(20, min_periods=16).mean()
    down3 = (ret1 < 0) & (ret1.shift() < 0) & (ret1.shift(2) < 0)
    up_t = c > ma[200]
    mom12_1 = c.shift(21) / c.shift(252) - 1
    mom60 = c / c.shift(60) - 1
    z20 = (c - ma[20]) / sd20
    ret5 = c / c.shift(5) - 1
    gap = o / c.shift() - 1
    S: dict[str, pd.DataFrame] = {}
    # 反转类
    S["RSI2<10"] = r2 < 10
    S["RSI2<5"] = r2 < 5
    S["RSI3<15"] = r3 < 15
    S["RSI2<10 且 站上200日线"] = (r2 < 10) & up_t
    S["RSI2<5 且 站上200日线"] = (r2 < 5) & up_t
    S["IBS<0.1（收在当日最低附近）"] = ibs < 0.1
    S["IBS<0.1 且 站上200日线"] = (ibs < 0.1) & up_t
    S["连跌3天"] = down3
    S["连跌3天 且 站上200日线"] = down3 & up_t
    S["收盘跌破布林下轨(20,2)"] = z20 < -2
    S["跌破布林下轨 且 站上200日线"] = (z20 < -2) & up_t
    S["5日跌幅>10%"] = ret5 < -0.10
    S["5日跌幅>10% 且 站上200日线"] = (ret5 < -0.10) & up_t
    S["当日跳空低开>2%"] = gap < -0.02
    S["距MA20 低于-3ATR 且 站上200日线"] = ((c - ma[20]) / (atr_pct * c) < -3) & up_t
    S["RSI14<30 且 站上200日线"] = (r14 < 30) & up_t
    S["RSI14<30 且 60日动量前30%"] = (r14 < 30) & (xrank(mom60, elig) > 0.7)
    S["回踩MA50(±1.5%) 且 MA50>MA200 且 RSI14<45"] = ((c / ma[50] - 1).abs() < .015) & (ma[50] > ma[200]) & (r14 < 45)
    S["缩量回调：近3日跌且量比<0.7 且 站上200日线"] = (c < c.shift(3)) & (volr < 0.7) & up_t
    # 动量 / 趋势类
    S["创20日新高"] = c >= c.rolling(20, min_periods=16).max()
    S["创52周新高"] = c >= hh252
    S["距52周高点<3%"] = c / hh252 > 0.97
    S["12-1动量前10%"] = xrank(mom12_1, elig) > 0.9
    S["60日动量前10%"] = xrank(mom60, elig) > 0.9
    S["放量大涨>5%且量比>2"] = (ret1 > 0.05) & (volr > 2)
    S["三线多头(MA5>10>20>50)"] = (ma[5] > ma[10]) & (ma[10] > ma[20]) & (ma[20] > ma[50])
    # 风险 / 质量类（横截面）
    S["低波动前20%"] = xrank(vol20, elig) < 0.2
    S["高波动前20%"] = xrank(vol20, elig) > 0.8
    S["低波动前20% 且 RSI2<20"] = (xrank(vol20, elig) < 0.2) & (r2 < 20)
    S["低换手(量比<0.5)"] = volr < 0.5
    S["地量(量比<0.4) 且 站上200日线"] = (volr < 0.4) & up_t
    # 复合：趋势里的急跌，且是强势股、低波动
    S["复合：站上200日线+RSI2<10+60日动量前50%"] = (r2 < 10) & up_t & (xrank(mom60, elig) > 0.5)
    S["复合：站上200日线+IBS<0.2+连跌2天+低波动前50%"] = (ibs < .2) & (ret1 < 0) & (ret1.shift() < 0) & up_t & (xrank(vol20, elig) < 0.5)
    S["复合：RSI2<10+站上200日线+次日不跳空(IBS<0.1)"] = (r2 < 10) & up_t & (ibs < 0.1)
    return {k: s.fillna(False) & elig for k, s in S.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="CN")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--load-from", default="2017-01-01")
    ap.add_argument("--out", default=None)
    ap.add_argument("--min-events", type=int, default=3000)
    a = ap.parse_args()
    from server import db, panel, settings
    cost = 0.0025 if a.market == "CN" else 0.001
    t0 = time.time()
    with settings.market_ctx(a.market), db.market_db(a.market) as conn:
        P = panel.load_panel(conn, a.load_from, None, only_l1=(a.market == "CN"), float32=True, market=a.market)
    print(f"{a.market}: {len(P.symbols)} 只 × {len(P.dates)} 天，加载 {time.time()-t0:.0f}s", flush=True)
    o, c = P.adj["open"].astype("float64"), P.adj["close"].astype("float64")
    amt = P.amount.astype("float64") if a.market == "CN" else (P.raw["close"].astype("float64") * P.volume.astype("float64"))
    liq = amt.rolling(20, min_periods=15).mean()
    liq_ok = liq.rank(axis=1, pct=True) > 0.4                      # 剔除成交最清淡的 40%
    px_ok = P.raw["close"].astype("float64") >= (2 if a.market == "CN" else 3)
    tradable_T1 = (P.status.shift(-1) > 0)
    open_T1, close_T = o.shift(-1), c
    gap_up = open_T1 / c - 1
    lim = 0.095 if a.market == "CN" else 9
    elig = liq_ok & px_ok & tradable_T1 & (P.is_st == 0) & (gap_up < lim) & c.notna() & open_T1.notna()
    elig &= pd.Series(P.dates >= a.start, index=P.dates).to_numpy()[:, None]
    # 前向收益：次日开盘买，第 h 天收盘卖（h=1 即次日开盘买、次日收盘卖）
    fwd = {h: (c.shift(-h) / open_T1 - 1 - cost) for h in HOLDS}
    # 市场多空：等权指数是否在 200 日线上
    mk = c.pct_change().where(P.status > 0).mean(axis=1).fillna(0)
    mk_idx = (1 + mk).cumprod()
    bull = (mk_idx > mk_idx.rolling(200, min_periods=150).mean())
    dates = P.dates[P.dates >= a.start]
    split = dates[int(len(dates) * 0.6)]
    ctx = {"IS": (P.dates >= a.start) & (P.dates < split), "OOS": P.dates >= split, "牛": bull.to_numpy(), "熊": ~bull.to_numpy()}
    print(f"前 60% 样本内 < {split} <= 样本外；牛市日占比 {bull[dates].mean():.0%}", flush=True)
    sigs = build_signals(P, elig)
    # 对照（基础）：按日聚合均值
    base = {}
    for h in HOLDS:
        f = fwd[h].where(elig)
        base[h] = {"mean": f.mean(axis=1), "win": (f > 0).where(f.notna()).mean(axis=1)}

    def stat(sig: pd.DataFrame, h: int, mask: np.ndarray):
        f = fwd[h].where(sig)
        n_day = f.notna().sum(axis=1)
        m = (n_day > 0).to_numpy() & mask
        if m.sum() < 30:
            return None
        mean_d, win_d = f.mean(axis=1)[m], (f > 0).where(f.notna()).mean(axis=1)[m]
        ex = mean_d - base[h]["mean"][m]
        exw = win_d - base[h]["win"][m]
        n = int(n_day[m].sum())
        w = n_day[m] / n_day[m].sum()
        return {"n": n, "days": int(m.sum()), "win": float((win_d * w).sum()), "mean": float((mean_d * w).sum()),
                "base_win": float(base[h]["win"][m].mean()), "base_mean": float(base[h]["mean"][m].mean()),
                "ex_win": float(exw.mean()), "t": float(ex.mean() / (ex.std() / np.sqrt(len(ex)) + 1e-12) / np.sqrt(h) ** 0.5)}

    rows = []
    allm = np.ones(len(P.dates), dtype=bool)
    for name, sig in sigs.items():
        if int(sig.sum().sum()) < a.min_events:
            continue
        r = {"signal": name}
        for h in HOLDS:
            r[f"h{h}"] = {"ALL": stat(sig, h, allm), **{k: stat(sig, h, m) for k, m in ctx.items()}}
        rows.append(r)
    out = a.out or f"data/signal_zoo_{a.market}.json"
    Path(out).write_text(json.dumps({"market": a.market, "split": split, "cost": cost, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    hb = 5
    print(f"\n持有 {hb} 天（次日开盘买、第{hb}天收盘卖，扣成本）；win=胜率 ex=相对基础胜率的超额（百分点）；样本外要与样本内同方向才算数")
    print(f"{'信号':46s} {'次数':>7s} | 全部 胜率  超额  均值  | 样本内 胜率/均值 | 样本外 胜率/均值  t | 牛  胜率 | 熊  胜率")
    for r in sorted(rows, key=lambda r: -(r[f'h{hb}']['OOS'] or {'ex_win': -1})['ex_win']):
        s = r[f"h{hb}"]
        if not s["ALL"]:
            continue
        g = lambda k, f: (f"{s[k][f]:.1%}" if s[k] else "  -  ")  # noqa: E731
        print(f"{r['signal']:46s} {s['ALL']['n']:7d} | {s['ALL']['win']:.1%} {s['ALL']['ex_win']*100:+5.1f} {s['ALL']['mean']:+6.2%} |"
              f" {g('IS','win')}/{g('IS','mean')} | {g('OOS','win')}/{g('OOS','mean')} {s['OOS']['t'] if s['OOS'] else 0:+5.1f} | {g('牛','win')} | {g('熊','win')}")
    print(f"\n基础（全部合格股票）持有{hb}天胜率 {base[hb]['win'][dates].mean():.1%}，均值 {base[hb]['mean'][dates].mean():+.2%}；用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
