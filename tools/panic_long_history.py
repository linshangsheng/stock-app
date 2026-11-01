"""恐慌日规则放到长历史里检验（含 2000~2002、2008、2015 的大熊市 / 股灾）。数据用 tools/ma_research.py 已缓存的长历史（data/cache/ma_research，不联网）。
美股：触发 = VIX 收盘 >= 25（1990 起）；买标普500 / 纳斯达克100 价格指数，次日开盘买、持有 H 天收盘卖，成本 0.1%。
  2018 年以前是这条规则「校准之前」的数据（规则是用 2018~2026 的数据定的），相当于真正的样本外。
A 股：全市场温度只有 2016-10 起；更早用沪深300 自己的走势做替身（先在 2016-11 之后和真实温度对照、挑最贴的替身，再用到 2005~2016）。
  沪深300 长历史只有收盘价：次日收盘买、再持有 H 天（和「平时」用同样的方法比）。
都按「行情段」统计：相隔不超过 5 个交易日的恐慌日算一段，每段一个结果；和「平时」（非恐慌日）比，差的 90% 可信范围用对段做自助法。
用法：python -m tools.panic_long_history [--write]"""
from __future__ import annotations
import argparse, json, sys, warnings
from datetime import date
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd

CACHE = ROOT / "data" / "cache" / "ma_research"
GAP, HOLDS = 5, (5, 20)
rng = np.random.default_rng(3)


def rd(name, cols=None):
    df = pd.read_csv(CACHE / f"{name}.csv", index_col=0)
    return df if cols is None else df[cols]


def fwd(open_s, close_s, H, cost, entry):
    if entry == "open":
        return close_s.shift(-H) / open_s.shift(-1) - 1 - cost
    return close_s.shift(-(H + 1)) / close_s.shift(-1) - 1 - cost      # 只有收盘价：次日收盘买


def ci(x):
    m = np.array([x[rng.integers(0, len(x), len(x))].mean() for _ in range(8000)])
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def segments(f: pd.Series, trig: pd.Series):
    ok = f.notna().to_numpy()
    days = np.where(trig.reindex(f.index).fillna(False).to_numpy() & ok)[0]
    if len(days) == 0:
        return pd.DataFrame(columns=["start", "days", "ret", "win", "first"])
    seg = np.cumsum(np.r_[1, (np.diff(days) > GAP).astype(int)])
    rows = []
    for sg in np.unique(seg):
        ix = days[seg == sg]
        rows.append((f.index[ix[0]], len(ix), float(f.iloc[ix].mean()), float((f.iloc[ix] > 0).mean()), float(f.iloc[ix[0]])))
    return pd.DataFrame(rows, columns=["start", "days", "ret", "win", "first"])


def report(title, f_all: dict, trig, eras, out: dict, key):
    print(f"\n=== {title} ===")
    out[key] = {}
    for H, f in f_all.items():
        out[key][str(H)] = {}
        for lab, (a, b) in eras.items():
            idx = f.index[(f.index >= a) & (f.index <= b)]
            ff = f.reindex(idx); tt = trig.reindex(idx).fillna(False)
            seg = segments(ff, tt)
            normal = ff[(~tt) & ff.notna()]
            if len(seg) < 4 or len(normal) < 100:
                print(f"  持有{H:>2}天 {lab}: 段数 {len(seg)}（太少，不统计）"); continue
            dm = seg["ret"].to_numpy() - float(normal.mean())
            lo, hi = ci(dm)
            worst = seg.sort_values("ret").head(3)
            r = {"segs": int(len(seg)), "mean": round(float(seg.ret.mean()), 4), "win": round(float(seg.win.mean()), 3),
                 "base_mean": round(float(normal.mean()), 4), "base_win": round(float((normal > 0).mean()), 3),
                 "diff": round(float(dm.mean()), 4), "lo": round(lo, 4), "hi": round(hi, 4), "better": int((dm > 0).sum()),
                 "worst": round(float(seg.ret.min()), 4), "worst_start": str(seg.sort_values("ret").iloc[0]["start"]),
                 "worst3": [[str(w.start), round(float(w.ret), 4)] for w in worst.itertuples()]}
            fd = seg["first"].to_numpy() - float(normal.mean())
            flo, fhi = ci(fd)
            fw = seg.sort_values("first").head(4)
            r.update({"first_mean": round(float(seg["first"].mean()), 4), "first_win": round(float((seg["first"] > 0).mean()), 3),
                      "first_diff": round(float(fd.mean()), 4), "first_lo": round(flo, 4), "first_hi": round(fhi, 4),
                      "first_worst": [[str(w.start), round(float(w.first), 4)] for w in fw.itertuples()], "median_days": float(seg.days.median()), "max_days": int(seg.days.max())})
            out[key][str(H)][lab] = r
            print(f"    └ 每段第一天买：平均{r['first_mean']:+.2%} 胜率{r['first_win']:.0%} 比平时多 {r['first_diff']:+.2%}（{flo:+.2%}~{fhi:+.2%}）  最差4次 {r['first_worst']}  段长中位数 {r['median_days']:.0f} 天，最长 {r['max_days']} 天")
            print(f"  持有{H:>2}天 {lab:11s} {r['segs']:3d}段  恐慌日后 平均{r['mean']:+.2%} 胜率{r['win']:.0%} | 平时 {r['base_mean']:+.2%} {r['base_win']:.0%} | 多出 {r['diff']:+.2%}（{lo:+.2%}~{hi:+.2%}） 好于平时 {r['better']}/{r['segs']}  最差段 {r['worst']:+.1%}（{r['worst_start']}）  最差3段 {r['worst3']}")


def us(out):
    vix = rd("VIX", "Close").astype(float); vix.index = pd.to_datetime(vix.index).strftime("%Y-%m-%d")
    trig = vix >= 25
    eras = {"1990~2017": ("1990-01-01", "2017-12-31"), "2018~2026": ("2018-01-01", "2026-12-31"),
            "1990~1999": ("1990-01-01", "1999-12-31"), "2000~2009": ("2000-01-01", "2009-12-31"), "2010~2026": ("2010-01-01", "2026-12-31"), "全部": ("1990-01-01", "2026-12-31")}
    for name, idx in (("标普500（^GSPC）", "GSPC"), ("纳斯达克100（^NDX）", "NDX")):
        d = rd(idx).astype(float); d = d[d["Open"] > 0]; d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d")
        d = d[d.index >= "1990-01-01"]
        f = {H: fwd(d["Open"], d["Close"], H, 0.001, "open") for H in HOLDS}
        report(f"美股 {name}  VIX>=25", f, trig, eras, out, "US_" + idx)


def cn(out):
    from server import db, settings
    px = rd("HS300", "close").astype(float); px.index = pd.to_datetime(px.index).strftime("%Y-%m-%d")
    with settings.market_ctx("CN"), db.market_db("CN") as conn:
        b20 = pd.read_sql_query("SELECT date, b20 FROM market_breadth ORDER BY date", conn).set_index("date")["b20"]
    real = (b20 < 0.15)
    ma20 = px.rolling(20).mean()
    cands = {}
    for t in (0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10):
        cands[f"收盘低于 20 日线 {t:.0%}"] = (px / ma20 - 1) <= -t
    for t in (0.05, 0.07, 0.10):
        cands[f"10 日跌幅 ≥ {t:.0%}"] = (px / px.shift(10) - 1) <= -t
    for t in (0.10, 0.15, 0.20):
        cands[f"距 60 日高点回撤 ≥ {t:.0%}"] = (px / px.rolling(60).max() - 1) <= -t
    ref = real.reindex(px.index[px.index >= "2016-11-01"]).fillna(False)
    best, bf = None, -1
    print("\n--- A 股替身：2016-11 之后和真实「市场温度<15%」对照（日度 F1，越高越贴）---")
    for k, s in cands.items():
        s2 = s.reindex(ref.index).fillna(False)
        tp = float((s2 & ref).sum()); p = tp / max(s2.sum(), 1); r = tp / max(ref.sum(), 1); f1 = 2 * p * r / max(p + r, 1e-9)
        print(f"  {k:18s} 触发 {int(s2.sum()):4d} 天（真实 {int(ref.sum())} 天） 命中率 {p:.0%} 覆盖率 {r:.0%} F1 {f1:.2f}")
        if f1 > bf: best, bf = k, f1
    print(f"选用替身：{best}（F1 {bf:.2f}）")
    proxy = cands[best]
    out["CN_proxy"] = {"name": best, "f1": round(bf, 3)}
    eras = {"2005~2016（替身）": ("2005-06-01", "2016-10-31"), "2016-11~2026（替身）": ("2016-11-01", "2026-12-31"), "2016-11~2026（真实温度）": ("2016-11-01", "2026-12-31")}
    f = {H: fwd(px, px, H, 0.001, "close") for H in HOLDS}
    report(f"A 股 沪深300  替身={best}", f, proxy, {k: v for k, v in eras.items() if "真实" not in k}, out, "CN_hs300_proxy")
    report("A 股 沪深300  真实市场温度<15%（对照，同样用收盘价买）", f, real.reindex(px.index).fillna(False), {"2016-11~2026": ("2016-11-01", "2026-12-31")}, out, "CN_hs300_real")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--write", action="store_true"); a = ap.parse_args()
    out = {"generated": date.today().isoformat()}
    us(out); cn(out)
    if a.write:
        (ROOT / "server" / "panic_long_evidence.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n已写入 server/panic_long_evidence.json")


if __name__ == "__main__":
    main()
