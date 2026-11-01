"""恐慌日怎么买：第一次触发就买 vs 等恐慌持续几天再买 vs 分三批。长历史（美股 1990 起；A 股沪深300 2005 起，2016-11 前用替身、之后用真实温度）。
上一步（panic_long_history.py）发现：「按行情段平均」优势明显，但「每段第一天买」没有优势、且 2008 / 2015 / 2020 亏过 17%~31%——
恐慌持续一阵之后买，比第一个触发日买更好。这里把它量化成可执行的规则：
  A. 第 k 个恐慌日买（k=0 是第一个触发日；恐慌没持续到第 k 天的段不买）；
  B. 分三批：第 0 / 5 / 10 个恐慌日各买 1/3（没持续到的那批留作现金，收益记 0），每批持有 H 天；
都按「行情段」统计（每段一个结果），和「平时」（非恐慌日）比，差的 90% 可信范围对段做自助法。
用法：python -m tools.panic_staging [--write]"""
from __future__ import annotations
import argparse, json, sys, warnings
from datetime import date
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from tools.panic_long_history import GAP, ci, fwd, rd

KS = (0, 2, 5, 10, 15)
TRANCHES = (0, 5, 10)


def seg_days(trig: pd.Series, f: pd.Series):
    ok = f.notna().to_numpy()
    t = trig.reindex(f.index).fillna(False).to_numpy()
    days = np.where(t)[0]                               # 触发日（不要求有收益，段按触发日连续性划分）
    if len(days) == 0:
        return []
    seg = np.cumsum(np.r_[1, (np.diff(days) > GAP).astype(int)])
    return [days[seg == s] for s in np.unique(seg)], ok


def analyse(label, f_by_H: dict, trig: pd.Series, eras: dict, out: dict, key: str):
    print(f"\n=== {label} ===")
    out[key] = {}
    for H, f in f_by_H.items():
        out[key][str(H)] = {}
        for lab, (a, b) in eras.items():
            idx = f.index[(f.index >= a) & (f.index <= b)]
            ff = f.reindex(idx); tt = trig.reindex(idx).fillna(False)
            r = seg_days(tt, ff)
            if not r:
                continue
            segs, ok = r
            normal = ff[(~tt) & ff.notna()]
            base = float(normal.mean())
            rows = {}
            for k in KS:                                      # A. 第 k 个恐慌日买
                v = [float(ff.iloc[ix[k]]) for ix in segs if len(ix) > k and ok[ix[k]]]
                rows[f"第{k}天"] = np.array(v)
            for name, tr_set in (("分三批(0/5/10)", TRANCHES),):   # B. 分批：每批 1/3，没持续到的留现金
                v_inv, v_tot = [], []
                for ix in segs:
                    got = [float(ff.iloc[ix[k]]) for k in tr_set if len(ix) > k and ok[ix[k]]]
                    if got:
                        v_inv.append(float(np.mean(got))); v_tot.append(float(np.sum(got) / len(tr_set)))
                rows[name + "·已投部分"] = np.array(v_inv); rows[name + "·占总预算"] = np.array(v_tot)
            for N in (5, 10, 20):                              # C. 恐慌持续的每个交易日买 1/N，最多买 N 天（没持续满 N 天，剩下的留作现金）
                v_inv, v_tot = [], []
                for ix in segs:
                    got = [float(ff.iloc[k]) for k in ix[:N] if ok[k]]
                    if got:
                        v_inv.append(float(np.mean(got))); v_tot.append(float(np.sum(got) / N))
                rows[f"每天买(最多{N}天)·已投部分"] = np.array(v_inv); rows[f"每天买(最多{N}天)·占总预算"] = np.array(v_tot)
            print(f"  -- 持有{H}天 {lab}（平时平均 {base:+.2%}，共 {len(segs)} 段）")
            out[key][str(H)][lab] = {"segs": len(segs), "base": round(base, 4), "rows": {}}
            for name, v in rows.items():
                if len(v) < 5:
                    print(f"     {name:16s} 只有 {len(v)} 段，不统计"); continue
                d = v - (base if "总预算" not in name else base * 1.0)   # 占总预算的收益里现金部分记 0，对比仍用平时平均（保守地偏向基准）
                lo, hi = ci(d)
                res = {"n": int(len(v)), "mean": round(float(v.mean()), 4), "win": round(float((v > 0).mean()), 3), "diff": round(float(d.mean()), 4),
                       "lo": round(lo, 4), "hi": round(hi, 4), "worst": round(float(v.min()), 4), "p10": round(float(np.percentile(v, 10)), 4)}
                out[key][str(H)][lab]["rows"][name] = res
                print(f"     {name:16s} {res['n']:3d}段  平均{res['mean']:+.2%} 胜率{res['win']:.0%} 比平时多 {res['diff']:+.2%}（{lo:+.2%}~{hi:+.2%}）  最差{res['worst']:+.1%}  最差10%分位 {res['p10']:+.1%}")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--write", action="store_true"); a = ap.parse_args()
    out = {"generated": date.today().isoformat()}
    vix = rd("VIX", "Close").astype(float); vix.index = pd.to_datetime(vix.index).strftime("%Y-%m-%d")
    eras_us = {"1990~2017": ("1990-01-01", "2017-12-31"), "2018~2026": ("2018-01-01", "2026-12-31"), "全部": ("1990-01-01", "2026-12-31")}
    for name, idx in (("标普500（^GSPC）", "GSPC"), ("纳斯达克100（^NDX）", "NDX")):
        d = rd(idx).astype(float); d = d[d["Open"] > 0]; d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d"); d = d[d.index >= "1990-01-01"]
        f = {H: fwd(d["Open"], d["Close"], H, 0.001, "open") for H in (5, 20)}
        analyse(f"美股 {name}  VIX>=25", f, vix >= 25, eras_us, out, "US_" + idx)
    from server import db, settings
    px = rd("HS300", "close").astype(float); px.index = pd.to_datetime(px.index).strftime("%Y-%m-%d")
    with settings.market_ctx("CN"), db.market_db("CN") as conn:
        b20 = pd.read_sql_query("SELECT date, b20 FROM market_breadth ORDER BY date", conn).set_index("date")["b20"]
    proxy = ((px / px.rolling(20).mean() - 1) <= -0.04)
    real = (b20 < 0.15).reindex(px.index).fillna(False)
    trig = proxy.copy(); trig[trig.index >= "2016-11-01"] = real[real.index >= "2016-11-01"]
    f = {H: fwd(px, px, H, 0.001, "close") for H in (5, 20)}
    analyse("A 股 沪深300（2016-11 前=收盘低于 20 日线 4% 的替身；之后=真实市场温度<15%；次日收盘买）", f, trig,
            {"2005~2016(替身)": ("2005-06-01", "2016-10-31"), "2016-11~2026(真实温度)": ("2016-11-01", "2026-12-31")}, out, "CN_hs300")
    if a.write:
        (ROOT / "server" / "panic_staging_evidence.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n已写入 server/panic_staging_evidence.json")


if __name__ == "__main__":
    main()
