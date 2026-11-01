"""用「真正会买的 ETF」验证恐慌日规则（不是指数）：A 股 510300 / 159915（沪深300ETF / 创业板ETF），QDII 513500 / 513100（标普500ETF / 纳指ETF，A 股账户买，有溢价）。
价格取 Yahoo（.SS / .SZ，不复权的真实成交价，含溢价 / 折价）。触发与软件一致：A 股市场温度<15%（2016-10 起）；美股 VIX>=25（用于 QDII）。
次日开盘买、持有 H 天收盘卖，成本 0.1%。对比同一 ETF「平时」买入。同时和对应的美股 ETF（SPY / QQQ）比较：差异 = 溢价变化 + 汇率 + 时差。
价格下载后缓存到 data/cache/ma_research/etf_*.csv。用法：python -m tools.panic_real_etf"""
from __future__ import annotations
import argparse, json, sys, warnings
from datetime import date
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from tools.panic_long_history import CACHE, GAP, ci, rd

ETFS = {"510300": ("沪深300ETF", "CN", "510300.SS"), "159915": ("创业板ETF", "CN", "159915.SZ"),
        "513500": ("标普500ETF（QDII）", "US", "513500.SS"), "513100": ("纳指ETF（QDII）", "US", "513100.SS")}
PAIR = {"513500": "SPY", "513100": "QQQ"}


def load(code):
    f = CACHE / f"etf_{code}.csv"
    if not f.exists():
        import yfinance as yf
        df = yf.download(ETFS[code][2], period="max", auto_adjust=False, progress=False, timeout=30)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = df.index.strftime("%Y-%m-%d"); df.to_csv(f)
    d = pd.read_csv(f, index_col=0).astype(float)
    return d[(d["Open"] > 0) & (d["Close"] > 0)]


def segs(f, trig):
    t = trig.reindex(f.index).fillna(False).to_numpy(); ok = f.notna().to_numpy()
    days = np.where(t & ok)[0]
    if len(days) == 0:
        return []
    sg = np.cumsum(np.r_[1, (np.diff(days) > GAP).astype(int)])
    return [days[sg == s] for s in np.unique(sg)]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--write", action="store_true"); a = ap.parse_args()
    out = {"generated": date.today().isoformat(), "etfs": {}}
    from server import db, settings
    with settings.market_ctx("CN"), db.market_db("CN") as conn:
        b20 = pd.read_sql_query("SELECT date, b20 FROM market_breadth ORDER BY date", conn).set_index("date")["b20"]
    vix = rd("VIX", "Close").astype(float); vix.index = pd.to_datetime(vix.index).strftime("%Y-%m-%d")
    for code, (name, mk, _) in ETFS.items():
        d = load(code)
        trig = (b20 < 0.15) if mk == "CN" else (vix >= 25)
        print(f"\n=== {code} {name}  数据 {d.index[0]} ~ {d.index[-1]}  触发={'温度<15%' if mk == 'CN' else 'VIX>=25'} ===")
        for H in (5, 20):
            f = d["Close"].shift(-H) / d["Open"].shift(-1) - 1 - 0.001
            tt = trig.reindex(f.index).fillna(False)
            if mk == "CN":
                tt = tt & (f.index >= "2016-11-01")
            ss = segs(f, tt)
            normal = f[(~tt) & f.notna() & (f.index >= ("2016-11-01" if mk == "CN" else d.index[0]))]
            if len(ss) < 5:
                print(f"  持有{H}天：只有 {len(ss)} 段"); continue
            seg_ret = np.array([float(f.iloc[ix].mean()) for ix in ss]); seg_first = np.array([float(f.iloc[ix[0]]) for ix in ss])
            seg_win = np.array([float((f.iloc[ix] > 0).mean()) for ix in ss])
            dm = seg_ret - float(normal.mean()); lo, hi = ci(dm)
            fl, fh = ci(seg_first - float(normal.mean()))
            line = (f"  持有{H:>2}天 {len(ss)}段  每段平均 {seg_ret.mean():+.2%} 胜率 {seg_win.mean():.0%} | 平时 {normal.mean():+.2%} {float((normal > 0).mean()):.0%} | 多出 {dm.mean():+.2%}（{lo:+.2%}~{hi:+.2%}）"
                    f" | 第一天买 {seg_first.mean():+.2%}（比平时多 {np.mean(seg_first - normal.mean()):+.2%}，{fl:+.2%}~{fh:+.2%}） | 最差段 {seg_ret.min():+.1%}")
            print(line)
            r = out["etfs"].setdefault(code, {"name": name, "market": mk, "from": d.index[0], "hold": {}})["hold"]
            r[str(H)] = {"segs": len(ss), "mean": round(float(seg_ret.mean()), 4), "win": round(float(seg_win.mean()), 3), "base_mean": round(float(normal.mean()), 4),
                         "base_win": round(float((normal > 0).mean()), 3), "diff": round(float(dm.mean()), 4), "lo": round(lo, 4), "hi": round(hi, 4),
                         "first_diff": round(float(np.mean(seg_first - normal.mean())), 4), "first_lo": round(fl, 4), "first_hi": round(fh, 4), "worst": round(float(seg_ret.min()), 4)}
            if code in PAIR:                                     # 和对应美股 ETF 同样的触发日比较
                u = rd(PAIR[code]).astype(float); u = u[u["Open"] > 0]; u.index = pd.to_datetime(u.index).strftime("%Y-%m-%d")
                fu = (u["Close"].shift(-H) / u["Open"].shift(-1) - 1 - 0.001).reindex(f.index)
                days = np.concatenate(ss)
                diff = (f.iloc[days] - fu.iloc[days]).dropna()
                r[str(H)]["vs_us"] = {"ticker": PAIR[code], "mean_gap": round(float(diff.mean()), 4), "std": round(float(diff.std()), 4), "worst": round(float(diff.min()), 4), "n": int(len(diff))}
                print(f"      对比 {PAIR[code]}（同一批触发日，每日差 = QDII ETF 涨幅 − {PAIR[code]} 涨幅）：平均差 {diff.mean():+.2%}，标准差 {diff.std():.2%}，最差 {diff.min():+.1%}，最好 {diff.max():+.1%}（{len(diff)} 天）")
    if a.write:
        (ROOT / "server" / "panic_real_etf_evidence.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("已写入 server/panic_real_etf_evidence.json")


if __name__ == "__main__":
    main()
