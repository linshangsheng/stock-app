"""恐慌日之后直接买宽基指数 / ETF：和「平时」相比到底多了多少？按「行情段」统计（相隔不超过 5 个交易日的恐慌日算同一段，每段一个结果）。
触发与软件一致：A 股 全市场温度（站上 20 日线的比例）< 15%；美股 VIX 收盘 >= 25。信号日收盘后、次日开盘买、持有 H 个交易日收盘卖，成本按 0.1% 往返。
A 股对比 沪深300、创业板指；美股对比 SPY、QQQ。结果写入 server/panic_etf_evidence.json（软件「恐慌日」栏读它），同时打印。
用法：python -m tools.panic_etf_baseline [--write]"""
from __future__ import annotations
import argparse, json, sys, warnings
from datetime import date
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd

GAP, COST, START = 5, 0.001, "2018-01-01"
HOLDS = (5, 10, 20)
TARGETS = {"CN": [("沪深300", "sh.000300", "hs300"), ("创业板指", "sz.399006", "cyb")],
           "US": [("标普500（SPY）", "SPY", "spx"), ("纳斯达克100（QQQ）", "QQQ", "ndx")]}


def _ci(x: np.ndarray, rng, n=10000):
    m = np.array([x[rng.integers(0, len(x), len(x))].mean() for _ in range(n)])
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def analyse(mk: str) -> dict:
    from server import db, settings
    with settings.market_ctx(mk), db.market_db(mk) as conn:
        if mk == "CN":
            b = pd.read_sql_query("SELECT date, b20 FROM market_breadth ORDER BY date", conn).set_index("date")["b20"]
            sig = b < 0.15
        else:
            v = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol='^VIX' ORDER BY date", conn).set_index("date")["close"]
            sig = v >= 25
        bars = {n: pd.read_sql_query("SELECT date, open, close FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(s,)).set_index("date") for n, s, _ in TARGETS[mk]}
    cfg = {e["key"]: e.get("etf", "") for e in settings.cfg()["key_ma"]["indices"]}
    rng = np.random.default_rng(5)
    out = {"market": mk, "etfs": []}
    for name, sym, key in TARGETS[mk]:
        d = bars[name].copy()
        idx = d.index[d.index >= START]
        s = sig.reindex(d.index).fillna(False)
        res = {"name": name, "symbol": sym, "etf": cfg.get(key, ""), "hold": {}}
        for H in HOLDS:
            f = (d["close"].shift(-H) / d["open"].shift(-1) - 1 - COST)
            f = f.reindex(idx); st = s.reindex(idx)
            ok = f.notna()
            tdays = np.where(st.to_numpy() & ok.to_numpy())[0]
            if len(tdays) < 10:
                continue
            seg = np.cumsum(np.r_[1, (np.diff(tdays) > GAP).astype(int)])
            rows = []
            for sg in np.unique(seg):
                ix = tdays[seg == sg]
                rows.append((idx[ix[0]], len(ix), float(f.iloc[ix].mean()), float((f.iloc[ix] > 0).mean())))
            df = pd.DataFrame(rows, columns=["start", "days", "ret", "win"])
            normal = f[(~st) & ok]
            base_mean, base_win = float(normal.mean()), float((normal > 0).mean())
            dm = df["ret"].to_numpy() - base_mean
            dw = df["win"].to_numpy() - base_win
            sp = idx[int(len(idx) * .6)]
            isr = df[df["start"] < sp]; oos = df[df["start"] >= sp]
            lo, hi = _ci(dm, rng); wlo, whi = _ci(dw, rng)
            res["hold"][str(H)] = {
                "segs": int(len(df)), "days": int(df.days.sum()), "mean": round(float(df.ret.mean()), 4), "win": round(float(df.win.mean()), 3),
                "worst": round(float(df.ret.min()), 4), "std": round(float(df.ret.std(ddof=0)), 4),
                "base_mean": round(base_mean, 4), "base_win": round(base_win, 3),
                "diff_mean": round(float(dm.mean()), 4), "diff_lo": round(lo, 4), "diff_hi": round(hi, 4),
                "diff_win": round(float(dw.mean()), 3), "win_lo": round(wlo, 3), "win_hi": round(whi, 3),
                "better": int((dm > 0).sum()),
                "is_mean": round(float(isr.ret.mean()), 4) if len(isr) else None, "is_segs": int(len(isr)),
                "oos_mean": round(float(oos.ret.mean()), 4) if len(oos) else None, "oos_segs": int(len(oos)),
            }
        out["etfs"].append(res)
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--write", action="store_true"); a = ap.parse_args()
    ev = {"generated": date.today().isoformat(),
          "rule": "触发：A 股全市场温度<15%；美股 VIX>=25。次日开盘买、持有 H 天收盘卖，成本 0.1%。相隔不超过 5 个交易日的恐慌日算同一段，每段一个结果（段内各日等权）；"
                  "「平时」= 非恐慌日的所有交易日；差 = 每段结果 − 平时平均，可信范围为对段做自助法的 90% 范围。2018 年起。", "markets": {}}
    for mk in ("CN", "US"):
        r = analyse(mk); ev["markets"][mk] = r["etfs"]
        print(f"\n===== {mk} =====")
        for e in r["etfs"]:
            print(f"-- {e['name']}  {e['etf']}")
            for H, x in e["hold"].items():
                print(f"   持有{H:>2}天 {x['segs']}段  恐慌日后 平均{x['mean']:+.2%} 胜率{x['win']:.0%} 最差段{x['worst']:+.1%}  | 平时 平均{x['base_mean']:+.2%} 胜率{x['base_win']:.0%}"
                      f"  | 多出 {x['diff_mean']:+.2%}（{x['diff_lo']:+.2%}~{x['diff_hi']:+.2%}）胜率多 {x['diff_win']:+.0%}（{x['win_lo']:+.0%}~{x['win_hi']:+.0%}） 好于平时 {x['better']}/{x['segs']}段"
                      f"  IS {x['is_mean']:+.2%}({x['is_segs']}) OOS {x['oos_mean']:+.2%}({x['oos_segs']})")
    if a.write:
        (ROOT / "server" / "panic_etf_evidence.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n已写入 server/panic_etf_evidence.json")


main()
