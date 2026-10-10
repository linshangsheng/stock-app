"""「每笔最多亏多少」（risk_per_trade）怎么定：在给定账户资金下（整手、最低佣金 5 元都计入），比较不同风险比例的
收益、最大回撤、最长连亏、交易笔数与「资金不足一手」放弃的次数。组合总风险上限 = 8 × 单笔风险（最多 8 只同时持有）。
用法：python tools/risk_sizing.py [--equity 100000] [--seeds 7,11,23] [--start 2018-01-01]
注意：单笔风险主要改变收益与回撤的「幅度」，不改变策略本身有没有优势；选它看的是你能承受多大的回撤。"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RISKS = (0.0025, 0.005, 0.0075, 0.01, 0.015)
# 回撤降风险：账户从最高点回撤达到阈值后，单笔风险减半，直到创新高
DD_CUT_VARIANTS = ((0.0075, {"threshold": 0.10, "factor": 0.5}), (0.0075, {"threshold": 0.08, "factor": 0.5}),
                   (0.01, {"threshold": 0.10, "factor": 0.5}), (0.01, {"threshold": 0.08, "factor": 0.5}))


def longest_losing_streak(trades: list[dict]) -> int:
    best = cur = 0
    for t in sorted(trades, key=lambda x: x.get("exit_date") or ""):
        cur = cur + 1 if (t.get("pnl") or 0) < 0 else 0
        best = max(best, cur)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=100000)
    ap.add_argument("--seeds", default="7,11,23")
    ap.add_argument("--n", type=int, default=1200)
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--out", default="data/risk_sizing.json")
    ap.add_argument("--ddcut", action="store_true", help="只跑「回撤降风险」变体（与不降风险对照）")
    a = ap.parse_args()
    from server import backtest, db, settings

    rows = []
    for seed in [int(s) for s in a.seeds.split(",")]:
        t0 = time.time()
        with settings.market_ctx("CN"), db.market_db("CN") as c:
            pool = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE (in_l1=1 OR status='delisted') AND sec_type='stock' ORDER BY symbol")]
            syms = sorted(random.Random(seed).sample(pool, min(a.n, len(pool))))
            end = c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
            ctx = backtest.BtContext(c, "CN", start=a.start, end=end, symbols=syms)
        print(f"seed {seed}: 准备 {time.time() - t0:.0f}s", flush=True)
        variants = ([(r, None) for r in (0.0075, 0.01)] + list(DD_CUT_VARIANTS)) if a.ddcut else [(r, None) for r in RISKS]
        for r, cut in variants:
            ov = {"start": a.start, "end": end, "initial_equity": a.equity,
                  "portfolio": {"risk_per_trade": r, "max_total_risk": round(8 * r, 4), "dd_cut": cut or {}}}
            with settings.market_ctx("CN"):
                res = ctx.run(ov, detail=True)
            m = res["metrics"]
            tr = res.get("trades") or []
            ab = m.get("sample", {}).get("abandoned") or m.get("abandoned") or {}
            lot_skips = sum(v for k, v in ab.items() if "一手" in k or "资金" in k) if isinstance(ab, dict) else None
            row = {"seed": seed, "risk": r, "dd_cut": cut, "n": m.get("n", 0), "cagr": m.get("cagr"), "total_return": m.get("total_return"),
                   "max_drawdown": m.get("max_drawdown"), "sharpe": m.get("sharpe"), "win_rate": m.get("win_rate"),
                   "longest_losing_streak": longest_losing_streak(tr), "lot_or_cash_skips": lot_skips,
                   "avg_notional": round(sum(t.get("notional") or 0 for t in tr) / len(tr), 0) if tr else None,
                   "fees_pct_of_pnl": None}
            rows.append(row)
            print(f"  单笔风险 {r:.2%}{'' if not cut else f' 回撤{cut["threshold"]:.0%}后减半'}: 年化 {row['cagr']} 最大回撤 {row['max_drawdown']} Sharpe {row['sharpe']} 笔数 {row['n']} "
                  f"最长连亏 {row['longest_losing_streak']} 不足一手/资金不足放弃 {lot_skips} 平均每笔金额 {row['avg_notional']}", flush=True)
    Path(a.out).write_text(json.dumps({"equity": a.equity, "start": a.start, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("写入", a.out)


if __name__ == "__main__":
    main()
