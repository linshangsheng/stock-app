"""低风险多因子组合：事先写好的 8 个方案（综合分 2 种 × 持有 10/20 只 × 每 10/20 个交易日调仓），全市场回测。
样本内 2018~2022 选方案，样本外 2023~ 检验；对照：交易池等权、沪深300、中证500。
用法：python tools/factor_research.py [--equity 1000000] [--start 2018-01-01] [--split 2023-01-01]"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=1_000_000)
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--split", default="2023-01-01")
    ap.add_argument("--load-from", default="2017-06-01")
    ap.add_argument("--out", default="data/factor_research.json")
    a = ap.parse_args()
    import pandas as pd
    from server import db, factor_portfolio as fp, settings

    t0 = time.time()
    with settings.market_ctx("CN"), db.market_db("CN") as c:
        D = fp.Data(c, a.load_from, methods=["lowrisk", "lowrisk_rev"])      # 按年分块加载：峰值约 2 GB（一次性加载约 8.5 GB）
        idx = {s: pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol=? ORDER BY date", c, params=(s,)).set_index("date")["close"]
               for s in ("sh.000300", "sh.000905")}
    print(f"全市场 {len(D.syms)} 只 × {len(D.dates)} 天，准备 {time.time() - t0:.0f}s；L2 日均 {int(D.l2a.sum(1).mean())} 只", flush=True)
    end = D.dates[-1]
    prev_split = D.dates[max(i for i, d in enumerate(D.dates) if d < a.split)]
    parts = (("IS", a.start, prev_split), ("OOS", a.split, end))

    def fmt(m):
        return f"{m['cagr']:+6.1%} / {m['mdd']:6.1%} / {m['sharpe']:5.2f}"

    rows = []
    print(f"\n{'方案':34s} | 样本内 年化 / 回撤 / Sharpe | 样本外 年化 / 回撤 / Sharpe | 年换手  平均只数", flush=True)
    for label, (s, e) in (("对照：交易池等权（不计成本）", (None, None)),):
        ms = [fp.metrics(fp.ew_benchmark(D, ps, pe)) for _, ps, pe in parts]
        print(f"{label:34s} | {fmt(ms[0])}      | {fmt(ms[1])}", flush=True)
        rows.append({"variant": label, "is": ms[0], "oos": ms[1]})
    for sym, nm in (("sh.000300", "对照：沪深300"), ("sh.000905", "对照：中证500")):
        ms = [fp.metrics(idx[sym][(idx[sym].index >= ps) & (idx[sym].index <= pe)]) for _, ps, pe in parts]
        print(f"{nm:34s} | {fmt(ms[0])}      | {fmt(ms[1])}", flush=True)
        rows.append({"variant": nm, "is": ms[0], "oos": ms[1]})
    for score, n, k in itertools.product(("lowrisk", "lowrisk_rev"), (10, 20), (10, 20)):
        label = f"{'低换手+低波动' if score == 'lowrisk' else '低换手+低波动+反转'} {n}只 每{k}天"
        res = [fp.run(D, {"score": score, "n": n, "rebalance_days": k}, ps, pe, a.equity) for _, ps, pe in parts]
        print(f"{label:34s} | {fmt(res[0]['metrics'])}      | {fmt(res[1]['metrics'])}      | {res[0]['turnover']:5.1f}x  {res[0]['holdings'].mean():5.1f}", flush=True)
        rows.append({"variant": label, "params": {"score": score, "n": n, "rebalance_days": k},
                     "is": res[0]["metrics"], "oos": res[1]["metrics"], "turnover_is": round(res[0]["turnover"], 2)})
    Path(a.out).write_text(json.dumps({"equity": a.equity, "rows": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n写入 {a.out}，总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
