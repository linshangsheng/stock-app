"""关键均线研究：创业板指 / 标普500 / 纳斯达克100，每条常用均线（20 / 60 / 120 / 200 / 250 日）单独当作择时规则检验，
再看「距年线（250 日均线）多远」之后的涨跌。结果写入 server/key_ma_evidence.json，供「行情」页的关键均线面板引用。

规则（事先写好，不调参）：收盘高于均线（或高于均线 ×1.02，滞回版）→ 第二天起持有；收盘低于均线（或低于 ×0.98）→ 第二天起空仓。
执行：T 日收盘出信号，T+1 收盘按新仓位计收益（比次日开盘保守）；每次换仓扣成本（A 股 0.1%、美股 0.05%）；空仓拿利息
（美股按 13 周国债利率 ^IRX，A 股按 2%/年）。持有期计股息：美股 1993 年后用 SPY / QQQ 复权收益，之前用指数价格 + 近似股息。
样本内 = 前 60% 的日期，样本外 = 后 40%。
「可操作」判定（事先定好）：样本内、样本外都满足 Sharpe ≥ 一直持有，且最大回撤比一直持有浅 10 个百分点以上。

用法：python tools/ma_research.py        （首次会从 yfinance / BaoStock 拉长历史到 data/cache/ma_research/，之后复用）
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
sys.path.insert(0, str(ROOT))
CACHE = ROOT / "data" / "cache" / "ma_research"
OUT = ROOT / "server" / "key_ma_evidence.json"
MAS = (20, 60, 120, 200, 250)
BANDS = (0.0, 0.02)
ZONES = ((-1.0, -0.20), (-0.20, -0.10), (-0.10, 0.0), (0.0, 0.10), (0.10, 0.20), (0.20, 9.0))


def fetch():
    CACHE.mkdir(parents=True, exist_ok=True)
    import yfinance as yf
    for t in ("^GSPC", "^NDX", "^IRX", "SPY", "QQQ"):
        f = CACHE / f"{t.replace('^', '')}.csv"
        if f.exists():
            continue
        df = yf.download(t, period="max", auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = df.index.strftime("%Y-%m-%d")
        df.to_csv(f)
        print("下载", t, df.index[0], df.index[-1], len(df), flush=True)
    f = CACHE / "CYB.csv"
    if not f.exists():
        import baostock as bs
        bs.login()
        rs = bs.query_history_k_data_plus("sz.399006", "date,close", start_date="2010-06-01", end_date=date.today().isoformat(), frequency="d")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        pd.DataFrame(rows, columns=["date", "close"]).set_index("date").astype(float).to_csv(f)
        print("下载 sz.399006", rows[0][0], rows[-1][0], len(rows), flush=True)


def rd(n, col="Close"):
    return pd.read_csv(CACHE / f"{n}.csv", index_col=0)[col].astype(float).dropna()


def load():
    """返回 {key: (信号用的指数价格, 持有期总收益日收益率, 空仓利息日收益率, 换仓成本)}。"""
    irx = rd("IRX")
    cash_us = (1 + irx / 100) ** (1 / 252) - 1
    out = {}
    for key, idx, etf, start, div_pre in (("spx", "GSPC", "SPY", "1950-01-01", 0.03), ("ndx", "NDX", "QQQ", "1985-10-01", 0.008)):
        px = rd(idx)
        px = px[px.index >= start]
        tr_etf = rd(etf, "Adj Close").pct_change()
        r = px.pct_change().fillna(0) + div_pre / 252
        r[tr_etf.index[1:]] = tr_etf.iloc[1:].reindex(r.index).dropna()   # 有 ETF 之后用 ETF 复权收益（含股息、扣管理费）
        r = r.reindex(px.index).fillna(0)
        out[key] = (px, r, cash_us.reindex(px.index).ffill().fillna(0), 0.0005)
    px = rd("CYB", "close")
    out["cyb"] = (px, px.pct_change().fillna(0), pd.Series(1.02 ** (1 / 252) - 1, index=px.index), 0.001)
    return out


def signal(px: pd.Series, n: int, band: float) -> pd.Series:
    ma = px.rolling(n).mean()
    s = pd.Series(np.nan, index=px.index)
    s[px > ma * (1 + band)] = 1.0
    s[px < ma * (1 - band)] = 0.0
    return s.ffill().fillna(0.0)


def daily(w: pd.Series, r: pd.Series, cash: pd.Series, cost: float) -> pd.Series:
    pos = w.shift(1).fillna(0.0)
    return pos * r + (1 - pos) * cash - pos.diff().abs().fillna(0.0) * cost


def stats(d: pd.Series) -> dict:
    eq = (1 + d).cumprod()
    yrs = len(d) / 252
    sd = d.std()
    return {"cagr": round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4), "mdd": round(float((eq / eq.cummax() - 1).min()), 4),
            "sharpe": round(float(d.mean() / sd * np.sqrt(252)), 2) if sd > 0 else None,
            "worst_year": round(float(((1 + d).groupby(d.index.str[:4]).prod() - 1).min()), 4)}


def segs(d: pd.Series, split: str) -> dict:
    return {"all": stats(d), "is": stats(d[d.index < split]), "oos": stats(d[d.index >= split])}


def zones(px: pd.Series) -> list[dict]:
    """距年线（250 日均线）的乖离分区：之后 60 / 120 个交易日指数的涨跌（价格，不含股息）。episodes = 进入该区的独立次数。"""
    ma = px.rolling(250).mean()
    dev = px / ma - 1
    out = []
    for lo, hi in ZONES:
        m = (dev > lo) & (dev <= hi)
        row = {"lo": lo, "hi": hi, "days": int(m.sum()), "episodes": int((m & ~m.shift(1, fill_value=False)).sum())}
        for h in (60, 120):
            f = (px.shift(-h) / px - 1)[m].dropna()
            row[f"fwd{h}"] = {"n": int(len(f)), "mean": round(float(f.mean()), 4) if len(f) else None,
                              "median": round(float(f.median()), 4) if len(f) else None,
                              "win": round(float((f > 0).mean()), 3) if len(f) else None}
        out.append(row)
    allf = {h: (px.shift(-h) / px - 1).dropna() for h in (60, 120)}
    return out, {f"fwd{h}": {"mean": round(float(v.mean()), 4), "win": round(float((v > 0).mean()), 3)} for h, v in allf.items()}


def main():
    fetch()
    data = load()
    names = {"cyb": "创业板指", "spx": "标普500", "ndx": "纳斯达克100"}
    res = {"generated": date.today().isoformat(), "rule": __doc__.split("\n\n")[1], "indices": {}}
    for key, (px, r, cash, cost) in data.items():
        warm = 250
        idx = px.index[warm:]
        split = idx[int(len(idx) * 0.6)]
        bh = daily(pd.Series(1.0, index=px.index), r, cash, cost).loc[idx]
        B = segs(bh, split)
        print(f"\n【{names[key]}】{idx[0]} ~ {idx[-1]}，样本内到 {split}")
        print(f"  一直持有            全程 {B['all']['cagr']:+.1%} / {B['all']['mdd']:.0%} / {B['all']['sharpe']} | 样本内 {B['is']['cagr']:+.1%} / {B['is']['mdd']:.0%} / {B['is']['sharpe']} | 样本外 {B['oos']['cagr']:+.1%} / {B['oos']['mdd']:.0%} / {B['oos']['sharpe']}")
        mas = {}
        for n in MAS:
            for band in BANDS:
                w = signal(px, n, band)
                S = segs(daily(w, r, cash, cost).loc[idx], split)
                sw = float((w.diff().abs() > 0).loc[idx].sum() / (len(idx) / 252))
                ok = all((S[k]["sharpe"] or 0) >= (B[k]["sharpe"] or 0) and S[k]["mdd"] >= B[k]["mdd"] + 0.10 for k in ("is", "oos"))
                mas[f"{n}_{int(band * 100)}"] = {"n": n, "band": band, **S, "switches_per_year": round(sw, 1), "actionable": ok}
                print(f"  {n:3d}日均线{'±2%' if band else '    '}  全程 {S['all']['cagr']:+.1%} / {S['all']['mdd']:.0%} / {S['all']['sharpe']} | "
                      f"样本内 {S['is']['cagr']:+.1%} / {S['is']['mdd']:.0%} / {S['is']['sharpe']} | 样本外 {S['oos']['cagr']:+.1%} / {S['oos']['mdd']:.0%} / {S['oos']['sharpe']} | "
                      f"换仓 {sw:4.1f}/年 {'✓ 可操作' if ok else ''}", flush=True)
        ok = [v for v in mas.values() if v["actionable"]]
        primary = max(ok, key=lambda v: (v["is"]["sharpe"] or 0)) if ok else None     # 只用样本内选主线
        zs, uncond = zones(px)
        print(f"  主线（可操作里样本内 Sharpe 最高）：{primary and str(primary['n']) + '日' + ('±2%' if primary['band'] else '')}")
        print("  距年线分区 → 之后 120 日：" + "  ".join(
            f"[{z['lo']:+.0%},{z['hi']:+.0%}] 均值 {z['fwd120']['mean']:+.1%} 上涨 {z['fwd120']['win']:.0%} (天数 {z['days']}, 次数 {z['episodes']})"
            for z in zs if z['fwd120']['n']) + f"  | 全部 {uncond['fwd120']['mean']:+.1%} / {uncond['fwd120']['win']:.0%}")
        res["indices"][key] = {"name": names[key], "from": idx[0], "to": idx[-1], "split": split, "hold": B, "mas": mas,
                               "primary": None if not primary else {"n": primary["n"], "band": primary["band"]},
                               "zones": zs, "unconditional": uncond}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写入", OUT)


if __name__ == "__main__":
    main()
